"""Dispatch, delivery, retry classification and safety pause.

These are the tests behind the reliability claims in the README: no duplicate
delivery, independent per-destination results, flood waits obeyed in full, and
permanent failures not retried.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.adapters.base import AmbiguousDeliveryError, ChatRef, InboundMessage, MediaType, PeerKind
from app.db.models import (
    ConnectionStatus,
    EventOutcome,
    ForwardingEvent,
    ForwardingJob,
    JobStatus,
    RuleStatus,
    TelegramConnection,
)
from app.domain import reasons
from app.services import connections as connection_service
from app.services.delivery import backoff_seconds, execute_job
from app.services.dispatch import dispatch_inbound
from tests.conftest import chat_ref, connect_bot, discovered, script_for, sync_with_chats

SOURCE_ID = -1001000
DEST_IDS = (-1002000, -1002001, -1002002)


async def build_rule(actor, *, destinations: int = 3, delay_ms: int = 0, **overrides) -> dict:
    """A synchronized connection with one source and N destinations, activated."""
    connection_id = await connect_bot(actor)
    chats = [discovered(SOURCE_ID, "Source")] + [
        discovered(DEST_IDS[i], f"Destination {i + 1}", chat_kind="supergroup")
        for i in range(destinations)
    ]
    await sync_with_chats(actor, connection_id, chats)

    listed = (await actor.get("/telegram/chats")).json()
    source = next(c for c in listed if c["title"] == "Source")
    dests = sorted(
        (c for c in listed if c["title"].startswith("Destination")), key=lambda c: c["title"]
    )

    payload = {
        "name": "Announcements",
        "connection_id": connection_id,
        "source_chat_ids": [source["id"]],
        "destination_chat_ids": [d["id"] for d in dests],
        "delay_ms": delay_ms,
    }
    payload.update(overrides)

    created = await actor.post("/forwarding-rules", payload)
    assert created.status_code == 201, created.text
    rule = created.json()
    activated = await actor.post(f"/forwarding-rules/{rule['id']}/activate", idem=uuid.uuid4().hex)
    assert activated.status_code == 202, activated.text

    return {
        "connection_id": connection_id,
        "rule_id": rule["id"],
        "source": source,
        "destinations": dests,
    }


def inbound(message_id: int = 500, text: str = "hello", media=MediaType.text) -> InboundMessage:
    return InboundMessage(
        source=ChatRef(PeerKind.channel, SOURCE_ID),
        message_ids=[message_id],
        media_type=media,
        text=text,
    )


async def run_dispatch(session, connection_id: str, message: InboundMessage):
    result = await dispatch_inbound(
        session, connection_id=uuid.UUID(connection_id), message=message
    )
    await session.commit()
    return result


async def jobs_for(session, rule_id: str) -> list[ForwardingJob]:
    # Expire first: the API runs on its own session, so anything it committed
    # would otherwise be masked by this session's identity map.
    session.expire_all()
    rows = await session.execute(
        select(ForwardingJob).where(ForwardingJob.rule_id == uuid.UUID(rule_id))
    )
    return list(rows.scalars().all())


async def run_job(session, job: ForwardingJob):
    connection = (
        await session.execute(
            select(TelegramConnection).where(TelegramConnection.id == job.connection_id)
        )
    ).scalar_one()
    adapter = await connection_service.adapter_for(session, connection)
    outcome = await execute_job(session, job=job, adapter=adapter, connection=connection)
    await session.commit()
    return outcome


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
async def test_one_message_creates_one_job_per_destination(client, actor, session):
    ctx = await build_rule(actor)
    result = await run_dispatch(session, ctx["connection_id"], inbound())
    assert len(result.created_job_ids) == 3
    assert len(await jobs_for(session, ctx["rule_id"])) == 3


async def test_replayed_update_creates_zero_new_jobs(client, actor, session):
    """A reconnect or restart re-delivering the same update must not duplicate."""
    ctx = await build_rule(actor)
    await run_dispatch(session, ctx["connection_id"], inbound())

    replay = await run_dispatch(session, ctx["connection_id"], inbound())
    assert replay.created_job_ids == []
    assert replay.suppressed == 3
    assert len(await jobs_for(session, ctx["rule_id"])) == 3


async def test_album_arriving_in_any_order_is_one_job_set(client, actor, session):
    ctx = await build_rule(actor, destinations=1)
    album = inbound()
    album.message_ids = [12, 10, 11]
    await run_dispatch(session, ctx["connection_id"], album)

    reordered = inbound()
    reordered.message_ids = [10, 11, 12]
    replay = await run_dispatch(session, ctx["connection_id"], reordered)
    assert replay.suppressed == 1


async def test_filtered_message_creates_no_jobs_but_records_why(client, actor, session):
    ctx = await build_rule(actor, destinations=1, keyword_include=["launch"])
    result = await run_dispatch(session, ctx["connection_id"], inbound(text="unrelated"))

    assert result.created_job_ids == []
    events = (
        (
            await session.execute(
                select(ForwardingEvent).where(ForwardingEvent.rule_id == uuid.UUID(ctx["rule_id"]))
            )
        )
        .scalars()
        .all()
    )
    assert [e.reason_code for e in events] == [reasons.FILTERED_KEYWORD_INCLUDE]


async def test_paused_rule_creates_no_jobs(client, actor, session):
    ctx = await build_rule(actor, destinations=1)
    await actor.post(f"/forwarding-rules/{ctx['rule_id']}/pause", idem=uuid.uuid4().hex)

    result = await run_dispatch(session, ctx["connection_id"], inbound())
    assert result.matched_rules == 0
    assert result.created_job_ids == []


async def test_unknown_source_chat_is_not_an_authorized_source(client, actor, session):
    ctx = await build_rule(actor, destinations=1)
    stranger = InboundMessage(
        source=ChatRef(PeerKind.channel, -1009999999),
        message_ids=[1],
        media_type=MediaType.text,
        text="hi",
    )
    result = await run_dispatch(session, ctx["connection_id"], stranger)
    assert result.skipped_reason == reasons.SOURCE_NOT_ELIGIBLE
    assert result.created_job_ids == []


async def test_per_rule_delay_staggers_destinations(client, actor, session):
    ctx = await build_rule(actor, delay_ms=1000)
    await run_dispatch(session, ctx["connection_id"], inbound())
    jobs = sorted(await jobs_for(session, ctx["rule_id"]), key=lambda j: j.not_before)
    gaps = [
        (jobs[i + 1].not_before - jobs[i].not_before).total_seconds() for i in range(len(jobs) - 1)
    ]
    assert all(abs(gap - 1.0) < 0.05 for gap in gaps), gaps


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
async def test_successful_delivery_records_a_forwarded_event(client, actor, session):
    ctx = await build_rule(actor, destinations=1)
    await run_dispatch(session, ctx["connection_id"], inbound())
    job = (await jobs_for(session, ctx["rule_id"]))[0]

    outcome = await run_job(session, job)
    assert outcome.status is JobStatus.succeeded
    assert job.destination_message_id is not None

    events = (await actor.get(f"/forwarding-rules/{ctx['rule_id']}/events")).json()
    assert events[0]["outcome"] == EventOutcome.forwarded.value


async def test_each_destination_has_an_independent_result(client, actor, session):
    ctx = await build_rule(actor)
    script = script_for(ctx["connection_id"])
    # Destination 2: transient. Destination 3: permission. Destination 1: fine.
    script.fail_delivery(chat_ref(DEST_IDS[1]), TimeoutError("network blip"))
    script.fail_delivery(chat_ref(DEST_IDS[2]), Exception("Bad Request: CHAT_WRITE_FORBIDDEN"))

    await run_dispatch(session, ctx["connection_id"], inbound())
    for job in await jobs_for(session, ctx["rule_id"]):
        await run_job(session, job)

    by_dest = {j.destination_chat_id: j for j in await jobs_for(session, ctx["rule_id"])}
    statuses = sorted(j.status.value for j in by_dest.values())
    # succeeded, pending (retry scheduled), skipped (permission) — three outcomes.
    assert statuses == ["pending", "skipped", "succeeded"]


async def test_transient_failure_is_retried_and_then_succeeds(client, actor, session):
    ctx = await build_rule(actor, destinations=1)
    script = script_for(ctx["connection_id"])
    script.fail_delivery(chat_ref(DEST_IDS[0]), TimeoutError("blip"))

    await run_dispatch(session, ctx["connection_id"], inbound())
    job = (await jobs_for(session, ctx["rule_id"]))[0]

    first = await run_job(session, job)
    assert first.status is JobStatus.pending
    assert job.attempt_count == 1

    second = await run_job(session, job)
    assert second.status is JobStatus.succeeded


async def test_permission_failure_is_not_retried(client, actor, session):
    ctx = await build_rule(actor, destinations=1)
    script = script_for(ctx["connection_id"])
    script.fail_delivery(chat_ref(DEST_IDS[0]), Exception("Bad Request: CHAT_WRITE_FORBIDDEN"))

    await run_dispatch(session, ctx["connection_id"], inbound())
    job = (await jobs_for(session, ctx["rule_id"]))[0]
    outcome = await run_job(session, job)

    assert outcome.status is JobStatus.skipped
    assert job.attempt_count == 0  # never retried


async def test_protected_content_failure_is_skipped_with_a_clear_reason(client, actor, session):
    ctx = await build_rule(actor, destinations=1)
    script = script_for(ctx["connection_id"])
    script.fail_delivery(chat_ref(DEST_IDS[0]), Exception("Bad Request: CHAT_FORWARDS_RESTRICTED"))

    await run_dispatch(session, ctx["connection_id"], inbound())
    job = (await jobs_for(session, ctx["rule_id"]))[0]
    outcome = await run_job(session, job)

    assert outcome.status is JobStatus.skipped
    assert outcome.reason_code == reasons.PROTECTED_CONTENT


async def test_flood_wait_duration_is_obeyed_in_full(client, actor, session):
    """The wait Telegram asks for is never shortened by our backoff."""

    class FloodWaitError(Exception):
        def __init__(self, seconds):
            super().__init__(f"A wait of {seconds} seconds is required")
            self.seconds = seconds

    ctx = await build_rule(actor, destinations=1)
    script = script_for(ctx["connection_id"])
    script.fail_delivery(chat_ref(DEST_IDS[0]), FloodWaitError(90))

    await run_dispatch(session, ctx["connection_id"], inbound())
    job = (await jobs_for(session, ctx["rule_id"]))[0]

    before = job.not_before
    outcome = await run_job(session, job)

    assert outcome.retry_after_s == 90.0
    waited = (job.not_before - before).total_seconds()
    assert waited >= 90.0, f"backoff shortened Telegram's wait to {waited}s"


async def test_long_flood_wait_pauses_the_rule(client, actor, session):
    class FloodWaitError(Exception):
        def __init__(self, seconds):
            super().__init__("flood")
            self.seconds = seconds

    ctx = await build_rule(actor, destinations=1)
    script = script_for(ctx["connection_id"])
    script.fail_delivery(chat_ref(DEST_IDS[0]), FloodWaitError(600))

    await run_dispatch(session, ctx["connection_id"], inbound())
    job = (await jobs_for(session, ctx["rule_id"]))[0]
    await run_job(session, job)

    detail = (await actor.get(f"/forwarding-rules/{ctx['rule_id']}")).json()
    assert detail["status"] == RuleStatus.paused.value
    assert detail["paused_reason_code"] == reasons.FLOOD_WAIT_PAUSE
    assert "long wait" in detail["paused_reason_text"]


async def test_auth_failure_pauses_the_whole_connection(client, actor, session):
    ctx = await build_rule(actor)
    script = script_for(ctx["connection_id"])
    script.fail_delivery(chat_ref(DEST_IDS[0]), Exception("Unauthorized: ACCESS_TOKEN_INVALID"))

    await run_dispatch(session, ctx["connection_id"], inbound())
    jobs = await jobs_for(session, ctx["rule_id"])
    await run_job(session, next(j for j in jobs if j.attempt_count == 0))

    connection = (
        await session.execute(
            select(TelegramConnection).where(
                TelegramConnection.id == uuid.UUID(ctx["connection_id"])
            )
        )
    ).scalar_one()
    await session.refresh(connection)
    assert connection.status is ConnectionStatus.paused_safety

    detail = (await actor.get(f"/forwarding-rules/{ctx['rule_id']}")).json()
    assert detail["status"] == RuleStatus.paused.value


async def test_ambiguous_timeout_is_not_retried(client, actor, session):
    """No idempotency token on the Bot API, so we prefer a visible gap over a
    possible duplicate broadcast."""
    ctx = await build_rule(actor, destinations=1)
    script = script_for(ctx["connection_id"])
    script.fail_delivery(chat_ref(DEST_IDS[0]), AmbiguousDeliveryError("timeout"))

    await run_dispatch(session, ctx["connection_id"], inbound())
    job = (await jobs_for(session, ctx["rule_id"]))[0]
    outcome = await run_job(session, job)

    assert outcome.status is JobStatus.needs_attention
    assert job.attempt_count == 0

    events = (await actor.get(f"/forwarding-rules/{ctx['rule_id']}/events")).json()
    assert events[0]["reason_code"] == reasons.AMBIGUOUS_TIMEOUT
    assert "may or may not" in events[0]["reason_text"]


async def test_destination_revalidated_immediately_before_delivery(client, actor, session):
    """Authorization revoked after the job was queued must stop the delivery."""
    ctx = await build_rule(actor, destinations=1)
    await run_dispatch(session, ctx["connection_id"], inbound())

    script = script_for(ctx["connection_id"])
    script.allow_destination(chat_ref(DEST_IDS[0]), False, reasons.BANNED)

    job = (await jobs_for(session, ctx["rule_id"]))[0]
    outcome = await run_job(session, job)

    assert outcome.status is JobStatus.skipped
    assert outcome.reason_code == reasons.BANNED
    assert script.calls_to("forward_message") == []


async def test_retry_failed_requeues_only_the_failure(client, actor, session):
    """Destination 2 exhausts its retries and dead-letters; retry-failed must
    requeue that one and leave the two successful deliveries untouched."""
    ctx = await build_rule(actor)
    script = script_for(ctx["connection_id"])
    failing = chat_ref(DEST_IDS[1])
    # Exactly max_attempts failures, so the job dead-letters and the later
    # manual retry finds a healthy destination.
    script.fail_delivery(failing, *[TimeoutError("blip") for _ in range(5)])

    await run_dispatch(session, ctx["connection_id"], inbound())
    for _ in range(6):
        for job in await jobs_for(session, ctx["rule_id"]):
            if job.status is JobStatus.pending:
                await run_job(session, job)

    jobs = {j.destination_chat_id: j for j in await jobs_for(session, ctx["rule_id"])}
    dead = [j for j in jobs.values() if j.status is JobStatus.dead_letter]
    succeeded = [j for j in jobs.values() if j.status is JobStatus.succeeded]
    assert len(dead) == 1 and len(succeeded) == 2

    delivered_to_successes = {
        call.args[2].peer_id for call in script.calls_to("forward_message")
    } - {failing.peer_id}
    calls_before = len(
        [
            c
            for c in script.calls_to("forward_message")
            if c.args[2].peer_id in delivered_to_successes
        ]
    )

    response = await actor.post(
        f"/forwarding-rules/{ctx['rule_id']}/retry-failed", idem=uuid.uuid4().hex
    )
    assert response.status_code == 202
    assert "1 failed destination" in response.json()["message"]

    # Drain the queue again: only the previously dead-lettered job may move.
    for job in await jobs_for(session, ctx["rule_id"]):
        if job.status is JobStatus.pending:
            await run_job(session, job)

    calls_after = len(
        [
            c
            for c in script.calls_to("forward_message")
            if c.args[2].peer_id in delivered_to_successes
        ]
    )
    assert calls_after == calls_before, "a successful delivery was replayed"

    refreshed = {j.destination_chat_id: j for j in await jobs_for(session, ctx["rule_id"])}
    assert sum(1 for j in refreshed.values() if j.status is JobStatus.succeeded) == 3


async def test_rule_edit_does_not_redeliver_and_skips_removed_destinations(client, actor, session):
    ctx = await build_rule(actor)
    await run_dispatch(session, ctx["connection_id"], inbound())

    # Drop destination 3 from the rule; its queued job must be skipped.
    removed = ctx["destinations"][2]
    await actor.patch(
        f"/forwarding-rules/{ctx['rule_id']}",
        {
            "name": "Announcements",
            "connection_id": ctx["connection_id"],
            "source_chat_ids": [ctx["source"]["id"]],
            "destination_chat_ids": [d["id"] for d in ctx["destinations"][:2]],
        },
    )

    for job in await jobs_for(session, ctx["rule_id"]):
        await run_job(session, job)

    jobs = {str(j.destination_chat_id): j for j in await jobs_for(session, ctx["rule_id"])}
    assert jobs[removed["id"]].status is JobStatus.skipped
    assert jobs[removed["id"]].last_error_code == reasons.DESTINATION_REMOVED
    assert sum(1 for j in jobs.values() if j.status is JobStatus.succeeded) == 2


async def test_backoff_is_bounded_and_never_negative():
    for attempt in range(0, 10):
        delay = backoff_seconds(attempt)
        assert 0 < delay <= 18.1, f"attempt {attempt} produced {delay}"


async def test_copy_mode_is_refused_for_a_protected_source(client, actor):
    """copyMessage could technically reproduce protected content. We refuse."""
    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor,
        connection_id,
        [
            discovered(SOURCE_ID, "Protected source", protected=True),
            discovered(DEST_IDS[0], "Destination", chat_kind="supergroup"),
        ],
    )
    listed = (await actor.get("/telegram/chats")).json()
    source = next(c for c in listed if c["title"] == "Protected source")
    dest = next(c for c in listed if c["title"] == "Destination")

    response = await actor.post(
        "/forwarding-rules",
        {
            "name": "Should be refused",
            "connection_id": connection_id,
            "source_chat_ids": [source["id"]],
            "destination_chat_ids": [dest["id"]],
            "forward_mode": "copy",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "protected_source"
