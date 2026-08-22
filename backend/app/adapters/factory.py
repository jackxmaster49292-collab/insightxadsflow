"""Adapter construction.

``TELEGRAM_PROVIDER`` defaults to ``mock``, so a misconfigured process can never
accidentally reach Telegram. Only ``live`` builds a real client.
"""

from __future__ import annotations

import uuid

import structlog

from app.adapters.base import ChatRef, DiscoveredChat, PeerKind, TelegramAdapter
from app.adapters.mock import MockAdapter, MockScript
from app.config import get_settings
from app.db.models import ConnectionKind, TelegramConnection
from app.security.crypto import SealedSecret, unseal_str

log = structlog.get_logger(__name__)

#: Per-connection mock scripts, so a test can steer behaviour by connection id.
MOCK_REGISTRY: dict[uuid.UUID, MockScript] = {}


def _demo_chats(connection_id: uuid.UUID) -> list[DiscoveredChat]:
    """A deterministic fake chat list, derived from the connection id.

    The registry lives in process memory, so the API and the worker each hold
    their own. Deriving the chats from the connection id means both processes
    discover the *same* chats, which is what makes ``TELEGRAM_PROVIDER=mock`` a
    usable local setup rather than an empty screen. Tests override
    ``script.chats`` and are unaffected.
    """
    base = -1_000_000_000_000 - (connection_id.int % 1_000_000)
    return [
        DiscoveredChat(
            ref=ChatRef(PeerKind.channel, base),
            title="Demo announcements",
            chat_kind="channel",
            username="demo_announcements",
            is_public=True,
            has_protected_content=False,
        ),
        *[
            DiscoveredChat(
                ref=ChatRef(PeerKind.channel, base - index),
                title=f"Demo partner {index}",
                chat_kind="supergroup",
                username=None,
                is_public=False,
                has_protected_content=False,
            )
            for index in range(1, 4)
        ],
        DiscoveredChat(
            ref=ChatRef(PeerKind.channel, base - 90),
            title="Demo protected channel",
            chat_kind="channel",
            username=None,
            is_public=False,
            # Exercises the refusal path: never eligible as a source.
            has_protected_content=True,
        ),
    ]


def mock_script_for(connection_id: uuid.UUID) -> MockScript:
    script = MOCK_REGISTRY.get(connection_id)
    if script is None:
        script = MockScript(chats=_demo_chats(connection_id))
        MOCK_REGISTRY[connection_id] = script
    return script


def reset_mock_registry() -> None:
    MOCK_REGISTRY.clear()


def _bot_token(connection: TelegramConnection) -> str:
    if (
        connection.bot_token_ciphertext is None
        or connection.bot_token_wrapped_dek is None
        or connection.bot_token_key_version is None
    ):
        raise RuntimeError("Connection has no stored bot token")
    sealed = SealedSecret(
        ciphertext=connection.bot_token_ciphertext,
        wrapped_dek=connection.bot_token_wrapped_dek,
        key_version=connection.bot_token_key_version,
    )
    return unseal_str(sealed, connection_id=str(connection.id), field="bot_token")


def build_adapter(
    connection: TelegramConnection, *, session_string: str | None = None
) -> TelegramAdapter:
    settings = get_settings()

    if not settings.live_telegram:
        return MockAdapter(mock_script_for(connection.id), kind=connection.kind.value)

    if connection.kind is ConnectionKind.bot:
        from app.adapters.bot import BotAdapter

        return BotAdapter(_bot_token(connection), poll_timeout_s=settings.bot_poll_timeout_s)

    from app.adapters.user import UserAdapter

    api_id, api_hash = settings.require_mtproto_credentials()
    return UserAdapter(
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        album_buffer_ms=settings.album_buffer_ms,
    )
