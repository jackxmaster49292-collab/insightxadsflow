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

import asyncio
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

#: How many chats' details to fetch per archived round, and the pause between
#: fetches. ``GetFullChannel`` is one of Telegram's most eagerly rate-limited
#: calls, so this is bounded and paced rather than "all 500 now" — details are
#: cached on the chat row, so repeat rounds converge to full coverage.
_DETAILS_PER_ROUND = 25
_DETAILS_GAP_S = 0.5

#: Re-learn a chat's details after this long. Bios change; the archive's value
#: is recognising the group *later*, so a years-old bio serves that less.
_DETAILS_STALE_DAYS = 30

#: How much of a bio the index carries. Enough to recognise the group, without
#: one chatty bio swallowing the chunk budget for everyone else's lines.
_BIO_CHARS = 160


async def archive_chat(session: AsyncSession, *, user_id: uuid.UUID) -> TelegramChat | None:
    """The group this customer keeps their copies in, if they chose one."""
    setting = await session.get(AppSetting, user_id)
    if setting is None or setting.archive_chat_id is None:
        return None
    return await session.get(TelegramChat, setting.archive_chat_id)


async def _delivered(
    session: AsyncSession, *, broadcast_id: uuid.UUID
) -> list[tuple[BroadcastTarget, TelegramChat]]:
    result = await session.execute(
        select(BroadcastTarget, TelegramChat)
        .join(TelegramChat, TelegramChat.id == BroadcastTarget.chat_id)
        .where(BroadcastTarget.broadcast_id == broadcast_id)
        .order_by(BroadcastTarget.position)
    )
    return [(target, chat) for target, chat in result.all() if target.status is JobStatus.succeeded]


async def _fill_details(
    session: AsyncSession,
    rows: list[tuple[BroadcastTarget, TelegramChat]],
    adapter: TelegramAdapter,
) -> None:
    """Learn the bio and size of private groups the index will name.

    Only groups whose link is not durable: a public group is already findable
    by its username, but a private one is exactly the group that is hard to
    find again months later — which is why the operator asked for the bio, "so
    nothing is lost". Bounded and paced, cached on the chat row, so full
    coverage arrives over rounds without hammering Telegram's most eagerly
    rate-limited lookup in one burst.
    """
    from datetime import UTC, datetime, timedelta

    stale_before = datetime.now(UTC) - timedelta(days=_DETAILS_STALE_DAYS)
    fetched = 0
    for _target, chat in rows:
        if fetched >= _DETAILS_PER_ROUND:
            log.info("archive_details_deferred", reason="per-round cap; next round continues")
            break
        if chat.username:
            continue
        if chat.details_synced_at is not None and chat.details_synced_at > stale_before:
            continue
        try:
            details = await adapter.chat_details(chat_repo.to_ref(chat))
        except Exception as exc:
            log.warning("archive_details_failed", chat_id=str(chat.id), error=str(exc))
            continue
        await chat_repo.set_details(session, chat=chat, details=details)
        fetched += 1
        await asyncio.sleep(_DETAILS_GAP_S)


def _index_lines(rows: list[tuple[BroadcastTarget, TelegramChat]]) -> list[str]:
    """One line per group: where it went, how to get back, and — for a private
    group — what it says about itself, because the title alone will not be
    enough to recognise it later."""
    lines: list[str] = []
    for target, chat in rows:
        link = link_for(
            chat_kind=chat.chat_kind.value,
            peer_id=chat.peer_id,
            username=chat.username,
            message_id=target.destination_message_id,
        )
        if link.url and link.kind is LinkKind.public:
            entry = f"{chat.title}\n{link.url}"
        elif link.url:
            # Honest about the catch: this one needs the account to still be a
            # member, which is exactly what an archive cannot assume.
            entry = f"{chat.title}\n{link.url}  (members only)"
        else:
            entry = f"{chat.title}\nno link — Telegram publishes none for this kind of group"

        if not chat.username:
            facts = []
            if chat.description:
                bio = " ".join(chat.description.split())
                facts.append(bio[:_BIO_CHARS] + ("…" if len(bio) > _BIO_CHARS else ""))
            if chat.member_count:
                facts.append(f"{chat.member_count:,} members")
            if facts:
                entry += "\n" + " · ".join(facts)
        lines.append(entry)
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

    rows = await _delivered(session, broadcast_id=broadcast.id)
    if not rows:
        return 0
    await _fill_details(session, rows, adapter)
    lines = _index_lines(rows)

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
