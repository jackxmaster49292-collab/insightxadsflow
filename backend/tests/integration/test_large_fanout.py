"""Fan-out behaviour at realistic scale: one source, 300 destination groups.

Answers a concrete operational question — "I have 300 groups, does one message
reach all of them, and how long does it take?" — with a measurement rather than
an estimate.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import func, select

from app.adapters.base import ChatRef, DiscoveredChat, InboundMessage, MediaType, PeerKind
from app.config import get_settings
from app.db.models import ForwardingJob, JobStatus
from app.services.dispatch import dispatch_inbound
from tests.conftest import Actor, connect_bot, script_for, sync_with_chats
from tests.integration.test_forwarding import run_job

SOURCE_PEER = -1002_000_000
#: The chats endpoint caps a page at 200, so 300 chats must be paged through —
#: exactly what the panel does.
PAGE = 200
TOTAL_GROUPS = 300
PRIVATE_GROUPS = 150


async def fetch_all_chats(actor: Actor) -> list[dict]:
    """Page through every chat, the way the panel does."""
    collected: list[dict] = []
    offset = 0
    while True:
        page = (await actor.get(f"/telegram/chats?limit={PAGE}&offset={offset}")).json()
        assert isinstance(page, list), page
        collected.extend(page)
        if len(page) < PAGE:
            return collected
        offset += PAGE


def build_chats() -> list[DiscoveredChat]:
    """One source plus 300 groups: 150 private, 150 public."""
    chats = [
        DiscoveredChat(
            ref=ChatRef(PeerKind.channel, SOURCE_PEER),
            title="Source channel",
            chat_kind="channel",
            username="source_channel",
            is_public=True,
            has_protected_content=False,
        )
    ]
    for index in range(TOTAL_GROUPS):
        is_private = index < PRIVATE_GROUPS
        chats.append(
            DiscoveredChat(
                ref=ChatRef(
                    PeerKind.channel,
                    SOURCE_PEER - 1 - index,
                    # Private peers are unreachable without a per-account
                    # access_hash; public ones can be resolved by username.
                    access_hash=None if not is_private else 10_000 + index,
                ),
                title=f"{'Private' if is_private else 'Public'} group {index:03d}",
                chat_kind="supergroup",
                username=None if is_private else f"public_group_{index:03d}",
                is_public=not is_private,
                has_protected_content=False,
            )
        )
    return chats


async def test_one_message_reaches_all_300_groups(client, actor, session):
    connection_id = await connect_bot(actor, "Fan-out bot")
    await sync_with_chats(actor, connection_id, build_chats())

    chats = await fetch_all_chats(actor)
    assert len(chats) == TOTAL_GROUPS + 1

    source = next(c for c in chats if c["title"] == "Source channel")
    groups = [c for c in chats if c["title"] != "Source channel"]
    assert len(groups) == TOTAL_GROUPS

    private = [c for c in groups if c["is_public"] is False]
    public = [c for c in groups if c["is_public"] is True]
    assert len(private) == PRIVATE_GROUPS and len(public) == TOTAL_GROUPS - PRIVATE_GROUPS

    # Both kinds are equally postable once the connection is authorized —
    # public/private changes discovery, not delivery.
    assert all(c["destination_eligible"] for c in groups)

    created = await actor.post(
        "/forwarding-rules",
        {
            "name": "Broadcast to all groups",
            "connection_id": connection_id,
            "source_chat_ids": [source["id"]],
            "destination_chat_ids": [c["id"] for c in groups],
            "delay_ms": 0,
        },
    )
    assert created.status_code == 201, created.text
    rule_id = created.json()["id"]
    assert len(created.json()["destinations"]) == TOTAL_GROUPS

    await actor.post(f"/forwarding-rules/{rule_id}/activate", idem=uuid.uuid4().hex)

    # --- one source message -------------------------------------------------
    started = time.perf_counter()
    result = await dispatch_inbound(
        session,
        connection_id=uuid.UUID(connection_id),
        message=InboundMessage(
            source=ChatRef(PeerKind.channel, SOURCE_PEER),
            message_ids=[7001],
            media_type=MediaType.text,
            text="Announcement",
        ),
    )
    await session.commit()
    dispatch_s = time.perf_counter() - started

    assert len(result.created_job_ids) == TOTAL_GROUPS, "every group must get its own job"
    assert dispatch_s < 10, f"dispatch took {dispatch_s:.1f}s"

    # --- deliver ------------------------------------------------------------
    session.expire_all()
    jobs = (
        (
            await session.execute(
                select(ForwardingJob).where(ForwardingJob.rule_id == uuid.UUID(rule_id))
            )
        )
        .scalars()
        .all()
    )
    for job in jobs:
        await run_job(session, job)

    session.expire_all()
    succeeded = (
        await session.execute(
            select(func.count())
            .select_from(ForwardingJob)
            .where(
                ForwardingJob.rule_id == uuid.UUID(rule_id),
                ForwardingJob.status == JobStatus.succeeded,
            )
        )
    ).scalar_one()
    assert succeeded == TOTAL_GROUPS, f"only {succeeded}/{TOTAL_GROUPS} delivered"

    script = script_for(connection_id)
    assert len(script.calls_to("forward_message")) == TOTAL_GROUPS


async def test_replaying_the_message_does_not_redeliver_to_300_groups(client, actor, session):
    """Duplicate prevention has to hold at scale too — a re-sent update must not
    produce a second broadcast to 300 groups."""
    connection_id = await connect_bot(actor, "Fan-out bot")
    await sync_with_chats(actor, connection_id, build_chats())

    chats = await fetch_all_chats(actor)
    source = next(c for c in chats if c["title"] == "Source channel")
    groups = [c for c in chats if c["title"] != "Source channel"]

    created = await actor.post(
        "/forwarding-rules",
        {
            "name": "Broadcast",
            "connection_id": connection_id,
            "source_chat_ids": [source["id"]],
            "destination_chat_ids": [c["id"] for c in groups],
        },
    )
    rule_id = created.json()["id"]
    await actor.post(f"/forwarding-rules/{rule_id}/activate", idem=uuid.uuid4().hex)

    message = InboundMessage(
        source=ChatRef(PeerKind.channel, SOURCE_PEER),
        message_ids=[7002],
        media_type=MediaType.text,
        text="Announcement",
    )
    first = await dispatch_inbound(session, connection_id=uuid.UUID(connection_id), message=message)
    await session.commit()
    replay = await dispatch_inbound(
        session, connection_id=uuid.UUID(connection_id), message=message
    )
    await session.commit()

    assert len(first.created_job_ids) == TOTAL_GROUPS
    assert replay.created_job_ids == []
    assert replay.suppressed == TOTAL_GROUPS


async def test_exceeding_the_destination_bound_is_refused_with_a_clear_reason(
    client, actor, monkeypatch
):
    """The bound is an operational safety control, and says so — it is not a
    plan limit."""
    monkeypatch.setattr(get_settings(), "max_destinations_per_rule", 10, raising=False)

    connection_id = await connect_bot(actor, "Fan-out bot")
    await sync_with_chats(actor, connection_id, build_chats()[:20])
    chats = await fetch_all_chats(actor)
    source = next(c for c in chats if c["title"] == "Source channel")
    groups = [c for c in chats if c["title"] != "Source channel"]

    response = await actor.post(
        "/forwarding-rules",
        {
            "name": "Too wide",
            "connection_id": connection_id,
            "source_chat_ids": [source["id"]],
            "destination_chat_ids": [c["id"] for c in groups],
        },
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "too_many_destinations"
    assert "not a plan limit" in body["message"]


async def test_a_new_message_mid_delivery_queues_instead_of_overtaking(client, actor, session):
    """The realistic worry with big fan-outs: a second message arrives while the
    first is still going out. Both batches must survive and stay separate — no
    message silently dropped, no delivery skipped."""
    connection_id = await connect_bot(actor, "Fan-out bot")
    await sync_with_chats(actor, connection_id, build_chats())

    chats = await fetch_all_chats(actor)
    source = next(c for c in chats if c["title"] == "Source channel")
    groups = [c for c in chats if c["title"] != "Source channel"]

    created = await actor.post(
        "/forwarding-rules",
        {
            "name": "Paced broadcast",
            "connection_id": connection_id,
            "source_chat_ids": [source["id"]],
            "destination_chat_ids": [c["id"] for c in groups],
            # 1s apart => the tail of message 1 is ~5 minutes out.
            "delay_ms": 1000,
        },
    )
    rule_id = created.json()["id"]
    await actor.post(f"/forwarding-rules/{rule_id}/activate", idem=uuid.uuid4().hex)

    async def send(message_id: int) -> int:
        result = await dispatch_inbound(
            session,
            connection_id=uuid.UUID(connection_id),
            message=InboundMessage(
                source=ChatRef(PeerKind.channel, SOURCE_PEER),
                message_ids=[message_id],
                media_type=MediaType.text,
                text=f"Post {message_id}",
            ),
        )
        await session.commit()
        return len(result.created_job_ids)

    assert await send(8001) == TOTAL_GROUPS
    # Second message lands while message 1 is still spread across the next
    # several minutes.
    assert await send(8002) == TOTAL_GROUPS

    session.expire_all()
    jobs = (
        (
            await session.execute(
                select(ForwardingJob).where(ForwardingJob.rule_id == uuid.UUID(rule_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(jobs) == TOTAL_GROUPS * 2, "both batches must be queued, not merged"

    # Each (message, destination) pair is distinct: nothing overwrote anything.
    pairs = {(tuple(j.source_message_ids), j.destination_chat_id) for j in jobs}
    assert len(pairs) == TOTAL_GROUPS * 2
    assert len({j.idempotency_key for j in jobs}) == TOTAL_GROUPS * 2

    for job in jobs:
        await run_job(session, job)

    session.expire_all()
    delivered = (
        await session.execute(
            select(func.count())
            .select_from(ForwardingJob)
            .where(
                ForwardingJob.rule_id == uuid.UUID(rule_id),
                ForwardingJob.status == JobStatus.succeeded,
            )
        )
    ).scalar_one()
    assert delivered == TOTAL_GROUPS * 2


async def test_a_delay_that_would_take_days_is_refused(client, actor):
    """delay_ms multiplies by destination count. One hour across 300 chats is
    12.5 days of tail — refuse it rather than silently accept it."""
    connection_id = await connect_bot(actor, "Fan-out bot")
    await sync_with_chats(actor, connection_id, build_chats())

    chats = await fetch_all_chats(actor)
    source = next(c for c in chats if c["title"] == "Source channel")
    groups = [c for c in chats if c["title"] != "Source channel"]

    response = await actor.post(
        "/forwarding-rules",
        {
            "name": "Absurdly slow",
            "connection_id": connection_id,
            "source_chat_ids": [source["id"]],
            "destination_chat_ids": [c["id"] for c in groups],
            "delay_ms": 3_600_000,  # 1 hour, per destination
        },
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "delay_spread_too_long"
    assert "hours to finish delivering" in body["message"]
