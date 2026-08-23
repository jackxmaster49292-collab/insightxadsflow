"""Forwarding worker.

Claims due jobs with ``FOR UPDATE SKIP LOCKED``, delivers them, and heartbeats
its lease so a crash returns the work to the queue instead of losing it.

Concurrency is bounded globally *and* per connection, so one busy connection
cannot consume all worker capacity.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import time
import uuid

import structlog

from app import preflight
from app.config import get_settings
from app.db.models import (
    BroadcastTarget,
    ControlTask,
    ControlTaskKind,
    ControlTaskStatus,
    ForwardingJob,
    JobStatus,
)
from app.db.session import dispose_engine, session_scope
from app.logging_setup import configure_logging
from app.repositories import broadcasts as broadcast_repo
from app.repositories import connections as connection_repo
from app.repositories import jobs as job_repo
from app.security.ratelimit import close_redis, connection_bucket, destination_bucket
from app.services import broadcast as broadcast_service
from app.services import connections as connection_service
from app.services import delivery, sync

log = structlog.get_logger(__name__)

WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
_shutdown = asyncio.Event()


async def _pace(connection_id: uuid.UUID, chat) -> None:  # type: ignore[no-untyped-def]
    """Stay under the documented platform limits. A Telegram flood wait always
    overrides this — see delivery._handle_failure."""
    is_group = chat.chat_kind.value in ("group", "supergroup")
    now = time.time()
    for bucket in (
        destination_bucket(
            str(connection_id), f"{chat.peer_type.value}:{chat.peer_id}", is_group=is_group
        ),
        connection_bucket(str(connection_id)),
    ):
        wait = await bucket.acquire(now_s=now)
        if wait > 0:
            await asyncio.sleep(min(wait, 30))


async def process_job(job_ref: ForwardingJob) -> None:
    settings = get_settings()
    job_id = job_ref.id

    async with session_scope() as session:
        job = await session.get(ForwardingJob, job_id)
        if job is None:
            return
        connection = await connection_repo.get_unscoped_for_worker(
            session, connection_id=job.connection_id
        )
        if connection is None:
            return

        destination_chat = await delivery._load_chat(session, job.destination_chat_id)
        if destination_chat is not None:
            await _pace(job.connection_id, destination_chat)

        adapter = await connection_service.adapter_for(session, connection)
        heartbeat = asyncio.create_task(_heartbeat(job.id))
        try:
            outcome = await delivery.execute_job(
                session, job=job, adapter=adapter, connection=connection
            )
            log.info(
                "job_processed",
                job_id=str(job.id),
                status=outcome.status.value,
                reason_code=outcome.reason_code,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if settings.live_telegram:
                with contextlib.suppress(Exception):
                    await adapter.disconnect()


async def _heartbeat(job_id: uuid.UUID) -> None:
    settings = get_settings()
    interval = max(5, settings.lease_seconds // 3)
    while True:
        await asyncio.sleep(interval)
        async with session_scope() as session:
            await job_repo.heartbeat(
                session, job_id=job_id, owner=WORKER_ID, lease_seconds=settings.lease_seconds
            )


async def process_broadcast_target(target_ref: BroadcastTarget) -> None:
    """Post one broadcast to one group.

    Structurally identical to :func:`process_job` — same pacing, same lease
    heartbeat, same adapter lifecycle — because the guarantees a broadcast needs
    are the guarantees forwarding needs.
    """
    settings = get_settings()
    target_id = target_ref.id

    async with session_scope() as session:
        target = await session.get(BroadcastTarget, target_id)
        if target is None:
            return
        broadcast = await broadcast_repo.get_unscoped_for_worker(
            session, broadcast_id=target.broadcast_id
        )
        if broadcast is None:
            return
        connection = await connection_repo.get_unscoped_for_worker(
            session, connection_id=broadcast.connection_id
        )
        if connection is None:
            return

        chat = await delivery._load_chat(session, target.chat_id)
        if chat is not None:
            await _pace(broadcast.connection_id, chat)

        adapter = await connection_service.adapter_for(session, connection)
        heartbeat = asyncio.create_task(_broadcast_heartbeat(target.id))
        try:
            outcome = await broadcast_service.execute_target(
                session, target=target, adapter=adapter, connection=connection
            )
            log.info(
                "broadcast_target_processed",
                target_id=str(target.id),
                broadcast_id=str(broadcast.id),
                status=outcome.status.value,
                reason_code=outcome.reason_code,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if settings.live_telegram:
                with contextlib.suppress(Exception):
                    await adapter.disconnect()


async def _broadcast_heartbeat(target_id: uuid.UUID) -> None:
    settings = get_settings()
    interval = max(5, settings.lease_seconds // 3)
    while True:
        await asyncio.sleep(interval)
        async with session_scope() as session:
            await broadcast_repo.heartbeat(
                session, target_id=target_id, owner=WORKER_ID, lease_seconds=settings.lease_seconds
            )


async def process_control_task(task: ControlTask) -> None:
    async with session_scope() as session:
        row = await session.get(ControlTask, task.id)
        if row is None:
            return
        connection = (
            await connection_repo.get_unscoped_for_worker(session, connection_id=row.connection_id)
            if row.connection_id
            else None
        )
        try:
            if connection is None:
                row.status = ControlTaskStatus.failed
                row.last_error_code = "connection_missing"
                return

            adapter = await connection_service.adapter_for(session, connection)

            if row.kind is ControlTaskKind.sync_chats:
                report = await sync.synchronize(session, connection=connection, adapter=adapter)
                log.info(
                    "sync_complete",
                    connection_id=str(connection.id),
                    discovered=report.discovered,
                    source_eligible=report.source_eligible,
                    destination_eligible=report.destination_eligible,
                    deactivated=report.deactivated,
                    errors=report.errors,
                )
                await _report_sync_result(session, task=row, connection=connection, report=report)
            elif row.kind is ControlTaskKind.health_check:
                await connection_service.run_health_check(session, connection=connection)
            elif row.kind is ControlTaskKind.check_chat_access:
                chat_id = uuid.UUID(str(row.payload.get("chat_id")))
                chat = await delivery._load_chat(session, chat_id)
                if chat is not None:
                    await sync.recheck_chat(session, chat=chat, adapter=adapter)
            elif row.kind is ControlTaskKind.disconnect:
                await connection_service.disconnect(
                    session, connection=connection, revoke=bool(row.payload.get("revoke"))
                )

            row.status = ControlTaskStatus.succeeded
        except Exception as exc:
            from app.adapters.errors import classify_error

            classified = classify_error(exc)
            row.attempt_count += 1
            row.last_error_code = classified.code
            row.status = (
                ControlTaskStatus.failed
                if row.attempt_count >= 3 or not classified.retryable
                else ControlTaskStatus.pending
            )
            log.warning("control_task_failed", task_id=str(row.id), code=classified.code)

            if row.status is ControlTaskStatus.failed:
                # The panel showed "queued" and nothing else. Without this, a
                # failure here is indistinguishable from a button that does
                # nothing — which is precisely how it was reported.
                await _report_control_failure(session, task=row, code=classified.code)


#: What the customer is told when a queued command gives up, keyed by kind.
CONTROL_TASK_LABELS = {
    ControlTaskKind.sync_chats: "Reading your groups",
    ControlTaskKind.health_check: "Checking the connection",
    ControlTaskKind.disconnect: "Disconnecting",
    ControlTaskKind.check_chat_access: "Re-checking a group",
}


async def _report_sync_result(session, *, task, connection, report) -> None:  # type: ignore[no-untyped-def]
    """Tell the customer what the sync found, including when it found nothing.

    Zero is the answer that most needs saying out loud: it is what a broken
    sync and an empty account look like from the panel, and they need different
    responses.
    """
    from app.repositories import admins as admin_repo

    if report.discovered == 0:
        body = (
            "I read this account's chat list and it came back empty.\n\n"
            "That usually means the account has not joined any groups yet, or "
            "the sign-in is no longer valid. Try Check health on the connection."
        )
    else:
        body = (
            f"{report.discovered} chats read.\n\n"
            f"• {report.destination_eligible} you can post in — these are what "
            "an ad can be sent to\n"
            f"• {report.source_eligible} you can read from — these can be a "
            "forwarding source\n"
        )
        if report.errors:
            body += f"\n{report.errors} could not be checked and were left unavailable."

    await admin_repo.notify(
        session,
        user_id=task.user_id,
        kind="sync_complete",
        title=f"Groups synced — {connection.label}",
        body=body,
        # One alert per run of this task, so a retry cannot double up.
        dedupe_key=f"sync_complete:{task.id}",
        connection_id=connection.id,
    )


async def _report_control_failure(session, *, task: ControlTask, code: str) -> None:  # type: ignore[no-untyped-def]
    """Push a plain-language alert for a queued command that failed for good."""
    from app.domain import reasons
    from app.repositories import admins as admin_repo

    label = CONTROL_TASK_LABELS.get(task.kind, "A background command")
    await admin_repo.notify(
        session,
        user_id=task.user_id,
        kind="control_task_failed",
        title=f"{label} failed",
        body=(
            f"{label} did not work.\n\n{reasons.describe(code)}\n\n"
            "Nothing was changed. You can try again from the panel."
        ),
        # One alert per task, so a retried task cannot produce a storm.
        dedupe_key=f"control_task_failed:{task.id}",
        connection_id=task.connection_id,
    )


async def run() -> None:
    settings = get_settings()
    configure_logging(json_output=settings.environment != "local")
    await preflight.run()
    log.info("worker_starting", worker_id=WORKER_ID, concurrency=settings.worker_concurrency)

    semaphore = asyncio.Semaphore(settings.worker_concurrency)
    running: set[asyncio.Task[None]] = set()

    async def guarded(coro) -> None:  # type: ignore[no-untyped-def]
        async with semaphore:
            try:
                await coro
            except Exception as exc:
                log.error("worker_task_failed", error=exc)

    while not _shutdown.is_set():
        claimed_any = False

        async with session_scope() as session:
            control_tasks = await job_repo.claim_control_batch(
                session, owner=WORKER_ID, limit=4, lease_seconds=settings.lease_seconds
            )
        for task in control_tasks:
            claimed_any = True
            running.add(asyncio.create_task(guarded(process_control_task(task))))

        capacity = settings.worker_concurrency - len(running)
        if capacity > 0:
            async with session_scope() as session:
                jobs = await _claim_within_limits(session, capacity)
            for job in jobs:
                claimed_any = True
                running.add(asyncio.create_task(guarded(process_job(job))))

        # Broadcasts share the same worker pool and the same per-connection
        # in-flight budget. Claiming them after forwarding jobs means a large
        # broadcast cannot starve the forwarding a customer set up first.
        capacity = settings.worker_concurrency - len(running)
        if capacity > 0:
            async with session_scope() as session:
                targets = await _claim_broadcasts_within_limits(session, capacity)
            for target in targets:
                claimed_any = True
                running.add(asyncio.create_task(guarded(process_broadcast_target(target))))

        running = {task for task in running if not task.done()}
        if not claimed_any:
            # Backpressure-friendly idle. A pub/sub wake-up is a V1 optimization.
            await asyncio.sleep(1.0)

    log.info("worker_draining", in_flight=len(running))
    if running:
        await asyncio.gather(*running, return_exceptions=True)
    await close_redis()
    await dispose_engine()
    log.info("worker_stopped")


async def _claim_within_limits(session, capacity: int) -> list[ForwardingJob]:  # type: ignore[no-untyped-def]
    """Claim jobs, then hand back any that exceed a connection's in-flight limit."""
    settings = get_settings()
    jobs = await job_repo.claim_batch(
        session, owner=WORKER_ID, limit=capacity, lease_seconds=settings.lease_seconds
    )
    if not jobs:
        return []

    # The batch's own rows are already leased, so they are excluded from the
    # baseline: measuring the ceiling against the work being admitted is what
    # collapsed throughput to one item at a time.
    batch_ids = [job.id for job in jobs]
    accepted: list[ForwardingJob] = []
    per_connection: dict[uuid.UUID, int] = {}
    for job in jobs:
        in_flight = per_connection.get(job.connection_id)
        if in_flight is None:
            in_flight = await job_repo.in_flight_count(
                session, connection_id=job.connection_id, exclude_ids=batch_ids
            )
        if in_flight >= settings.per_connection_inflight:
            # Backpressure: release the lease rather than hold one we will not act on.
            job.status = JobStatus.pending
            job.lease_owner = None
            job.lease_expires_at = None
            continue
        per_connection[job.connection_id] = in_flight + 1
        accepted.append(job)
    return accepted


