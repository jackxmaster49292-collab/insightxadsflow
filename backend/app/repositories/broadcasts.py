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
    """Set the target list while the broadcast is still a draft.

    Written as DELETE + INSERT rather than mutating ``broadcast.targets``:
    touching the collection triggers a lazy load, which raises MissingGreenlet
    under async SQLAlchemy.
    """
    await session.execute(
        delete(BroadcastTarget).where(BroadcastTarget.broadcast_id == broadcast.id)
    )
    seen: set[uuid.UUID] = set()
    position = 0
    for chat_id in chat_ids:
        if chat_id in seen:
            continue
        seen.add(chat_id)
        session.add(
            BroadcastTarget(
                broadcast_id=broadcast.id,
                chat_id=chat_id,
                position=position,
                status=JobStatus.pending,
            )
        )
        position += 1
    await session.flush()
    return position


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


async def status_counts(session: AsyncSession, *, broadcast_id: uuid.UUID) -> dict[str, int]:
    result = await session.execute(
        select(BroadcastTarget.status, func.count())
        .where(BroadcastTarget.broadcast_id == broadcast_id)
        .group_by(BroadcastTarget.status)
    )
    return {status.value: int(count) for status, count in result.all()}


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


async def in_flight_count(session: AsyncSession, *, connection_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(BroadcastTarget)
        .join(Broadcast, Broadcast.id == BroadcastTarget.broadcast_id)
        .where(Broadcast.connection_id == connection_id, BroadcastTarget.status == JobStatus.leased)
    )
    return int(result.scalar_one())
