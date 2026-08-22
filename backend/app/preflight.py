"""Startup connectivity checks that fail with an answer, not a traceback.

A misconfigured ``DATABASE_URL`` used to surface as thirty lines of asyncpg
internals ending in ``socket.gaierror: [Errno -2] Name or service not known``,
which says nothing about what to change. Every process runs this first and turns
the common failures into one actionable line.

The three that actually happen in practice:

* wrong hostname — ``db`` or ``localhost`` instead of the compose service name;
* wrong password — ``POSTGRES_PASSWORD`` changed after the volume already
  existed, so Postgres still expects the original;
* nothing listening — the database container is not up yet.
"""

from __future__ import annotations

import socket
import sys
from urllib.parse import urlparse

import structlog

from app.config import get_settings

log = structlog.get_logger(__name__)


class PreflightError(Exception):
    """Carries a message meant for a human, not a stack trace."""


def _host_of(dsn: str) -> str | None:
    try:
        return urlparse(dsn).hostname
    except ValueError:  # pragma: no cover - malformed DSN
        return None


def explain_database_failure(exc: BaseException, dsn: str) -> str:
    """Turn a driver exception into something the operator can act on."""
    host = _host_of(dsn) or "?"
    name = type(exc).__name__

    if (
        isinstance(exc, socket.gaierror)
        or "gaierror" in name
        or "Name or service not known" in str(exc)
    ):
        return (
            f"Cannot resolve the database host {host!r}.\n\n"
            "Inside Docker the host must be the compose service name, which in "
            "this project is 'postgres' — not 'db', and not 'localhost'.\n\n"
            "Compose already sets DATABASE_URL correctly for every service, so "
            "you should not need to set it in .env at all. If .env has a "
            "DATABASE_URL line, it is only used when running the backend "
            "outside Docker; delete or comment it out to avoid confusion.\n\n"
            "Check with:  docker compose config | grep DATABASE_URL"
        )

    if "InvalidPassword" in name or "password authentication failed" in str(exc):
        return (
            f"Postgres rejected the password for host {host!r}.\n\n"
            "The usual cause: POSTGRES_PASSWORD was changed after the database "
            "volume already existed. Postgres only applies that variable when it "
            "initialises a brand-new data directory, so an existing volume keeps "
            "the original password.\n\n"
            "Either put the original password back in .env, or destroy the "
            "volume and start over — which DELETES ALL DATA:\n"
            "  docker compose down -v && docker compose up -d\n"
            "Take a backup first if the data matters."
        )

    if (
        "ConnectionRefused" in name
        or "Connect call failed" in str(exc)
        or "refused" in str(exc).lower()
    ):
        return (
            f"Nothing is listening on the database host {host!r}.\n\n"
            "The database container is probably not running yet. Check with:\n"
            "  docker compose ps\n"
            "  docker compose logs postgres"
        )

    if "does not exist" in str(exc):
        return (
            f"The database named in DATABASE_URL does not exist on {host!r}.\n\n"
            "Create the schema with:  docker compose exec api alembic upgrade head"
        )

    return f"Could not connect to the database at {host!r}: {name}"


async def check_database() -> None:
    from sqlalchemy import text

    from app.db.session import get_engine

    settings = get_settings()
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise PreflightError(explain_database_failure(exc, settings.database_url)) from exc


async def check_schema() -> None:
    """Confirm migrations have been applied.

    Reachable-but-empty is a distinct failure from unreachable, and it is what a
    first deployment hits when `alembic upgrade head` is forgotten. Without this
    the worker dies on ``relation "control_tasks" does not exist`` several frames
    deep in SQLAlchemy.
    """
    from sqlalchemy import text

    from app.db.session import get_engine

    try:
        async with get_engine().connect() as connection:
            applied = (
                await connection.execute(text("SELECT to_regclass('public.alembic_version')"))
            ).scalar()
    except Exception as exc:  # pragma: no cover - covered by check_database
        raise PreflightError(explain_database_failure(exc, get_settings().database_url)) from exc

    if applied is None:
        raise PreflightError(
            "The database is reachable but has no tables yet.\n\n"
            "Create the schema before starting the workers:\n"
            "  docker compose exec api alembic upgrade head"
        )


async def check_redis() -> None:
    from app.security.ratelimit import get_redis

    settings = get_settings()
    host = _host_of(settings.redis_url) or "?"
    try:
        await get_redis().ping()
    except Exception as exc:
        raise PreflightError(
            f"Cannot reach Redis at {host!r}: {type(exc).__name__}.\n\n"
            "Inside Docker the host must be the compose service name 'redis'. "
            "Compose sets REDIS_URL for you, so a REDIS_URL line in .env is only "
            "for running outside Docker."
        ) from exc


async def run(*, require_redis: bool = True) -> None:
    """Check everything, or exit with a readable explanation."""
    try:
        await check_database()
        await check_schema()
        if require_redis:
            await check_redis()
    except PreflightError as exc:
        # Deliberately print rather than log: this is the first thing an operator
        # sees in `docker compose logs`, and it should not be buried in JSON.
        print("\n" + "=" * 72, file=sys.stderr)
        print("STARTUP CHECK FAILED", file=sys.stderr)
        print("=" * 72, file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("=" * 72 + "\n", file=sys.stderr)
        raise SystemExit(1) from exc

    log.info("preflight_ok", database=True, redis=require_redis)
