"""Building the database DSN from parts.

A password is data, not URL syntax. Interpolating one straight into a DSN is how
a production deployment spent an evening chasing a DNS error: the password
contained "@", and the URL spec splits userinfo from host at the *last* "@"
while libpq and asyncpg split at the *first*. The two parsers disagreed, so the
diagnostic reported the host it read ("postgres") while the driver tried to
resolve what it read ("2026@postgres") and failed.

Every component is percent-encoded now, so the parsers cannot disagree.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from app.config import Settings


def build(**overrides: str) -> Settings:
    base = {
        "postgres_user": "insight",
        "postgres_password": "insight",
        "postgres_host": "postgres",
        "postgres_db": "insight",
        "database_dsn_override": "",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def first_at_host(dsn: str) -> str:
    """How libpq and asyncpg read the host: split at the first '@'."""
    return dsn.split("://", 1)[1].split("@", 1)[1].split("/")[0]


@pytest.mark.parametrize(
    "password",
    [
        "InsightFlowSecretPass@2026",  # the one that actually broke production
        "p@ss@word",
        "with/slash",
        "with:colon",
        "with#hash",
        "with?question",
        "with%percent",
        "with spaces",
        "plain",
    ],
)
def test_both_parsers_agree_on_the_host_whatever_the_password(password):
    dsn = build(postgres_password=password).database_url

    assert urlparse(dsn).hostname == "postgres"
    assert first_at_host(dsn) == "postgres:5432", (
        f"password {password!r} truncated the host for the driver"
    )


def test_the_exact_password_that_broke_production():
    dsn = build(postgres_password="InsightFlowSecretPass@2026").database_url

    assert "%40" in dsn, "the @ must be percent-encoded"
    # Both parsers must land on the same host. Checking for the substring
    # "2026@postgres" would be wrong: it still appears after the encoded "@",
    # and it is the parse result that matters, not the raw text.
    assert urlparse(dsn).hostname == "postgres"
    assert first_at_host(dsn) == "postgres:5432"


def test_a_percent_in_the_password_survives_encoding():
    """It must not be double-encoded into something else."""
    from urllib.parse import unquote

    dsn = build(postgres_password="50%off").database_url
    userinfo = dsn.split("://", 1)[1].rsplit("@", 1)[0]
    assert unquote(userinfo.split(":", 1)[1]) == "50%off"


def test_components_land_in_the_right_places():
    dsn = build(
        postgres_user="someone",
        postgres_password="secret",
        postgres_host="db-host",
        postgres_db="mydb",
    ).database_url
    parsed = urlparse(dsn)

    assert parsed.username == "someone"
    assert parsed.hostname == "db-host"
    assert parsed.port == 5432
    assert parsed.path == "/mydb"
    assert dsn.startswith("postgresql+asyncpg://")


def test_an_explicit_dsn_is_used_verbatim():
    """The escape hatch for running outside Docker stays untouched."""
    explicit = "postgresql+asyncpg://u:p@localhost:5433/other"
    assert build(database_dsn_override=explicit).database_url == explicit


def test_database_url_env_var_still_works(monkeypatch):
    """DATABASE_URL is the name people know; it must keep overriding."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a:b@somewhere:5432/x")
    assert Settings().database_url == "postgresql+asyncpg://a:b@somewhere:5432/x"


def test_an_empty_database_url_falls_back_to_components(monkeypatch):
    """Compose sets DATABASE_URL="" to neutralise a stale value in .env; that
    must mean "build it from parts", not "connect to nothing"."""
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw@1")
    assert urlparse(Settings().database_url).hostname == "postgres"


def test_the_dsn_is_never_logged_in_the_clear():
    """The DSN embeds the password, so it must not survive redaction."""
    from app.security.redaction import redact

    dsn = build(postgres_password="SuperSecret@2026").database_url
    assert redact({"database_url": dsn})["database_url"] == "[REDACTED]"
