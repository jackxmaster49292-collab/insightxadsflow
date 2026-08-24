"""Starting an ad at a chosen time.

The hard part is not the timer, it is the timezone: a clock time read in the
wrong zone is a five-and-a-half-hour error that looks perfectly fine on screen.
So the parser is tested against a real zone, and the screen is checked for
showing the answer twice — once absolute, once relative — because a wrong zone
is invisible in the first form and obvious in the second.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.adminbot import views
from app.db.models import AppSetting, Broadcast, BroadcastStatus
from app.domain import when as when_domain
from app.repositories import broadcasts as broadcast_repo
from app.services import broadcast as broadcast_service
from tests.integration.test_broadcast import build_broadcast

IST = when_domain.zone("Asia/Kolkata")
NOW = datetime(2026, 8, 24, 16, 0, tzinfo=UTC)  # 21:30 in Kolkata


# --------------------------------------------------------------------------- #
# Reading what someone typed
# --------------------------------------------------------------------------- #
def test_a_clock_time_is_read_in_the_customers_zone():
    at = when_domain.parse_when("10:00", now=NOW, tz=IST)
    assert at.astimezone(IST).strftime("%d %b %H:%M") == "25 Aug 10:00"
    assert at == datetime(2026, 8, 25, 4, 30, tzinfo=UTC)


def test_a_clock_time_already_past_today_means_tomorrow():
    """Nobody schedules something for the past, and refusing would be pedantry."""
    at = when_domain.parse_when("21:00", now=NOW, tz=IST)  # 21:30 local already
    assert at.astimezone(IST).day == 25


def test_a_clock_time_still_to_come_means_today():
    at = when_domain.parse_when("22:00", now=NOW, tz=IST)
    assert at.astimezone(IST).day == 24


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("2h", 7200), ("90m", 5400), ("1d", 86_400), ("3h 30m", 12_600), ("45 minutes", 2700)],
)
def test_relative_times_need_no_timezone_at_all(text, seconds):
    assert when_domain.parse_when(text, now=NOW, tz=IST) == NOW + timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["kal", "", "25:00", "10:99", "soon", "tomorrow 10", "0h"])
def test_nonsense_is_refused_rather_than_guessed(text):
    assert when_domain.parse_when(text, now=NOW, tz=IST) is None


def test_a_year_out_is_not_a_schedule():
    assert when_domain.parse_when("400d", now=NOW, tz=IST) is None


def test_an_unknown_zone_falls_back_rather_than_crashing():
    """A bad name in a settings row should cost a wrong-looking time, not a
    dead scheduler."""
    assert when_domain.zone("Mars/Olympus").key == "UTC"
    assert not when_domain.is_a_zone("Mars/Olympus")
    assert when_domain.is_a_zone("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #
def test_the_start_time_is_shown_both_ways():
    """A wrong timezone looks fine as a clock time and obviously wrong as
    "in 14 hours"."""
    from tests.conftest import fake_broadcast

    at = datetime.now(UTC) + timedelta(hours=14)
    label = views.start_label(at, IST)
    assert "in about" in label
    assert at.astimezone(IST).strftime("%H:%M") in label

    screen = views.ad_compose(
        broadcast=fake_broadcast(scheduled_for=at), target_count=1, estimate_s=0, tz=IST
    )
    assert "Starts" in screen.text

    unscheduled = views.ad_compose(broadcast=fake_broadcast(), target_count=1, estimate_s=0)
    assert "as soon as you send it" in unscheduled.text


# --------------------------------------------------------------------------- #
# Queuing and starting
# --------------------------------------------------------------------------- #
async def test_sending_a_scheduled_ad_queues_it_instead_of_posting(client, actor, state, session):
    from app.adminbot import handlers
    from tests.conftest import script_for
    from tests.integration.test_bot_flows import Sent, a_callback

    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.scheduled_for = datetime.now(UTC) + timedelta(hours=3)
    await session.commit()

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:send"), user_id=uuid.UUID(actor.id), state=state
    )

    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.scheduled
    assert script_for(ctx["connection_id"]).calls_to("send_text") == [], "nothing posted yet"
    assert any("Scheduled" in alert for alert in Sent.alerts)


async def test_an_unusable_ad_is_refused_now_not_at_six_in_the_morning(
    client, actor, state, session
):
    """Validation happens when they press Send, whatever the start time is."""
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import Sent, a_callback

    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_repo.replace_targets(session, broadcast=broadcast, chat_ids=[])
    broadcast.scheduled_for = datetime.now(UTC) + timedelta(hours=3)
    await session.commit()

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:send"), user_id=uuid.UUID(actor.id), state=state
    )

    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.draft
    assert any("at least one group" in alert for alert in Sent.alerts)


async def test_a_due_ad_is_started_by_the_scheduler(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.status = BroadcastStatus.scheduled
    broadcast.scheduled_for = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    due = await broadcast_repo.due_scheduled(session)
    assert [b.id for b in due] == [broadcast.id]

    queued = await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    assert queued == 2
    assert broadcast.status is BroadcastStatus.sending


async def test_an_ad_whose_time_has_not_come_is_left_alone(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.status = BroadcastStatus.scheduled
    broadcast.scheduled_for = datetime.now(UTC) + timedelta(hours=2)
    await session.commit()

    assert await broadcast_repo.due_scheduled(session) == []


async def test_a_backlog_goes_out_oldest_first(client, actor, session):
    """After downtime, in the order they were asked for — not newest-wins."""
    from app.repositories import broadcasts as repo

    ctx = await build_broadcast(actor, session, groups=1)
    first = await session.get(Broadcast, ctx["broadcast_id"])
    first.status = BroadcastStatus.scheduled
    first.scheduled_for = datetime.now(UTC) - timedelta(hours=3)

    second = await repo.create(
        session,
        user_id=uuid.UUID(actor.id),
        connection_id=first.connection_id,
        name="Later",
        delay_ms=0,
    )
    second.status = BroadcastStatus.scheduled
    second.scheduled_for = datetime.now(UTC) - timedelta(hours=1)
    await session.commit()

    assert [b.id for b in await repo.due_scheduled(session)] == [first.id, second.id]


# --------------------------------------------------------------------------- #
# Setting the time from the panel
# --------------------------------------------------------------------------- #
async def test_the_time_can_be_set_and_cleared(client, actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback, a_message

    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:sched"), user_id=uuid.UUID(actor.id), state=state
    )
    await handlers.ad_schedule(a_message("2h"), user_id=uuid.UUID(actor.id), state=state)

    await session.refresh(broadcast)
    assert broadcast.scheduled_for is not None
    assert broadcast.scheduled_for > datetime.now(UTC)

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:sched"), user_id=uuid.UUID(actor.id), state=state
    )
    await handlers.ad_schedule(a_message("now"), user_id=uuid.UUID(actor.id), state=state)
    await session.refresh(broadcast)
    assert broadcast.scheduled_for is None


async def test_a_timezone_can_be_answered_instead_of_a_time(client, actor, state, session):
    """What someone does when the confirmed time came back wrong."""
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import Sent, a_callback, a_message

    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:sched"), user_id=uuid.UUID(actor.id), state=state
    )
    await handlers.ad_schedule(a_message("Asia/Kolkata"), user_id=uuid.UUID(actor.id), state=state)

    setting = (
        await session.execute(select(AppSetting).where(AppSetting.user_id == uuid.UUID(actor.id)))
    ).scalar_one()
    await session.refresh(setting)
    assert setting.timezone == "Asia/Kolkata"
    assert "Timezone set" in Sent.last()
    assert broadcast.scheduled_for is None, "the time itself still has to be sent"


async def test_a_time_it_cannot_read_says_so_and_offers_the_timezone(client, actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import Sent, a_callback, a_message

    ctx = await build_broadcast(actor, session, groups=1)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])

    await handlers.ad_actions(
        a_callback(f"ad:{broadcast.id}:sched"), user_id=uuid.UUID(actor.id), state=state
    )
    await handlers.ad_schedule(a_message("kal subah"), user_id=uuid.UUID(actor.id), state=state)

    assert "not a time I can read" in Sent.last()
    assert "timezone" in Sent.last()
    await session.refresh(broadcast)
    assert broadcast.scheduled_for is None
