"""The Telegram control panel: the access gate, the screens, and push alerts.

Anyone on Telegram can find and message a bot, so the middleware is the entire
security model for this surface and gets adversarial coverage. It decides four
things in order — allowed in, not throttled, not suspended, terms accepted —
and each has its own failure, so each is tested separately.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.adminbot import views
from app.adminbot.auth import AccessMiddleware
from app.config import get_settings
from app.db.models import AdminNotification, RuleStatus, User
from app.repositories import admins as admin_repo
from tests.conftest import fake_broadcast

ADMIN_ID = 900_100_200
STRANGER_ID = 111_222_333


@pytest.fixture(autouse=True)
def admin_settings(monkeypatch):
    """Closed mode: only the operator ids may use the bot.

    Set explicitly rather than relied on as the default, so a future change to
    the default cannot quietly turn these into tests of something else.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_bot_token", "555000111:AAEtoken", raising=False)
    monkeypatch.setattr(settings, "admin_telegram_ids", str(ADMIN_ID), raising=False)
    monkeypatch.setattr(settings, "access_mode", "closed", raising=False)
    return settings


# --------------------------------------------------------------------------- #
# Fake aiogram objects
# --------------------------------------------------------------------------- #
class CapturingMessage(Message):
    """A real ``Message``, which is what makes this test meaningful.

    The middleware's rejection path branches on ``isinstance``, so a duck-typed
    stand-in would take neither branch and the test would pass while the
    stranger heard nothing. aiogram's models are frozen pydantic, so replies are
    collected on the class rather than patched onto the instance.
    """

    replies: ClassVar[list[str]] = []

    async def answer(self, text: str = "", **_kwargs: Any) -> Any:  # type: ignore[override]
        CapturingMessage.replies.append(text)


class CapturingCallback(CallbackQuery):
    replies: ClassVar[list[str]] = []

    async def answer(self, text: str | None = None, **_kwargs: Any) -> Any:  # type: ignore[override]
        CapturingCallback.replies.append(text or "")


@pytest.fixture(autouse=True)
def _clear_replies():
    CapturingMessage.replies.clear()
    CapturingCallback.replies.clear()
    yield


def a_message() -> CapturingMessage:
    return CapturingMessage(
        message_id=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
    )


def a_callback() -> CapturingCallback:
    return CapturingCallback(
        id="1",
        from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="A"),
        chat_instance="x",
        data="nav:home",
    )


async def accept_terms_for(telegram_id: int) -> None:
    """Mark this Telegram identity as having accepted, so a test about the
    allowlist is not really a test about the terms gate."""
    from app.db.session import session_scope
    from app.repositories import admins as admin_repo
    from app.services import users as user_service

    async with session_scope() as session:
        user = await admin_repo.upsert_user(
            session, telegram_user_id=telegram_id, username="operator"
        )
        await user_service.accept_terms(session, user=user)


async def run_middleware(event: object, telegram_id: int, *, accepted: bool = True) -> dict:
    """Push one update through the guard and report what the handler received."""
    if accepted:
        await accept_terms_for(telegram_id)

    seen: dict = {}

    async def handler(_event: object, data: dict) -> str:
        seen.update(data)
        return "handled"

    result = await AccessMiddleware()(
        handler,
        event,  # type: ignore[arg-type]
        {"event_from_user": TgUser(id=telegram_id, is_bot=False, first_name="Op")},
    )
    seen["_result"] = result
    return seen


# --------------------------------------------------------------------------- #
# The allowlist
# --------------------------------------------------------------------------- #
async def test_an_allowlisted_admin_reaches_the_handler():
    seen = await run_middleware(a_message(), ADMIN_ID)
    assert seen["_result"] == "handled"
    assert isinstance(seen["user_id"], uuid.UUID)


async def test_a_stranger_never_reaches_the_handler():
    seen = await run_middleware(a_message(), STRANGER_ID)
    assert seen["_result"] is None
    assert "user_id" not in seen
    assert CapturingMessage.replies, "the stranger must be told, not silently ignored"


