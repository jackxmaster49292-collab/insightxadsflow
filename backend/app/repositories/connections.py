from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    IN_PROGRESS_CONNECTION_STATUSES,
    ConnectionKind,
    ConnectionStatus,
    TelegramConnection,
    TelegramSession,
)
from app.security.crypto import SealedSecret, seal, unseal_str


async def list_for_user(
    session: AsyncSession, *, user_id: uuid.UUID
) -> Sequence[TelegramConnection]:
    result = await session.execute(
        select(TelegramConnection)
        .where(TelegramConnection.user_id == user_id)
        .order_by(TelegramConnection.created_at.desc())
    )
    return result.scalars().all()


async def get(
    session: AsyncSession, *, user_id: uuid.UUID, connection_id: uuid.UUID
) -> TelegramConnection | None:
    result = await session.execute(
        select(TelegramConnection).where(
            TelegramConnection.id == connection_id,
            TelegramConnection.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def get_unscoped_for_worker(
    session: AsyncSession, *, connection_id: uuid.UUID
) -> TelegramConnection | None:
    """Background processes act on behalf of the system, not a request principal.

    Named explicitly so its use is greppable and obvious in review. It must never
    be reachable from an HTTP handler.
    """
    result = await session.execute(
        select(TelegramConnection).where(TelegramConnection.id == connection_id)
    )
    return result.scalar_one_or_none()


async def has_in_progress_attempt(session: AsyncSession, *, user_id: uuid.UUID) -> bool:
    result = await session.execute(
        select(TelegramConnection.id).where(
            TelegramConnection.user_id == user_id,
            TelegramConnection.status.in_(
                [ConnectionStatus(s) for s in IN_PROGRESS_CONNECTION_STATUSES]
            ),
        )
    )
    return result.first() is not None


async def create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: ConnectionKind,
    label: str,
    status: ConnectionStatus,
) -> TelegramConnection:
    connection = TelegramConnection(user_id=user_id, kind=kind, label=label, status=status)
    session.add(connection)
    await session.flush()
    return connection


def store_bot_token(connection: TelegramConnection, token: str) -> None:
    sealed = seal(token, connection_id=str(connection.id), field="bot_token")
    connection.bot_token_ciphertext = sealed.ciphertext
    connection.bot_token_wrapped_dek = sealed.wrapped_dek
    connection.bot_token_key_version = sealed.key_version


def read_bot_token(connection: TelegramConnection) -> str:
    sealed = SealedSecret(
        ciphertext=connection.bot_token_ciphertext or b"",
        wrapped_dek=connection.bot_token_wrapped_dek or b"",
        key_version=connection.bot_token_key_version or 0,
    )
    return unseal_str(sealed, connection_id=str(connection.id), field="bot_token")


async def store_session_string(
    session: AsyncSession, *, connection: TelegramConnection, session_string: str
) -> None:
    sealed = seal(session_string, connection_id=str(connection.id), field="mtproto_session")
    result = await session.execute(
        select(TelegramSession).where(TelegramSession.connection_id == connection.id)
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        session.add(
            TelegramSession(
                connection_id=connection.id,
                session_ciphertext=sealed.ciphertext,
                wrapped_dek=sealed.wrapped_dek,
                key_version=sealed.key_version,
            )
        )
    else:
        existing.session_ciphertext = sealed.ciphertext
        existing.wrapped_dek = sealed.wrapped_dek
        existing.key_version = sealed.key_version
    await session.flush()


async def read_session_string(
    session: AsyncSession, *, connection: TelegramConnection
) -> str | None:
    result = await session.execute(
        select(TelegramSession).where(TelegramSession.connection_id == connection.id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return unseal_str(
        SealedSecret(row.session_ciphertext, row.wrapped_dek, row.key_version),
        connection_id=str(connection.id),
        field="mtproto_session",
    )


async def delete_session_string(session: AsyncSession, *, connection: TelegramConnection) -> None:
    result = await session.execute(
        select(TelegramSession).where(TelegramSession.connection_id == connection.id)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        await session.delete(row)


async def list_active_for_intake(session: AsyncSession) -> Sequence[TelegramConnection]:
    """System-scoped: the listener needs every active connection."""
    result = await session.execute(
        select(TelegramConnection).where(TelegramConnection.status == ConnectionStatus.active)
    )
    return result.scalars().all()
