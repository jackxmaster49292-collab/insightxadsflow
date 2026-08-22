"""Telegram-identified accounts and the notification outbox."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminNotification, AppSetting, User


async def get_by_telegram_id(session: AsyncSession, telegram_user_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    return result.scalar_one_or_none()


async def upsert_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    username: str | None,
) -> User:
    """Find or create the account behind a Telegram identity.

    Called only after the middleware has decided the caller may be here. This
    function authorizes nothing on its own — creating a row is not permission,
    which is why a new account starts with no accepted terms and can see only
    the terms screen.
    """
    user = await get_by_telegram_id(session, telegram_user_id)
    if user is not None:
        if username and user.telegram_username != username:
            user.telegram_username = username
        return user

    user = User(
        # Synthetic and non-routable: a Telegram account has no email here, and
        # a real address would imply a login path that does not exist.
        email=f"tg-{telegram_user_id}@telegram.local",
        telegram_user_id=telegram_user_id,
        telegram_username=username,
        password_hash=None,
        timezone="UTC",
    )
    session.add(user)
    await session.flush()
    session.add(AppSetting(user_id=user.id, timezone="UTC"))
    await session.flush()
    return user


async def notify(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: str,
    title: str,
    body: str,
    dedupe_key: str,
    rule_id: uuid.UUID | None = None,
    connection_id: uuid.UUID | None = None,
) -> None:
    """Queue an alert. Duplicate ``dedupe_key`` values collapse into one message,
    so a failing rule cannot spam the operator."""
    await session.execute(
        pg_insert(AdminNotification)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            kind=kind,
            title=title,
            body=body,
            dedupe_key=dedupe_key,
            rule_id=rule_id,
            connection_id=connection_id,
        )
        .on_conflict_do_nothing(index_elements=[AdminNotification.dedupe_key])
    )


async def claim_unsent(session: AsyncSession, *, limit: int = 20) -> Sequence[AdminNotification]:
    result = await session.execute(
        select(AdminNotification)
        .where(AdminNotification.sent_at.is_(None), AdminNotification.send_attempts < 5)
        .order_by(AdminNotification.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return result.scalars().all()


async def mark_sent(session: AsyncSession, *, notification_id: uuid.UUID) -> None:
    await session.execute(
        update(AdminNotification)
        .where(AdminNotification.id == notification_id)
        .values(sent_at=datetime.now(UTC))
    )


async def mark_attempt(session: AsyncSession, *, notification_id: uuid.UUID) -> None:
    await session.execute(
        update(AdminNotification)
        .where(AdminNotification.id == notification_id)
        .values(send_attempts=AdminNotification.send_attempts + 1)
    )
