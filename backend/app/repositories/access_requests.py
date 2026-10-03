"""People who asked to use this deployment, and what was decided.

One row per Telegram id, for ever. A decision — either way — is kept rather
than cleared: without that, a denied stranger reappears in the operator's
messages every time they tap /start, and an approval would have nothing
durable behind it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccessRequest, AccessRequestStatus


async def get(session: AsyncSession, *, telegram_user_id: int) -> AccessRequest | None:
    result = await session.execute(
        select(AccessRequest).where(AccessRequest.telegram_user_id == telegram_user_id)
    )
    return result.scalar_one_or_none()


async def by_id(session: AsyncSession, *, request_id: uuid.UUID) -> AccessRequest | None:
    return await session.get(AccessRequest, request_id)


async def record(
    session: AsyncSession, *, telegram_user_id: int, username: str | None
) -> tuple[AccessRequest, bool]:
    """Note that this person asked. Returns the row and whether it is new.

    Upserted, because the middleware runs on every update: someone tapping
    /start three times in a row must produce one request and one message to the
    operator, not three. An existing decision is never overwritten — asking
    again does not undo a denial.
    """
    result = await session.execute(
        pg_insert(AccessRequest)
        .values(
            id=uuid.uuid4(),
            telegram_user_id=telegram_user_id,
            telegram_username=username,
            status=AccessRequestStatus.pending,
        )
        .on_conflict_do_nothing(index_elements=[AccessRequest.telegram_user_id])
        .returning(AccessRequest.id)
    )
    inserted = result.scalar_one_or_none()
    await session.flush()

    row = await get(session, telegram_user_id=telegram_user_id)
    assert row is not None
    if inserted is None and username and row.telegram_username != username:
        # A username can change between asking and being decided on, and the
        # operator should be looking at the current one.
        row.telegram_username = username
        await session.flush()
    return row, inserted is not None


async def decide(
    session: AsyncSession,
    *,
    request: AccessRequest,
    approved: bool,
    decided_by: uuid.UUID | None,
) -> AccessRequest:
    request.status = AccessRequestStatus.approved if approved else AccessRequestStatus.denied
    request.decided_at = datetime.now(UTC)
    request.decided_by = decided_by
    await session.flush()
    return request


async def pending(session: AsyncSession, *, limit: int = 100) -> Sequence[AccessRequest]:
    result = await session.execute(
        select(AccessRequest)
        .where(AccessRequest.status == AccessRequestStatus.pending)
        .order_by(AccessRequest.created_at)
        .limit(limit)
    )
    return list(result.scalars().all())


async def pending_count(session: AsyncSession) -> int:
    return len(await pending(session))
