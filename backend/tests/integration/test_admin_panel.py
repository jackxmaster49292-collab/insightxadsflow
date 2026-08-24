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
from app.db.models import AdminNotification, User
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


async def register(telegram_id: int) -> None:
    """Give this Telegram id an account. There is no gate left to pass."""
    from app.db.session import session_scope

    async with session_scope() as session:
        await admin_repo.upsert_user(session, telegram_user_id=telegram_id, username="operator")


async def run_middleware(event: object, telegram_id: int, *, accepted: bool = True) -> dict:
    """Push one update through the guard and report what the handler received."""
    if accepted:
        await register(telegram_id)

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

    for feature in ("*Ads*", "Auto\\-reply"):
        assert feature in screen.text
    assert "Forwarding" not in screen.text, "removed from the panel"
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


def test_ads_list_paginates():
    """Out-of-range pages clamp rather than crash — the pager is the one place
    an index arrives from a button someone pressed twice."""
    from tests.conftest import fake_broadcast

    ads = [fake_broadcast(name=f"Ad {i}") for i in range(15)]
    first = views.ads_list(broadcasts=ads, page=0, can_create=True)
    assert "1/3" in " ".join(b.text for row in first.keyboard.inline_keyboard for b in row)

    last = views.ads_list(broadcasts=ads, page=99, can_create=True)
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


def test_the_connection_screen_reports_what_that_account_did():
    """The Accounts screen said what an account *is*; this says what it has
    done, which is the question anyone opens it with."""
    from types import SimpleNamespace

    connection = SimpleNamespace(
        id=uuid.uuid4(),
        label="Jack",
        kind=SimpleNamespace(value="user"),
        status=SimpleNamespace(value="active"),
        telegram_username="Insightxstorex",
        last_error_message_safe=None,
    )
    screen = views.connection_detail(
        connection=connection,
        chat_count=720,
        counts={"forwarded": 147, "skipped": 8, "failed": 1, "retry_scheduled": 2},
        ads=10,
    )

    assert "Groups known* — 720" in screen.text
    assert "Ads from this account* — 10" in screen.text
    assert "Delivered* — 147" in screen.text
    assert "Did not arrive* — 9", "skipped and failed together"
    assert "Retrying* — 2" in screen.text


def test_a_fresh_connection_shows_no_empty_counters():
    """Four zeroes on a brand-new account is noise, not information."""
    from types import SimpleNamespace

    connection = SimpleNamespace(
        id=uuid.uuid4(),
        label="Jack",
        kind=SimpleNamespace(value="user"),
        status=SimpleNamespace(value="active"),
        telegram_username=None,
        last_error_message_safe=None,
    )
    screen = views.connection_detail(connection=connection, chat_count=0, counts={}, ads=0)
    assert "Ads from this account" not in screen.text


def test_the_accounts_screen_no_longer_offers_a_bot():
    from types import SimpleNamespace

    connection = SimpleNamespace(
        id=uuid.uuid4(),
        label="Jack",
        kind=SimpleNamespace(value="user"),
        status=SimpleNamespace(value="active"),
        telegram_username=None,
        last_error_message_safe=None,
    )
    screen = views.connections_list(connections=[connection])
    callbacks = [b.callback_data for row in screen.keyboard.inline_keyboard for b in row]
    assert "add:user" in callbacks
    assert "add:bot" not in callbacks


# --------------------------------------------------------------------------- #
# An operator looking at one account's groups
# --------------------------------------------------------------------------- #
async def test_an_operator_can_see_which_groups_an_account_is_in(client, actor, session):
    """Metadata only: titles and whether each can be posted in. The same thing
    that account's own Groups screen shows it."""
    from tests.conftest import connect_bot, discovered, sync_with_chats
    from tests.integration.test_bot_flows import (
        assert_keyboard_is_sendable,
        assert_valid_markdown_v2,
    )

    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor,
        connection_id,
        [
            discovered(-1002001, "A supergroup", chat_kind="supergroup"),
            discovered(-1001001, "A channel", chat_kind="channel"),
        ],
    )

    from app.repositories import chats as chat_repo
    from app.repositories import users as user_repo

    target = await user_repo.get_by_id(session, uuid.UUID(actor.id))
    chats = await chat_repo.list_filtered(session, user_id=target.id, limit=1000)
    screen = views.user_groups(user=target, chats=chats, page=0)

    assert "Groups* — 1" in screen.text
    assert "Channels* — 1" in screen.text
    assert "A supergroup" in screen.text
    assert "📢" in screen.text, "a channel is marked as one"
    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)


