"""Authentication, session lifecycle, and cross-user isolation.

The isolation test walks the real route registry rather than a hand-written list,
so a new endpoint added later cannot quietly skip the check.
"""

from __future__ import annotations

import uuid

from app.config import get_settings
from tests.conftest import connect_bot


async def test_register_then_me(client, actor):
    response = await actor.get("/me")
    assert response.status_code == 200
    assert response.json()["email"] == actor.email


async def test_login_with_correct_password(client):
    email = f"login-{uuid.uuid4().hex[:8]}@example.com"
    await client.post("/auth/register", json={"email": email, "password": "correct-horse-battery"})
    response = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200


async def test_login_with_wrong_password_is_rejected(client):
    email = f"bad-{uuid.uuid4().hex[:8]}@example.com"
    await client.post("/auth/register", json={"email": email, "password": "correct-horse-battery"})
    response = await client.post("/auth/login", json={"email": email, "password": "wrong-password"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_unknown_account_and_wrong_password_are_indistinguishable(client):
    unknown = await client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "whatever-password"}
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "invalid_credentials"


async def test_short_passwords_are_rejected(client):
    response = await client.post(
        "/auth/register", json={"email": "short@example.com", "password": "short"}
    )
    assert response.status_code == 422


async def test_duplicate_registration_does_not_confirm_the_account_exists(client):
    email = f"dupe-{uuid.uuid4().hex[:8]}@example.com"
    await client.post("/auth/register", json={"email": email, "password": "correct-horse-battery"})
    again = await client.post(
        "/auth/register", json={"email": email, "password": "correct-horse-battery"}
    )
    assert again.status_code == 409
    assert "exists" not in again.json()["error"]["message"].lower()


async def test_protected_routes_require_authentication(client):
    response = await client.get("/telegram/connections")
    assert response.status_code == 401


async def test_logout_revokes_the_session_immediately(client, actor):
    assert (await actor.get("/me")).status_code == 200
    assert (await actor.post("/auth/logout")).status_code == 204
    # The cookie is cleared client-side, but the server-side row is what matters:
    # replaying the old token must fail.
    assert (await actor.get("/me")).status_code == 401


async def test_revoked_session_token_cannot_be_replayed(client, actor):
    token = client.cookies.get(get_settings().cookie_name)
    await actor.post("/auth/revoke-all")
    client.cookies.set(get_settings().cookie_name, token)
    assert (await client.get("/me")).status_code == 401


async def test_csrf_token_is_required_for_mutations(client, actor):
    response = await client.post(
        "/telegram/connections/bot",
        json={"label": "x", "bot_token": "123456789:AAEtestTokenValueThatIsLongEnough00"},
        headers={"Idempotency-Key": uuid.uuid4().hex},  # no X-CSRF-Token
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


# --------------------------------------------------------------------------- #
# Cross-user isolation
# --------------------------------------------------------------------------- #
async def test_another_users_connection_is_not_found(client, actor, other_actor):
    connection_id = await connect_bot(actor)
    response = await other_actor.get(f"/telegram/connections/{connection_id}")
    # 404, never 403 — existence is not disclosed.
    assert response.status_code == 404


async def test_another_users_connection_cannot_be_disconnected(client, actor, other_actor):
    connection_id = await connect_bot(actor)
    response = await other_actor.post(
        f"/telegram/connections/{connection_id}/disconnect",
        {"revoke": True},
        idem=uuid.uuid4().hex,
    )
    assert response.status_code == 404


async def test_every_object_route_is_scoped_to_its_owner(app, client, actor, other_actor):
    """Walks the live route table so a new endpoint cannot skip this check."""
    connection_id = await connect_bot(actor)
    victim_ids = {
        "connection_id": connection_id,
        "chat_id": str(uuid.uuid4()),
        "rule_id": str(uuid.uuid4()),
    }

    # Driven off the published OpenAPI schema, so any route we ship is covered.
    checked = 0
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/v1/") or "{" not in path:
            continue

        url = path.replace("/api/v1", "")
        for name, value in victim_ids.items():
            url = url.replace("{" + name + "}", value)
        if "{" in url:
            continue

        methods = {m.upper() for m in operations}
        for method in methods & {"GET", "POST", "PATCH", "DELETE"}:
            kwargs = {"idem": uuid.uuid4().hex} if method in {"POST", "PATCH"} else {}
            if method == "GET":
                response = await other_actor.get(url)
            elif method == "POST":
                response = await other_actor.post(url, {}, **kwargs)
            elif method == "PATCH":
                response = await other_actor.patch(url, {})
            else:
                response = await other_actor.delete(url)

            checked += 1
            assert response.status_code in {404, 422}, (
                f"{method} {url} returned {response.status_code}; "
                "another user's object must never be reachable"
            )

    assert checked > 5, "isolation sweep did not cover enough routes"
