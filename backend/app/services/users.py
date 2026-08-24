"""Account lifecycle: accepting the terms, suspension, reinstatement.

Suspension has to be more than a flag. Work is already queued when an operator
decides to stop someone — rules are active, a broadcast is halfway through a
hundred groups, a listener is holding their account's connection open. Flipping
``is_active`` alone would let all of that keep running, so suspending does the
same cascade that pausing a connection does, and every delivery path re-checks
the owner before sending.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Broadcast,
    BroadcastStatus,
    EventOutcome,
    ForwardingRule,
    RuleStatus,
    TelegramConnection,
    User,
)
from app.domain import reasons
from app.repositories import broadcasts as broadcast_repo
from app.repositories import events as event_repo
from app.repositories import jobs as job_repo

log = structlog.get_logger(__name__)

#: What a suspended person is told when no reason was given.
DEFAULT_REASON = "Suspended by the operator."


async def accept_terms(session: AsyncSession, *, user: User) -> None:
    if user.terms_accepted_at is None:
        user.terms_accepted_at = datetime.now(UTC)


async def suspend(
    session: AsyncSession, *, user: User, reason: str | None = None, by: uuid.UUID | None = None
) -> dict[str, int]:
    """Stop this account, including work already in flight.

    Returns what was stopped, so the operator sees the effect rather than a bare
    confirmation. Deliveries that already happened stay where they are —
    suspending cannot unsend anything.
    """
    if not user.is_active:
        return {"rules": 0, "broadcasts": 0, "jobs": 0, "targets": 0}

    user.is_active = False
    user.suspended_at = datetime.now(UTC)
    user.suspended_reason = (reason or DEFAULT_REASON)[:200]

    rules = (
        (
            await session.execute(
                select(ForwardingRule).where(
                    ForwardingRule.user_id == user.id,
                    ForwardingRule.status == RuleStatus.active,
                )
            )
        )
        .scalars()
        .all()
    )
    for rule in rules:
        rule.status = RuleStatus.paused
        rule.paused_reason_code = reasons.ACCOUNT_SUSPENDED
        await event_repo.record(
            session,
            rule_id=rule.id,
            connection_id=rule.connection_id,
            outcome=EventOutcome.paused,
            reason_code=reasons.ACCOUNT_SUSPENDED,
        )

    broadcasts = (
        (
            await session.execute(
                select(Broadcast).where(
                    Broadcast.user_id == user.id,
                    Broadcast.status == BroadcastStatus.sending,
                )
            )
        )
        .scalars()
        .all()
    )
    for broadcast in broadcasts:
        broadcast.status = BroadcastStatus.paused
        broadcast.paused_reason_code = reasons.ACCOUNT_SUSPENDED

    # Cancel queued work per connection, so nothing keeps firing at Telegram.
    connections = (
        (
            await session.execute(
                select(TelegramConnection).where(TelegramConnection.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    jobs = targets = 0
    for connection in connections:
        jobs += await job_repo.cancel_pending_for_connection(session, connection_id=connection.id)
        targets += await broadcast_repo.cancel_pending_for_connection(
            session, connection_id=connection.id
        )

    await event_repo.audit(
        session,
        user_id=by,
        action="user.suspend",
        object_type="user",
        object_id=str(user.id),
        payload={
            "reason": user.suspended_reason,
            "rules": len(rules),
            "broadcasts": len(broadcasts),
        },
    )
    log.warning("user_suspended", user_id=str(user.id), rules=len(rules))

    return {
        "rules": len(rules),
        "broadcasts": len(broadcasts),
        "jobs": jobs,
        "targets": targets,
    }


async def reinstate(session: AsyncSession, *, user: User, by: uuid.UUID | None = None) -> None:
    """Let the account back in.

    Deliberately does **not** resume anything. Their rules and broadcasts stay
    paused until they choose to restart them — silently resuming a broadcast
    someone was suspended over is the wrong default, and the person can see
    exactly what is paused and why.
    """
    if user.is_active:
        return
    user.is_active = True
    user.suspended_at = None
    user.suspended_reason = None
    await event_repo.audit(
        session, user_id=by, action="user.reinstate", object_type="user", object_id=str(user.id)
    )
    log.info("user_reinstated", user_id=str(user.id))


async def is_active(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """The guard every delivery path calls before sending.

    Checked at delivery time rather than only at queue time, so suspending
    stops work that was already sitting in the queue.
    """
    user = await session.get(User, user_id)
    return bool(user and user.is_active)


async def is_operator(session: AsyncSession, *, telegram_user_id: int | None) -> bool:
    """Is this Telegram account an operator of this deployment?

    Two sources, and the order is the point. ``ADMIN_TELEGRAM_IDS`` is the root
    of trust: an id there is an operator whether or not a database row exists,
    so a lost or corrupted row cannot lock the owner out of their own
    deployment. The database is the *addition* — a second account promoted from
    inside the panel, which is what stops the owner editing a file and
    redeploying every time they message the bot from a different phone.

    Never inferred from anything else. On an open deployment anyone can get an
    account by messaging the bot, so operator status has to be granted, not
    acquired.
    """
    if telegram_user_id is None:
        return False
    if get_settings().is_admin(telegram_user_id):
        return True
    result = await session.execute(
        select(User.is_operator).where(User.telegram_user_id == telegram_user_id)
    )
    return bool(result.scalar_one_or_none())
