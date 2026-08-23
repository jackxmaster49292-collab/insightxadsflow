"""Durable job store.

Postgres is the source of truth (ADR-006). Claiming uses
``FOR UPDATE SKIP LOCKED`` so N workers can drain the queue without coordination,
and a lease + heartbeat means a crashed worker's jobs return to the queue rather
than being lost.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ControlTask,
    ControlTaskKind,
    ControlTaskStatus,
    ForwardingJob,
    JobStatus,
)


def now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Forwarding jobs
# --------------------------------------------------------------------------- #
async def create_if_absent(
    session: AsyncSession,
    *,
    rule_id: uuid.UUID,
    rule_version: int,
    connection_id: uuid.UUID,
    source_chat_id: uuid.UUID,
    source_message_ids: Sequence[int],
    destination_chat_id: uuid.UUID,
    idempotency_key: str,
    mtproto_random_id: int | None,
    not_before: datetime | None = None,
) -> ForwardingJob | None:
    """Insert, or return ``None`` when this delivery already exists.

    The unique constraint on ``idempotency_key`` is what actually prevents a
    duplicate — the check is done by the database, not by a prior SELECT that
    could race.
    """
    stmt = (
        pg_insert(ForwardingJob)
        .values(
            id=uuid.uuid4(),
            rule_id=rule_id,
            rule_version=rule_version,
            connection_id=connection_id,
            source_chat_id=source_chat_id,
            source_message_ids=list(source_message_ids),
            destination_chat_id=destination_chat_id,
            idempotency_key=idempotency_key,
            mtproto_random_id=mtproto_random_id,
            status=JobStatus.pending,
            attempt_count=0,
            not_before=not_before or now(),
        )
        .on_conflict_do_nothing(index_elements=[ForwardingJob.idempotency_key])
        .returning(ForwardingJob.id)
    )
    result = await session.execute(stmt)
    job_id = result.scalar_one_or_none()
    if job_id is None:
        return None
    return await session.get(ForwardingJob, job_id)


async def claim_batch(
    session: AsyncSession, *, owner: str, limit: int, lease_seconds: int
) -> list[ForwardingJob]:
    """Atomically lease up to ``limit`` due jobs."""
    deadline = now() + timedelta(seconds=lease_seconds)
    candidates = (
        select(ForwardingJob.id)
        .where(ForwardingJob.status == JobStatus.pending, ForwardingJob.not_before <= now())
        .order_by(ForwardingJob.not_before.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(
        update(ForwardingJob)
        .where(ForwardingJob.id.in_(candidates.scalar_subquery()))
        .values(status=JobStatus.leased, lease_owner=owner, lease_expires_at=deadline)
        .returning(ForwardingJob.id)
    )
    ids = [row[0] for row in result.all()]
    if not ids:
        return []
    fetched = await session.execute(select(ForwardingJob).where(ForwardingJob.id.in_(ids)))
    return list(fetched.scalars().all())


async def heartbeat(
    session: AsyncSession, *, job_id: uuid.UUID, owner: str, lease_seconds: int
) -> None:
    await session.execute(
        update(ForwardingJob)
        .where(ForwardingJob.id == job_id, ForwardingJob.lease_owner == owner)
        .values(lease_expires_at=now() + timedelta(seconds=lease_seconds))
    )


async def reclaim_expired(session: AsyncSession, *, limit: int = 200) -> int:
    """Return jobs abandoned by a crashed worker to the queue."""
    result = await session.execute(
        update(ForwardingJob)
        .where(
            ForwardingJob.status == JobStatus.leased,
            ForwardingJob.lease_expires_at.isnot(None),
            ForwardingJob.lease_expires_at < now(),
            ForwardingJob.id.in_(
                select(ForwardingJob.id)
                .where(
                    ForwardingJob.status == JobStatus.leased,
                    ForwardingJob.lease_expires_at < now(),
                )
                .limit(limit)
                .scalar_subquery()
            ),
        )
        .values(status=JobStatus.pending, lease_owner=None, lease_expires_at=None)
        .returning(ForwardingJob.id)
    )
    return len(result.all())


async def finish(
    session: AsyncSession,
    *,
    job: ForwardingJob,
    status: JobStatus,
    destination_message_id: int | None = None,
    error_class: str | None = None,
    error_code: str | None = None,
) -> None:
    job.status = status
    job.lease_owner = None
    job.lease_expires_at = None
    job.destination_message_id = destination_message_id
    job.last_error_class = error_class
    job.last_error_code = error_code


async def reschedule(
    session: AsyncSession, *, job: ForwardingJob, delay_s: float, error_class: str, error_code: str
) -> None:
    job.status = JobStatus.pending
    job.lease_owner = None
    job.lease_expires_at = None
    job.attempt_count += 1
    job.not_before = now() + timedelta(seconds=delay_s)
    job.last_error_class = error_class
    job.last_error_code = error_code


async def get_for_rule(
    session: AsyncSession, *, rule_id: uuid.UUID, statuses: Sequence[JobStatus]
) -> list[ForwardingJob]:
    result = await session.execute(
        select(ForwardingJob).where(
            ForwardingJob.rule_id == rule_id, ForwardingJob.status.in_(list(statuses))
        )
    )
    return list(result.scalars().all())


async def cancel_pending_for_rule(session: AsyncSession, *, rule_id: uuid.UUID) -> int:
    result = await session.execute(
        update(ForwardingJob)
        .where(
            ForwardingJob.rule_id == rule_id,
            ForwardingJob.status.in_([JobStatus.pending, JobStatus.leased]),
        )
        .values(status=JobStatus.skipped, lease_owner=None, lease_expires_at=None)
        .returning(ForwardingJob.id)
    )
    return len(result.all())


async def cancel_pending_for_connection(session: AsyncSession, *, connection_id: uuid.UUID) -> int:
    result = await session.execute(
        update(ForwardingJob)
        .where(
            ForwardingJob.connection_id == connection_id,
            ForwardingJob.status.in_([JobStatus.pending, JobStatus.leased]),
        )
        .values(status=JobStatus.skipped, lease_owner=None, lease_expires_at=None)
        .returning(ForwardingJob.id)
    )
    return len(result.all())


async def requeue_failed(session: AsyncSession, *, rule_id: uuid.UUID) -> int:
    """Retry only genuinely unfinished work. Successes are never replayed."""
    result = await session.execute(
        update(ForwardingJob)
        .where(
            ForwardingJob.rule_id == rule_id,
            ForwardingJob.status.in_(
                [JobStatus.failed, JobStatus.needs_attention, JobStatus.dead_letter]
            ),
        )
        .values(status=JobStatus.pending, attempt_count=0, not_before=now(), lease_owner=None)
        .returning(ForwardingJob.id)
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
        .select_from(ForwardingJob)
        .where(
            ForwardingJob.connection_id == connection_id,
            ForwardingJob.status == JobStatus.leased,
        )
    )
    if exclude_ids:
        query = query.where(ForwardingJob.id.notin_(exclude_ids))
    result = await session.execute(query)
    return int(result.scalar_one())


async def count_recent_failures(
    session: AsyncSession, *, rule_id: uuid.UUID, window_minutes: int = 60
) -> int:
    since = now() - timedelta(minutes=window_minutes)
    result = await session.execute(
        select(func.count())
        .select_from(ForwardingJob)
        .where(
            ForwardingJob.rule_id == rule_id,
            ForwardingJob.updated_at >= since,
            ForwardingJob.status.in_([JobStatus.failed, JobStatus.dead_letter]),
        )
    )
    return int(result.scalar_one())


async def purge_terminal(session: AsyncSession, *, older_than_days: int) -> int:
    from sqlalchemy import delete

    cutoff = now() - timedelta(days=older_than_days)
    result = await session.execute(
        delete(ForwardingJob)
        .where(
            ForwardingJob.updated_at < cutoff,
            ForwardingJob.status.in_(
                [JobStatus.succeeded, JobStatus.skipped, JobStatus.dead_letter]
            ),
        )
        .returning(ForwardingJob.id)
    )
    return len(result.all())


# --------------------------------------------------------------------------- #
# Control tasks — how the API returns 202 without ever blocking on Telegram
# --------------------------------------------------------------------------- #
async def enqueue_control(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: ControlTaskKind,
    connection_id: uuid.UUID | None = None,
    rule_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> ControlTask:
    task = ControlTask(
        user_id=user_id,
        kind=kind,
        connection_id=connection_id,
        rule_id=rule_id,
        payload=payload or {},
    )
    session.add(task)
    await session.flush()
    return task


async def claim_control_batch(
    session: AsyncSession, *, owner: str, limit: int, lease_seconds: int
) -> list[ControlTask]:
    deadline = now() + timedelta(seconds=lease_seconds)
    candidates = (
        select(ControlTask.id)
        .where(ControlTask.status == ControlTaskStatus.pending, ControlTask.not_before <= now())
        .order_by(ControlTask.not_before.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(
        update(ControlTask)
        .where(ControlTask.id.in_(candidates.scalar_subquery()))
        .values(status=ControlTaskStatus.leased, lease_owner=owner, lease_expires_at=deadline)
        .returning(ControlTask.id)
    )
    ids = [row[0] for row in result.all()]
    if not ids:
        return []
    fetched = await session.execute(select(ControlTask).where(ControlTask.id.in_(ids)))
    return list(fetched.scalars().all())


async def reclaim_expired_control(session: AsyncSession) -> int:
    result = await session.execute(
        update(ControlTask)
        .where(
            ControlTask.status == ControlTaskStatus.leased,
            ControlTask.lease_expires_at.isnot(None),
            ControlTask.lease_expires_at < now(),
        )
        .values(status=ControlTaskStatus.pending, lease_owner=None, lease_expires_at=None)
        .returning(ControlTask.id)
    )
    return len(result.all())


async def pending_control_for(
    session: AsyncSession, *, user_id: uuid.UUID, kind: ControlTaskKind, connection_id: uuid.UUID
) -> ControlTask | None:
    result = await session.execute(
        select(ControlTask).where(
            ControlTask.user_id == user_id,
            ControlTask.kind == kind,
            ControlTask.connection_id == connection_id,
            or_(
                ControlTask.status == ControlTaskStatus.pending,
                and_(
                    ControlTask.status == ControlTaskStatus.leased,
                    ControlTask.lease_expires_at > now(),
                ),
            ),
        )
    )
    return result.scalars().first()
