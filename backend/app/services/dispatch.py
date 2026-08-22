"""Intake → durable forwarding jobs.

Runs in the listener, never in an HTTP request. For one inbound source message
this resolves every active rule that reads that chat, applies filters once, and
creates one durable job per eligible destination.

Duplicate suppression is done by the database: the unique constraint on
``idempotency_key`` means a replayed update after a reconnect creates zero new
jobs, with no read-then-write race.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import InboundMessage
from app.db.models import (
    EventOutcome,
    ForwardingRule,
    RuleStatus,
    TelegramChat,
)
from app.domain import reasons
from app.domain.filters import FilterConfig, evaluate
from app.domain.idempotency import build_idempotency_key, build_mtproto_random_id
from app.repositories import chats as chat_repo
from app.repositories import events as event_repo
from app.repositories import jobs as job_repo
from app.repositories import rules as rule_repo

log = structlog.get_logger(__name__)


@dataclass
class DispatchResult:
    created_job_ids: list[uuid.UUID] = field(default_factory=list)
    suppressed: int = 0
    skipped_reason: str | None = None
    matched_rules: int = 0


def filter_config_for(rule: ForwardingRule) -> FilterConfig:
    return FilterConfig(
        keyword_include=list(rule.keyword_include or []),
        keyword_exclude=list(rule.keyword_exclude or []),
        keyword_match_mode=rule.keyword_match_mode.value,
        media_types=list(rule.media_types or []),
    )


async def dispatch_inbound(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    message: InboundMessage,
) -> DispatchResult:
    result = DispatchResult()

    source_chat = await chat_repo.find_by_peer(
        session, connection_id=connection_id, ref=message.source
    )
    if source_chat is None or not source_chat.is_active:
        # A chat we have never synchronized is not an authorized source.
        result.skipped_reason = reasons.SOURCE_NOT_ELIGIBLE
        return result

    rules = await rule_repo.list_active_with_source(
        session, connection_id=connection_id, chat_id=source_chat.id
    )
    result.matched_rules = len(rules)

    for rule in rules:
        await _dispatch_for_rule(
            session,
            rule=rule,
            connection_id=connection_id,
            source_chat=source_chat,
            message=message,
            result=result,
        )

    await _advance_cursor(session, connection_id=connection_id, chat=source_chat, message=message)
    return result


async def _dispatch_for_rule(
    session: AsyncSession,
    *,
    rule: ForwardingRule,
    connection_id: uuid.UUID,
    source_chat: TelegramChat,
    message: InboundMessage,
    result: DispatchResult,
) -> None:
    decision = evaluate(message, filter_config_for(rule))
    if not decision.passed:
        result.skipped_reason = decision.reason_code
        await event_repo.record(
            session,
            rule_id=rule.id,
            connection_id=connection_id,
            outcome=EventOutcome.skipped,
            reason_code=decision.reason_code,
            source_chat_id=source_chat.id,
            source_message_ids=message.message_ids,
        )
        return

    destinations = await rule_repo.destination_chats(session, rule=rule)
    # Bounded, transparent pacing: the Nth destination is released N*delay later.
    base = datetime.now(UTC)

    for index, destination in enumerate(destinations):
        access = destination.access
        if not destination.is_active or access is None or not access.can_post_destination:
            await event_repo.record(
                session,
                rule_id=rule.id,
                connection_id=connection_id,
                outcome=EventOutcome.skipped,
                reason_code=reasons.DESTINATION_NOT_ELIGIBLE,
                source_chat_id=source_chat.id,
                source_message_ids=message.message_ids,
                destination_chat_id=destination.id,
            )
            continue

        key = build_idempotency_key(
            rule_id=rule.id,
            source=message.source,
            message_ids=message.message_ids,
            destination=chat_repo.to_ref(destination),
        )
        job = await job_repo.create_if_absent(
            session,
            rule_id=rule.id,
            rule_version=rule.version,
            connection_id=connection_id,
            source_chat_id=source_chat.id,
            source_message_ids=message.message_ids,
            destination_chat_id=destination.id,
            idempotency_key=key,
            mtproto_random_id=build_mtproto_random_id(key),
            not_before=base + timedelta(milliseconds=rule.delay_ms * index),
        )
        if job is None:
            # Already delivered or already queued for this exact combination.
            result.suppressed += 1
            continue
        result.created_job_ids.append(job.id)

    if message.partial_album:
        await event_repo.record(
            session,
            rule_id=rule.id,
            connection_id=connection_id,
            outcome=EventOutcome.skipped,
            reason_code=reasons.PARTIAL_ALBUM,
            source_chat_id=source_chat.id,
            source_message_ids=message.message_ids,
        )

    rule.last_activity_at = datetime.now(UTC)
    if rule.status is RuleStatus.error:
        rule.status = RuleStatus.active


async def _advance_cursor(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    chat: TelegramChat,
    message: InboundMessage,
) -> None:
    """Second dedupe layer, and what makes a restart resume rather than replay."""
    from sqlalchemy import select

    from app.db.models import SourceCursor

    result = await session.execute(
        select(SourceCursor).where(
            SourceCursor.connection_id == connection_id, SourceCursor.chat_id == chat.id
        )
    )
    cursor = result.scalar_one_or_none()
    highest = max(message.message_ids)
    if cursor is None:
        session.add(
            SourceCursor(
                connection_id=connection_id, chat_id=chat.id, last_processed_message_id=highest
            )
        )
    elif highest > cursor.last_processed_message_id:
        cursor.last_processed_message_id = highest
