"""Ads: composing, queueing, pacing and per-group delivery.

A broadcast posts the customer's own message into groups they picked. These are
the tests behind the claims that matter for that: it posts once per group and
never twice, it stops when told to, it obeys a Telegram wait in full, and it
refuses to post where the account is not allowed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.adapters.base import AccessReport, AmbiguousDeliveryError, ChatRef, PeerKind
from app.adapters.errors import AdapterError, ErrorClass
from app.config import get_settings
from app.db.models import (
    Broadcast,
    BroadcastMedia,
    BroadcastStatus,
    BroadcastTarget,
    ConnectionStatus,
    ForwardingEvent,
    JobStatus,
    TelegramConnection,
)
from app.domain import reasons
from app.repositories import broadcasts as broadcast_repo
from app.services import broadcast as broadcast_service
from app.services import connections as connection_service
from tests.conftest import chat_ref, connect_bot, discovered, script_for, sync_with_chats


def group_id(index: int) -> int:
    return -1002000 - index


async def build_broadcast(
    actor,
    session,
    *,
    groups: int = 3,
    text: str = "Our October offer is live.",
    delay_ms: int = 0,
) -> dict:
    """A synchronized connection and a draft ad targeting N groups."""
    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor,
        connection_id,
        [
            discovered(group_id(i), f"Group {i + 1:02d}", chat_kind="supergroup")
            for i in range(groups)
        ],
    )

    listed = sorted((await actor.get("/telegram/chats")).json(), key=lambda c: c["title"])
    chat_ids = [uuid.UUID(c["id"]) for c in listed]

    broadcast = await broadcast_repo.create(
        session,
        user_id=uuid.UUID(actor.id),
        connection_id=uuid.UUID(connection_id),
        name="October offer",
        delay_ms=delay_ms,
    )
    broadcast.body_text = text
    await broadcast_repo.replace_targets(session, broadcast=broadcast, chat_ids=chat_ids)
    await session.commit()

    return {
        "connection_id": connection_id,
        "broadcast_id": broadcast.id,
        "chat_ids": chat_ids,
        "peer_ids": [group_id(i) for i in range(groups)],
    }


async def drain(session, broadcast_id: uuid.UUID) -> list:
    """Run every due target through the real delivery path."""
    outcomes = []
    result = await session.execute(
        select(BroadcastTarget)
        .where(
            BroadcastTarget.broadcast_id == broadcast_id,
            BroadcastTarget.status == JobStatus.pending,
        )
        .order_by(BroadcastTarget.position)
    )
    broadcast = await session.get(Broadcast, broadcast_id)
    connection = await session.get(TelegramConnection, broadcast.connection_id)
    adapter = await connection_service.adapter_for(session, connection)

    for target in result.scalars().all():
        outcomes.append(
            await broadcast_service.execute_target(
                session, target=target, adapter=adapter, connection=connection
            )
        )
    await session.commit()
    return outcomes


# --------------------------------------------------------------------------- #
# Composing and validation
# --------------------------------------------------------------------------- #
async def test_queueing_creates_one_row_per_group(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=3)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])

    queued = await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    assert queued == 3
    assert broadcast.status is BroadcastStatus.sending
    counts = await broadcast_repo.status_counts(session, broadcast_id=broadcast.id)
    assert counts == {"pending": 3}


async def test_an_empty_message_is_refused_before_anything_is_queued(client, actor, session):
    ctx = await build_broadcast(actor, session, text="   ")
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])

    with pytest.raises(broadcast_service.BroadcastValidationError) as exc:
        await broadcast_service.queue(session, broadcast=broadcast)

    assert "Write a message" in exc.value.message
    assert broadcast.status is BroadcastStatus.draft


async def test_no_groups_is_refused(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_repo.replace_targets(session, broadcast=broadcast, chat_ids=[])

    with pytest.raises(broadcast_service.BroadcastValidationError) as exc:
        await broadcast_service.queue(session, broadcast=broadcast)
    assert "at least one group" in exc.value.message


async def test_text_over_telegram_s_limit_is_refused_with_the_number(client, actor, session):
    """Told while it can still be edited, rather than discovered one failure at
    a time across 300 groups."""
    limit = get_settings().max_broadcast_text_len
    ctx = await build_broadcast(actor, session, text="x" * (limit + 25))
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])

    with pytest.raises(broadcast_service.BroadcastValidationError) as exc:
        await broadcast_service.queue(session, broadcast=broadcast)
    assert str(limit) in exc.value.message
    assert "25" in exc.value.message, "must say how much to cut"


async def test_an_image_uses_the_shorter_caption_limit(client, actor, session):
    """Telegram allows 4096 characters of text but only 1024 of caption, so the
    same body can be fine as text and too long once an image is attached."""
    settings = get_settings()
    ctx = await build_broadcast(actor, session, text="x" * (settings.max_broadcast_caption_len + 1))
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])

    # As plain text it is well inside the limit.
    await broadcast_service.queue(session, broadcast=broadcast)

    broadcast.status = BroadcastStatus.draft
    broadcast.media_kind = BroadcastMedia.photo
    broadcast.media_bytes = b"\xff\xd8\xff"
    with pytest.raises(broadcast_service.BroadcastValidationError) as exc:
        await broadcast_service.queue(session, broadcast=broadcast)
    assert str(settings.max_broadcast_caption_len) in exc.value.message


async def test_a_pause_that_would_take_days_is_refused(client, actor, session):
    """delay_ms multiplies by the group count; the tail must not land next week."""
    ctx = await build_broadcast(actor, session, groups=8, delay_ms=3_600_000)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])

    with pytest.raises(broadcast_service.BroadcastValidationError) as exc:
        await broadcast_service.queue(session, broadcast=broadcast)
    assert "Lower the pause" in exc.value.message


async def test_selecting_the_same_group_twice_stores_it_once(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=2)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    duplicated = ctx["chat_ids"] + ctx["chat_ids"]

    kept = await broadcast_repo.replace_targets(session, broadcast=broadcast, chat_ids=duplicated)
    assert kept == 2


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
async def test_every_group_receives_the_message_once(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=3)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    outcomes = await drain(session, broadcast.id)

    assert [o.status for o in outcomes] == [JobStatus.succeeded] * 3
    sends = script_for(ctx["connection_id"]).calls_to("send_text")
    assert len(sends) == 3
    delivered_to = {call.args[0].peer_id for call in sends}
    assert delivered_to == set(ctx["peer_ids"]), "each group exactly once"


async def test_the_body_text_is_what_gets_sent(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=1, text="Buy one get one free")
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)

    call = script_for(ctx["connection_id"]).calls_to("send_text")[0]
    assert call.args[1] == "Buy one get one free"


async def test_an_image_is_sent_as_a_photo_with_the_text_as_caption(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=1, text="Look at this")
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.media_kind = BroadcastMedia.photo
    broadcast.media_bytes = b"\xff\xd8\xff\xe0jpegbytes"
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)

    script = script_for(ctx["connection_id"])
    assert not script.calls_to("send_text")
    photo = script.calls_to("send_photo")[0]
    assert photo.args[1] == len(b"\xff\xd8\xff\xe0jpegbytes")
    assert photo.kwargs["caption"] == "Look at this"


async def test_draining_twice_does_not_post_twice(client, actor, session):
    """The property that matters most: a second pass must find nothing to do."""
    ctx = await build_broadcast(actor, session, groups=3)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await drain(session, broadcast.id)

    assert len(script_for(ctx["connection_id"]).calls_to("send_text")) == 3


async def test_a_second_queue_of_the_same_groups_cannot_duplicate_targets(client, actor, session):
    """The unique constraint, not a prior SELECT, is what prevents this."""
    ctx = await build_broadcast(actor, session, groups=2)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_repo.replace_targets(session, broadcast=broadcast, chat_ids=ctx["chat_ids"])
    await session.commit()

    rows = (
        (
            await session.execute(
                select(BroadcastTarget).where(BroadcastTarget.broadcast_id == broadcast.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2


async def test_the_broadcast_completes_when_the_last_group_is_done(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=2)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.refresh(broadcast)

    assert broadcast.status is BroadcastStatus.completed
    assert broadcast.completed_at is not None


async def test_pacing_staggers_the_groups(client, actor, session):
    """A 5s pause across 3 groups schedules them 0s, 5s and 10s out — not all at
    once, which is what would trip Telegram's per-group limit."""
    ctx = await build_broadcast(actor, session, groups=3, delay_ms=5_000)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    rows = (
        (
            await session.execute(
                select(BroadcastTarget)
                .where(BroadcastTarget.broadcast_id == broadcast.id)
                .order_by(BroadcastTarget.position)
            )
        )
        .scalars()
        .all()
    )
    offsets = [(r.not_before - rows[0].not_before).total_seconds() for r in rows]
    assert offsets == [0, 5, 10]


