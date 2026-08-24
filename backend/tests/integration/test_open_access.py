"""Open access: who gets in, and how they are stopped.

With ``ACCESS_MODE=open`` anyone on Telegram can reach this bot, so the
properties tested here are the ones the allowlist used to provide for free:

* one account cannot see or touch another's ads, rules or connections;
* an operator can suspend an account, and suspension stops work already queued
  rather than only new work;
* an operator cannot read what anyone sends.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.adminbot import handlers, views
from app.adminbot.auth import AccessMiddleware
from app.config import get_settings
from app.db.models import (
    Broadcast,
    BroadcastStatus,
    BroadcastTarget,
    ConnectionStatus,
    JobStatus,
    RuleStatus,
    User,
)
from app.db.session import session_scope
from app.domain import reasons
from app.repositories import admins as admin_repo
from app.repositories import broadcasts as broadcast_repo
from app.repositories import users as user_repo
from app.services import broadcast as broadcast_service
from app.services import users as user_service
from tests.conftest import connect_bot, discovered, sync_with_chats

OPERATOR_ID = 900_100_200
STRANGER_ID = 700_100_300
OTHER_ID = 700_100_301


@pytest.fixture(autouse=True)
def open_mode(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_telegram_ids", str(OPERATOR_ID), raising=False)
    monkeypatch.setattr(settings, "access_mode", "open", raising=False)
    return settings


# --------------------------------------------------------------------------- #
# Recording aiogram objects
# --------------------------------------------------------------------------- #
class Sent:
    messages: ClassVar[list[tuple[str, InlineKeyboardMarkup | None]]] = []
    alerts: ClassVar[list[str]] = []

    @classmethod
    def reset(cls) -> None:
        cls.messages = []
        cls.alerts = []

    @classmethod
    def last(cls) -> str:
        assert cls.messages, "the bot sent nothing"
        return cls.messages[-1][0]

    @classmethod
    def buttons(cls) -> list[str]:
        markup = cls.messages[-1][1] if cls.messages else None
        if markup is None:
            return []
        return [b.callback_data or "" for row in markup.inline_keyboard for b in row]

    @classmethod
    def text(cls) -> str:
        return "\n".join(t for t, _ in cls.messages)


class BotMessage(Message):
    async def answer(
        self, text: str = "", reply_markup: Any = None, parse_mode: str | None = None, **_kw: Any
    ) -> Any:
        Sent.messages.append((text, reply_markup))
        return self

    async def edit_text(
        self, text: str = "", reply_markup: Any = None, parse_mode: str | None = None, **_kw: Any
    ) -> Any:
        Sent.messages.append((text, reply_markup))
        return self

    async def delete(self, **_kw: Any) -> bool:
        return True


class BotCallback(CallbackQuery):
    async def answer(self, text: str | None = None, **_kw: Any) -> Any:
        if text:
            Sent.alerts.append(text)
        return True


def a_message(telegram_id: int, text: str = "") -> BotMessage:
    return BotMessage(
        message_id=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(id=telegram_id, type="private"),
        from_user=TgUser(id=telegram_id, is_bot=False, first_name="P"),
        text=text,
    )


def a_callback(telegram_id: int, data: str) -> BotCallback:
    return BotCallback(
        id="1",
        from_user=TgUser(id=telegram_id, is_bot=False, first_name="P"),
        chat_instance="ci",
        data=data,
        message=a_message(telegram_id),
    )


def a_state(telegram_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=telegram_id, user_id=telegram_id),
    )


@pytest.fixture(autouse=True)
def _reset():
    Sent.reset()
    yield


async def through_gate(event: object, telegram_id: int) -> dict:
    """Drive a real update through the middleware into the real handler."""
    captured: dict = {}

    async def handler(inner_event: object, data: dict) -> str:
        captured.update(data)
        return "handled"

    captured["_result"] = await AccessMiddleware()(
        handler,
        event,  # type: ignore[arg-type]
        {"event_from_user": TgUser(id=telegram_id, is_bot=False, first_name="P")},
    )
    return captured


async def onboard(telegram_id: int) -> uuid.UUID:
    """A person who has arrived. There is no gate left to pass."""
    async with session_scope() as session:
        user = await admin_repo.upsert_user(
            session, telegram_user_id=telegram_id, username=f"u{telegram_id}"
        )
        return user.id


# --------------------------------------------------------------------------- #
# Getting in
# --------------------------------------------------------------------------- #
async def test_a_stranger_is_let_in_when_access_is_open(client):
    result = await through_gate(a_message(STRANGER_ID, "/start"), STRANGER_ID)
    assert result["_result"] == "handled", "no gate stands between arriving and the panel"
    assert result["user_id"] is not None, "and an account exists for them"


async def test_the_same_stranger_is_refused_when_access_is_closed(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "access_mode", "closed", raising=False)
    result = await through_gate(a_message(STRANGER_ID, "/start"), STRANGER_ID)
    assert result["_result"] is None
    assert "not authorized" in Sent.text()


async def test_an_operator_gets_in_either_way(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "access_mode", "closed", raising=False)
    await onboard(OPERATOR_ID)
    result = await through_gate(a_message(OPERATOR_ID, "/start"), OPERATOR_ID)
    assert result["_result"] == "handled"
    assert result["is_operator"] is True


# --------------------------------------------------------------------------- #
# One account cannot reach another's
# --------------------------------------------------------------------------- #
async def test_two_people_get_separate_accounts(client, session):
    first = await onboard(STRANGER_ID)
    second = await onboard(OTHER_ID)
    assert first != second

    rows = (await session.execute(select(User).where(User.telegram_user_id.isnot(None)))).scalars()
    assert {u.telegram_user_id for u in rows} == {STRANGER_ID, OTHER_ID}


async def test_one_person_cannot_open_another_s_ad(client, actor, other_actor, session):
    """The isolation that matters most once the bot is open."""
    connection_id = await connect_bot(actor)
    await sync_with_chats(actor, connection_id, [discovered(-1002000, "G", chat_kind="supergroup")])

    broadcast = await broadcast_repo.create(
        session,
        user_id=uuid.UUID(actor.id),
        connection_id=uuid.UUID(connection_id),
        name="Private",
        delay_ms=0,
    )
    broadcast.body_text = "secret"
    await session.commit()

    Sent.reset()
    await handlers.ad_actions(
        a_callback(OTHER_ID, f"ad:{broadcast.id}"),
        user_id=uuid.UUID(other_actor.id),
        state=a_state(OTHER_ID),
    )
    assert any("no longer exists" in alert for alert in Sent.alerts)
    assert "secret" not in Sent.text()


async def test_one_person_cannot_sync_another_s_connection(client, actor, other_actor):
    connection_id = await connect_bot(actor)
    Sent.reset()
    await handlers.connection_actions(
        a_callback(OTHER_ID, f"conn:{connection_id}:sync"), user_id=uuid.UUID(other_actor.id)
    )
    assert any("no longer exists" in alert for alert in Sent.alerts)


async def test_the_users_screen_is_operator_only(client, actor):
    """Hiding the button is presentation; the handler is the gate. A callback
    can be replayed by anyone who has seen it."""
    Sent.reset()
    await handlers.nav_users(a_callback(STRANGER_ID, "nav:users:0"), is_operator=False)
    assert any("not available" in alert for alert in Sent.alerts)
    assert not Sent.messages, "no user list may be rendered"


async def test_suspending_is_operator_only(client, actor):
    Sent.reset()
    await handlers.user_actions(
        a_callback(STRANGER_ID, f"usr:{uuid.uuid4()}:sus"),
        user_id=uuid.UUID(actor.id),
        is_operator=False,
    )
    assert any("not available" in alert for alert in Sent.alerts)


async def test_the_home_screen_hides_the_users_button_from_ordinary_people():
    ordinary = views.home(connections=[], rules=[], broadcasts=[], counts={})
    operator = views.home(connections=[], rules=[], broadcasts=[], counts={}, is_operator=True)

    def buttons(screen):
        return [b.callback_data for row in screen.keyboard.inline_keyboard for b in row]

    assert "nav:users:0" not in buttons(ordinary)
    assert "nav:users:0" in buttons(operator)


# --------------------------------------------------------------------------- #
# Suspension
# --------------------------------------------------------------------------- #
async def test_a_suspended_account_is_turned_away_with_the_reason(client, session):
    user_id = await onboard(STRANGER_ID)
    user = await session.get(User, user_id)
    await user_service.suspend(session, user=user, reason="Reported for spam")
    await session.commit()

    Sent.reset()
    result = await through_gate(a_message(STRANGER_ID, "/start"), STRANGER_ID)

    assert result["_result"] is None
    assert "suspended" in Sent.text().lower()
    assert "Reported for spam" in Sent.text()


async def test_suspending_pauses_their_rules(client, actor, session):
    from tests.integration.test_forwarding import build_rule

    ctx = await build_rule(actor, destinations=1)
    user = await session.get(User, uuid.UUID(actor.id))

    stopped = await user_service.suspend(session, user=user, reason="abuse")
    await session.commit()

    assert stopped["rules"] == 1
    from app.db.models import ForwardingRule

    rule = await session.get(ForwardingRule, uuid.UUID(ctx["rule_id"]))
    assert rule.status is RuleStatus.paused
    assert rule.paused_reason_code == reasons.ACCOUNT_SUSPENDED


async def test_suspending_stops_a_broadcast_already_in_flight(client, actor, session):
    """The property that makes suspension real: work is already queued when an
    operator decides to stop someone."""
    from tests.integration.test_broadcast import build_broadcast

    ctx = await build_broadcast(actor, session, groups=3)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    user = await session.get(User, uuid.UUID(actor.id))
    stopped = await user_service.suspend(session, user=user)
    await session.commit()

    assert stopped["targets"] == 3
    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.paused

    remaining = (
        (
            await session.execute(
                select(BroadcastTarget).where(
                    BroadcastTarget.broadcast_id == broadcast.id,
                    BroadcastTarget.status == JobStatus.pending,
                )
            )
        )
        .scalars()
        .all()
    )
    assert not remaining, "nothing may still be waiting to send"


async def test_a_queued_delivery_checks_the_owner_at_send_time(client, actor, session):
    """Belt and braces: even a target the cascade somehow missed must not send."""
    from tests.integration.test_broadcast import build_broadcast, drain

    ctx = await build_broadcast(actor, session, groups=2)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    # Suspend the flag only, leaving the queue untouched.
    user = await session.get(User, uuid.UUID(actor.id))
    user.is_active = False
    await session.commit()

    outcomes = await drain(session, broadcast.id)
    assert all(o.reason_code == reasons.ACCOUNT_SUSPENDED for o in outcomes)

    from tests.conftest import script_for

    assert not script_for(ctx["connection_id"]).calls_to("send_text")


async def test_the_listener_stops_opening_a_suspended_account_s_connection(client, actor, session):
    from app.repositories import connections as connection_repo

    await connect_bot(actor)
    await session.commit()

    before = await connection_repo.list_active_for_intake(session)
    assert len(before) == 1

    user = await session.get(User, uuid.UUID(actor.id))
    await user_service.suspend(session, user=user)
    await session.commit()

    after = await connection_repo.list_active_for_intake(session)
    assert not after, "a suspended account's client must be released, not left connected"


async def test_reinstating_does_not_silently_resume_anything(client, actor, session):
    """Restarting a broadcast someone was suspended over is the operator's
    decision to invite, not ours to make."""
    from tests.integration.test_broadcast import build_broadcast

    ctx = await build_broadcast(actor, session, groups=2)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    user = await session.get(User, uuid.UUID(actor.id))
    await user_service.suspend(session, user=user)
    await session.commit()

    await user_service.reinstate(session, user=user)
    await session.commit()

    assert user.is_active
    assert user.suspended_reason is None
    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.paused


async def test_suspending_twice_is_harmless(client, actor, session):
    user = await session.get(User, uuid.UUID(actor.id))
    await user_service.suspend(session, user=user, reason="first")
    second = await user_service.suspend(session, user=user, reason="second")
    await session.commit()

    assert second == {"rules": 0, "broadcasts": 0, "jobs": 0, "targets": 0}
    assert user.suspended_reason == "first", "the original reason is not overwritten"


async def test_an_operator_cannot_suspend_themselves(client, session):
    """Locking the only operator out of their own deployment is not a mistake
    worth allowing."""
    operator_id = await onboard(OPERATOR_ID)

    Sent.reset()
    await handlers.user_actions(
        a_callback(OPERATOR_ID, f"usr:{operator_id}:sus"),
        user_id=operator_id,
        is_operator=True,
    )
    assert any("cannot suspend yourself" in alert for alert in Sent.alerts)

    session.expire_all()
    user = await session.get(User, operator_id)
    assert user.is_active


async def test_suspension_is_audited(client, actor, session):
    from app.db.models import AuditEvent

    user = await session.get(User, uuid.UUID(actor.id))
    await user_service.suspend(session, user=user, reason="abuse", by=user.id)
    await session.commit()

    rows = (
        (await session.execute(select(AuditEvent).where(AuditEvent.action == "user.suspend")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload["reason"] == "abuse"


# --------------------------------------------------------------------------- #
# What an operator can and cannot see
# --------------------------------------------------------------------------- #
async def test_the_user_detail_screen_shows_counts_and_no_content(client, actor, session):
    """Suspending does not need to read anyone's ads, and reading them would be
    a privacy breach the product does not make."""
    from tests.integration.test_broadcast import build_broadcast

    await build_broadcast(actor, session, groups=1, text="TOP SECRET OFFER")
    await session.commit()

    user = await session.get(User, uuid.UUID(actor.id))
    activity = await user_repo.activity_for(session, user_id=user.id)
    screen = views.user_detail(user=user, activity=activity)

    assert "TOP SECRET OFFER" not in screen.text
    assert "Ads created" in screen.text
    assert "Counts only" in screen.text


async def test_the_user_list_counts_everyone(client, session):
    await onboard(STRANGER_ID)
    await onboard(OTHER_ID)
    suspended_id = await onboard(OPERATOR_ID)
    await user_service.suspend(session, user=await session.get(User, suspended_id))
    await session.commit()

    totals = await user_repo.counts(session)
    assert totals["total"] == 3
    assert totals["suspended"] == 1


# --------------------------------------------------------------------------- #
# Operational limits
# --------------------------------------------------------------------------- #
async def test_one_account_cannot_hold_unlimited_connections(client, actor, monkeypatch, session):
    """Each connection is a live Telethon client in the listener. This is an
    operational ceiling, not a product limit."""
    from app.repositories import connections as connection_repo
    from app.services import connections as connection_service

    monkeypatch.setattr(get_settings(), "max_connections_per_user", 2, raising=False)

    for index in range(2):
        await connection_service.create_bot_connection(
            session,
            user_id=uuid.UUID(actor.id),
            label=f"Bot {index}",
            bot_token="123456789:AAEtestTokenValueThatIsLongEnough00",
        )
        # The partial unique index allows one in-progress attempt at a time.
        rows = await connection_repo.list_for_user(session, user_id=uuid.UUID(actor.id))
        rows[-1].status = ConnectionStatus.active
        await session.flush()

    with pytest.raises(connection_service.TooManyConnections) as exc:
        await connection_service.create_bot_connection(
            session,
            user_id=uuid.UUID(actor.id),
            label="Bot 3",
            bot_token="123456789:AAEtestTokenValueThatIsLongEnough00",
        )
    assert "maximum" in exc.value.message


async def test_flooding_the_bot_is_throttled(client, monkeypatch):
    """An open bot is reachable by anyone, so one client cannot occupy it."""
    from app.security.ratelimit import LIMITS, RateLimit

    monkeypatch.setitem(LIMITS, "bot_update", RateLimit(3, 60))
    await onboard(STRANGER_ID)

    results = [
        (await through_gate(a_message(STRANGER_ID, "/start"), STRANGER_ID))["_result"]
        for _ in range(5)
    ]
    assert results[:3] == ["handled"] * 3
    assert results[3:] == [None, None]
    assert "too quickly" in Sent.text()
