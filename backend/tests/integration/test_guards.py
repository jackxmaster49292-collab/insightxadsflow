"""Structural guards.

These do not test a feature; they test that a promise cannot be broken by
future code. Each one fails loudly if someone adds a route, model, or import
that violates an invariant.
"""

from __future__ import annotations

import ast
import pathlib
import uuid

import pytest

from app.config import get_settings
from app.security.redaction import DENYLISTED_KEYS, KEY_ALLOWLIST
from tests.conftest import connect_bot, discovered, sync_with_chats

APP_DIR = pathlib.Path(__file__).resolve().parents[2] / "app"


def test_tests_never_target_a_live_telegram_provider():
    assert get_settings().telegram_provider == "mock"
    assert not get_settings().live_telegram


#: The only two packages allowed to know Telegram's client libraries exist.
#: `adapters` wraps Telegram for the forwarding engine; `adminbot` *is* a
#: Telegram client, so importing aiogram there is the point, not a leak.
TELEGRAM_AWARE_PACKAGES = {"adapters", "adminbot"}

#: The engine. If a Telegram type reaches any of these, the abstraction has
#: failed and MockAdapter stops being a faithful stand-in.
ENGINE_PACKAGES = {"domain", "services", "repositories", "db", "api"}


def _telegram_imports(path: pathlib.Path) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(ast.parse(path.read_text())):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        if any(n.split(".")[0] in {"telethon", "aiogram"} for n in names):
            lines.append(node.lineno)
    return lines


def test_telegram_libraries_stay_inside_the_two_telegram_aware_packages():
    offenders = [
        f"{path.relative_to(APP_DIR)}:{line}"
        for path in APP_DIR.rglob("*.py")
        if not TELEGRAM_AWARE_PACKAGES & set(path.parts)
        for line in _telegram_imports(path)
    ]
    assert not offenders, (
        "Telegram libraries may only be imported from "
        f"{sorted(TELEGRAM_AWARE_PACKAGES)}: {offenders}"
    )


def test_the_forwarding_engine_never_touches_a_telegram_library():
    """The stricter half of the rule, stated separately so widening the
    allowlist above can never quietly weaken it."""
    offenders = [
        f"{path.relative_to(APP_DIR)}:{line}"
        for package in ENGINE_PACKAGES
        for path in (APP_DIR / package).rglob("*.py")
        for line in _telegram_imports(path)
    ]
    assert not offenders, f"engine layer imports a Telegram library: {offenders}"


#: Reviewed exceptions to the secret-field sweep. The log denylist is
#: deliberately broad ("code" catches login codes), so a genuinely safe field
#: that trips it is listed here explicitly rather than by loosening the denylist.
REVIEWED_SAFE_FIELDS = {
    "ErrorBody.code",  # a machine-readable error code, e.g. "destination_not_eligible"
}


def test_no_response_model_exposes_a_secret_field():
    """Reflection over every Pydantic response model in the schema module."""
    from pydantic import BaseModel

    import app.schemas as schemas

    leaks: list[str] = []
    for name in dir(schemas):
        candidate = getattr(schemas, name)
        if not (isinstance(candidate, type) and issubclass(candidate, BaseModel)):
            continue
        # Request models legitimately accept secrets; responses never return them.
        if name.endswith("Request"):
            continue
        for field in candidate.model_fields:
            lowered = field.lower()
            if lowered in KEY_ALLOWLIST:
                continue
            if f"{name}.{field}" in REVIEWED_SAFE_FIELDS:
                continue
            if any(bad in lowered for bad in DENYLISTED_KEYS):
                leaks.append(f"{name}.{field}")
    assert not leaks, f"response models expose secret-shaped fields: {leaks}"


def test_no_quota_or_subscription_concepts_exist_in_the_codebase():
    """The product promises no artificial limits. This makes that greppable."""
    banned = ("subscription_plan", "billing", "quota_limit", "plan_tier", "free_tier", "vip_tier")
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text().lower()
        for term in banned:
            if term in text:
                offenders.append(f"{path.relative_to(APP_DIR)}: {term}")
    assert not offenders, f"monetization concepts found: {offenders}"


