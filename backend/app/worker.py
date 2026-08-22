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
    ControlTask,
    ControlTaskKind,
    ControlTaskStatus,
    ForwardingJob,
    JobStatus,
)
from app.db.session import dispose_engine, session_scope
from app.logging_setup import configure_logging
from app.repositories import connections as connection_repo
from app.repositories import jobs as job_repo
from app.security.ratelimit import close_redis, connection_bucket, destination_bucket
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
                )
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

    accepted: list[ForwardingJob] = []
    per_connection: dict[uuid.UUID, int] = {}
    for job in jobs:
        in_flight = per_connection.get(job.connection_id)
        if in_flight is None:
            in_flight = await job_repo.in_flight_count(session, connection_id=job.connection_id)
        if in_flight >= settings.per_connection_inflight:
            # Backpressure: release the lease rather than hold one we will not act on.
            job.status = JobStatus.pending
            job.lease_owner = None
            job.lease_expires_at = None
            continue
        per_connection[job.connection_id] = in_flight + 1
        accepted.append(job)
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