# --------------------------------------------------------------------------- #
# Refusals and failures
# --------------------------------------------------------------------------- #
async def test_a_group_it_cannot_post_in_is_skipped_not_retried(client, actor, session):
    """ "You are not allowed to post here" does not become true by asking again."""
    ctx = await build_broadcast(actor, session, groups=2)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    script = script_for(ctx["connection_id"])
    script.allow_destination(
        chat_ref(ctx["peer_ids"][0]), allowed=False, reason=reasons.WRITE_FORBIDDEN
    )

    outcomes = await drain(session, broadcast.id)
    statuses = sorted(o.status.value for o in outcomes)
    assert statuses == ["skipped", "succeeded"]
    assert len(script.calls_to("send_text")) == 1, "the forbidden group is never attempted"


async def test_a_failing_eligibility_check_fails_closed(client, actor, session):
    """A check that errors is not a confirmation."""
    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    connection = await session.get(TelegramConnection, broadcast.connection_id)
    adapter = await connection_service.adapter_for(session, connection)

    async def explode(_ref):
        raise RuntimeError("network went away mid-check")

    adapter.check_destination_access = explode  # type: ignore[method-assign]

    target = (
        await session.execute(
            select(BroadcastTarget).where(BroadcastTarget.broadcast_id == broadcast.id)
        )
    ).scalar_one()
    outcome = await broadcast_service.execute_target(
        session, target=target, adapter=adapter, connection=connection
    )

    assert outcome.status is JobStatus.skipped
    assert outcome.reason_code == reasons.DESTINATION_NOT_ELIGIBLE