#: Things that would turn broadcasting into spam tooling. The product refuses
#: all of them by design (see the master prompt), so their absence is checked
#: structurally rather than left to review.
BANNED_CAPABILITIES = (
    "join_chat",
    "joinchannel",
    "importchatinvite",
    "add_contacts",
    "importcontacts",
    "getparticipants",
    "get_participants",
    "scrape",
    "harvest",
    "proxy_rotat",
    "rotate_account",
    "rotate_proxy",
    "captcha",
    "anti_detect",
    "antidetect",
    "spintax",
    "bulk_dm",
    "mass_dm",
    "cold_outreach",
)


def test_nothing_in_the_codebase_joins_chats_or_collects_members():
    """A broadcast posts to groups the account already belongs to.

    Joining a group, importing an invite, or reading a member list would each
    turn this into a different product — one that reaches people who never
    opted in. None of them exist, and this makes that greppable.
    """
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text().lower()
        for term in BANNED_CAPABILITIES:
            if term in text:
                offenders.append(f"{path.relative_to(APP_DIR)}: {term}")
    assert not offenders, f"spam-enabling capability found: {offenders}"


def test_auto_reply_has_no_way_to_address_someone_who_did_not_write_first():
    """The whole safety argument for auto-reply is that it cannot initiate.

    Its only entry point takes a single sender that already messaged us. A
    function taking a list of recipients would be unsolicited messaging, so the
    module's public surface is pinned.
    """
    import inspect

    from app.services import autoreply

    public = {
        name: obj
        for name, obj in vars(autoreply).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == autoreply.__name__
    }
    assert set(public) == {"handle_incoming"}, (
        f"auto-reply gained a new entry point: {sorted(public)}"
    )

    signature = inspect.signature(public["handle_incoming"])
    assert "sender" in signature.parameters
    for name, parameter in signature.parameters.items():
        annotation = str(parameter.annotation)
        assert "list" not in annotation.lower(), (
            f"{name} accepts a collection; auto-reply must answer one sender at a time"
        )


def test_a_broadcast_can_only_target_stored_chats():
    """Targets are foreign keys into synchronized membership, not raw peer ids.

    A ``peer_id`` column on broadcast_targets would let a broadcast address a
    chat the account was never confirmed to be in.
    """
    from app.db.models import BroadcastTarget

    columns = set(BroadcastTarget.__table__.columns.keys())
    assert "chat_id" in columns
    assert "peer_id" not in columns
    assert "username" not in columns

    chat_fk = next(iter(BroadcastTarget.__table__.c.chat_id.foreign_keys))
    assert chat_fk.target_fullname == "telegram_chats.id"


def test_the_word_campaign_is_not_used(client, actor):
    """Product terminology is binding: rules are never called campaigns."""
    offenders = [
        str(path.relative_to(APP_DIR))
        for path in APP_DIR.rglob("*.py")
        if "campaign" in path.read_text().lower()
    ]
    assert not offenders, f"'campaign' appears in: {offenders}"


async def test_openapi_publishes_no_secret_returning_endpoint(app):
    schema = app.openapi()
    text = str(schema).lower()
    # A response schema must never define these.
    for forbidden in ("session_string", "access_hash", "bot_token_ciphertext"):
        assert forbidden not in text, f"{forbidden} appears in the published API contract"


async def test_api_never_calls_telegram_inside_a_request_handler(client, actor):
    """Acceptance criterion: the HTTP API does not block on forwarding.

    Sync, health-check, disconnect and retry all return 202 and enqueue work; the
    adapter is only touched by the worker.
    """
    from app.adapters.factory import mock_script_for

    connection_id = await connect_bot(actor)
    script = mock_script_for(uuid.UUID(connection_id))
    script.calls.clear()

    accepted = []
    accepted.append(await actor.post(f"/telegram/connections/{connection_id}/sync"))
    accepted.append(await actor.post(f"/telegram/connections/{connection_id}/health-check"))

    for response in accepted:
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "accepted"

    # No Telegram I/O happened during those requests.
    assert script.calls_to("list_available_chats") == []
    assert script.calls_to("health_check") == []


