from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting, User
from app.security.auth import hash_password


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def create(
    session: AsyncSession, *, email: str, password: str, timezone: str = "UTC"
) -> User:
    user = User(email=email, password_hash=hash_password(password), timezone=timezone)
    session.add(user)
    await session.flush()
    session.add(AppSetting(user_id=user.id, timezone=timezone))
    await session.flush()
    return user


async def get_settings_row(session: AsyncSession, *, user_id: uuid.UUID) -> AppSetting | None:
    result = await session.execute(select(AppSetting).where(AppSetting.user_id == user_id))
    return result.scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Operator views
# --------------------------------------------------------------------------- #
async def list_all(
    session: AsyncSession, *, limit: int = 500, include_inactive: bool = True
) -> list[User]:
    """Every account, newest first. For the operator's user list.

    Returns the rows themselves and nothing about what they contain — an
    operator needs to know an account exists and how busy it is, not what it
    says.
    """
    stmt = select(User).where(User.telegram_user_id.isnot(None))
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    result = await session.execute(stmt.order_by(User.created_at.desc()).limit(limit))
    return list(result.scalars().all())


async def counts(session: AsyncSession) -> dict[str, int]:
    from sqlalchemy import func

    total = await session.execute(
        select(func.count()).select_from(User).where(User.telegram_user_id.isnot(None))
    )
    suspended = await session.execute(
        select(func.count())
        .select_from(User)
        .where(User.telegram_user_id.isnot(None), User.is_active.is_(False))
    )
    return {"total": int(total.scalar_one()), "suspended": int(suspended.scalar_one())}


async def activity_for(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, int]:
    """Counts only — never content. Enough to spot an account behaving unlike
    the others without reading anyone's ads."""
    from sqlalchemy import func

    from app.db.models import Broadcast, ForwardingRule, TelegramConnection

    async def count_of(model) -> int:  # type: ignore[no-untyped-def]
        result = await session.execute(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )
        return int(result.scalar_one())

    return {
        "connections": await count_of(TelegramConnection),
        "rules": await count_of(ForwardingRule),
        "broadcasts": await count_of(Broadcast),
    }