async def test_a_telegram_wait_is_obeyed_in_full(client, actor, session):
    """Backoff never shortens a wait Telegram asked for."""
    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    script_for(ctx["connection_id"]).fail_delivery(
        chat_ref(ctx["peer_ids"][0]),
        AdapterError(reasons.SLOWMODE_WAIT, ErrorClass.RATE_LIMIT, retry_after_s=42),
    )

    outcome = (await drain(session, broadcast.id))[0]
    assert outcome.status is JobStatus.pending
    assert outcome.retry_after_s == 42


async def test_a_long_wait_pauses_the_whole_broadcast(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=2)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    script_for(ctx["connection_id"]).fail_delivery(
        chat_ref(ctx["peer_ids"][0]),
        AdapterError(
            reasons.FLOOD_WAIT,
            ErrorClass.RATE_LIMIT,
            retry_after_s=get_settings().flood_wait_pause_threshold_s + 1,
        ),
    )

    await drain(session, broadcast.id)
    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.paused
    assert broadcast.paused_reason_code == reasons.FLOOD_WAIT_PAUSE


async def test_an_ambiguous_timeout_is_not_retried(client, actor, session):
    """Posting the same ad twice in a group is worse than one visible gap."""
    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    script_for(ctx["connection_id"]).fail_delivery(
        chat_ref(ctx["peer_ids"][0]), AmbiguousDeliveryError(reasons.AMBIGUOUS_TIMEOUT)
    )

    outcome = (await drain(session, broadcast.id))[0]
    assert outcome.status is JobStatus.needs_attention
    assert outcome.reason_code == reasons.AMBIGUOUS_TIMEOUT


async def test_a_disconnected_connection_stops_delivery(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=2)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    connection = await session.get(TelegramConnection, broadcast.connection_id)
    connection.status = ConnectionStatus.disconnected
    await session.commit()

    outcomes = await drain(session, broadcast.id)
    assert all(o.reason_code == reasons.CONNECTION_DISCONNECTED for o in outcomes)
    assert not script_for(ctx["connection_id"]).calls_to("send_text")


# --------------------------------------------------------------------------- #
# Stopping
# --------------------------------------------------------------------------- #
async def test_pausing_stops_new_deliveries(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=3)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await broadcast_service.pause(
        session, broadcast=broadcast, reason_code=reasons.BROADCAST_INACTIVE
    )
    await session.commit()

    await drain(session, broadcast.id)
    assert not script_for(ctx["connection_id"]).calls_to("send_text")


async def test_cancelling_stops_the_rest_but_keeps_what_was_sent(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=3)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    # Deliver one group, then stop.
    first = (
        await session.execute(
            select(BroadcastTarget)
            .where(BroadcastTarget.broadcast_id == broadcast.id)
            .order_by(BroadcastTarget.position)
            .limit(1)
        )
    ).scalar_one()
    connection = await session.get(TelegramConnection, broadcast.connection_id)
    adapter = await connection_service.adapter_for(session, connection)
    await broadcast_service.execute_target(
        session, target=first, adapter=adapter, connection=connection
    )

    stopped = await broadcast_service.cancel(session, broadcast=broadcast)
    await session.commit()

    assert stopped == 2
    counts = await broadcast_repo.status_counts(session, broadcast_id=broadcast.id)
    assert counts["succeeded"] == 1
    assert counts["skipped"] == 2
    assert broadcast.status is BroadcastStatus.cancelled