async def test_a_button_press_is_guarded_too():
    """A callback_query does not pass through message middleware. Missing this
    would leave every button in the panel unguarded."""
    seen = await run_middleware(a_callback(), STRANGER_ID)
    assert seen["_result"] is None
    assert CapturingCallback.replies


async def test_an_empty_allowlist_locks_everyone_out(monkeypatch):
    """A misconfigured deploy must fail closed, not open."""
    monkeypatch.setattr(get_settings(), "admin_telegram_ids", "", raising=False)
    seen = await run_middleware(a_message(), ADMIN_ID)
    assert seen["_result"] is None


async def test_a_denied_attempt_is_audited(session):
    await run_middleware(a_message(), STRANGER_ID)
    from app.db.models import AuditEvent

    rows = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "admin.access_denied")
            )
        )
        .scalars()
        .all()
    )
    assert [r.object_id for r in rows] == [str(STRANGER_ID)]


async def test_the_admin_account_is_created_once_and_reused(session):
    first = await run_middleware(a_message(), ADMIN_ID)
    second = await run_middleware(a_message(), ADMIN_ID)
    assert first["user_id"] == second["user_id"]

    rows = (
        (await session.execute(select(User).where(User.telegram_user_id == ADMIN_ID)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].password_hash is None, "a Telegram-only admin never gets a password"


async def test_a_passwordless_account_cannot_be_logged_into_with_any_password(client, session):
    """The real defense: `password_hash IS NULL` must never verify.

    Uses a routable-looking address so the request reaches the password check
    rather than stopping at email validation.
    """
    from app.repositories import users as user_repo

    user = await user_repo.create(session, email="tg-admin@example.com", password="temporary-pass")
    user.password_hash = None
    user.telegram_user_id = ADMIN_ID
    await session.commit()

    for attempt in ("temporary-pass", "anything", "None", "null", " "):
        response = await client.post(
            "/auth/login", json={"email": "tg-admin@example.com", "password": attempt}
        )
        assert response.status_code == 401, f"password {attempt!r} was accepted"


# --------------------------------------------------------------------------- #
# Allowlist parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("123", {123}),
        ("123,456", {123, 456}),
        (" 123 , 456 ", {123, 456}),
        ("123;456", {123, 456}),
        ("", set()),
        ("not-a-number", set()),
        ("123,oops,456", {123, 456}),
    ],
)
def test_allowlist_parsing_drops_garbage_rather_than_guessing(monkeypatch, raw, expected):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_telegram_ids", raw, raising=False)
    assert settings.admin_ids == expected


