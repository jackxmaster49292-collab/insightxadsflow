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
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ChatRef, PeerKind, TelegramAdapter, TextEntity
from app.config import get_settings
from app.db.models import (
    AppSetting,
    Broadcast,
    BroadcastMedia,
    BroadcastTarget,
    JobStatus,
    TelegramChat,
    User,
)
from app.domain.message_links import LinkKind, link_for
from app.repositories import chats as chat_repo

log = structlog.get_logger(__name__)

#: Telegram's message ceiling, with room left for the header of each chunk.
_CHUNK_BUDGET = 3_600

#: Pause between detail lookups. ``GetFullChannel`` is one of Telegram's most
#: eagerly rate-limited calls, so the walk is paced — but it covers **every**
#: private group in the round. The operator was offered a capped version and
#: rejected it, correctly: an archive that identifies only some of the groups
#: is not the record they asked for. The price is time — ~2 minutes for 150
#: private groups on the first round — and details are cached on the chat row,
#: so later rounds cost nothing.
_DETAILS_GAP_S = 0.5

#: A Telegram-supplied wait up to this long is obeyed in place, mid-walk.
#: Longer than this, the walk stops and the remainder is picked up next round —
#: stopped, never shortened.
_DETAILS_WAIT_CEILING_S = 60.0

#: Backstop on one round's walk. At the normal pace this allows ~1000 lookups,
#: double the broadcast ceiling, so it only ever fires when Telegram is
#: throwing repeated waits — exactly when continuing would make things worse.
_DETAILS_TIME_BUDGET_S = 600.0

#: Test seam: the module sleeps through this name so a test can stub it.
_sleep = asyncio.sleep

#: Re-learn a chat's details after this long. Bios change; the archive's value
#: is recognising the group *later*, so a years-old bio serves that less.
_DETAILS_STALE_DAYS = 30

#: Stable key for the admin bot's mock script, so a test can steer what the
#: bot-delivered archive does without a connection row to hang it off.
_ARCHIVE_BOT_ID = uuid.UUID("00000000-0000-0000-0000-0000000a4c41")

#: How much of a bio the index carries. Enough to recognise the group, without
#: one chatty bio swallowing the chunk budget for everyone else's lines.
_BIO_CHARS = 160


@dataclass(frozen=True, slots=True)
class Destination:
    """Where the archive goes, and who carries it there."""

    ref: ChatRef
    #: True when the admin bot delivers, rather than the posting account.
    via_bot: bool


async def archive_chat(session: AsyncSession, *, user_id: uuid.UUID) -> TelegramChat | None:
    """The synced group this customer keeps account-delivered copies in."""
    setting = await session.get(AppSetting, user_id)
    if setting is None or setting.archive_chat_id is None:
        return None
    return await session.get(TelegramChat, setting.archive_chat_id)


async def destination_for(session: AsyncSession, *, user_id: uuid.UUID) -> Destination | None:
    """Resolve the archive destination, preferring the bot when one is set.

    The bot wins because of *why* the archive exists: an archive the account
    delivers stops the day that account is lost, which is the event being
    insured against. The account route stays for anyone who would rather the
    copies come from the same account that posted them.
    """
    # Operator-only, checked here and not just in the panel. The screens hide
    # the control and the handlers refuse the callback, but this is the layer
    # that holds however a row got written — a leftover from before the feature
    # was restricted, a direct database edit, a future API. "Only the admin id,
    # nowhere else" is a property of the system, not of one screen.
    owner = await session.get(User, user_id)
    if owner is None or owner.telegram_user_id is None:
        return None
    if not get_settings().is_admin(owner.telegram_user_id):
        return None

    setting = await session.get(AppSetting, user_id)
    if setting is None:
        return None

    if setting.archive_bot_chat_id is not None:
        peer_id = setting.archive_bot_chat_id
        kind = PeerKind.channel if str(peer_id).startswith("-100") else PeerKind.chat
        return Destination(ChatRef(kind, peer_id), via_bot=True)

    chat = await archive_chat(session, user_id=user_id)
    return Destination(chat_repo.to_ref(chat), via_bot=False) if chat else None


