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


async def test_a_failing_eligibility_check_fails_closed_but_not_forever(client, actor, session):
    """A check that errors is not a confirmation — and it is not a refusal
    either. The network blinking during the check used to skip the group
    permanently, which read as "this group refused you" when the truth was
    "nothing was learned". Now it retries like any transient failure; nothing
    is ever sent until a check has actually passed."""
    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    connection = await session.get(TelegramConnection, broadcast.connection_id)
    adapter = await connection_service.adapter_for(session, connection)

    real_check = adapter.check_destination_access

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

    assert outcome.status is JobStatus.pending, "scheduled to try again, not given up on"
    assert target.status is JobStatus.pending
    assert target.attempt_count == 1
    sends = script_for(ctx["connection_id"]).calls_to("send_text")
    assert sends == [], "fail-closed: nothing sent while the check cannot pass"

    # The network comes back; the next attempt delivers.
    adapter.check_destination_access = real_check  # type: ignore[method-assign]
    outcome = await broadcast_service.execute_target(
        session, target=target, adapter=adapter, connection=connection
    )
    assert outcome.status is JobStatus.succeeded


async def test_a_permanent_check_error_still_skips(client, actor, session):
    """ "This channel is private" from the check itself is a fact, not a blink."""
    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    connection = await session.get(TelegramConnection, broadcast.connection_id)
    adapter = await connection_service.adapter_for(session, connection)

    async def refuse(_ref):
        raise AdapterError(reasons.WRITE_FORBIDDEN, ErrorClass.PERMISSION)

    adapter.check_destination_access = refuse  # type: ignore[method-assign]

    target = (
        await session.execute(
            select(BroadcastTarget).where(BroadcastTarget.broadcast_id == broadcast.id)
        )
    ).scalar_one()
    outcome = await broadcast_service.execute_target(
        session, target=target, adapter=adapter, connection=connection
    )

    assert outcome.status is JobStatus.skipped
    assert script_for(ctx["connection_id"]).calls_to("send_text") == []


async def test_a_check_that_never_recovers_becomes_a_visible_dead_letter(client, actor, session):
    """Bounded retries, then it shows up in Retry — never a silent skip and
    never an infinite loop."""
    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    connection = await session.get(TelegramConnection, broadcast.connection_id)
    adapter = await connection_service.adapter_for(session, connection)

    async def explode(_ref):
        raise RuntimeError("network is having a very bad day")

    adapter.check_destination_access = explode  # type: ignore[method-assign]

    target = (
        await session.execute(
            select(BroadcastTarget).where(BroadcastTarget.broadcast_id == broadcast.id)
        )
    ).scalar_one()

    outcome = None
    for _ in range(get_settings().max_attempts):
        target.status = JobStatus.pending
        outcome = await broadcast_service.execute_target(
            session, target=target, adapter=adapter, connection=connection
        )

    assert outcome is not None
    assert outcome.status is JobStatus.dead_letter
    assert script_for(ctx["connection_id"]).calls_to("send_text") == []


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
    assert broadcast.paused_reason_code == reasons.BROADCAST_FLOOD_WAIT
    assert "by itself" in reasons.describe(reasons.BROADCAST_FLOOD_WAIT)

    # While Telegram's wait is still running, the sweep must not shorten it.
    resumed = await broadcast_repo.resume_flood_paused(session)
    assert resumed == 0
    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.paused

    # The wait passes; the scheduler's sweep resumes the ad on its own.
    await session.execute(
        update(BroadcastTarget)
        .where(BroadcastTarget.broadcast_id == broadcast.id)
        .values(not_before=datetime.now(UTC) - timedelta(seconds=1))
    )
    resumed = await broadcast_repo.resume_flood_paused(session)
    assert resumed == 1
    await session.commit()
    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.sending
    assert broadcast.paused_reason_code is None


