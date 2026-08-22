"""Scheduler — exactly one instance.

Does the housekeeping that makes crash recovery real:

* reclaims leases from crashed workers (this is *how* a crashed job comes back);
* runs periodic connection health checks;
* enforces retention on events, terminal jobs, and idempotency keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, select

from app import preflight
from app.config import get_settings
from app.db.models import (
    AppSession,
    ConnectionStatus,
    ForwardingEvent,
    IdempotencyKey,
    TelegramConnection,
)
from app.db.session import dispose_engine, session_scope
from app.logging_setup import configure_logging
from app.repositories import autoreply as autoreply_repo
from app.repositories import broadcasts as broadcast_repo
from app.repositories import jobs as job_repo
from app.security.ratelimit import close_redis
from app.services import connections as connection_service

log = structlog.get_logger(__name__)

_shutdown = asyncio.Event()

RECLAIM_INTERVAL_S = 15
HEALTH_INTERVAL_S = 300
RETENTION_INTERVAL_S = 3600


async def reclaim_loop() -> None:
    while not _shutdown.is_set():
        try:
            async with session_scope() as session:
                jobs = await job_repo.reclaim_expired(session)
                tasks = await job_repo.reclaim_expired_control(session)
                targets = await broadcast_repo.reclaim_expired(session)
            if jobs or tasks or targets:
                log.info(
                    "leases_reclaimed",
                    jobs=jobs,
                    control_tasks=tasks,
                    broadcast_targets=targets,
                )
        except Exception as exc:
            log.error("reclaim_failed", error=exc)
        await asyncio.sleep(RECLAIM_INTERVAL_S)


async def health_loop() -> None:
    while not _shutdown.is_set():
        await asyncio.sleep(HEALTH_INTERVAL_S)
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(TelegramConnection).where(
                        TelegramConnection.status.in_(
                            [ConnectionStatus.active, ConnectionStatus.error]
                        )
                    )
                )
                for connection in result.scalars().all():
                    with contextlib.suppress(Exception):
                        await connection_service.run_health_check(session, connection=connection)
        except Exception as exc:
            log.error("health_loop_failed", error=exc)


async def retention_loop() -> None:
    settings = get_settings()
    while not _shutdown.is_set():
        await asyncio.sleep(RETENTION_INTERVAL_S)
        try:
            now = datetime.now(UTC)
            async with session_scope() as session:
                events = await session.execute(
                    delete(ForwardingEvent)
                    .where(
                        ForwardingEvent.occurred_at
                        < now - timedelta(days=settings.event_retention_days)
                    )
                    .returning(ForwardingEvent.id)
                )
                keys = await session.execute(
                    delete(IdempotencyKey)
                    .where(IdempotencyKey.expires_at < now)
                    .returning(IdempotencyKey.key)
                )
                sessions = await session.execute(
                    delete(AppSession)
                    .where(AppSession.absolute_expires_at < now - timedelta(days=30))
                    .returning(AppSession.id)
                )
                jobs = await job_repo.purge_terminal(session, older_than_days=30)
                # Only entries older than any usable cooldown. Purging an
                # in-force entry would let the same person be answered twice.
                replies = await autoreply_repo.purge_log(session, older_than_days=30)
            log.info(
                "retention_applied",
                events=len(events.all()),
                idempotency_keys=len(keys.all()),
                app_sessions=len(sessions.all()),
                jobs=jobs,
                auto_reply_log=replies,
            )
        except Exception as exc:
            log.error("retention_failed", error=exc)


async def run() -> None:
    settings = get_settings()
    configure_logging(json_output=settings.environment != "local")
    await preflight.run()
    log.info("scheduler_starting")

    tasks = [
        asyncio.create_task(reclaim_loop()),
        asyncio.create_task(health_loop()),
        asyncio.create_task(retention_loop()),
    ]
    await _shutdown.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await close_redis()
    await dispose_engine()
    log.info("scheduler_stopped")


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