def bot_adapter() -> TelegramAdapter | None:
    """An adapter for the admin bot, or None when this deployment has no token.

    Built through the same factory the rest of the system uses, so a mock
    deployment stays a mock deployment and no test can reach Telegram.
    """
    settings = get_settings()
    # Mode before token, exactly as ``build_adapter`` orders it: a mock
    # deployment needs no credential to pretend, and checking the token first
    # would make every test silently take the "no archive" path.
    if not settings.live_telegram:
        from app.adapters.factory import mock_script_for
        from app.adapters.mock import MockAdapter

        return MockAdapter(mock_script_for(_ARCHIVE_BOT_ID), kind="bot")

    token = settings.admin_bot_token
    if not token:
        return None

    from app.adapters.bot import BotAdapter

    return BotAdapter(token)


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
    nothing is lost". **Every** such group in the round is walked, paced, with
    each Telegram wait obeyed in place up to a ceiling; a longer wait stops the
    walk and the remainder is picked up next round. A chat whose lookup fails
    is not marked synced, so it is retried on every later round until it is
    learned — the operator asked for exactly that persistence.
    """
    import time
    from datetime import UTC, datetime, timedelta

    from app.adapters.errors import ErrorClass, classify_error

    stale_before = datetime.now(UTC) - timedelta(days=_DETAILS_STALE_DAYS)
    started = time.monotonic()

    for _target, chat in rows:
        if chat.username:
            continue
        if chat.details_synced_at is not None and chat.details_synced_at > stale_before:
            continue
        if time.monotonic() - started > _DETAILS_TIME_BUDGET_S:
            # Only reachable under repeated Telegram waits — the one situation
            # where pressing on would make the waits longer.
            log.info("archive_details_deferred", reason="time budget; next round continues")
            break

        try:
            details = await adapter.chat_details(chat_repo.to_ref(chat))
        except Exception as exc:
            classified = classify_error(exc)
            wait = classified.retry_after_s
            if (
                classified.error_class is ErrorClass.RATE_LIMIT
                and wait
                and wait <= _DETAILS_WAIT_CEILING_S
            ):
                # Obeyed in full, in place, then one more try for this chat.
                await _sleep(wait)
                try:
                    details = await adapter.chat_details(chat_repo.to_ref(chat))
                except Exception as retry_exc:
                    log.warning(
                        "archive_details_failed",
                        chat_id=str(chat.id),
                        error=str(retry_exc),
                    )
                    continue
            elif classified.error_class is ErrorClass.RATE_LIMIT and wait:
                log.info(
                    "archive_details_deferred",
                    reason=f"telegram asked for {wait:.0f}s; next round continues",
                )
                break
            else:
                # Unmarked, therefore retried next round — and every round
                # after, until it is learned.
                log.warning("archive_details_failed", chat_id=str(chat.id), error=str(exc))
                continue

        await chat_repo.set_details(session, chat=chat, details=details)
        await _sleep(_DETAILS_GAP_S)


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
    adapter: TelegramAdapter | None = None,
) -> int:
    """Post the round's copy and index. Returns how many messages were sent.

    ``adapter`` is the *account's*, and is optional: stopping an ad archives
    what it already delivered, and building an MTProto client for that when the
    bot is the courier would be waste. Without one, group bios are not learned
    this time — cached ones still appear — and the account route cannot
    deliver, which is said out loud rather than passed over.

    Best-effort by contract: an archive that fails must never fail the round it
    is describing. The ads are already delivered by the time this runs, and a
    missing copy is a smaller loss than a broadcast marked failed because its
    bookkeeping did not go through.
    """
    destination = await destination_for(session, user_id=broadcast.user_id)
    if destination is None:
        return 0

    rows = await _delivered(session, broadcast_id=broadcast.id)
    if not rows:
        return 0

    # Details are always learned through the *account*: only it is a member of
    # the groups being described, and the bot is not.
    if adapter is not None:
        await _fill_details(session, rows, adapter)
    lines = _index_lines(rows)

    if destination.via_bot:
        courier = bot_adapter()
        if courier is None:
            log.warning("archive_no_bot_token", broadcast_id=str(broadcast.id))
            return 0
    elif adapter is None:
        log.warning(
            "archive_needs_the_account",
            broadcast_id=str(broadcast.id),
            detail="destination is a synced group, which only the account can post to",
        )
        return 0
    else:
        courier = adapter

    ref = destination.ref
    round_label = f" — round {broadcast.repeat_count}" if broadcast.repeat_every_s else ""
    sent = 0

    # The copy first. This is the part that outlives the account, so it goes
    # out before the index that merely points at other people's groups.
    try:
        entities = [TextEntity.from_json(e) for e in broadcast.body_entities]
        has_media = broadcast.media_kind is not BroadcastMedia.none and broadcast.media_bytes
        if has_media:
            await courier.send_photo(
                ref,
                bytes(broadcast.media_bytes or b""),
                caption=broadcast.body_text,
                caption_entities=entities,
                filename=broadcast.media_filename or "image.jpg",
            )
        elif broadcast.body_text.strip():
            await courier.send_text(ref, broadcast.body_text, entities=entities)
        sent += 1
    except Exception as exc:
        log.warning("archive_copy_failed", broadcast_id=str(broadcast.id), error=str(exc))

    header = f"📁 {broadcast.name}{round_label} — {len(lines)} groups"
    for chunk in _chunks(lines, header=header):
        try:
            await courier.send_text(ref, chunk)
            sent += 1
        except Exception as exc:
            log.warning("archive_index_failed", broadcast_id=str(broadcast.id), error=str(exc))
            break

    log.info("archive_stored", broadcast_id=str(broadcast.id), messages=sent, groups=len(lines))
    return sent
