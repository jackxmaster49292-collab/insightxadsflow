"""Startup connectivity checks that fail with an answer, not a traceback.

A misconfigured ``DATABASE_URL`` used to surface as thirty lines of asyncpg
internals ending in ``socket.gaierror: [Errno -2] Name or service not known``,
which says nothing about what to change. Every process runs this first and turns
the common failures into one actionable line.

The ones that actually happen in practice:

* wrong hostname — ``db`` or ``localhost`` instead of the compose service name;
* right hostname, container down — Docker DNS only answers for running
  containers, so this looks identical to a typo but has a different fix;
* wrong password — ``POSTGRES_PASSWORD`` changed after the volume already
  existed, so Postgres still expects the original;
* nothing listening — the database container is not up yet;
* no tables — ``alembic upgrade head`` has not been run.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from urllib.parse import urlparse

import structlog

from app.config import get_settings

log = structlog.get_logger(__name__)

#: Hostnames Compose is expected to provide. If one of these fails to resolve
#: the name is right and the container is wrong — a completely different fix
#: from "you typed the wrong host", so the two must not share a message.
COMPOSE_SERVICE_HOSTS = frozenset({"postgres", "redis"})


class PreflightError(Exception):
    """Carries a message meant for a human, not a stack trace.

    ``transient`` marks failures that can heal on their own — a container that
    is still starting, or Docker DNS that has not caught up with a freshly
    created network. Those are worth retrying. A rejected password or a missing
    database never fixes itself, so retrying only delays the real message.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


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
        if host in COMPOSE_SERVICE_HOSTS:
            # The name is right, so this is not a typo in the configuration.
            # Docker's DNS only answers for containers that are running and on
            # the same network — a completely different fix, so it must not
            # share a message with the wrong-hostname case.
            return (
                f"The database host {host!r} is the correct name, but it does "
                "not resolve.\n\n"
                "Docker's DNS only answers for containers that are running and "
                f"attached to the same network. So {host!r} failing to resolve "
                "means that container is not up — not that the name is wrong.\n\n"
                "Check, in order:\n"
                f"  docker compose ps                  # is {host} running?\n"
                f"  docker compose logs {host}         # why did it stop?\n"
                "  docker compose config --services   # is it even defined?\n\n"
                "The usual cause is starting the stack with only one compose "
                "file. Always use both:\n"
                "  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d\n\n"
                "The prod file alone does not define postgres or redis at all.\n"
                "Or just run:  ./deploy.sh"
            )

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
            "Create the schema with:  docker compose run --rm api alembic upgrade head"
        )

    return f"Could not connect to the database at {host!r}: {name}"


def _is_transient(exc: BaseException) -> bool:
    """Can this failure resolve itself if we simply wait?

    A container that has not finished starting, or a name that Docker's DNS has
    not published yet, will. Bad credentials will not.
    """
    name = type(exc).__name__
    text = str(exc)
    if "InvalidPassword" in name or "password authentication failed" in text:
        return False
    if "does not exist" in text:
        return False
    return (
        isinstance(exc, socket.gaierror)
        or "gaierror" in name
        or "Name or service not known" in text
        or "ConnectionRefused" in name
        or "Connect call failed" in text
        or "refused" in text.lower()
    )


async def check_database() -> None:
    from sqlalchemy import text

    from app.db.session import get_engine

    settings = get_settings()
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise PreflightError(
            explain_database_failure(exc, settings.database_url),
            transient=_is_transient(exc),
        ) from exc


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
            "  docker compose run --rm api alembic upgrade head"
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
            "for running outside Docker.",
            transient=_is_transient(exc),
        ) from exc


#: How long to keep retrying a failure that can heal. Docker publishes a name on
#: a freshly created network within a second or two on a fast machine, but a
#: loaded VPS can take noticeably longer — and checking once, immediately after
#: creating the network, is a race we lost in production.
STARTUP_GRACE_S = 60
RETRY_INTERVAL_S = 3


async def run(*, require_redis: bool = True, require_schema: bool = True) -> None:
    """Check everything, or exit with a readable explanation.

    Transient failures are retried for up to :data:`STARTUP_GRACE_S`; a rejected
    password or a missing schema fails immediately, because waiting cannot help
    and the operator should see the real reason at once.
    """
    deadline = time.monotonic() + STARTUP_GRACE_S
    attempt = 0

    while True:
        attempt += 1
        try:
            await check_database()
            if require_schema:
                await check_schema()
            if require_redis:
                await check_redis()
            break
        except PreflightError as exc:
            if exc.transient and time.monotonic() < deadline:
                log.info(
                    "preflight_retry",
                    attempt=attempt,
                    seconds_left=int(deadline - time.monotonic()),
                    detail=str(exc).splitlines()[0],
                )
                await asyncio.sleep(RETRY_INTERVAL_S)
                continue

            # Deliberately print rather than log: this is the first thing an
            # operator sees in `docker compose logs`, and it should not be
            # buried in JSON.
            print("\n" + "=" * 72, file=sys.stderr)
            print("STARTUP CHECK FAILED", file=sys.stderr)
            print("=" * 72, file=sys.stderr)
            print(str(exc), file=sys.stderr)
            if exc.transient:
                print(
                    f"\nRetried for {STARTUP_GRACE_S}s before giving up, so this "
                    "is not a slow start.",
                    file=sys.stderr,
                )
            print("=" * 72 + "\n", file=sys.stderr)
            raise SystemExit(1) from exc

    log.info("preflight_ok", database=True, redis=require_redis, attempts=attempt)


def main() -> None:
    """``python -m app.preflight [--no-schema]``

    Run by deploy.sh before migrations, so a bad database configuration is
    reported in plain language instead of surfacing as an alembic traceback.
    ``--no-schema`` skips the tables check, which has not been created yet at
    that point in a first deployment.
    """
    import asyncio
    import sys

    from app.logging_setup import configure_logging

    configure_logging(json_output=False)
    require_schema = "--no-schema" not in sys.argv

    async def go() -> None:
        from app.db.session import dispose_engine
        from app.security.ratelimit import close_redis

        try:
            await run(require_schema=require_schema)
        finally:
            await close_redis()
            await dispose_engine()

    asyncio.run(go())


if __name__ == "__main__":
    main()
