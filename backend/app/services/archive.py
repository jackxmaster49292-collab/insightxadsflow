"""Keep a copy of what was posted, in a group the customer controls.

The reason this exists is worth stating, because it decides the design: an
account can be lost, and when it is, everything it posted goes with it. So the
archive is not a list of links — a link into a private group is worthless from
an account that is no longer in that group. The archive is **the post itself**,
re-posted into a group the customer keeps, followed by an index of where it
went.

That ordering is the whole point. The copy survives the account. The index says
which groups received it, with a link where Telegram offers one and a plain
statement where it does not.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ChatRef, TelegramAdapter, TextEntity
from app.db.models import (
    AppSetting,
    Broadcast,
    BroadcastMedia,
    BroadcastTarget,
    JobStatus,
    TelegramChat,
)
from app.domain.message_links import LinkKind, link_for
from app.repositories import chats as chat_repo

log = structlog.get_logger(__name__)

#: Telegram's message ceiling, with room left for the header of each chunk.
_CHUNK_BUDGET = 3_600


async def archive_chat(session: AsyncSession, *, user_id: uuid.UUID) -> TelegramChat | None:
    """The group this customer keeps their copies in, if they chose one."""
    setting = await session.get(AppSetting, user_id)
    if setting is None or setting.archive_chat_id is None:
        return None
    return await session.get(TelegramChat, setting.archive_chat_id)


async def _index_lines(session: AsyncSession, *, broadcast_id: uuid.UUID) -> list[str]:
    """One line per group: where it went, and how to get back to it."""
    result = await session.execute(
        select(BroadcastTarget, TelegramChat)
        .join(TelegramChat, TelegramChat.id == BroadcastTarget.chat_id)
        .where(BroadcastTarget.broadcast_id == broadcast_id)
        .order_by(BroadcastTarget.position)
    )
    lines: list[str] = []
    for target, chat in result.all():
        if target.status is not JobStatus.succeeded:
            continue
        link = link_for(
            chat_kind=chat.chat_kind.value,
            peer_id=chat.peer_id,
            username=chat.username,
            message_id=target.destination_message_id,
        )
        if link.url and link.kind is LinkKind.public:
            lines.append(f"{chat.title}\n{link.url}")
        elif link.url:
            # Honest about the catch: this one needs the account to still be a
            # member, which is exactly what an archive cannot assume.
            lines.append(f"{chat.title}\n{link.url}  (members only)")
        else:
            lines.append(f"{chat.title}\nno link — Telegram publishes none for this kind of group")
    return lines


def _chunks(lines: list[str], *, header: str) -> list[str]:
    """Pack the index into as few messages as Telegram's limit allows."""
    messages: list[str] = []
    current = header
    for line in lines:
        candidate = f"{current}\n\n{line}" if current != header else f"{header}\n\n{line}"
        if len(candidate) > _CHUNK_BUDGET and current != header:
            messages.append(current)
            current = f"{header} (continued)\n\n{line}"
        else:
            current = candidate
    messages.append(current)
    return messages


async def store_round(
    session: AsyncSession,
    *,
    broadcast: Broadcast,
    adapter: TelegramAdapter,
) -> int:
    """Post the round's copy and index. Returns how many messages were sent.

    Best-effort by contract: an archive that fails must never fail the round it
    is describing. The ads are already delivered by the time this runs, and a
    missing copy is a smaller loss than a broadcast marked failed because its
    bookkeeping did not go through.
    """
    destination = await archive_chat(session, user_id=broadcast.user_id)
    if destination is None:
        return 0

    lines = await _index_lines(session, broadcast_id=broadcast.id)
    if not lines:
        return 0

    ref: ChatRef = chat_repo.to_ref(destination)
    round_label = f" — round {broadcast.repeat_count}" if broadcast.repeat_every_s else ""
    sent = 0

    # The copy first. This is the part that outlives the account, so it goes
    # out before the index that merely points at other people's groups.
    try:
        entities = [TextEntity.from_json(e) for e in broadcast.body_entities]
        has_media = broadcast.media_kind is not BroadcastMedia.none and broadcast.media_bytes
        if has_media:
            await adapter.send_photo(
                ref,
                bytes(broadcast.media_bytes or b""),
                caption=broadcast.body_text,
                caption_entities=entities,
                filename=broadcast.media_filename or "image.jpg",
            )
        elif broadcast.body_text.strip():
            await adapter.send_text(ref, broadcast.body_text, entities=entities)
        sent += 1
    except Exception as exc:
        log.warning("archive_copy_failed", broadcast_id=str(broadcast.id), error=str(exc))

    header = f"📁 {broadcast.name}{round_label} — {len(lines)} groups"
    for chunk in _chunks(lines, header=header):
        try:
            await adapter.send_text(ref, chunk)
            sent += 1
        except Exception as exc:
            log.warning("archive_index_failed", broadcast_id=str(broadcast.id), error=str(exc))
            break

    log.info("archive_stored", broadcast_id=str(broadcast.id), messages=sent, groups=len(lines))
    return sent
