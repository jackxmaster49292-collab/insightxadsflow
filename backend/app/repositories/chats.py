from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.base import ChatRef, DiscoveredChat, PeerKind
from app.db.models import (
    ChatKind,
    ConnectionChatAccess,
    PeerType,
    TelegramChat,
    TelegramConnection,
)
from app.domain import reasons
from app.security.crypto import SealedSecret, seal, unseal


def _owned(user_id: uuid.UUID) -> Select[tuple[TelegramChat]]:
    return (
        select(TelegramChat)
        .join(TelegramConnection, TelegramConnection.id == TelegramChat.connection_id)
        .where(TelegramConnection.user_id == user_id)
    )


async def get(
    session: AsyncSession, *, user_id: uuid.UUID, chat_id: uuid.UUID
) -> TelegramChat | None:
    result = await session.execute(
        _owned(user_id).where(TelegramChat.id == chat_id).options(selectinload(TelegramChat.access))
    )
    return result.scalar_one_or_none()


async def get_many(
    session: AsyncSession, *, user_id: uuid.UUID, chat_ids: Sequence[uuid.UUID]
) -> list[TelegramChat]:
    if not chat_ids:
        return []
    result = await session.execute(
        _owned(user_id)
        .where(TelegramChat.id.in_(list(chat_ids)))
        .options(selectinload(TelegramChat.access))
    )
    return list(result.scalars().all())


async def list_filtered(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    connection_id: uuid.UUID | None = None,
    source_eligible: bool | None = None,
    destination_eligible: bool | None = None,
    chat_kind: str | None = None,
    is_public: bool | None = None,
    is_active: bool | None = None,
    has_error: bool | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[TelegramChat]:
    stmt = (
        _owned(user_id)
        .outerjoin(ConnectionChatAccess, ConnectionChatAccess.chat_id == TelegramChat.id)
        .options(selectinload(TelegramChat.access))
    )
    if connection_id is not None:
        stmt = stmt.where(TelegramChat.connection_id == connection_id)
    if source_eligible is not None:
        stmt = stmt.where(ConnectionChatAccess.can_read_source.is_(source_eligible))
    if destination_eligible is not None:
        stmt = stmt.where(ConnectionChatAccess.can_post_destination.is_(destination_eligible))
    if chat_kind is not None:
        stmt = stmt.where(TelegramChat.chat_kind == ChatKind(chat_kind))
    if is_public is not None:
        stmt = stmt.where(TelegramChat.is_public.is_(is_public))
    if is_active is not None:
        stmt = stmt.where(TelegramChat.is_active.is_(is_active))
    if has_error is not None:
        stmt = (
            stmt.where(TelegramChat.last_error_code.isnot(None))
            if has_error
            else stmt.where(TelegramChat.last_error_code.is_(None))
        )
    if query:
        stmt = stmt.where(TelegramChat.title.ilike(f"%{query}%"))

    stmt = stmt.order_by(TelegramChat.title.asc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_by_peer(
    session: AsyncSession, *, connection_id: uuid.UUID, ref: ChatRef
) -> TelegramChat | None:
    """Keyed on ``(connection, peer_type, peer_id)`` — never ``peer_id`` alone."""
    result = await session.execute(
        select(TelegramChat)
        .where(
            TelegramChat.connection_id == connection_id,
            TelegramChat.peer_type == PeerType(ref.peer_type.value),
            TelegramChat.peer_id == ref.peer_id,
        )
        .options(selectinload(TelegramChat.access))
    )
    return result.scalar_one_or_none()


def store_access_hash(chat: TelegramChat, access_hash: int | None) -> None:
    if access_hash is None:
        return
    sealed = seal(str(access_hash), connection_id=str(chat.connection_id), field="access_hash")
    chat.access_hash_ciphertext = sealed.ciphertext
    chat.access_hash_wrapped_dek = sealed.wrapped_dek
    chat.access_hash_key_version = sealed.key_version


def read_access_hash(chat: TelegramChat) -> int | None:
    if (
        chat.access_hash_ciphertext is None
        or chat.access_hash_wrapped_dek is None
        or chat.access_hash_key_version is None
    ):
        return None
    raw = unseal(
        SealedSecret(
            chat.access_hash_ciphertext, chat.access_hash_wrapped_dek, chat.access_hash_key_version
        ),
        connection_id=str(chat.connection_id),
        field="access_hash",
    )
    return int(raw.decode())


def to_ref(chat: TelegramChat) -> ChatRef:
    return ChatRef(
        peer_type=PeerKind(chat.peer_type.value),
        peer_id=chat.peer_id,
        access_hash=read_access_hash(chat),
    )


async def set_details(session: AsyncSession, *, chat: TelegramChat, details) -> None:  # type: ignore[no-untyped-def]
    """Record the chat's own description and size, with when we learned it."""
    from datetime import UTC, datetime

    chat.description = details.description
    chat.member_count = details.member_count
    chat.details_synced_at = datetime.now(UTC)
    await session.flush()


async def upsert_discovered(
    session: AsyncSession, *, connection_id: uuid.UUID, discovered: DiscoveredChat
) -> TelegramChat:
    existing = await find_by_peer(session, connection_id=connection_id, ref=discovered.ref)
    if existing is None:
        existing = TelegramChat(
            connection_id=connection_id,
            peer_type=PeerType(discovered.ref.peer_type.value),
            peer_id=discovered.ref.peer_id,
            title=discovered.title,
            chat_kind=ChatKind(discovered.chat_kind),
        )
        session.add(existing)

    existing.title = discovered.title
    existing.username = discovered.username
    existing.chat_kind = ChatKind(discovered.chat_kind)
    existing.is_public = discovered.is_public
    existing.has_protected_content = discovered.has_protected_content
    existing.is_active = True
    existing.last_synced_at = datetime.now(UTC)
    existing.last_error_code = None
    existing.last_error_message_safe = None
    store_access_hash(existing, discovered.ref.access_hash)
    await session.flush()
    return existing


async def set_access(
    session: AsyncSession,
    *,
    chat: TelegramChat,
    can_read_source: bool,
    source_reason_code: str,
    can_post_destination: bool,
    destination_reason_code: str,
    check_source: str = "sync",
) -> ConnectionChatAccess:
    result = await session.execute(
        select(ConnectionChatAccess).where(ConnectionChatAccess.chat_id == chat.id)
    )
    access = result.scalar_one_or_none()
    if access is None:
        access = ConnectionChatAccess(chat_id=chat.id)
        session.add(access)

    access.can_read_source = can_read_source
    access.source_reason_code = source_reason_code
    access.can_post_destination = can_post_destination
    access.destination_reason_code = destination_reason_code
    access.checked_at = datetime.now(UTC)
    access.check_source = check_source
    await session.flush()
    return access


async def deactivate_missing(
    session: AsyncSession, *, connection_id: uuid.UUID, seen: set[tuple[str, int]]
) -> int:
    """A chat that disappeared from discovery is deactivated, never deleted —
    forwarding events still reference it."""
    result = await session.execute(
        select(TelegramChat).where(
            TelegramChat.connection_id == connection_id, TelegramChat.is_active.is_(True)
        )
    )
    count = 0
    for chat in result.scalars().all():
        if (chat.peer_type.value, chat.peer_id) not in seen:
            chat.is_active = False
            chat.last_error_code = reasons.NOT_A_MEMBER
            count += 1
    return count
