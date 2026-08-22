"""The end-to-end scenario required by docs/TESTING.md §4.

One test, the real stack, Telegram mocked: create a user and connection,
synchronize chats, create and activate a rule, receive a source message, create
destination jobs, process a success and two different failures, retry only the
transient one, prove a replayed source event cannot duplicate a delivery, pause
the rule after repeated serious errors, and confirm every step is visible on the
Rule Detail view.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.adapters.base import ChatRef, InboundMessage, MediaType, PeerKind
from app.db.models import ForwardingJob, JobStatus, RuleStatus
from app.domain import reasons
from app.services.dispatch import dispatch_inbound
from tests.conftest import chat_ref, connect_bot, discovered, register, script_for, sync_with_chats
from tests.integration.test_forwarding import run_job

SOURCE = -1005550001
DESTINATIONS = (-1005550101, -1005550102, -1005550103)


async def test_end_to_end_forwarding_flow(client, session):
    # 1. Test user and connection ------------------------------------------ #
    actor = await register(client, "e2e@example.com")
    connection_id = await connect_bot(actor, "E2E bot")

    connection = (await actor.get(f"/telegram/connections/{connection_id}")).json()
    assert connection["status"] == "active"
    assert connection["kind"] == "bot"

    # 2. Synchronize mock source and destination chats --------------------- #
    await sync_with_chats(
        actor,
        connection_id,
        [
            discovered(SOURCE, "Announcements"),
            *[
                discovered(peer, f"Partner {i + 1}", chat_kind="supergroup")
                for i, peer in enumerate(DESTINATIONS)
            ],
        ],
    )
    chats = (await actor.get("/telegram/chats")).json()
    assert len(chats) == 4
    source = next(c for c in chats if c["title"] == "Announcements")
    partners = sorted(
        (c for c in chats if c["title"].startswith("Partner")), key=lambda c: c["title"]
    )
    assert source["source_eligible"] and all(p["destination_eligible"] for p in partners)

    # 3. Create and activate a forwarding rule ----------------------------- #
    created = await actor.post(
        "/forwarding-rules",
        {
            "name": "Announcements to partners",
            "connection_id": connection_id,
            "source_chat_ids": [source["id"]],
            "destination_chat_ids": [p["id"] for p in partners],
            "keyword_exclude": ["internal"],
            "delay_ms": 500,
        },
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    rule_id = rule["id"]

    assert rule["status"] == RuleStatus.draft.value
    assert "When a new eligible message appears in Announcements" in rule["preview"]
    assert "Partner 1, Partner 2, and Partner 3" in rule["preview"]

    activated = await actor.post(f"/forwarding-rules/{rule_id}/activate", idem=uuid.uuid4().hex)
    assert activated.status_code == 202
    assert (await actor.get(f"/forwarding-rules/{rule_id}")).json()["status"] == "active"

    # 4. Receive a mock source message ------------------------------------- #
    script = script_for(connection_id)
    script.fail_delivery(chat_ref(DESTINATIONS[1]), TimeoutError("transient network blip"))
    script.fail_delivery(chat_ref(DESTINATIONS[2]), Exception("Bad Request: CHAT_WRITE_FORBIDDEN"))

    message = InboundMessage(
        source=ChatRef(PeerKind.channel, SOURCE),
        message_ids=[9001],
        media_type=MediaType.text,
        text="Public launch is live",
    )
    result = await dispatch_inbound(
        session, connection_id=uuid.UUID(connection_id), message=message
    )
    await session.commit()

    # 5. One durable job per destination ----------------------------------- #
    assert len(result.created_job_ids) == 3

    async def all_jobs() -> list[ForwardingJob]:
        session.expire_all()
        rows = await session.execute(
            select(ForwardingJob).where(ForwardingJob.rule_id == uuid.UUID(rule_id))
        )
        return list(rows.scalars().all())

    async def jobs() -> dict[str, ForwardingJob]:
        """Keyed by destination — only valid while there is one job per destination."""
        return {str(j.destination_chat_id): j for j in await all_jobs()}

    assert len(await jobs()) == 3

    # 6. Process: one success, one transient failure, one permission failure - #
    for job in (await jobs()).values():
        await run_job(session, job)

    current = await jobs()
    assert current[partners[0]["id"]].status is JobStatus.succeeded
    assert current[partners[1]["id"]].status is JobStatus.pending  # retry scheduled
    assert current[partners[2]["id"]].status is JobStatus.skipped  # not retried
    assert current[partners[2]["id"]].last_error_code == reasons.WRITE_FORBIDDEN

    # 7. Only the transient failure is retried ----------------------------- #
    forwarded_before = len(script.calls_to("forward_message"))
    for job in (await jobs()).values():
        if job.status is JobStatus.pending:
            await run_job(session, job)

    current = await jobs()
    assert current[partners[1]["id"]].status is JobStatus.succeeded
    assert current[partners[2]["id"]].status is JobStatus.skipped, (
        "a permission failure must never be retried"
    )
    # Exactly one further delivery attempt: the transient one.
    assert len(script.calls_to("forward_message")) == forwarded_before + 1

    # 8. A replayed source event cannot duplicate a successful delivery ----- #
    replay = await dispatch_inbound(
        session, connection_id=uuid.UUID(connection_id), message=message
    )
    await session.commit()
    assert replay.created_job_ids == []
    assert replay.suppressed == 3
    assert len(await jobs()) == 3

    delivered = [j for j in (await jobs()).values() if j.status is JobStatus.succeeded]
    assert len(delivered) == 2

    # 9. Repeated serious errors pause the rule automatically --------------- #
    # Enough failures that every one of these jobs exhausts max_attempts and
    # dead-letters, which is what trips the safety threshold.
    script.fail_delivery(
        chat_ref(DESTINATIONS[0]), *[TimeoutError("still failing") for _ in range(100)]
    )
    for message_id in range(9002, 9010):
        await dispatch_inbound(
            session,
            connection_id=uuid.UUID(connection_id),
            message=InboundMessage(
                source=ChatRef(PeerKind.channel, SOURCE),
                message_ids=[message_id],
                media_type=MediaType.text,
                text="Public launch is live",
            ),
        )
        await session.commit()

    for _ in range(6):
        for job in await all_jobs():
            if job.status is JobStatus.pending:
                await run_job(session, job)

    detail = (await actor.get(f"/forwarding-rules/{rule_id}")).json()
    assert detail["status"] == RuleStatus.paused.value
    assert detail["paused_reason_code"] == reasons.SAFETY_PAUSE
    assert "paused automatically" in detail["paused_reason_text"]

    # 10. Everything is visible on the Rule Detail view --------------------- #
    events = (await actor.get(f"/forwarding-rules/{rule_id}/events?limit=200")).json()
    outcomes = {e["outcome"] for e in events}
    assert {"forwarded", "skipped", "retry_scheduled", "paused"} <= outcomes

    codes = {e["reason_code"] for e in events}
    assert reasons.DELIVERED in codes
    assert reasons.WRITE_FORBIDDEN in codes
    assert reasons.RETRYING in codes
    assert reasons.SAFETY_PAUSE in codes
    assert all(e["reason_text"] for e in events), "every event must be explainable"

    per_destination = (await actor.get(f"/forwarding-rules/{rule_id}/jobs")).json()
    assert len(per_destination) >= 3
    assert all(j["destination_title"].startswith("Partner") for j in per_destination)

    summary = (await actor.get("/usage/summary?period=24h")).json()
    assert summary["forwarded"] >= 2
    assert summary["skipped"] >= 1

    # No secret is anywhere in what the customer can see.
    serialized = str(events) + str(detail) + str(per_destination)
    assert "AAEtestToken" not in serialized