async def test_private_chats_are_not_listed_among_the_groups(client, actor, session):
    """They are synchronized, but listing them buried the chats that matter
    under hundreds titled with somebody's name, or "." ."""
    from tests.conftest import connect_bot, discovered, sync_with_chats

    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor,
        connection_id,
        [
            discovered(-1002001, "A supergroup", chat_kind="supergroup"),
            discovered(700100, ".", chat_kind="private"),
            discovered(700101, "-", chat_kind="private"),
        ],
    )

    from app.repositories import chats as chat_repo
    from app.repositories import users as user_repo

    target = await user_repo.get_by_id(session, uuid.UUID(actor.id))
    chats = await chat_repo.list_filtered(session, user_id=target.id, limit=1000)
    screen = views.user_groups(user=target, chats=chats, page=0)

    assert "A supergroup" in screen.text
    assert "Groups* — 1" in screen.text
    body = screen.text.split("Can post in")[1]
    assert "\n💭 *\\.*" not in body and "*\\-*" not in body, "no private chats in the list"


async def test_each_chat_carries_a_link_where_telegram_offers_one(client, actor, session):
    from types import SimpleNamespace

    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    def chat(title, kind, peer, username=None):
        return SimpleNamespace(
            id=uuid.uuid4(),
            title=title,
            chat_kind=SimpleNamespace(value=kind),
            peer_id=peer,
            username=username,
            access=SimpleNamespace(can_post_destination=True),
        )

    user = SimpleNamespace(id=uuid.uuid4(), telegram_username="me", telegram_user_id=1)
    screen = views.user_groups(
        user=user,
        chats=[
            chat("Public channel", "channel", -1001111111111, username="insightxstore"),
            chat("Private group", "supergroup", -1002001234567),
            chat("Basic group", "group", -412345678),
        ],
        page=0,
    )

    # Escaped for MarkdownV2 — the dots in a URL must carry a backslash or
    # Telegram rejects the whole message; it still renders as a link.
    plain = screen.text.replace("\\", "")
    assert "t.me/insightxstore" in plain
    assert "t.me/c/2001234567" in plain
    assert "members only" in plain
    assert "no link" in plain, "and the case with none says so"
    assert_valid_markdown_v2(screen.text)


async def test_the_channels_only_view_shows_just_channels(client, actor, session):
    from types import SimpleNamespace

    def chat(title, kind, peer):
        return SimpleNamespace(
            id=uuid.uuid4(),
            title=title,
            chat_kind=SimpleNamespace(value=kind),
            peer_id=peer,
            username=None,
            access=SimpleNamespace(can_post_destination=True),
        )

    user = SimpleNamespace(id=uuid.uuid4(), telegram_username="me", telegram_user_id=1)
    chats = [chat("A group", "supergroup", -1002001), chat("A channel", "channel", -1001001)]

    channels = views.user_groups(user=user, chats=chats, page=0, kind="channels")
    assert "A channel" in channels.text
    assert "A group" not in channels.text.split("Can post in")[1]

    groups = views.user_groups(user=user, chats=chats, page=0, kind="groups")
    assert "A group" in groups.text
    assert "A channel" not in groups.text.split("Can post in")[1]


async def test_a_non_operator_cannot_see_anyone_s_groups(client, actor, state, session):
    from app.adminbot import handlers
    from app.repositories import users as user_repo
    from tests.integration.test_bot_flows import Sent, a_callback

    target = await user_repo.get_by_id(session, uuid.UUID(actor.id))
    await handlers.user_actions(
        a_callback(f"usr:{target.id}:groups:0"),
        user_id=uuid.UUID(actor.id),
        is_operator=False,
    )
    assert any("not available" in alert for alert in Sent.alerts)


def test_the_groups_screen_says_so_when_nothing_is_synced():
    from types import SimpleNamespace

    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    user = SimpleNamespace(id=uuid.uuid4(), telegram_username="someone", telegram_user_id=1)
    screen = views.user_groups(user=user, chats=[], page=0)
    assert "Nothing synced" in screen.text
    assert_valid_markdown_v2(screen.text)


