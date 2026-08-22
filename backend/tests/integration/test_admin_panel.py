"""Telegram control panel: login, allowlist, screens, and push alerts.

The allowlist is the whole security model for this surface — anyone on Telegram
can message the bot — so it gets adversarial coverage.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import select

from app.adminbot import views
from app.config import get_settings
from app.db.models import AdminNotification, RuleStatus, User
from app.repositories import admins as admin_repo
from app.security.miniapp import build_init_data

BOT_TOKEN = "555000111:AAEadminBotTokenForTestsLongEnough00"
ADMIN_ID = 900_100_200
STRANGER_ID = 111_222_333


@pytest.fixture(autouse=True)
def admin_settings(monkeypatch):
    """Point the app at a known admin bot token and allowlist."""
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_bot_token", BOT_TOKEN, raising=False)
    monkeypatch.setattr(settings, "admin_telegram_ids", str(ADMIN_ID), raising=False)
    monkeypatch.setattr(settings, "miniapp_url", "https://panel.example.com", raising=False)
    return settings


def launch(telegram_id: int = ADMIN_ID, *, token: str = BOT_TOKEN) -> str:
    return build_init_data(
        {
            "auth_date": str(int(time.time())),
            "query_id": "AAHtest",
            "user": json.dumps({"id": telegram_id, "username": "operator"}),
        },
        bot_token=token,
    )


# --------------------------------------------------------------------------- #
# Mini App login
# --------------------------------------------------------------------------- #
async def test_admin_can_sign_in_from_the_mini_app(client):
    response = await client.post("/auth/telegram", json={"init_data": launch()})
    assert response.status_code == 200, response.text
    assert (await client.get("/me")).status_code == 200


async def test_login_creates_a_passwordless_account(client, session):
    await client.post("/auth/telegram", json={"init_data": launch()})
    user = (
        await session.execute(select(User).where(User.telegram_user_id == ADMIN_ID))
    ).scalar_one()
    assert user.password_hash is None
    assert user.telegram_username == "operator"


async def test_signing_in_twice_reuses_the_same_account(client, session):
    await client.post("/auth/telegram", json={"init_data": launch()})
    await client.post("/auth/logout")
    await client.post("/auth/telegram", json={"init_data": launch()})

    rows = (
        (await session.execute(select(User).where(User.telegram_user_id == ADMIN_ID)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_verified_but_not_allowlisted_is_refused(client):
    """Telegram proved who they are — that is still not authorization."""
    response = await client.post("/auth/telegram", json={"init_data": launch(STRANGER_ID)})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_an_admin"


async def test_payload_signed_by_another_bot_is_refused(client):
    forged = launch(ADMIN_ID, token="999888777:AAEsomeoneElsesBotTokenLongEnough00")
    response = await client.post("/auth/telegram", json={"init_data": forged})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_init_data"


async def test_rejection_reason_is_not_disclosed(client):
    """A precise error would help an attacker tune a forgery."""
    response = await client.post("/auth/telegram", json={"init_data": "hash=deadbeef"})
    body = response.json()["error"]
    assert response.status_code == 401
    for leak in ("signature", "hmac", "auth_date", "hash"):
        assert leak not in body["message"].lower()


async def test_empty_allowlist_locks_everyone_out(client, monkeypatch):
    """A misconfigured deploy must fail closed, not open."""
    monkeypatch.setattr(get_settings(), "admin_telegram_ids", "", raising=False)
    response = await client.post("/auth/telegram", json={"init_data": launch()})
    assert response.status_code == 403


async def test_denied_attempt_is_audited(client, session):
    await client.post("/auth/telegram", json={"init_data": launch(STRANGER_ID)})
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


async def test_synthetic_telegram_address_is_not_a_usable_login(client):
    """Belt and braces: the generated address is non-routable, so the login form
    rejects it before the password is even considered."""
    await client.post("/auth/telegram", json={"init_data": launch()})
    response = await client.post(
        "/auth/login",
        json={"email": f"tg-{ADMIN_ID}@telegram.local", "password": "anything-at-all"},
    )
    assert response.status_code in {401, 422}


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
def test_home_screen_without_a_connection_guides_the_operator():
    screen = views.home(
        connections=[], rules=[], counts={}, miniapp_url="https://panel.example.com"
    )
    assert "No Telegram connection yet" in screen.text
    assert any(
        button.web_app is not None for row in screen.keyboard.inline_keyboard for button in row
    )


def test_mini_app_button_is_hidden_when_not_configured_over_https():
    """A button that fails when tapped is worse than no button."""
    screen = views.home(connections=[], rules=[], counts={}, miniapp_url="http://insecure")
    assert not any(
        button.web_app is not None for row in screen.keyboard.inline_keyboard for button in row
    )


def test_markdown_special_characters_in_titles_are_escaped():
    """Chat titles are attacker-influenced; an unescaped one makes Telegram
    reject the whole message with a 400."""
    escaped = views._esc("*bold* [link](x) _under_ `code` ~s~")
    for char in "*[]()_`~":
        assert f"\\{char}" in escaped


def test_callback_data_stays_within_telegram_s_64_byte_limit():
    rule_id = uuid.uuid4()
    for data in (
        f"rule:{rule_id}",
        f"rule:{rule_id}:pause",
        f"rule:{rule_id}:resume",
        f"rule:{rule_id}:retry",
        f"rule:{rule_id}:events",
        f"conn:{rule_id}:sync",
        "nav:rules:99",
        "nav:chats:99",
    ):
        assert len(data.encode()) <= 64, f"{data} is {len(data.encode())} bytes"


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
    first = views.rules_list(rules=rules, page=0, miniapp_url="https://p.example.com")
    assert "Page 1 of 3" in first.text

    last = views.rules_list(rules=rules, page=99, miniapp_url="https://p.example.com")
    assert "Page 3 of 3" in last.text, "out-of-range pages must clamp, not crash"


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

    rows = (await session.execute(select(AdminNotification))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "rule_paused"
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

    rows = (await session.execute(select(AdminNotification))).scalars().all()
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
