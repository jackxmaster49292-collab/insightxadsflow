from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditEvent,
    EventOutcome,
    ForwardingEvent,
    ForwardingRule,
    TelegramConnection,
)
from app.domain import reasons
from app.security.redaction import scrub_text


async def record(
    session: AsyncSession,
    *,
    rule_id: uuid.UUID,
    connection_id: uuid.UUID,
    outcome: EventOutcome,
    reason_code: str,
    job_id: uuid.UUID | None = None,
    source_chat_id: uuid.UUID | None = None,
    source_message_ids: Sequence[int] = (),
    destination_chat_id: uuid.UUID | None = None,
    attempt: int = 0,
    detail: str | None = None,
) -> ForwardingEvent:
    """``detail_safe`` is redacted at write time — it is the only field the UI shows."""
    event = ForwardingEvent(
        rule_id=rule_id,
        job_id=job_id,
        connection_id=connection_id,
        source_chat_id=source_chat_id,
        source_message_ids=list(source_message_ids),
        destination_chat_id=destination_chat_id,
        outcome=outcome,
        reason_code=reason_code,
        detail_safe=scrub_text(detail) if detail else reasons.describe(reason_code),
        attempt=attempt,
    )
    session.add(event)
    await session.flush()
    return event


async def list_for_rule(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    rule_id: uuid.UUID,
    outcome: str | None = None,
    destination_chat_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ForwardingEvent]:
    stmt = (
        select(ForwardingEvent)
        .join(ForwardingRule, ForwardingRule.id == ForwardingEvent.rule_id)
        .where(ForwardingEvent.rule_id == rule_id, ForwardingRule.user_id == user_id)
    )
    if outcome:
        stmt = stmt.where(ForwardingEvent.outcome == EventOutcome(outcome))
    if destination_chat_id:
        stmt = stmt.where(ForwardingEvent.destination_chat_id == destination_chat_id)
    stmt = stmt.order_by(ForwardingEvent.occurred_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_recent_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    outcome: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ForwardingEvent]:
    stmt = (
        select(ForwardingEvent)
        .join(TelegramConnection, TelegramConnection.id == ForwardingEvent.connection_id)
        .where(TelegramConnection.user_id == user_id)
    )
    if outcome:
        stmt = stmt.where(ForwardingEvent.outcome == EventOutcome(outcome))
    stmt = stmt.order_by(ForwardingEvent.occurred_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def summary(
    session: AsyncSession, *, user_id: uuid.UUID, period_hours: int = 24
) -> dict[str, int]:
    """Operational counters. Explicitly not quota accounting."""
    since = datetime.now(UTC) - timedelta(hours=period_hours)
    result = await session.execute(
        select(ForwardingEvent.outcome, func.count())
        .join(TelegramConnection, TelegramConnection.id == ForwardingEvent.connection_id)
        .where(TelegramConnection.user_id == user_id, ForwardingEvent.occurred_at >= since)
        .group_by(ForwardingEvent.outcome)
    )
    counts = {outcome.value: 0 for outcome in EventOutcome}
    for outcome, count in result.all():
        counts[outcome.value] = int(count)
    return counts


async def audit(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    action: str,
    object_type: str,
    object_id: str | None = None,
    ip_hash: str | None = None,
    user_agent: str | None = None,
    correlation_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            user_id=user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            ip_hash=ip_hash,
            user_agent=(user_agent or "")[:512] or None,
            correlation_id=correlation_id,
            payload=payload or {},
        )
    )


async def list_audit(
    session: AsyncSession, *, user_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> list[AuditEvent]:
    result = await session.execute(
        select(AuditEvent)
        .where(AuditEvent.user_id == user_id)
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())