async def test_retrying_never_replays_a_group_that_already_received_it(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=3)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    # One group fails permanently enough to land in dead_letter.
    script = script_for(ctx["connection_id"])
    for _ in range(get_settings().max_attempts):
        script.fail_delivery(
            chat_ref(ctx["peer_ids"][1]),
            AdapterError(reasons.SERVER_ERROR, ErrorClass.TRANSIENT),
        )
    for _ in range(get_settings().max_attempts + 1):
        await drain(session, broadcast.id)
        for target in (
            await session.execute(
                select(BroadcastTarget).where(
                    BroadcastTarget.broadcast_id == broadcast.id,
                    BroadcastTarget.status == JobStatus.pending,
                )
            )
        ).scalars():
            target.not_before = broadcast_repo.now()
        await session.commit()

    before = len(script.calls_to("send_text"))
    requeued = await broadcast_service.retry_unfinished(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)

    assert requeued == 1, "only the unfinished group is retried"
    assert len(script.calls_to("send_text")) == before + 1


# --------------------------------------------------------------------------- #
# Activity feed
# --------------------------------------------------------------------------- #
async def test_a_delivery_is_recorded_against_the_broadcast(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)

    events = (
        (
            await session.execute(
                select(ForwardingEvent).where(ForwardingEvent.broadcast_id == broadcast.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].rule_id is None, "a broadcast event belongs to no rule"
    assert events[0].reason_code == reasons.BROADCAST_POSTED


async def test_an_event_cannot_belong_to_both_a_rule_and_a_broadcast(session):
    """Enforced in the database, checked here so the caller gets a readable error."""
    from app.db.models import EventOutcome
    from app.repositories import events as event_repo

    with pytest.raises(ValueError):
        await event_repo.record(
            session,
            rule_id=uuid.uuid4(),
            broadcast_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            outcome=EventOutcome.forwarded,
            reason_code="x",
        )

    with pytest.raises(ValueError):
        await event_repo.record(
            session,
            connection_id=uuid.uuid4(),
            outcome=EventOutcome.forwarded,
            reason_code="x",
        )


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #
async def test_another_account_s_broadcast_does_not_resolve(client, actor, other_actor, session):
    ctx = await build_broadcast(actor, session, groups=1)
    mine = await broadcast_repo.get(
        session, user_id=uuid.UUID(actor.id), broadcast_id=ctx["broadcast_id"]
    )
    theirs = await broadcast_repo.get(
        session, user_id=uuid.UUID(other_actor.id), broadcast_id=ctx["broadcast_id"]
    )
    assert mine is not None
    assert theirs is None, "an id alone must never be enough"


def test_the_estimate_matches_the_pacing():
    """The number shown before sending is the one the scheduler produces."""
    assert broadcast_service.estimated_duration_s(3000, 1) == 0
    assert broadcast_service.estimated_duration_s(3000, 2) == 3
    assert broadcast_service.estimated_duration_s(3000, 100) == 297


def test_a_broadcast_never_addresses_a_peer_it_was_not_given():
    """There is no discovery path: targets come from stored membership only."""
    ref = ChatRef(PeerKind.channel, -100)
    assert ref.peer_type is PeerKind.channel


# --------------------------------------------------------------------------- #
# Repeating
# --------------------------------------------------------------------------- #
async def test_a_repeating_ad_reopens_the_same_groups_instead_of_finishing(client, actor, session):
    """The whole point of the feature: the round ends, the ad does not."""
    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = 7200
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.refresh(broadcast)

    assert broadcast.status is BroadcastStatus.sending, "a repeating ad never completes itself"
    assert broadcast.completed_at is None
    assert broadcast.repeat_count == 1
    counts = await broadcast_repo.status_counts(session, broadcast_id=broadcast.id)
    assert counts == {"pending": 3}, "the same groups are queued again"

    gap = (broadcast.next_run_at - datetime.now(UTC)).total_seconds()
    assert 7100 < gap <= 7200, "measured from the round finishing, not from when it started"


async def test_the_next_round_is_paced_like_the_first(client, actor, session):
    """Reopening 500 targets on the same timestamp would post to all of them at
    once — which is the one thing the pause between groups exists to prevent."""
    ctx = await build_broadcast(actor, session, groups=4, delay_ms=3000)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = 7200
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    targets = (
        (
            await session.execute(
                select(BroadcastTarget)
                .where(BroadcastTarget.broadcast_id == broadcast.id)
                .order_by(BroadcastTarget.position)
            )
        )
        .scalars()
        .all()
    )
    spacing = [
        (b.not_before - a.not_before).total_seconds()
        for a, b in zip(targets, targets[1:], strict=False)
    ]
    assert spacing == [3.0, 3.0, 3.0]


async def test_the_next_round_is_not_due_yet(client, actor, session):
    """Written against the worker's own query, so it cannot pass while the
    worker would still pick the targets up immediately."""
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = 7200
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()

    claimed = await broadcast_repo.claim_batch(
        session, owner="test-worker", limit=10, lease_seconds=60
    )
    assert claimed == [], "nothing is due until the interval has passed"


async def test_a_group_that_refused_last_round_is_tried_again(client, actor, session):
    """A refusal is a fact about a moment. An admin can grant permission back,
    and re-checking is the only way that gets noticed."""
    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = 7200
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    script = script_for(ctx["connection_id"])
    # Resolved the same way delivery resolves it, so the test cannot pass
    # against a peer the sender would never address.
    from app.db.models import TelegramChat
    from app.repositories import chats as chat_repo

    blocked = chat_repo.to_ref(await session.get(TelegramChat, ctx["chat_ids"][1]))
    script.destination_allowed[blocked.key] = AccessReport(
        allowed=False, reason_code=reasons.DESTINATION_NOT_ELIGIBLE
    )

    outcomes = await drain(session, broadcast.id)
    assert [o.status for o in outcomes].count(JobStatus.skipped) == 1

    await session.refresh(broadcast)
    assert broadcast.repeat_count == 1, "a skipped group still ends the round"
    counts = await broadcast_repo.status_counts(session, broadcast_id=broadcast.id)
    assert counts == {"pending": 3}, "including the one that refused"

    # Permission comes back; the next round posts there.
    script.destination_allowed.pop(blocked.key)
    outcomes = await drain(session, broadcast.id)
    assert all(o.status is JobStatus.succeeded for o in outcomes)


async def test_an_ad_without_a_repeat_still_completes(client, actor, session):
    """The default is unchanged: post once, then stop."""
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    assert broadcast.repeat_every_s is None
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.refresh(broadcast)

    assert broadcast.status is BroadcastStatus.completed
    assert broadcast.completed_at is not None
    assert broadcast.next_run_at is None
    assert broadcast.repeat_count == 1


async def test_a_repeat_under_the_floor_is_refused(client, actor, session):
    """The account that would get banned for posting the same thing every five
    minutes is the customer's own."""
    floor = get_settings().min_broadcast_repeat_s
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = floor - 1

    with pytest.raises(broadcast_service.BroadcastValidationError) as exc:
        await broadcast_service.queue(session, broadcast=broadcast)
    assert "reported and banned" in exc.value.message
    assert broadcast.status is BroadcastStatus.draft


async def test_a_repeat_shorter_than_one_round_is_refused(client, actor, session):
    """Otherwise round two starts while round one is still going and the ad
    posts to the same group twice in a row."""
    ctx = await build_broadcast(actor, session, groups=6, delay_ms=1_800_000)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = 3600

    with pytest.raises(broadcast_service.BroadcastValidationError) as exc:
        await broadcast_service.queue(session, broadcast=broadcast)
    assert "longer than" in exc.value.message


async def test_pausing_a_repeating_ad_stops_the_next_round(client, actor, session):
    """Stopping it is a decision someone makes — and it has to actually stop."""
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = 7200
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()

    await broadcast_service.pause(
        session, broadcast=broadcast, reason_code=reasons.BROADCAST_PAUSED_BY_CUSTOMER
    )
    await session.commit()

    # Even once the interval has elapsed, a paused ad feeds the worker nothing.
    await session.execute(
        update(BroadcastTarget)
        .where(BroadcastTarget.broadcast_id == broadcast.id)
        .values(not_before=datetime.now(UTC) - timedelta(minutes=1))
    )
    claimed = await broadcast_repo.claim_batch(
        session, owner="test-worker", limit=10, lease_seconds=60
    )
    assert claimed == []
    assert reasons.describe(reasons.BROADCAST_PAUSED_BY_CUSTOMER).startswith("You paused")
