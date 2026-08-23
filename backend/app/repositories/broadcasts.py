"""Durable store for broadcasts and their per-group targets.

Same claim/lease discipline as ``repositories.jobs`` — ``FOR UPDATE SKIP
LOCKED`` plus a lease, so N workers drain without coordination and a crashed
worker's targets return to the queue instead of vanishing. A broadcast that dies
halfway must resume, not restart: re-sending to groups already delivered is the
failure mode that matters here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Broadcast, BroadcastStatus, BroadcastTarget, JobStatus, TelegramChat


def now() -> datetime:
    return datetime.now(UTC)


#: Statuses that mean "this delivery is finished and must never be repeated".
TERMINAL = (JobStatus.succeeded, JobStatus.skipped, JobStatus.dead_letter)


# --------------------------------------------------------------------------- #
# Broadcasts
# --------------------------------------------------------------------------- #
async def create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    name: str,
    delay_ms: int,
) -> Broadcast:
    broadcast = Broadcast(
        user_id=user_id,
        connection_id=connection_id,
        name=name[:120],
        delay_ms=delay_ms,
        status=BroadcastStatus.draft,
    )
    session.add(broadcast)
    await session.flush()
    return broadcast


async def get(
    session: AsyncSession, *, user_id: uuid.UUID, broadcast_id: uuid.UUID
) -> Broadcast | None:
    """Always scoped by user. An id alone never resolves."""
    result = await session.execute(
        select(Broadcast)
        .where(Broadcast.id == broadcast_id, Broadcast.user_id == user_id)
        .options(selectinload(Broadcast.targets))
    )
    return result.scalar_one_or_none()


async def get_unscoped_for_worker(
    session: AsyncSession, *, broadcast_id: uuid.UUID
) -> Broadcast | None:
    """For the worker, which has already resolved ownership through the target."""
    return await session.get(Broadcast, broadcast_id)


async def list_for_user(
    session: AsyncSession, *, user_id: uuid.UUID, limit: int = 50
) -> list[Broadcast]:
    result = await session.execute(
        select(Broadcast)
        .where(Broadcast.user_id == user_id)
        .order_by(Broadcast.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def current_draft(session: AsyncSession, *, user_id: uuid.UUID) -> Broadcast | None:
    """The one being composed. Compose spans several Telegram messages, so the
    draft lives in the database rather than in the FSM state."""
    result = await session.execute(
        select(Broadcast)
        .where(Broadcast.user_id == user_id, Broadcast.status == BroadcastStatus.draft)
        .order_by(Broadcast.created_at.desc())
        .options(selectinload(Broadcast.targets))
        .limit(1)
    )
    return result.scalars().first()


async def discard_drafts(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    result = await session.execute(
        delete(Broadcast)
        .where(Broadcast.user_id == user_id, Broadcast.status == BroadcastStatus.draft)
        .returning(Broadcast.id)
    )
    return len(result.all())


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #
async def replace_targets(
    session: AsyncSession, *, broadcast: Broadcast, chat_ids: Sequence[uuid.UUID]
) -> int:
    """Set the target list, keeping what already happened to each group.

    A group that stays selected keeps its row, and with it the record of whether
    this round already posted there. That is the whole point: editing the groups
    of an ad mid-flight must not make it post twice to a group it has already
    reached. Only groups being *removed* lose their row, which is what removing
    them means.

    Written against the rows rather than ``broadcast.targets``: touching the
    collection triggers a lazy load, which raises MissingGreenlet under async
    SQLAlchemy.
    """
    result = await session.execute(
        select(BroadcastTarget).where(BroadcastTarget.broadcast_id == broadcast.id)
    )
    existing = {target.chat_id: target for target in result.scalars().all()}

    wanted: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for chat_id in chat_ids:
        if chat_id not in seen:
            seen.add(chat_id)
            wanted.append(chat_id)

    for chat_id, target in existing.items():
        if chat_id not in seen:
            await session.delete(target)

    for position, chat_id in enumerate(wanted):
        kept = existing.get(chat_id)
        if kept is None:
            session.add(
                BroadcastTarget(
                    broadcast_id=broadcast.id,
                    chat_id=chat_id,
                    position=position,
                    status=JobStatus.pending,
                )
            )
        else:
            kept.position = position
    await session.flush()
    return len(wanted)


async def targets_with_chats(
    session: AsyncSession, *, broadcast_id: uuid.UUID
) -> list[tuple[BroadcastTarget, TelegramChat]]:
    """Every target of an ad with its group, for the per-group report."""
    result = await session.execute(
        select(BroadcastTarget, TelegramChat)
        .join(TelegramChat, TelegramChat.id == BroadcastTarget.chat_id)
        .where(BroadcastTarget.broadcast_id == broadcast_id)
        .order_by(BroadcastTarget.position)
    )
    return [(row[0], row[1]) for row in result.all()]


async def chat_titles(session: AsyncSession, *, broadcast_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """Group titles for this ad's targets, keyed by chat id.

    The activity screen listed a reason with no group beside it, which answers
    "something was skipped" but not "which group, and should I care?" — the only
    two questions anyone opens that screen with.
    """
    result = await session.execute(
        select(TelegramChat.id, TelegramChat.title)
        .join(BroadcastTarget, BroadcastTarget.chat_id == TelegramChat.id)
        .where(BroadcastTarget.broadcast_id == broadcast_id)
    )
    return {row[0]: row[1] for row in result.all()}


async def target_chat_ids(session: AsyncSession, *, broadcast_id: uuid.UUID) -> list[uuid.UUID]:
    result = await session.execute(
        select(BroadcastTarget.chat_id)
        .where(BroadcastTarget.broadcast_id == broadcast_id)
        .order_by(BroadcastTarget.position)
    )
    return [row[0] for row in result.all()]


async def schedule_targets(
    session: AsyncSession, *, broadcast: Broadcast, start_at: datetime | None = None
) -> int:
    """Stagger the pending targets by ``delay_ms`` and hand them to the worker.

    Only pending rows are touched, so resuming a paused broadcast re-times the
    remainder without disturbing anything already delivered.
    """
    base = start_at or now()
    result = await session.execute(
        select(BroadcastTarget)
        .where(
            BroadcastTarget.broadcast_id == broadcast.id,
            BroadcastTarget.status == JobStatus.pending,
        )
        .order_by(BroadcastTarget.position)
    )
    targets = list(result.scalars().all())
    for offset, target in enumerate(targets):
        target.not_before = base + timedelta(milliseconds=broadcast.delay_ms * offset)
    await session.flush()
    return len(targets)


async def claim_batch(
    session: AsyncSession, *, owner: str, limit: int, lease_seconds: int
) -> list[BroadcastTarget]:
    """Atomically lease up to ``limit`` due targets."""
    deadline = now() + timedelta(seconds=lease_seconds)
    candidates = (
        select(BroadcastTarget.id)
        .join(Broadcast, Broadcast.id == BroadcastTarget.broadcast_id)
        .where(
            BroadcastTarget.status == JobStatus.pending,
            BroadcastTarget.not_before <= now(),
            # A paused or cancelled broadcast stops feeding the worker at the
            # source, so pausing takes effect immediately rather than after the
            # already-claimed batch drains.
            Broadcast.status == BroadcastStatus.sending,
        )
        .order_by(BroadcastTarget.not_before.asc())
        .limit(limit)
        .with_for_update(skip_locked=True, of=BroadcastTarget)
    )
    result = await session.execute(
        update(BroadcastTarget)
        .where(BroadcastTarget.id.in_(candidates.scalar_subquery()))
        .values(status=JobStatus.leased, lease_owner=owner, lease_expires_at=deadline)
        .returning(BroadcastTarget.id)
    )
    ids = [row[0] for row in result.all()]
    if not ids:
        return []
    fetched = await session.execute(select(BroadcastTarget).where(BroadcastTarget.id.in_(ids)))
    return list(fetched.scalars().all())


async def heartbeat(
    session: AsyncSession, *, target_id: uuid.UUID, owner: str, lease_seconds: int
) -> None:
    await session.execute(
        update(BroadcastTarget)
        .where(BroadcastTarget.id == target_id, BroadcastTarget.lease_owner == owner)
        .values(lease_expires_at=now() + timedelta(seconds=lease_seconds))
    )


async def resume_flood_paused(session: AsyncSession) -> int:
    """Resume broadcasts paused for a Telegram wait, once the wait has passed.

    The wait is obeyed in full — a broadcast resumes only when its earliest
    pending target's ``not_before`` (set from Telegram's own number) is behind
    us. Only the flood-wait pause is touched: a pause the customer chose, or one
    made for editing, ends when *they* say so, never by a sweep. The legacy
    ``FLOOD_WAIT_PAUSE`` code is included for broadcasts paused before this
    resume existed, which otherwise stay paused forever.
    """
    from app.domain import reasons

    result = await session.execute(
        select(Broadcast).where(
            Broadcast.status == BroadcastStatus.paused,
            Broadcast.paused_reason_code.in_(
                [reasons.BROADCAST_FLOOD_WAIT, reasons.FLOOD_WAIT_PAUSE]
            ),
        )
    )
    resumed = 0
    for broadcast in result.scalars().all():
        earliest = await session.execute(
            select(func.min(BroadcastTarget.not_before)).where(
                BroadcastTarget.broadcast_id == broadcast.id,
                BroadcastTarget.status == JobStatus.pending,
            )
        )
        due = earliest.scalar_one_or_none()
        if due is None or due > now():
            continue
        broadcast.status = BroadcastStatus.sending
        broadcast.paused_reason_code = None
        resumed += 1
    return resumed


async def reclaim_expired(session: AsyncSession, *, limit: int = 200) -> int:
    result = await session.execute(
        update(BroadcastTarget)
        .where(
            BroadcastTarget.status == JobStatus.leased,
            BroadcastTarget.lease_expires_at.isnot(None),
            BroadcastTarget.lease_expires_at < now(),
            BroadcastTarget.id.in_(
                select(BroadcastTarget.id)
                .where(
                    BroadcastTarget.status == JobStatus.leased,
                    BroadcastTarget.lease_expires_at < now(),
                )
                .limit(limit)
                .scalar_subquery()
            ),
        )
        .values(status=JobStatus.pending, lease_owner=None, lease_expires_at=None)
        .returning(BroadcastTarget.id)
    )
    return len(result.all())


async def finish(
    session: AsyncSession,
    *,
    target: BroadcastTarget,
    status: JobStatus,
    destination_message_id: int | None = None,
    error_class: str | None = None,
    error_code: str | None = None,
) -> None:
    target.status = status
    target.lease_owner = None
    target.lease_expires_at = None
    target.destination_message_id = destination_message_id
    target.last_error_class = error_class
    target.last_error_code = error_code


async def reschedule(
    session: AsyncSession,
    *,
    target: BroadcastTarget,
    delay_s: float,
    error_class: str,
    error_code: str,
) -> None:
    target.status = JobStatus.pending
    target.lease_owner = None
    target.lease_expires_at = None
    target.attempt_count += 1
    target.not_before = now() + timedelta(seconds=delay_s)
    target.last_error_class = error_class
    target.last_error_code = error_code


async def reopen_for_repeat(
    session: AsyncSession, *, broadcast: Broadcast, start_at: datetime
) -> int:
    """Put every target back to pending for the next round.

    All of them, including ones that were refused last time. A refusal is a fact
    about that moment — an admin can grant permission, or lift a mute — and
    re-checking is how that gets noticed. The pre-send check makes a refusal
    cheap, and the pacer spaces the attempts out anyway.

    The new round is staggered by ``delay_ms`` exactly like the first one. It has
    to be: giving every target the same ``not_before`` would hand the worker the
    whole list at once, and a second round is precisely when posting to hundreds
    of groups in one burst would look like what it would be.
    """
    result = await session.execute(
        select(BroadcastTarget)
        .where(BroadcastTarget.broadcast_id == broadcast.id)
        .order_by(BroadcastTarget.position)
    )
    targets = list(result.scalars().all())
    for offset, target in enumerate(targets):
        target.status = JobStatus.pending
        target.attempt_count = 0
        target.not_before = start_at + timedelta(milliseconds=broadcast.delay_ms * offset)
        target.lease_owner = None
        target.lease_expires_at = None
        target.destination_message_id = None
        target.last_error_class = None
        target.last_error_code = None
    await session.flush()
    return len(targets)


async def status_counts(session: AsyncSession, *, broadcast_id: uuid.UUID) -> dict[str, int]:
    result = await session.execute(
        select(BroadcastTarget.status, func.count())
        .where(BroadcastTarget.broadcast_id == broadcast_id)
        .group_by(BroadcastTarget.status)
    )
    return {status.value: int(count) for status, count in result.all()}


async def counts_for(
    session: AsyncSession, *, broadcast_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    """Delivery counts for several ads at once, for the list screen.

    One grouped query rather than one per ad: the list shows up to fifty, and a
    query per row is how a screen that used to open instantly stops doing so.
    """
    if not broadcast_ids:
        return {}
    result = await session.execute(
        select(BroadcastTarget.broadcast_id, BroadcastTarget.status, func.count())
        .where(BroadcastTarget.broadcast_id.in_(broadcast_ids))
        .group_by(BroadcastTarget.broadcast_id, BroadcastTarget.status)
    )
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for broadcast_id, status, count in result.all():
        counts.setdefault(broadcast_id, {})[status.value] = int(count)
    return counts


async def remaining_count(session: AsyncSession, *, broadcast_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(BroadcastTarget)
        .where(
            BroadcastTarget.broadcast_id == broadcast_id,
            BroadcastTarget.status.in_([JobStatus.pending, JobStatus.leased]),
        )
    )
    return int(result.scalar_one())


async def target_chats(session: AsyncSession, *, broadcast_id: uuid.UUID) -> list[TelegramChat]:
    result = await session.execute(
        select(TelegramChat)
        .join(BroadcastTarget, BroadcastTarget.chat_id == TelegramChat.id)
        .where(BroadcastTarget.broadcast_id == broadcast_id)
        .order_by(BroadcastTarget.position)
    )
    return list(result.scalars().all())


async def cancel_pending(session: AsyncSession, *, broadcast_id: uuid.UUID) -> int:
    """Stop undelivered targets. Deliveries that already happened stay recorded —
    cancelling a broadcast cannot unsend anything."""
    result = await session.execute(
        update(BroadcastTarget)
        .where(
            BroadcastTarget.broadcast_id == broadcast_id,
            BroadcastTarget.status.in_([JobStatus.pending, JobStatus.leased]),
        )
        .values(status=JobStatus.skipped, lease_owner=None, lease_expires_at=None)
        .returning(BroadcastTarget.id)
    )
    return len(result.all())


async def cancel_pending_for_connection(session: AsyncSession, *, connection_id: uuid.UUID) -> int:
    result = await session.execute(
        update(BroadcastTarget)
        .where(
            BroadcastTarget.broadcast_id.in_(
                select(Broadcast.id).where(Broadcast.connection_id == connection_id)
            ),
            BroadcastTarget.status.in_([JobStatus.pending, JobStatus.leased]),
        )
        .values(status=JobStatus.skipped, lease_owner=None, lease_expires_at=None)
        .returning(BroadcastTarget.id)
    )
    return len(result.all())


async def requeue_failed(session: AsyncSession, *, broadcast_id: uuid.UUID) -> int:
    """Retry only genuinely unfinished work. A succeeded target is never replayed."""
    result = await session.execute(
        update(BroadcastTarget)
        .where(
            BroadcastTarget.broadcast_id == broadcast_id,
            BroadcastTarget.status.in_(
                [JobStatus.failed, JobStatus.needs_attention, JobStatus.dead_letter]
            ),
        )
        .values(status=JobStatus.pending, attempt_count=0, not_before=now(), lease_owner=None)
        .returning(BroadcastTarget.id)
    )
    return len(result.all())


async def in_flight_count(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    exclude_ids: Sequence[uuid.UUID] = (),
) -> int:
    """Leased work on this connection, optionally ignoring a set of ids.

    ``exclude_ids`` exists because the caller has *already* leased the batch it
    is about to weigh: counting those rows would measure the ceiling against
    the very work being admitted, and the admitted count would collapse towards
    one no matter how high the ceiling. That is exactly what "1 leased · 145
    pending" looked like from the outside.
    """
    query = (
        select(func.count())
        .select_from(BroadcastTarget)
        .join(Broadcast, Broadcast.id == BroadcastTarget.broadcast_id)
        .where(Broadcast.connection_id == connection_id, BroadcastTarget.status == JobStatus.leased)
    )
    if exclude_ids:
        query = query.where(BroadcastTarget.id.notin_(exclude_ids))
    result = await session.execute(query)
    return int(result.scalar_one())
