"""Startup diagnostics.

These messages are the first thing an operator reads when a deployment will not
come up. A wrong one sends them debugging the wrong thing, so the mapping from
driver exception to advice is pinned here.
"""

from __future__ import annotations

import socket

import pytest

from app.preflight import PreflightError, explain_database_failure

DSN = "postgresql+asyncpg://insight:pw@postgres:5432/insight"


class InvalidPasswordError(Exception):
    pass


class ConnectionRefusedErrorLike(Exception):
    def __init__(self) -> None:
        super().__init__("[Errno 111] Connect call failed")


def test_unresolvable_host_names_the_host_and_the_fix():
    """The reported failure: DATABASE_URL pointing at a name that does not exist
    inside the Docker network."""
    message = explain_database_failure(
        socket.gaierror(-2, "Name or service not known"),
        "postgresql+asyncpg://insight:pw@db:5432/insight",
    )
    assert "'db'" in message
    assert "'postgres'" in message, "must name the correct service"
    assert "localhost" in message, "must rule out the other common wrong value"
    assert "docker compose config" in message, "must give a command to verify with"


def test_unresolvable_wrong_host_explains_why_editing_env_does_nothing():
    message = explain_database_failure(
        socket.gaierror(-2, "Name or service not known"),
        "postgresql+asyncpg://insight:pw@db:5432/insight",
    )
    assert "Compose already sets DATABASE_URL" in message


def test_correct_host_that_does_not_resolve_blames_the_container_not_the_name():
    """The reported second failure. 'postgres' IS the right name, so repeating
    "use postgres" sends the operator to fix something that is not broken —
    Docker DNS simply does not answer for a container that is not running."""
    message = explain_database_failure(socket.gaierror(-2, "Name or service not known"), DSN)

    assert "correct name" in message
    assert "not up" in message
    assert "docker compose ps" in message
    assert "docker compose logs postgres" in message
    # Must not repeat the wrong-hostname advice.
    assert "not 'db', and not 'localhost'" not in message


def test_the_two_dns_failures_give_different_advice():
    """They look identical in the traceback and have opposite fixes, so the
    messages must not converge."""
    wrong_host = explain_database_failure(
        socket.gaierror(-2, "x"), "postgresql+asyncpg://u:p@db:5432/d"
    )
    right_host_down = explain_database_failure(socket.gaierror(-2, "x"), DSN)
    assert wrong_host != right_host_down


def test_redis_host_is_also_treated_as_a_compose_service():
    message = explain_database_failure(
        socket.gaierror(-2, "x"), "postgresql+asyncpg://u:p@redis:5432/d"
    )
    assert "correct name" in message


def test_wrong_password_points_at_the_volume_not_the_password():
    """The non-obvious part: POSTGRES_PASSWORD only applies when the data
    directory is first created."""
    message = explain_database_failure(
        InvalidPasswordError('password authentication failed for user "insight"'), DSN
    )
    assert "initialises a brand-new data directory" in message
    assert "DELETES ALL DATA" in message, "the destructive fix must be flagged"
    assert "backup" in message.lower()


def test_connection_refused_points_at_the_container_not_the_config():
    message = explain_database_failure(ConnectionRefusedErrorLike(), DSN)
    assert "Nothing is listening" in message
    assert "docker compose ps" in message


def test_missing_database_suggests_migrations():
    message = explain_database_failure(Exception('database "insight" does not exist'), DSN)
    assert "alembic upgrade head" in message


def test_an_unrecognised_error_still_names_the_host_and_the_type():
    message = explain_database_failure(RuntimeError("something novel"), DSN)
    assert "'postgres'" in message
    assert "RuntimeError" in message


def test_every_explanation_is_actionable():
    """No message may be a dead end — each names a host, a command, or a change
    to make."""
    cases = [
        socket.gaierror(-2, "Name or service not known"),
        InvalidPasswordError("password authentication failed"),
        ConnectionRefusedErrorLike(),
        Exception('database "insight" does not exist'),
        RuntimeError("novel"),
    ]
    for exc in cases:
        message = explain_database_failure(exc, DSN)
        assert len(message) > 40, f"{type(exc).__name__} explanation is too thin"
        assert "Traceback" not in message
        assert "asyncpg.connect_utils" not in message, "internals must not leak"


def test_a_malformed_dsn_does_not_crash_the_explainer():
    """The diagnostic must survive whatever nonsense is in the config — failing
    here would hide the real problem."""
    message = explain_database_failure(socket.gaierror(-2, "x"), "not-a-url")
    assert message


def test_preflight_error_is_its_own_type():
    with pytest.raises(PreflightError):
        raise PreflightError("boom")