async def test_the_sweep_never_resumes_a_pause_the_customer_chose(client, actor, session):
    """ "You paused this ad" has to stay true until they resume it themselves —
    a sweep overriding a human decision is worse than any stuck state."""
    ctx = await build_broadcast(actor, session, groups=1, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await broadcast_service.pause(
        session, broadcast=broadcast, reason_code=reasons.BROADCAST_PAUSED_BY_CUSTOMER
    )
    await session.execute(
        update(BroadcastTarget)
        .where(BroadcastTarget.broadcast_id == broadcast.id)
        .values(not_before=datetime.now(UTC) - timedelta(minutes=5))
    )

    assert await broadcast_repo.resume_flood_paused(session) == 0
    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.paused


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


# --------------------------------------------------------------------------- #
# Editing an ad that is already running
# --------------------------------------------------------------------------- #
async def test_editing_the_groups_keeps_what_already_went_out(client, actor, session):
    """The one thing an edit must never do is make an ad post twice to a group
    it has already reached."""
    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()

    sent_first = len(script_for(ctx["connection_id"]).calls_to("send_text"))
    assert sent_first == 3

    # The customer re-opens the picker and adds nothing, keeping all three.
    await broadcast_repo.replace_targets(session, broadcast=broadcast, chat_ids=ctx["chat_ids"])
    await session.commit()

    counts = await broadcast_repo.status_counts(session, broadcast_id=broadcast.id)
    assert counts == {"succeeded": 3}, "history survived the edit"

    broadcast.status = BroadcastStatus.sending
    await broadcast_repo.schedule_targets(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    assert len(script_for(ctx["connection_id"]).calls_to("send_text")) == sent_first


async def test_a_group_removed_by_an_edit_stops_receiving_it(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])

    kept = await broadcast_repo.replace_targets(
        session, broadcast=broadcast, chat_ids=ctx["chat_ids"][:2]
    )
    await session.commit()

    assert kept == 2
    remaining = await broadcast_repo.target_chat_ids(session, broadcast_id=broadcast.id)
    assert remaining == ctx["chat_ids"][:2]


async def test_an_edit_that_adds_a_group_posts_only_to_the_new_one(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    # Start with two of the three, send, then add the third.
    await broadcast_repo.replace_targets(session, broadcast=broadcast, chat_ids=ctx["chat_ids"][:2])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()

    await broadcast_repo.replace_targets(session, broadcast=broadcast, chat_ids=ctx["chat_ids"])
    broadcast.status = BroadcastStatus.sending
    await broadcast_repo.schedule_targets(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)

    sends = script_for(ctx["connection_id"]).calls_to("send_text")
    assert len(sends) == 3, "two in the first pass, only the new group in the second"


async def test_positions_are_renumbered_so_the_pacing_stays_even(client, actor, session):
    """Gaps in position would leave the stagger uneven after an edit."""
    ctx = await build_broadcast(actor, session, groups=3, delay_ms=1000)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_repo.replace_targets(
        session, broadcast=broadcast, chat_ids=[ctx["chat_ids"][0], ctx["chat_ids"][2]]
    )
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
    assert [t.position for t in targets] == [0, 1]


async def test_sending_a_completed_ad_again_reopens_every_group(client, actor, session):
    """Without reopening there is nothing pending, nothing is scheduled, and
    the ad sits in "sending" forever with no work that could ever settle it."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.adminbot import handlers
    from tests.integration.test_bot_flows import ADMIN_CHAT, Sent, a_callback

    Sent.reset()
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_CHAT, user_id=ADMIN_CHAT),
    )

    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()
    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.completed

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:send"), user_id=uuid.UUID(actor.id), state=state
    )

    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.sending
    counts = await broadcast_repo.status_counts(session, broadcast_id=broadcast.id)
    assert counts == {"pending": 2}, "a re-run addresses every group again"

    await drain(session, broadcast.id)
    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.completed, "and it can finish again"


# --------------------------------------------------------------------------- #
# The per-group report
# --------------------------------------------------------------------------- #
async def test_the_group_report_names_every_group_and_what_happened(client, actor, session):
    """The counts say "1 of 158 did not receive it"; this screen says which one
    and why. Both questions, by name."""
    from app.adminbot import views

    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    # The middle group refuses.
    from app.adapters.base import AccessReport as _AccessReport
    from app.db.models import TelegramChat
    from app.repositories import chats as chat_repo

    script = script_for(ctx["connection_id"])
    blocked = chat_repo.to_ref(await session.get(TelegramChat, ctx["chat_ids"][1]))
    script.destination_allowed[blocked.key] = _AccessReport(
        allowed=False, reason_code=reasons.WRITE_FORBIDDEN
    )

    await drain(session, broadcast.id)
    await session.commit()

    rows = await broadcast_repo.targets_with_chats(session, broadcast_id=broadcast.id)
    screen = views.ad_group_report(broadcast=broadcast, rows=rows, page=0)

    from tests.integration.test_bot_flows import (
        assert_keyboard_is_sendable,
        assert_valid_markdown_v2,
    )

    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)
    assert "Delivered* — 2/3" in screen.text
    assert "problems* — 1" in screen.text
    # The refused group is listed first, by name, with the reason under it.
    refused_at = screen.text.index("Group 02")
    assert refused_at < screen.text.index("Group 01")
    assert reasons.describe(reasons.WRITE_FORBIDDEN)[:20] in screen.text


async def test_the_group_report_pages_and_puts_problems_first(client, actor, session):
    from app.adminbot import views

    ctx = await build_broadcast(actor, session, groups=25, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    from app.adapters.base import AccessReport as _AccessReport
    from app.db.models import TelegramChat
    from app.repositories import chats as chat_repo

    script = script_for(ctx["connection_id"])
    # The very last group refuses — on a position-ordered list it would sit on
    # the final page, exactly where nobody scrolls.
    blocked = chat_repo.to_ref(await session.get(TelegramChat, ctx["chat_ids"][-1]))
    script.destination_allowed[blocked.key] = _AccessReport(
        allowed=False, reason_code=reasons.WRITE_FORBIDDEN
    )

    await drain(session, broadcast.id)
    await session.commit()
    rows = await broadcast_repo.targets_with_chats(session, broadcast_id=broadcast.id)

    first_page = views.ad_group_report(broadcast=broadcast, rows=rows, page=0)
    assert "Group 25" in first_page.text, "the one problem leads the first page"
    assert len(first_page.text) <= 4096

    last_page = views.ad_group_report(broadcast=broadcast, rows=rows, page=2)
    assert "Group 25" not in last_page.text
    assert len(last_page.text) <= 4096


async def test_a_hostile_group_title_cannot_break_the_report(client, actor, session):
    from types import SimpleNamespace

    from app.adminbot import views
    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    target = SimpleNamespace(status=JobStatus.skipped, position=0, last_error_code=None)
    chat = SimpleNamespace(title="_evil* [x](y) `code` #tag!")
    screen = views.ad_group_report(
        broadcast=await session.get(
            Broadcast, (await build_broadcast(actor, session))["broadcast_id"]
        ),
        rows=[(target, chat)],
        page=0,
    )
    assert_valid_markdown_v2(screen.text)


# --------------------------------------------------------------------------- #
# Speed: how long a round actually takes
# --------------------------------------------------------------------------- #
async def test_fast_pacing_schedules_a_large_round_in_under_a_minute(client, actor, session):
    """150 groups at the old 3s default is 7.5 minutes of stagger before the
    worker even gets to the tail. Fast is the same machinery, paced for the
    fact that every target is a *different* group."""
    from app.adminbot import views

    fast_ms = dict(views.SPEED_PRESETS)["⚡ Fast"]
    ctx = await build_broadcast(actor, session, groups=150, delay_ms=fast_ms)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
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
    spread = (targets[-1].not_before - targets[0].not_before).total_seconds()
    expected = broadcast_service.estimated_duration_s(fast_ms, len(targets))
    assert spread == pytest.approx(expected, abs=0.1), "the estimate is what happens"
    assert spread > 0, "still paced — not one instantaneous burst"
    # The number that matters to the customer: a full 150-group round.
    assert broadcast_service.estimated_duration_s(fast_ms, 150) < 60


async def test_many_groups_are_in_flight_at_once(client, actor, session):
    """The screenshot showed "1 leased · 145 pending": one group at a time.
    The ceiling for broadcasts is higher because their targets never share a
    group, so the per-group limit cannot bind."""
    from app.worker import _claim_broadcasts_within_limits

    settings = get_settings()
    ctx = await build_broadcast(actor, session, groups=20, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    claimed = await _claim_broadcasts_within_limits(session, 50)
    assert len(claimed) == settings.broadcast_inflight
    assert settings.broadcast_inflight > settings.per_connection_inflight


async def test_the_speed_screen_shows_the_arithmetic(client, actor, session):
    from app.adminbot import views

    ctx = await build_broadcast(actor, session, groups=3, delay_ms=3000)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    screen = views.ad_speed(broadcast=broadcast, target_count=150)

    labels = [b.text for row in screen.keyboard.inline_keyboard for b in row]
    assert any("Fast" in label for label in labels)
    # Every preset states what it means for *this* group count.
    fast = next(label for label in labels if "Fast" in label)
    assert "seconds" in fast, f"the preset must say how long a round takes: {fast}"

    from tests.integration.test_bot_flows import (
        assert_keyboard_is_sendable,
        assert_valid_markdown_v2,
    )

    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)


async def test_a_pause_below_the_floor_is_refused(client, actor, state, session):
    """The dial stops where the gain is seconds and the risk is an account."""
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback, a_message

    ctx = await build_broadcast(actor, session, groups=2)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    before = broadcast.delay_ms

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:delay"), user_id=uuid.UUID(actor.id), state=state
    )
    await handlers.ad_delay(a_message("0"), user_id=uuid.UUID(actor.id), state=state)

    await session.refresh(broadcast)
    assert broadcast.delay_ms == before, "unchanged"


# --------------------------------------------------------------------------- #
# Reporting the round
# --------------------------------------------------------------------------- #
async def test_a_finished_round_reports_itself(client, actor, session):
    """A round can take a minute or an hour. Being told the outcome is the
    difference between a tool that ran and one that told you what it did."""
    from app.db.models import AdminNotification

    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    from app.adapters.base import AccessReport as _AccessReport
    from app.db.models import TelegramChat
    from app.repositories import chats as chat_repo

    blocked = chat_repo.to_ref(await session.get(TelegramChat, ctx["chat_ids"][1]))
    script_for(ctx["connection_id"]).destination_allowed[blocked.key] = _AccessReport(
        allowed=False, reason_code=reasons.WRITE_FORBIDDEN
    )

    await drain(session, broadcast.id)
    await session.commit()

    alert = (
        (
            await session.execute(
                select(AdminNotification).where(
                    AdminNotification.kind == "broadcast_round_finished"
                )
            )
        )
        .scalars()
        .one()
    )
    assert "2 of 3 groups received it" in alert.body
    assert "1 did not" in alert.body
    assert "Groups" in alert.body, "and where to look"


async def test_each_repeat_round_reports_once(client, actor, session):
    """Dedupe by round number: once per round, never twice, never only once."""
    from app.db.models import AdminNotification

    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = 7200
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()
    # A second settle of the same round must not produce a second alert.
    await broadcast_service.settle(session, broadcast=broadcast)
    await session.commit()

    alerts = (
        (
            await session.execute(
                select(AdminNotification).where(
                    AdminNotification.kind == "broadcast_round_finished"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "round 1" in alerts[0].title


# --------------------------------------------------------------------------- #
# Deleting an ad
# --------------------------------------------------------------------------- #
async def test_a_finished_ad_can_be_deleted(client, actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import Sent, a_callback

    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()
    assert broadcast.status is BroadcastStatus.completed

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:askdel"), user_id=uuid.UUID(actor.id), state=state
    )
    assert "Delete" in Sent.last()
    assert "cannot unsend" in Sent.last(), "and it is honest about what deleting does not do"

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:del"), user_id=uuid.UUID(actor.id), state=state
    )

    session.expire_all()
    assert await session.get(Broadcast, ctx["broadcast_id"]) is None
    remaining = (
        (
            await session.execute(
                select(BroadcastTarget).where(BroadcastTarget.broadcast_id == ctx["broadcast_id"])
            )
        )
        .scalars()
        .all()
    )
    assert remaining == [], "its target rows go with it"


async def test_a_running_ad_cannot_be_deleted(client, actor, state, session):
    """Deleting mid-flight would drop rows the worker holds leases on."""
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import Sent, a_callback

    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:del"), user_id=uuid.UUID(actor.id), state=state
    )

    session.expire_all()
    assert await session.get(Broadcast, ctx["broadcast_id"]) is not None
    assert any("Stop it first" in alert for alert in Sent.alerts)


async def test_the_delete_button_only_appears_when_it_is_allowed(client, actor, session):
    from app.adminbot import views

    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])

    running = views.ad_detail(
        broadcast=await _with_status(session, broadcast, BroadcastStatus.sending),
        counts={},
        target_count=1,
    )
    stopped = views.ad_detail(
        broadcast=await _with_status(session, broadcast, BroadcastStatus.cancelled),
        counts={},
        target_count=1,
    )

    def callbacks(screen):
        return [b.callback_data for row in screen.keyboard.inline_keyboard for b in row]

    assert any(c.endswith(":cancel") for c in callbacks(running))
    assert not any(c.endswith(":askdel") for c in callbacks(running))
    assert any(c.endswith(":askdel") for c in callbacks(stopped))


async def _with_status(session, broadcast, status):
    broadcast.status = status
    return broadcast


async def test_another_account_cannot_delete_your_ad(client, actor, other_actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback

    ctx = await build_broadcast(actor, session, groups=1)
    await handlers.ad_actions(
        a_callback(f"ad:{ctx['broadcast_id']}:del"),
        user_id=uuid.UUID(other_actor.id),
        state=state,
    )

    session.expire_all()
    assert await session.get(Broadcast, ctx["broadcast_id"]) is not None
