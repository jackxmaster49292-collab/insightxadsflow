"""Event intake.

One process can host many connections; a **Redis lock per connection** ensures
only one listener anywhere opens a client for a given connection. That matters
concretely: Telegram returns 409 for a second ``getUpdates`` on one bot token,
and two MTProto clients sharing a session produce duplicate updates.

Grouped media is buffered by ``media_group_id`` / ``grouped_id`` for a short
bounded window and emitted as one logical message. A group that never completes
is emitted anyway, flagged ``partial_album`` — never dropped, never held forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import uuid

import structlog

from app.adapters.base import InboundMessage
from app.config import get_settings
from app.db.models import ConnectionKind, TelegramConnection
from app.db.session import dispose_engine, session_scope
from app.logging_setup import configure_logging
from app.repositories import connections as connection_repo
from app.security.ratelimit import close_redis, get_redis
from app.services import connections as connection_service
from app.services.dispatch import dispatch_inbound

log = structlog.get_logger(__name__)

LISTENER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
LOCK_TTL_S = 60
_shutdown = asyncio.Event()


class ConnectionLock:
    """Single-owner lock with heartbeat renewal."""

    def __init__(self, connection_id: uuid.UUID) -> None:
        self.key = f"listener:lock:{connection_id}"
        self._task: asyncio.Task[None] | None = None

    async def acquire(self) -> bool:
        try:
            return bool(await get_redis().set(self.key, LISTENER_ID, nx=True, ex=LOCK_TTL_S))
        except Exception as exc:
            log.warning("lock_unavailable", key=self.key, error=exc)
            return False

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(LOCK_TTL_S / 3)
            with contextlib.suppress(Exception):
                current = await get_redis().get(self.key)
                if current == LISTENER_ID:
                    await get_redis().expire(self.key, LOCK_TTL_S)

    def start_renewal(self) -> None:
        self._task = asyncio.create_task(self._renew())

    async def release(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        with contextlib.suppress(Exception):
            if await get_redis().get(self.key) == LISTENER_ID:
                await get_redis().delete(self.key)


class AlbumBuffer:
    """Collects grouped media into one logical message."""

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._pending: dict[int, InboundMessage] = {}
        self._timers: dict[int, asyncio.Task[None]] = {}

    async def add(self, message: InboundMessage, emit: asyncio.Queue[InboundMessage]) -> None:
        group = message.grouped_id
        if group is None:
            await emit.put(message)
            return

        existing = self._pending.get(group)
        if existing is None:
            self._pending[group] = message
            self._timers[group] = asyncio.create_task(self._flush_later(group, emit))
        else:
            existing.message_ids = sorted(set(existing.message_ids) | set(message.message_ids))

    async def _flush_later(self, group: int, emit: asyncio.Queue[InboundMessage]) -> None:
        await asyncio.sleep(self.window_s)
        message = self._pending.pop(group, None)
        self._timers.pop(group, None)
        if message is not None:
            await emit.put(message)

    async def drain(self, emit: asyncio.Queue[InboundMessage]) -> None:
        for group, message in list(self._pending.items()):
            message.partial_album = True
            await emit.put(message)
            self._pending.pop(group, None)
        for task in self._timers.values():
            task.cancel()
        self._timers.clear()


async def run_connection(connection_id: uuid.UUID) -> None:
    settings = get_settings()
    lock = ConnectionLock(connection_id)
    if not await lock.acquire():
        log.info("listener_lock_held_elsewhere", connection_id=str(connection_id))
        return
    lock.start_renewal()

    queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
    buffer = AlbumBuffer(settings.album_buffer_ms / 1000)

    try:
        async with session_scope() as session:
            connection = await connection_repo.get_unscoped_for_worker(
                session, connection_id=connection_id
            )
            if connection is None:
                return
            adapter = await connection_service.adapter_for(session, connection)
            kind = connection.kind

        await _restore_cursor(adapter, connection_id, kind)
        consumer = asyncio.create_task(_consume(connection_id, queue))

        log.info("listener_started", connection_id=str(connection_id), kind=kind.value)
        async for message in adapter.receive_new_messages():
            if _shutdown.is_set():
                break
            await buffer.add(message, queue)

        await buffer.drain(queue)
        await queue.join()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
    except Exception as exc:
        log.error("listener_failed", connection_id=str(connection_id), error=exc)
    finally:
        await lock.release()


async def _restore_cursor(adapter, connection_id: uuid.UUID, kind: ConnectionKind) -> None:  # type: ignore[no-untyped-def]
    """Resume from the durable cursor rather than replaying from the start."""
    if kind is not ConnectionKind.bot:
        return
    from app.db.models import ConnectionUpdateState

    async with session_scope() as session:
        state = await session.get(ConnectionUpdateState, connection_id)
        if state is not None and state.bot_update_offset is not None:
            with contextlib.suppress(AttributeError):
                adapter.offset = state.bot_update_offset


async def _consume(connection_id: uuid.UUID, queue: asyncio.Queue[InboundMessage]) -> None:
    while True:
        message = await queue.get()
        try:
            async with session_scope() as session:
                result = await dispatch_inbound(
                    session, connection_id=connection_id, message=message
                )
            log.info(
                "message_dispatched",
                connection_id=str(connection_id),
                jobs_created=len(result.created_job_ids),
                duplicates_suppressed=result.suppressed,
                matched_rules=result.matched_rules,
                skipped_reason=result.skipped_reason,
            )
        except Exception as exc:
            log.error("dispatch_failed", connection_id=str(connection_id), error=exc)
        finally:
            queue.task_done()


async def run() -> None:
    settings = get_settings()
    configure_logging(json_output=settings.environment != "local")
    log.info("listener_starting", listener_id=LISTENER_ID)

    supervised: dict[uuid.UUID, asyncio.Task[None]] = {}
    while not _shutdown.is_set():
        async with session_scope() as session:
            active: list[TelegramConnection] = list(
                await connection_repo.list_active_for_intake(session)
            )

        for connection in active:
            task = supervised.get(connection.id)
            if task is None or task.done():
                supervised[connection.id] = asyncio.create_task(run_connection(connection.id))

        for connection_id in list(supervised):
            if connection_id not in {c.id for c in active}:
                supervised.pop(connection_id).cancel()

        await asyncio.sleep(10)

    for task in supervised.values():
        task.cancel()
    await asyncio.gather(*supervised.values(), return_exceptions=True)
    await close_redis()
    await dispose_engine()
    log.info("listener_stopped")


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