async def _claim_broadcasts_within_limits(session, capacity: int) -> list[BroadcastTarget]:  # type: ignore[no-untyped-def]
    """Claim broadcast targets, respecting the broadcast in-flight cap.

    The count still includes forwarding jobs *and* broadcast targets together:
    counting them separately would let one connection run two budgets at once
    by doing both, which is what a shared ceiling exists to prevent. What
    differs is the ceiling itself — ``broadcast_inflight`` rather than
    ``per_connection_inflight`` — because every target of a broadcast is a
    different group, so the per-group limit never binds and the connection-wide
    pacer is the real gate. Forwarding keeps its lower ceiling.
    """
    settings = get_settings()
    targets = await broadcast_repo.claim_batch(
        session, owner=WORKER_ID, limit=capacity, lease_seconds=settings.lease_seconds
    )
    if not targets:
        return []

    # One lookup per broadcast, since every target of a broadcast shares its
    # connection.
    connection_of: dict[uuid.UUID, uuid.UUID] = {}
    accepted: list[BroadcastTarget] = []
    per_connection: dict[uuid.UUID, int] = {}
    batch_ids = [target.id for target in targets]

    for target in targets:
        connection_id = connection_of.get(target.broadcast_id)
        if connection_id is None:
            broadcast = await broadcast_repo.get_unscoped_for_worker(
                session, broadcast_id=target.broadcast_id
            )
            if broadcast is None:
                target.status = JobStatus.skipped
                continue
            connection_id = broadcast.connection_id
            connection_of[target.broadcast_id] = connection_id

        in_flight = per_connection.get(connection_id)
        if in_flight is None:
            in_flight = await job_repo.in_flight_count(
                session, connection_id=connection_id
            ) + await broadcast_repo.in_flight_count(
                session, connection_id=connection_id, exclude_ids=batch_ids
            )
        if in_flight >= settings.broadcast_inflight:
            # Backpressure: release the lease rather than hold one we will not act on.
            target.status = JobStatus.pending
            target.lease_owner = None
            target.lease_expires_at = None
            continue
        per_connection[connection_id] = in_flight + 1
        accepted.append(target)
    return accepted


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown.set)
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
