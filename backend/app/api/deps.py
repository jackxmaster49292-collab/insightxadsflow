"""Request dependencies: authentication, rate limiting, CSRF, idempotency."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.config import get_settings
from app.db.models import IdempotencyKey, User
from app.db.session import get_session
from app.security import auth
from app.security.ratelimit import check_rate_limit

SessionDep = Annotated[AsyncSession, Depends(get_session)]

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "X-CSRF-Token"
CSRF_COOKIE = "insight_csrf"


async def current_user(request: Request, session: SessionDep) -> User:
    token = request.cookies.get(get_settings().cookie_name)
    if not token:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "not_authenticated", "Sign in to continue.")

    user = await auth.resolve_session(session, token)
    if user is None:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "session_expired", "Your session has expired.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def enforce_csrf(request: Request) -> None:
    """Double-submit token, in addition to ``SameSite=Strict`` cookies."""
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or cookie != header:
        raise ApiError(
            status.HTTP_403_FORBIDDEN, "csrf_failed", "The request could not be verified."
        )


def rate_limit(bucket: str):  # type: ignore[no-untyped-def]
    async def dependency(request: Request) -> None:
        identity = request.client.host if request.client else "unknown"
        cookie = request.cookies.get(get_settings().cookie_name)
        if cookie:
            identity = auth.hash_token(cookie)[:32]
        result = await check_rate_limit(bucket, identity)
        if not result.allowed:
            raise ApiError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "rate_limited",
                "Too many requests. Please wait before trying again.",
                {"retry_after_s": result.retry_after_s},
            )

    return Depends(dependency)


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def hash_body(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


async def idempotent(
    request: Request,
    session: AsyncSession,
    user: User,
    *,
    payload: Any = None,
    required: bool = False,
) -> tuple[str | None, dict[str, Any] | None]:
    """Returns ``(key, stored_response)``.

    A replay with a matching body returns the stored response; the same key with
    a different body is a conflict. This is what makes activate/pause/resume/
    retry safe to retry from a flaky network.
    """
    key = request.headers.get("Idempotency-Key")
    if not key:
        if required:
            raise ApiError(
                status.HTTP_400_BAD_REQUEST,
                "idempotency_key_required",
                "This operation requires an Idempotency-Key header.",
            )
        return None, None

    endpoint = f"{request.method} {request.url.path}"
    digest = hash_body({"endpoint": endpoint, "payload": payload})

    existing = await session.execute(
        select(IdempotencyKey).where(IdempotencyKey.user_id == user.id, IdempotencyKey.key == key)
    )
    row = existing.scalar_one_or_none()

    if row is not None:
        if row.request_hash != digest:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "idempotency_key_reused",
                "This Idempotency-Key was already used with a different request.",
            )
        if row.state == "completed":
            return key, row.response_body or {}
        return key, None

    session.add(
        IdempotencyKey(
            user_id=user.id,
            key=key,
            endpoint=endpoint[:160],
            request_hash=digest,
            state="in_progress",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )
    await session.flush()
    return key, None


async def store_idempotent_result(
    session: AsyncSession, *, user_id: uuid.UUID, key: str | None, status_code: int, body: Any
) -> None:
    if key is None:
        return
    result = await session.execute(
        select(IdempotencyKey).where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        row.state = "completed"
        row.response_status = status_code
        row.response_body = json.loads(json.dumps(body, default=str))


def set_session_cookies(response: Response, *, token: str, csrf: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.cookie_name,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_idle_ttl_s,
        path="/",
    )
    # Readable by the SPA on purpose: it is the double-submit half of CSRF.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_idle_ttl_s,
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(settings.cookie_name, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
