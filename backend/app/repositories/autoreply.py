"""Auto-reply settings and the record of who has already been answered.

The log is persisted rather than cached because an empty cache after a restart
would answer everyone a second time — and a second unrequested message is
exactly what the cooldown exists to prevent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AutoReply, AutoReplyLog, PeerType


def now() -> datetime:
    return datetime.now(UTC)


async def get_for_connection(
    session: AsyncSession, *, connection_id: uuid.UUID
) -> AutoReply | None:
    result = await session.execute(
        select(AutoReply).where(AutoReply.connection_id == connection_id)
    )
    return result.scalar_one_or_none()


async def get_for_user(session: AsyncSession, *, user_id: uuid.UUID) -> list[AutoReply]:
    result = await session.execute(select(AutoReply).where(AutoReply.user_id == user_id))
    return list(result.scalars().all())


async def upsert(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    body_text: str | None = None,
    enabled: bool | None = None,
    cooldown_s: int | None = None,
) -> AutoReply:
    reply = await get_for_connection(session, connection_id=connection_id)
    if reply is None:
        reply = AutoReply(user_id=user_id, connection_id=connection_id, body_text="", enabled=False)
        session.add(reply)

    if body_text is not None:
        reply.body_text = body_text
    if cooldown_s is not None:
        reply.cooldown_s = cooldown_s
    if enabled is not None:
        # Enabling with nothing to say would send an empty message, which
        # Telegram rejects anyway. Refuse it here where the reason is visible.
        if enabled and not reply.body_text.strip():
            raise ValueError("Write the reply text before turning auto-reply on.")
        reply.enabled = enabled

    await session.flush()
    return reply


async def claim_reply_slot(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    peer_id: int,
    peer_type: PeerType = PeerType.user,
    cooldown_s: int,
) -> bool:
    """Reserve the right to answer this person, or report that we may not.

    The decision and the record are one statement. Checking first and inserting
    afterwards would let two listeners both read "no recent reply" and both send
    — the duplicate this guard exists to prevent. ``ON CONFLICT ... WHERE`` makes
    the cooldown part of the write, so exactly one caller can win.
    """
    cutoff = now() - timedelta(seconds=cooldown_s)
    stmt = (
        pg_insert(AutoReplyLog)
        .values(
            connection_id=connection_id,
            peer_type=peer_type,
            peer_id=peer_id,
            replied_at=now(),
            reply_count=1,
        )
        .on_conflict_do_update(
            index_elements=[
                AutoReplyLog.connection_id,
                AutoReplyLog.peer_type,
                AutoReplyLog.peer_id,
            ],
            set_={
                "replied_at": now(),
                "reply_count": AutoReplyLog.reply_count + 1,
            },
            where=AutoReplyLog.replied_at < cutoff,
        )
        .returning(AutoReplyLog.peer_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


#: Where a released claim parks ``replied_at``. Any cooldown is shorter than the
#: distance from here to now, so the person becomes eligible again immediately.
_RELEASED = datetime(1970, 1, 1, tzinfo=UTC)


async def release_reply_slot(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    peer_id: int,
    peer_type: PeerType = PeerType.user,
) -> None:
    """Undo a claim whose send then failed.

    Without this, a transient failure would consume the person's slot and they
    would wait out the whole cooldown for an answer that never arrived.

    A first-ever claim leaves no trace, so its row is removed. A repeat claim
    cannot restore the previous timestamp — it was overwritten — so the row is
    parked in the past instead, making the person eligible again now. Answering
    a bit sooner than the strict cooldown is the right way to be wrong here: the
    previous send failed, so no message actually went out.
    """
    where = (
        AutoReplyLog.connection_id == connection_id,
        AutoReplyLog.peer_type == peer_type,
        AutoReplyLog.peer_id == peer_id,
    )
    result = await session.execute(
        delete(AutoReplyLog)
        .where(*where, AutoReplyLog.reply_count <= 1)
        .returning(AutoReplyLog.peer_id)
    )
    if result.scalar_one_or_none() is not None:
        return

    await session.execute(
        update(AutoReplyLog)
        .where(*where)
        .values(replied_at=_RELEASED, reply_count=AutoReplyLog.reply_count - 1)
    )


async def purge_log(session: AsyncSession, *, older_than_days: int) -> int:
    cutoff = now() - timedelta(days=older_than_days)
    result = await session.execute(
        delete(AutoReplyLog).where(AutoReplyLog.replied_at < cutoff).returning(AutoReplyLog.peer_id)
    )
    return len(result.all())
