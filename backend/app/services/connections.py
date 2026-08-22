"""Connection lifecycle.

The two flows are kept strictly separate and the active type is always visible.
There is no code path that falls back from a bot to an account, or between
accounts — the customer must always know what is being used.

Login codes and 2FA passwords pass through memory only. Neither is stored,
hashed, or logged in any form.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ConnectionState, TelegramAdapter
from app.adapters.factory import build_adapter
from app.config import get_settings
from app.db.models import ConnectionKind, ConnectionStatus, TelegramConnection
from app.repositories import connections as connection_repo
from app.repositories import jobs as job_repo
from app.repositories import rules as rule_repo

log = structlog.get_logger(__name__)


class DuplicateConnectionAttempt(Exception):
    """A connection attempt is already in progress for this user."""


class ConnectionNotReady(Exception):
    pass


class TooManyConnections(Exception):
    """The per-account connection ceiling was reached.

    Operational, not a product limit: every MTProto connection is a live
    Telethon client in the listener process holding a socket and its own update
    state, so this bounds what one account can pin down.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"limit is {limit}")
        self.limit = limit
        self.message = (
            f"You already have {limit} connections, which is the maximum. "
            "Disconnect one you are not using first."
        )


async def _enforce_connection_cap(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    limit = get_settings().max_connections_per_user
    existing = await connection_repo.list_for_user(session, user_id=user_id)
    if len([c for c in existing if c.status is not ConnectionStatus.disconnected]) >= limit:
        raise TooManyConnections(limit)


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """Short-lived MTProto login state.

    Held in memory only, keyed by connection id. It carries the phone number and
    ``phone_code_hash`` needed to complete sign-in, and never reaches the
    database or a log line.

    ``adapter`` is the half-authenticated client that requested the code, and
    keeping it is what makes the flow work at all: Telegram binds a login code to
    the connection that asked for it, so completing sign-in on a freshly built
    client fails with an invalid ``phone_code_hash``. It stays until the login
    finishes or is abandoned.
    """

    phone: str
    phone_code_hash: str
    adapter: TelegramAdapter | None = None


_PENDING_LOGINS: dict[uuid.UUID, PendingLogin] = {}


def remember_login(connection_id: uuid.UUID, pending: PendingLogin) -> None:
    _PENDING_LOGINS[connection_id] = pending


def take_login(connection_id: uuid.UUID) -> PendingLogin | None:
    return _PENDING_LOGINS.get(connection_id)


def forget_login(connection_id: uuid.UUID) -> None:
    """Drop the pending login and close the client it was holding open."""
    pending = _PENDING_LOGINS.pop(connection_id, None)
    if pending is not None and pending.adapter is not None:
        # Fire-and-forget: an abandoned half-authenticated client must not keep
        # a socket open, but failing to close one must not fail the login.
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().create_task(pending.adapter.disconnect())


def hash_phone(phone: str) -> str:
    return hashlib.sha256(phone.encode()).hexdigest()


async def adapter_for(session: AsyncSession, connection: TelegramConnection) -> TelegramAdapter:
    session_string = None
    if connection.kind is ConnectionKind.user:
        session_string = await connection_repo.read_session_string(session, connection=connection)
    return build_adapter(connection, session_string=session_string)


async def create_bot_connection(
    session: AsyncSession, *, user_id: uuid.UUID, label: str, bot_token: str
) -> TelegramConnection:
    if await connection_repo.has_in_progress_attempt(session, user_id=user_id):
        raise DuplicateConnectionAttempt
    await _enforce_connection_cap(session, user_id=user_id)

    connection = await connection_repo.create(
        session,
        user_id=user_id,
        kind=ConnectionKind.bot,
        label=label,
        status=ConnectionStatus.pending,
    )
    # Sealed before anything else touches it.
    connection_repo.store_bot_token(connection, bot_token)
    await session.flush()
    return connection


async def verify_bot_connection(
    session: AsyncSession, *, connection: TelegramConnection
) -> TelegramConnection:
    adapter = await adapter_for(session, connection)
    state = await adapter.connect()
    connection.status = ConnectionStatus.active
    connection.telegram_account_id = state.account_id
    connection.telegram_username = state.username
    connection.last_successful_check_at = job_repo.now()
    connection.last_health_check_at = job_repo.now()
    connection.last_error_code = None
    connection.last_error_message_safe = None
    return connection


async def start_user_connection(
    session: AsyncSession, *, user_id: uuid.UUID, label: str, phone: str
) -> TelegramConnection:
    if await connection_repo.has_in_progress_attempt(session, user_id=user_id):
        raise DuplicateConnectionAttempt
    await _enforce_connection_cap(session, user_id=user_id)

    # Only a live provider needs real API credentials; the mock never contacts
    # Telegram, so requiring them would make the product untestable.
    if get_settings().live_telegram:
        get_settings().require_mtproto_credentials()

    connection = await connection_repo.create(
        session,
        user_id=user_id,
        kind=ConnectionKind.user,
        label=label,
        status=ConnectionStatus.awaiting_code,
    )
    connection.phone_hash = hash_phone(phone)
    await session.flush()

    adapter = await adapter_for(session, connection)
    if hasattr(adapter, "start_login"):
        phone_code_hash = await adapter.start_login(phone)
    else:  # mock provider
        phone_code_hash = "mock-code-hash"
    # The adapter is kept, not rebuilt later: Telegram ties the code it just sent
    # to this client.
    remember_login(
        connection.id,
        PendingLogin(phone=phone, phone_code_hash=phone_code_hash, adapter=adapter),
    )
    return connection


async def abandon(session: AsyncSession, *, connection: TelegramConnection) -> None:
    """Drop a half-finished connection attempt.

    Without this a sign-in that fails — which the phone flow does routinely,
    because Telegram cancels codes posted in chats — leaves a row in
    ``awaiting_code`` forever. The partial unique index then refuses every new
    attempt with "already in progress", and there is no way out from the panel.
    """
    forget_login(connection.id)
    await session.delete(connection)


async def verify_user_code(
    session: AsyncSession, *, connection: TelegramConnection, code: str
) -> ConnectionStatus:
    pending = take_login(connection.id)
    if pending is None:
        raise ConnectionNotReady("No login is in progress for this connection.")

    # Must be the client that requested the code — see PendingLogin.
    adapter = pending.adapter or await adapter_for(session, connection)
    if not hasattr(adapter, "complete_login"):  # mock provider
        state = await adapter.connect()
        await _finalize_user(session, connection=connection, state=state)
        return ConnectionStatus.active

    from app.adapters.user import TwoFactorRequired

    try:
        state = await adapter.complete_login(pending.phone, code, pending.phone_code_hash)
    except TwoFactorRequired:
        connection.status = ConnectionStatus.awaiting_2fa
        # The adapter (and its half-authenticated session) must survive until the
        # password step, so the pending record stays put.
        return ConnectionStatus.awaiting_2fa

    await _finalize_user(session, connection=connection, state=state)
    return ConnectionStatus.active


async def verify_user_2fa(
    session: AsyncSession, *, connection: TelegramConnection, password: str
) -> ConnectionStatus:
    pending = take_login(connection.id)
    if pending is None:
        raise ConnectionNotReady("No login is in progress for this connection.")

    # The same half-authenticated client again: the password step continues the
    # sign-in the code step started.
    adapter = pending.adapter or await adapter_for(session, connection)
    if not hasattr(adapter, "complete_2fa"):  # mock provider
        state = await adapter.connect()
    else:
        state = await adapter.complete_2fa(password)
    # `password` goes out of scope here and is never persisted anywhere.
    await _finalize_user(session, connection=connection, state=state)
    return ConnectionStatus.active


async def _finalize_user(
    session: AsyncSession, *, connection: TelegramConnection, state: ConnectionState
) -> None:
    if state.session_string:
        await connection_repo.store_session_string(
            session, connection=connection, session_string=state.session_string
        )
    connection.status = ConnectionStatus.active
    connection.telegram_account_id = state.account_id
    connection.telegram_username = state.username
    connection.last_successful_check_at = job_repo.now()
    connection.last_health_check_at = job_repo.now()
    connection.last_error_code = None
    connection.last_error_message_safe = None
    forget_login(connection.id)


async def run_health_check(session: AsyncSession, *, connection: TelegramConnection) -> bool:
    adapter = await adapter_for(session, connection)
    report = await adapter.health_check()
    connection.last_health_check_at = job_repo.now()
    if report.healthy:
        connection.last_successful_check_at = job_repo.now()
        connection.last_error_code = None
        connection.last_error_message_safe = None
        connection.consecutive_failure_count = 0
        if connection.status is ConnectionStatus.error:
            connection.status = ConnectionStatus.active
    else:
        connection.last_error_code = report.reason_code
        connection.status = ConnectionStatus.error
    return report.healthy


async def disconnect(
    session: AsyncSession, *, connection: TelegramConnection, revoke: bool
) -> None:
    """``revoke=True`` also calls Telegram's logout so the session is invalidated
    server-side, not merely forgotten locally."""
    try:
        adapter = await adapter_for(session, connection)
        await adapter.disconnect(revoke=revoke)
    except Exception as exc:
        log.warning("disconnect_adapter_failed", connection_id=str(connection.id), error=exc)

    if revoke:
        await connection_repo.delete_session_string(session, connection=connection)
        connection.bot_token_ciphertext = None
        connection.bot_token_wrapped_dek = None
        connection.bot_token_key_version = None

    connection.status = ConnectionStatus.disconnected
    forget_login(connection.id)

    await rule_repo.disable_for_connection(session, connection_id=connection.id)
    await job_repo.cancel_pending_for_connection(session, connection_id=connection.id)
