"""Automatic safety pause.

The product never silently keeps hammering Telegram. A rule or connection stops
itself, durably and visibly, when:

* any authorization error occurs           → the **connection** pauses at once;
* Telegram asks for a long wait            → the **rule** pauses;
* serious failures repeat past a threshold → the **rule** pauses.

Resuming is always an explicit customer action.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Broadcast,
    BroadcastStatus,
    ConnectionStatus,
    EventOutcome,
    ForwardingRule,
    RuleStatus,
    TelegramConnection,
)
from app.domain import reasons
from app.repositories import admins as admin_repo
from app.repositories import broadcasts as broadcast_repo
from app.repositories import events as event_repo
from app.repositories import jobs as job_repo

log = structlog.get_logger(__name__)


async def pause_rule(
    session: AsyncSession, *, rule_id: uuid.UUID, reason_code: str
) -> ForwardingRule | None:
    rule = await session.get(ForwardingRule, rule_id)
    if rule is None or rule.status is RuleStatus.paused:
        return rule

    rule.status = RuleStatus.paused
    rule.paused_reason_code = reason_code
    await event_repo.record(
        session,
        rule_id=rule.id,
        connection_id=rule.connection_id,
        outcome=EventOutcome.paused,
        reason_code=reason_code,
    )
    # Push it to the operator instead of waiting for them to look.
    await admin_repo.notify(
        session,
        user_id=rule.user_id,
        kind="rule_paused",
        title="Rule paused automatically",
        body=(
            f"{rule.name} has been paused.\n\n{reasons.describe(reason_code)}\n\n"
            "Open the panel to review and resume it."
        ),
        dedupe_key=f"rule_paused:{rule.id}:{rule.version}:{reason_code}",
        rule_id=rule.id,
    )

    log.warning("rule_paused", rule_id=str(rule.id), reason_code=reason_code)
    return rule


async def pause_connection(
    session: AsyncSession, *, connection: TelegramConnection, reason_code: str
) -> None:
    """Pausing a connection also stops every rule that depends on it, and
    cancels queued jobs so nothing keeps firing at a revoked session."""
    if connection.status is ConnectionStatus.paused_safety:
        return

    connection.status = ConnectionStatus.paused_safety
    connection.last_error_code = reason_code
    connection.last_error_message_safe = reasons.describe(reason_code)

    result = await session.execute(
        select(ForwardingRule).where(
            ForwardingRule.connection_id == connection.id,
            ForwardingRule.status == RuleStatus.active,
        )
    )
    for rule in result.scalars().all():
        rule.status = RuleStatus.paused
        rule.paused_reason_code = reason_code
        await event_repo.record(
            session,
            rule_id=rule.id,
            connection_id=connection.id,
            outcome=EventOutcome.paused,
            reason_code=reason_code,
        )

    # Every broadcast on this connection stops too, for the same reason: firing
    # at a revoked session produces nothing but errors.
    sending = await session.execute(
        select(Broadcast).where(
            Broadcast.connection_id == connection.id,
            Broadcast.status == BroadcastStatus.sending,
        )
    )
    for broadcast in sending.scalars().all():
        broadcast.status = BroadcastStatus.paused
        broadcast.paused_reason_code = reason_code
        await event_repo.record(
            session,
            broadcast_id=broadcast.id,
            connection_id=connection.id,
            outcome=EventOutcome.paused,
            reason_code=reason_code,
        )

    await job_repo.cancel_pending_for_connection(session, connection_id=connection.id)
    await broadcast_repo.cancel_pending_for_connection(session, connection_id=connection.id)

    await admin_repo.notify(
        session,
        user_id=connection.user_id,
        kind="connection_paused",
        title="Telegram connection paused",
        body=(
            f"{connection.label} is no longer usable.\n\n{reasons.describe(reason_code)}\n\n"
            "Every rule on this connection has been paused. Reconnect it in the panel."
        ),
        dedupe_key=f"connection_paused:{connection.id}:{reason_code}",
        connection_id=connection.id,
    )

    log.warning("connection_paused", connection_id=str(connection.id), reason_code=reason_code)


async def note_failure(
    session: AsyncSession, *, rule_id: uuid.UUID, connection: TelegramConnection
) -> None:
    """Count a serious failure and pause the rule once the threshold is crossed."""
    settings = get_settings()
    connection.consecutive_failure_count += 1

    recent = await job_repo.count_recent_failures(session, rule_id=rule_id)
    if recent >= settings.safety_pause_threshold:
        await pause_rule(session, rule_id=rule_id, reason_code=reasons.SAFETY_PAUSE)


async def note_connection_failure(session: AsyncSession, *, connection: TelegramConnection) -> None:
    """Count a serious failure that has no rule behind it.

    A broadcast target's dead letter is still evidence the connection may be
    unhealthy, but there is no rule to pause — so only the counter moves, and
    the connection-level guards act on it.
    """
    connection.consecutive_failure_count += 1


async def clear_failures(connection: TelegramConnection) -> None:
    connection.consecutive_failure_count = 0
    connection.last_error_code = None
    connection.last_error_message_safe = None
