"""Password hashing and server-side sessions.

Sessions are database rows keyed by a SHA-256 hash of the cookie value, so
logout and "revoke all sessions" take effect immediately rather than waiting for
a token to expire. The raw cookie value is never stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AppSession, User

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:  # pragma: no cover - defensive
        return True


def hash_token(raw: str) -> str:
    """SHA-256 is correct here: the token is already 256 bits of entropy, so a
    slow KDF adds cost without adding security."""
    return hashlib.sha256(raw.encode()).hexdigest()


def hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    salt = get_settings().app_secret_key.encode()
    return hmac.new(salt, ip.encode(), hashlib.sha256).hexdigest()


def now() -> datetime:
    return datetime.now(UTC)


async def create_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, AppSession]:
    """Returns ``(raw_token, row)``. The raw token is handed to the cookie once."""
    settings = get_settings()
    raw_token = secrets.token_urlsafe(32)
    issued = now()
    row = AppSession(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        issued_at=issued,
        expires_at=issued + timedelta(seconds=settings.session_idle_ttl_s),
        absolute_expires_at=issued + timedelta(seconds=settings.session_absolute_ttl_s),
        last_seen_at=issued,
        ip_hash=hash_ip(ip),
        user_agent=(user_agent or "")[:512] or None,
    )
    session.add(row)
    await session.flush()
    return raw_token, row


async def resolve_session(session: AsyncSession, raw_token: str) -> User | None:
    """Validate a cookie and slide its idle expiry. Returns ``None`` if invalid."""
    settings = get_settings()
    current = now()

    stmt = (
        select(AppSession, User)
        .join(User, User.id == AppSession.user_id)
        .where(AppSession.token_hash == hash_token(raw_token))
    )
    found = (await session.execute(stmt)).first()
    if found is None:
        return None

    row, user = found
    if row.revoked_at is not None:
        return None
    if row.expires_at <= current or row.absolute_expires_at <= current:
        return None
    if not user.is_active:
        return None

    row.last_seen_at = current
    new_expiry = min(
        current + timedelta(seconds=settings.session_idle_ttl_s), row.absolute_expires_at
    )
    row.expires_at = new_expiry
    return user


async def revoke_session(session: AsyncSession, raw_token: str) -> None:
    await session.execute(
        update(AppSession)
        .where(AppSession.token_hash == hash_token(raw_token), AppSession.revoked_at.is_(None))
        .values(revoked_at=now())
    )


async def revoke_all_sessions(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(AppSession)
        .where(AppSession.user_id == user_id, AppSession.revoked_at.is_(None))
        .values(revoked_at=now())
    )