async def test_idempotent_replay_returns_the_stored_response(client, actor):
    connection_id = await connect_bot(actor)
    await sync_with_chats(actor, connection_id, [discovered(-1, "S"), discovered(-2, "D")])
    chats = (await actor.get("/telegram/chats")).json()

    created = await actor.post(
        "/forwarding-rules",
        {
            "name": "R",
            "connection_id": connection_id,
            "source_chat_ids": [chats[0]["id"]],
            "destination_chat_ids": [chats[1]["id"]],
        },
    )
    rule_id = created.json()["id"]

    key = uuid.uuid4().hex
    first = await actor.post(f"/forwarding-rules/{rule_id}/activate", idem=key)
    second = await actor.post(f"/forwarding-rules/{rule_id}/activate", idem=key)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()


async def test_reusing_an_idempotency_key_with_a_different_body_is_a_conflict(client, actor):
    key = uuid.uuid4().hex
    first = await actor.post(
        "/telegram/connections/bot",
        {"label": "One", "bot_token": "123456789:AAEtestTokenValueThatIsLongEnough00"},
        idem=key,
    )
    assert first.status_code == 201

    second = await actor.post(
        "/telegram/connections/bot",
        {"label": "Different", "bot_token": "123456789:AAEtestTokenValueThatIsLongEnough00"},
        idem=key,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_key_reused"


async def test_control_commands_require_an_idempotency_key(client, actor):
    connection_id = await connect_bot(actor)
    response = await actor.post(
        f"/telegram/connections/{connection_id}/disconnect", {"revoke": False}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"


async def test_errors_carry_a_correlation_id_and_no_stack_trace(client):
    response = await client.get("/telegram/connections")
    body = response.json()
    assert body["error"]["correlation_id"]
    assert "Traceback" not in str(body)
    assert "sqlalchemy" not in str(body).lower()


async def test_security_headers_are_present(client):
    response = await client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert response.headers["x-correlation-id"]


async def test_validation_errors_do_not_echo_submitted_values(client):
    """A rejected bot token must not come back in the error body."""
    secret = "123456789:AAEsecretThatMustNotBeEchoedBack0000"
    response = await client.post(
        "/auth/register", json={"email": "not-an-email", "password": secret}
    )
    assert response.status_code == 422
    assert secret not in response.text


@pytest.mark.parametrize("period", ["24h", "7d", "30d"])
async def test_usage_summary_is_operational_counters_only(client, actor, period):
    body = (await actor.get(f"/usage/summary?period={period}")).json()
    assert set(body) == {"period", "forwarded", "skipped", "failed", "retry_scheduled", "paused"}
    # No quota, no remaining allowance, no plan.
    assert not any(k in body for k in ("limit", "quota", "remaining", "plan"))


def test_every_error_code_has_a_customer_facing_explanation():
    """`classify_error` produces the codes the panel renders. A code with no
    entry in REASON_TEXT silently falls back to the "unknown" sentence, which
    told the customer "Eligibility has not been checked yet." for things like
    slow mode — technically a string, but nonsense in context.
    """
    from app.adapters.errors import _BY_EXCEPTION_NAME, _BY_MESSAGE_TOKEN
    from app.domain import reasons

    produced = {code for _, code in _BY_EXCEPTION_NAME.values()}
    produced |= {code for _, _, code in _BY_MESSAGE_TOKEN}

    missing = sorted(code for code in produced if code not in reasons.REASON_TEXT)
    assert not missing, (
        "these error codes reach the UI with no explanation and would render the "
        f"generic 'unknown' text: {missing}"
    )


def test_no_explanation_is_left_as_a_placeholder():
    """Every sentence should actually say something."""
    from app.domain import reasons

    for code, text in reasons.REASON_TEXT.items():
        assert text.strip(), f"{code} has an empty explanation"
        assert text.strip().endswith("."), f"{code} explanation is not a sentence: {text!r}"
        # "Available." is legitimately short; anything shorter is a placeholder.
        assert len(text) >= 10, f"{code} explanation is too terse to help: {text!r}"
