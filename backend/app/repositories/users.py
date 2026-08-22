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