async def test_a_members_only_chat_carries_its_bio_and_size(client, actor, session):
    """A t.me/c link shows no preview at all, so without this a private group
    is a title and a number with nothing to recognise it by. A public one is
    skipped — Telegram unfurls that link itself."""
    from types import SimpleNamespace

    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    def chat(title, kind, peer, username=None, description=None, members=None):
        return SimpleNamespace(
            id=uuid.uuid4(),
            title=title,
            chat_kind=SimpleNamespace(value=kind),
            peer_id=peer,
            username=username,
            description=description,
            member_count=members,
            access=SimpleNamespace(can_post_destination=True),
        )

    user = SimpleNamespace(id=uuid.uuid4(), telegram_username="me", telegram_user_id=1)
    screen = views.user_groups(
        user=user,
        chats=[
            chat(
                "Bugs",
                "supergroup",
                -1002292984243,
                description="Deals and offers, posted daily",
                members=12_400,
            ),
            chat(
                "Big Budget Market",
                "supergroup",
                -1002001,
                username="bigbudgetmarket",
                description="Big Budget Clients & Agencies Are Welcome",
                members=9_000,
            ),
        ],
        page=0,
    )

    assert "Deals and offers, posted daily" in screen.text
    assert "12,400 members" in screen.text
    assert "Big Budget Clients" not in screen.text, "a public link previews itself"
    assert_valid_markdown_v2(screen.text)


def _bio_chat(description, title="Chatty", peer=-1002001):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=uuid.uuid4(),
        title=title,
        chat_kind=SimpleNamespace(value="supergroup"),
        peer_id=peer,
        username=None,
        description=description,
        member_count=5,
        access=SimpleNamespace(can_post_destination=True),
    )


def test_a_description_at_telegrams_own_limit_arrives_whole():
    """255 characters is the most Telegram lets a chat description be, so every
    real one must survive intact. A members-only chat has no link preview: the
    description is the only thing left to recognise it by, and half of one is
    exactly the half that does not."""
    from types import SimpleNamespace

    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    bio = ("Deals, escrow and vouches. Owner @someone, backup @another. " * 5)[:255]
    user = SimpleNamespace(id=uuid.uuid4(), telegram_username="me", telegram_user_id=1)
    screen = views.user_groups(user=user, chats=[_bio_chat(bio)], page=0)

    assert views.escape(bio) in screen.text, "the whole description, not a prefix"
    assert "…" not in screen.text
    assert_valid_markdown_v2(screen.text)


def test_six_long_descriptions_still_fit_in_one_message():
    """Overrunning 4096 is not a long message — Telegram rejects it with a 400
    and the operator gets a blank screen. Escaping is what does it: a
    description of nothing but full stops doubles before Telegram counts it."""
    from types import SimpleNamespace

    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    worst = "." * 255  # every character escapes to two
    chats = [
        _bio_chat(worst, title=f"Chatty {i}", peer=-1002000 - i) for i in range(views.PAGE_SIZE)
    ]
    user = SimpleNamespace(id=uuid.uuid4(), telegram_username="me", telegram_user_id=1)
    screen = views.user_groups(user=user, chats=chats, page=0)

    assert len(screen.text) <= 4096, f"{len(screen.text)} characters would be a 400"
    assert "…" in screen.text, "and it says where it ran out"
    assert_valid_markdown_v2(screen.text)


def test_the_page_helper_matches_what_the_screen_renders():
    """The handler fetches details for exactly the chats about to be shown; a
    second implementation of "which six" would drift and fetch the wrong ones."""
    from types import SimpleNamespace

    def chat(i, kind):
        return SimpleNamespace(
            id=uuid.uuid4(),
            title=f"{kind} {i:02d}",
            chat_kind=SimpleNamespace(value=kind),
            peer_id=-1002000 - i,
            username=None,
            description=None,
            member_count=None,
            access=SimpleNamespace(can_post_destination=True),
        )

    chats = [chat(i, "supergroup") for i in range(10)] + [chat(i, "channel") for i in range(4)]
    user = SimpleNamespace(id=uuid.uuid4(), telegram_username="me", telegram_user_id=1)

    for kind in ("all", "groups", "channels"):
        for page in (0, 1):
            visible = views.page_of_chats(chats=chats, page=page, kind=kind)
            rendered = views.user_groups(user=user, chats=chats, page=page, kind=kind).text
            for c in visible:
                assert c.title in rendered, f"{kind} page {page} missing {c.title}"
