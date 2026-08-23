"""Queued commands: connecting first, and saying so when they fail.

Sync groups and the health check are queued and run in the worker, which makes
their failures invisible from the panel — the toast says "queued" and the screen
never changes. Both halves of that are tested here: the adapter reconnecting so
the work can happen at all, and the alert that fires when it cannot.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.adapters.errors import AdapterError, ErrorClass
from app.db.models import (
    AdminNotification,
    ControlTask,
    ControlTaskKind,
    ControlTaskStatus,
    TelegramChat,
)
from app.db.session import session_scope
from app.domain import reasons
from app.repositories import jobs as job_repo
from app.worker import process_control_task
from tests.conftest import connect_bot, discovered, script_for


async def queue_task(actor, connection_id: str, kind: ControlTaskKind) -> ControlTask:
    async with session_scope() as session:
        task = await job_repo.enqueue_control(
            session,
            user_id=uuid.UUID(actor.id),
            kind=kind,
            connection_id=uuid.UUID(connection_id),
        )
        return task


# --------------------------------------------------------------------------- #
# The happy path still works
# --------------------------------------------------------------------------- #
async def test_syncing_discovers_groups(client, actor, session):
    connection_id = await connect_bot(actor)
    script = script_for(connection_id)
    script.chats = [
        discovered(-1002000, "Group one", chat_kind="supergroup"),
        discovered(-1002001, "Group two", chat_kind="supergroup"),
    ]

    task = await queue_task(actor, connection_id, ControlTaskKind.sync_chats)
    await process_control_task(task)

    chats = (await session.execute(select(TelegramChat))).scalars().all()
    assert {c.title for c in chats} == {"Group one", "Group two"}

    session.expire_all()
    row = await session.get(ControlTask, task.id)
    assert row.status is ControlTaskStatus.succeeded


async def test_a_health_check_runs(client, actor, session):
    connection_id = await connect_bot(actor)
    task = await queue_task(actor, connection_id, ControlTaskKind.health_check)
    await process_control_task(task)

    session.expire_all()
    row = await session.get(ControlTask, task.id)
    assert row.status is ControlTaskStatus.succeeded
    assert script_for(connection_id).calls_to("health_check")


# --------------------------------------------------------------------------- #
# A failure has to be visible
# --------------------------------------------------------------------------- #
async def test_a_sync_that_fails_for_good_tells_the_customer(client, actor, session):
    """Otherwise a broken Sync is indistinguishable from a button that does
    nothing — which is how it was reported."""
    connection_id = await connect_bot(actor)
    script = script_for(connection_id)

    task = await queue_task(actor, connection_id, ControlTaskKind.sync_chats)
    for _ in range(3):
        # Not retryable, so it gives up on the first attempt; queued again here
        # only to prove repeats do not multiply the alert.
        script.fail_method(
            "list_available_chats", AdapterError(reasons.UNAUTHORIZED, ErrorClass.AUTH)
        )
        await process_control_task(task)

    session.expire_all()
    row = await session.get(ControlTask, task.id)
    assert row.status is ControlTaskStatus.failed

    alerts = (await session.execute(select(AdminNotification))).scalars().all()
    assert len(alerts) == 1, "one alert per task, not one per attempt"
    assert "Reading your groups" in alerts[0].title
    assert alerts[0].sent_at is None, "queued for the bot to deliver"


async def test_the_alert_explains_the_cause_in_plain_language(client, actor, session):
    connection_id = await connect_bot(actor)
    script_for(connection_id).fail_method(
        "list_available_chats", AdapterError(reasons.UNAUTHORIZED, ErrorClass.AUTH)
    )

    task = await queue_task(actor, connection_id, ControlTaskKind.sync_chats)
    await process_control_task(task)

    alert = (await session.execute(select(AdminNotification))).scalar_one()
    assert reasons.describe(reasons.UNAUTHORIZED) in alert.body
    assert "Nothing was changed" in alert.body
    assert "Traceback" not in alert.body


async def test_a_retryable_failure_stays_quiet_until_it_gives_up(client, actor, session):
    """A network hiccup that will be retried is not worth an alert."""
    connection_id = await connect_bot(actor)
    script = script_for(connection_id)
    script.fail_method(
        "list_available_chats", AdapterError(reasons.NETWORK_ERROR, ErrorClass.TRANSIENT)
    )

    task = await queue_task(actor, connection_id, ControlTaskKind.sync_chats)
    await process_control_task(task)

    session.expire_all()
    row = await session.get(ControlTask, task.id)
    assert row.status is ControlTaskStatus.pending, "still to be retried"
    assert not (await session.execute(select(AdminNotification))).scalars().all()

    # Third strike: now it is worth saying something.
    for _ in range(2):
        await process_control_task(task)

    session.expire_all()
    row = await session.get(ControlTask, task.id)
    assert row.status is ControlTaskStatus.failed
    assert len((await session.execute(select(AdminNotification))).scalars().all()) == 1


async def test_each_failing_command_gets_its_own_label(client, actor, session):
    connection_id = await connect_bot(actor)
    script = script_for(connection_id)
    error = AdapterError(reasons.UNAUTHORIZED, ErrorClass.AUTH)
    script.fail_method("list_available_chats", error)
    script.fail_method("health_check", error)

    for kind in (ControlTaskKind.sync_chats, ControlTaskKind.health_check):
        task = await queue_task(actor, connection_id, kind)
        await process_control_task(task)

    titles = {n.title for n in (await session.execute(select(AdminNotification))).scalars().all()}
    assert titles == {"Reading your groups failed", "Checking the connection failed"}