def test_is_admin_rejects_unknown_ids(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_telegram_ids", "42", raising=False)
    assert settings.is_admin(42)
    assert not settings.is_admin(43)


# --------------------------------------------------------------------------- #
# Screens
# --------------------------------------------------------------------------- #
def buttons(screen: views.Screen) -> list[str]:
    return [b.callback_data or "" for row in screen.keyboard.inline_keyboard for b in row]


def test_home_screen_without_a_connection_guides_the_operator():
    screen = views.home(connections=[], rules=[], broadcasts=[], counts={})
    assert "Start here" in screen.text
    assert "nav:conns" in buttons(screen), "the next step must be one tap away"


def test_home_screen_introduces_the_features():
    """Home is an introduction, not a status board: it reads the same on the
    first visit and the thousandth."""
    from types import SimpleNamespace

    connection = SimpleNamespace(
        id=uuid.uuid4(),
        label="Jack",
        kind=SimpleNamespace(value="user"),
        status=SimpleNamespace(value="active"),
    )
    screen = views.home(connections=[connection], rules=[], broadcasts=[], counts={})

    for feature in ("*Ads*", "Auto\\-reply", "*Forwarding*"):
        assert feature in screen.text
    # The counts and connection health belong to the screens that own them.
    assert "Jack" not in screen.text
    assert "Last 24h" not in screen.text


def test_the_accounts_screen_carries_the_health_home_used_to_show():
    """Moved, not dropped — otherwise "1 of 2 working" would be nowhere."""
    from types import SimpleNamespace

    def connection(status: str, label: str):
        return SimpleNamespace(
            id=uuid.uuid4(),
            label=label,
            kind=SimpleNamespace(value="user"),
            status=SimpleNamespace(value=status),
            telegram_username=None,
            last_error_message_safe=None,
        )

    screen = views.connections_list(
        connections=[connection("active", "Jack"), connection("error", "Spare")]
    )
    assert "1 of 2 working" in screen.text
    assert "Jack" in screen.text and "Spare" in screen.text


def test_home_screen_offers_ads_and_auto_reply():
    screen = views.home(connections=[], rules=[], broadcasts=[], counts={})
    assert "nav:ads:0" in buttons(screen)
    assert "nav:autoreply" in buttons(screen)


def test_markdown_special_characters_in_titles_are_escaped():
    """Chat titles are attacker-influenced; an unescaped one makes Telegram
    reject the whole message with a 400."""
    escaped = views.escape("*bold* [link](x) _under_ `code` ~s~")
    for char in "*[]()_`~":
        assert f"\\{char}" in escaped


def test_callback_data_stays_within_telegram_s_64_byte_limit():
    """Telegram rejects the entire keyboard, not just the offending button, so
    one long callback breaks a whole screen."""
    ident = uuid.uuid4()
    for data in (
        f"rule:{ident}",
        f"rule:{ident}:pause",
        f"rule:{ident}:resume",
        f"rule:{ident}:retry",
        f"rule:{ident}:events",
        f"rule:{ident}:askdel",
        f"rule:{ident}:save",
        f"conn:{ident}:sync",
        f"conn:{ident}:askdel",
        f"ad:{ident}:confirm",
        f"ad:{ident}:discard",
        f"ad:{ident}:media",
        "nav:rules:99",
        "nav:chats:99",
        "nav:ads:99",
        f"{views.PICK}t499",
        f"{views.PICK}p99",
    ):
        assert len(data.encode()) <= 64, f"{data} is {len(data.encode())} bytes"


def test_every_button_the_screens_emit_fits_the_limit():
    """A guard over the real screens, not a hand-written list — a new button
    with a long callback should fail here rather than in production."""

    chat = SimpleNamespace(
        id=uuid.uuid4(),
        title="A group",
        access=SimpleNamespace(
            can_read_source=True,
            can_post_destination=True,
            source_reason_code="ok",
            destination_reason_code="ok",
        ),
    )
    connection = SimpleNamespace(
        id=uuid.uuid4(),
        label="Main",
        kind=SimpleNamespace(value="user"),
        status=SimpleNamespace(value="active"),
        telegram_username="me",
        last_error_message_safe=None,
    )
    broadcast = fake_broadcast()

    screens = [
        views.home(connections=[connection], rules=[], broadcasts=[broadcast], counts={}),
        views.home(
            connections=[connection], rules=[], broadcasts=[broadcast], counts={}, is_operator=True
        ),
        views.terms(),
        views.connections_list(connections=[connection]),
        views.connection_detail(connection=connection, chat_count=3),
        views.confirm_disconnect(connection=connection),
        views.ads_list(broadcasts=[broadcast], page=0, can_create=True),
        views.ad_compose(broadcast=broadcast, target_count=2, estimate_s=6),
        views.ad_confirm(broadcast=broadcast, target_count=2, estimate_s=6),
        views.ad_detail(broadcast=broadcast, counts={"succeeded": 1}, target_count=2),
        views.group_picker(
            chats=[chat],
            selected=set(),
            page=0,
            title="Choose groups",
            hint="Tap to select.",
            done_callback=f"ad:{broadcast.id}",
        ),
        views.chats_list(chats=[chat], page=0),
        views.autoreply_screen(connection=connection, reply=None),
    ]

    for screen in screens:
        for data in buttons(screen):
            assert len(data.encode()) <= 64, f"{data!r} is {len(data.encode())} bytes"


def test_callback_parsing_round_trips():
    rule_id = str(uuid.uuid4())
    assert views.parse_callback(f"rule:{rule_id}:pause") == ("rule", rule_id, "pause")
    assert views.parse_callback("nav:home") == ("nav", "home", None)
    assert views.as_uuid("not-a-uuid") is None
    assert views.as_uuid(None) is None
    assert views.as_uuid(rule_id) == uuid.UUID(rule_id)


def test_rules_list_paginates():
    class FakeRule:
        def __init__(self, index: int) -> None:
            self.id = uuid.uuid4()
            self.name = f"Rule {index}"
            self.status = RuleStatus.active

    rules = [FakeRule(i) for i in range(15)]
    first = views.rules_list(rules=rules, page=0, can_create=True)
    assert "1/3" in " ".join(b.text for row in first.keyboard.inline_keyboard for b in row)

    last = views.rules_list(rules=rules, page=99, can_create=True)
    assert "3/3" in " ".join(b.text for row in last.keyboard.inline_keyboard for b in row), (
        "out-of-range pages must clamp, not crash"
    )


def test_creating_is_hidden_until_a_connection_exists():
    """Offering a button that cannot work is worse than not offering it."""
    screen = views.ads_list(broadcasts=[], page=0, can_create=False)
    assert "ad:new" not in buttons(screen)
    assert "Connect an account first" in screen.text


# --------------------------------------------------------------------------- #
# Push notifications
# --------------------------------------------------------------------------- #
async def test_pausing_a_rule_queues_an_alert(client, session):
    from app.services import safety
    from tests.conftest import register
    from tests.integration.test_forwarding import build_rule

    actor = await register(client, "alerts@example.com")
    ctx = await build_rule(actor, destinations=1)

    await safety.pause_rule(session, rule_id=uuid.UUID(ctx["rule_id"]), reason_code="safety_pause")
    await session.commit()

    # Scoped by kind: a synced connection also queues its own alert now, and
    # this test is about the pause.
    rows = (
        (
            await session.execute(
                select(AdminNotification).where(AdminNotification.kind == "rule_paused")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert "paused" in rows[0].body.lower()
    assert rows[0].sent_at is None


async def test_repeated_pauses_collapse_into_one_alert(client, session):
    """A failing rule must not turn into a notification storm."""
    from tests.conftest import register
    from tests.integration.test_forwarding import build_rule

    actor = await register(client, "storm@example.com")
    ctx = await build_rule(actor, destinations=1)
    rule_id = uuid.UUID(ctx["rule_id"])

    for _ in range(5):
        await admin_repo.notify(
            session,
            user_id=uuid.UUID(actor.id),
            kind="rule_paused",
            title="Rule paused automatically",
            body="again",
            dedupe_key=f"rule_paused:{rule_id}:1:safety_pause",
            rule_id=rule_id,
        )
    await session.commit()

    rows = (
        (
            await session.execute(
                select(AdminNotification).where(AdminNotification.kind == "rule_paused")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_alert_for_an_account_with_no_telegram_id_is_retired(client, session):
    """A password-only account has nowhere to deliver; the outbox must not spin."""
    from app.adminbot import notifier
    from tests.conftest import register

    actor = await register(client, "noteleg@example.com")
    await admin_repo.notify(
        session,
        user_id=uuid.UUID(actor.id),
        kind="rule_paused",
        title="t",
        body="b",
        dedupe_key="k1",
    )
    await session.commit()

    sent = await notifier.drain_once(bot=None)  # type: ignore[arg-type]
    assert sent == 0

    session.expire_all()
    row = (await session.execute(select(AdminNotification))).scalar_one()
    assert row.sent_at is not None
