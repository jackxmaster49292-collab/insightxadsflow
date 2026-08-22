"""Execute one forwarding job.

This is where the reliability guarantees actually live:

* Eligibility is revalidated immediately before sending, and **fails closed**.
* A job whose ``rule_version`` predates the rule's current version re-evaluates
  filters and destination membership before delivering.
* Retry classification decides retry vs. skip vs. pause. A Telegram-provided
  wait is obeyed in full; backoff never shortens it.
* An ambiguous Bot API timeout is not retried — it becomes ``needs_attention``,
  because a silent duplicate broadcast is worse than a visible gap.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import (
    AccessReport,
    AmbiguousDeliveryError,
    InboundMessage,
    MediaType,
    TelegramAdapter,
)
from app.adapters.errors import ErrorClass, classify_error
from app.config import get_settings
from app.db.models import (
    ConnectionStatus,
    EventOutcome,
    ForwardingJob,
    ForwardingRule,
    ForwardMode,
    JobStatus,
    RuleStatus,
    TelegramChat,
    TelegramConnection,
)
from app.domain import reasons
from app.domain.filters import evaluate
from app.repositories import chats as chat_repo
from app.repositories import events as event_repo
from app.repositories import jobs as job_repo
from app.repositories import rules as rule_repo
from app.services import safety

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    status: JobStatus
    reason_code: str
    retry_after_s: float | None = None


def backoff_seconds(attempt: int) -> float:
    """1s, 2s, 4s, 8s, 16s with bounded jitter.

    Jitter is applied only to TRANSIENT retries for orderly spreading. It is
    never used to disguise traffic, and never shortens a Telegram-supplied wait.
    """
    base = min(2.0**attempt, 16.0)
    return base + random.uniform(0, min(base * 0.25, 2.0))  # noqa: S311


async def execute_job(
    session: AsyncSession,
    *,
    job: ForwardingJob,
    adapter: TelegramAdapter,
    connection: TelegramConnection,
) -> DeliveryOutcome:
    settings = get_settings()

    rule = await rule_repo.get_unscoped_for_worker(session, rule_id=job.rule_id)
    if rule is None:
        return await _terminal(session, job, JobStatus.skipped, reasons.RULE_INACTIVE)

    # --- guards that must hold at delivery time, not just at intake ---------
    if connection.status is not ConnectionStatus.active:
        return await _terminal(
            session, job, JobStatus.skipped, reasons.CONNECTION_DISCONNECTED, rule_id=rule.id
        )

    if rule.status is not RuleStatus.active:
        return await _terminal(
            session, job, JobStatus.skipped, reasons.RULE_INACTIVE, rule_id=rule.id
        )

    source_chat = await _load_chat(session, job.source_chat_id)
    destination_chat = await _load_chat(session, job.destination_chat_id)
    if source_chat is None or destination_chat is None:
        return await _terminal(
            session, job, JobStatus.skipped, reasons.DESTINATION_REMOVED, rule_id=rule.id
        )

    # --- the rule was edited while this job was queued ----------------------
    if job.rule_version != rule.version:
        stale = _handle_stale_job(
            job=job, rule=rule, source_chat=source_chat, destination_id=destination_chat.id
        )
        if stale is not None:
            return await _terminal(session, job, JobStatus.skipped, stale, rule_id=rule.id)

    # --- revalidate authorization; fail closed on uncertainty ---------------
    destination_ref = chat_repo.to_ref(destination_chat)
    access: AccessReport | None
    try:
        access = await adapter.check_destination_access(destination_ref)
    except Exception as exc:  # a check that errors is not a confirmation
        classified = classify_error(exc)
        log.warning("destination_check_failed", job_id=str(job.id), code=classified.code)
        access = None

    if access is None or not access.allowed:
        reason = reasons.DESTINATION_NOT_ELIGIBLE if access is None else access.reason_code
        await chat_repo.set_access(
            session,
            chat=destination_chat,
            can_read_source=bool(
                destination_chat.access.can_read_source if destination_chat.access else False
            ),
            source_reason_code=(
                destination_chat.access.source_reason_code
                if destination_chat.access
                else reasons.UNKNOWN
            ),
            can_post_destination=False,
            destination_reason_code=reason,
            check_source="pre_delivery",
        )
        return await _terminal(
            session,
            job,
            JobStatus.skipped,
            reason,
            rule_id=rule.id,
            destination_id=destination_chat.id,
        )

    # --- deliver ------------------------------------------------------------
    source_ref = chat_repo.to_ref(source_chat)
    try:
        if rule.forward_mode is ForwardMode.copy:
            if source_chat.has_protected_content:
                # Copy mode must not become a content-protection bypass.
                return await _terminal(
                    session,
                    job,
                    JobStatus.skipped,
                    reasons.PROTECTED_CONTENT,
                    rule_id=rule.id,
                    destination_id=destination_chat.id,
                )
            receipt = await adapter.send_supported_content(
                source_ref,
                list(job.source_message_ids),
                destination_ref,
                preserve_caption=rule.preserve_caption,
            )
        else:
            receipt = await adapter.forward_message(
                source_ref,
                list(job.source_message_ids),
                destination_ref,
                random_id=job.mtproto_random_id,
            )
    except AmbiguousDeliveryError:
        # No idempotency token on this provider: refuse to guess.
        await job_repo.finish(
            session,
            job=job,
            status=JobStatus.needs_attention,
            error_class=ErrorClass.UNKNOWN.value,
            error_code=reasons.AMBIGUOUS_TIMEOUT,
        )
        await event_repo.record(
            session,
            rule_id=rule.id,
            connection_id=job.connection_id,
            job_id=job.id,
            outcome=EventOutcome.failed,
            reason_code=reasons.AMBIGUOUS_TIMEOUT,
            source_chat_id=job.source_chat_id,
            source_message_ids=job.source_message_ids,
            destination_chat_id=destination_chat.id,
            attempt=job.attempt_count,
        )
        return DeliveryOutcome(JobStatus.needs_attention, reasons.AMBIGUOUS_TIMEOUT)
    except Exception as exc:
        return await _handle_failure(
            session,
            job=job,
            rule_id=rule.id,
            destination_id=destination_chat.id,
            exc=exc,
            connection=connection,
            max_attempts=min(rule.max_attempts, settings.max_attempts),
        )

    await job_repo.finish(
        session,
        job=job,
        status=JobStatus.succeeded,
        destination_message_id=receipt.destination_message_id,
    )
    await event_repo.record(
        session,
        rule_id=rule.id,
        connection_id=job.connection_id,
        job_id=job.id,
        outcome=EventOutcome.forwarded,
        reason_code=reasons.DELIVERED,
        source_chat_id=job.source_chat_id,
        source_message_ids=job.source_message_ids,
        destination_chat_id=destination_chat.id,
        attempt=job.attempt_count,
    )
    rule.last_activity_at = datetime.now(UTC)
    connection.consecutive_failure_count = 0
    return DeliveryOutcome(JobStatus.succeeded, reasons.DELIVERED)


async def _load_chat(session: AsyncSession, chat_id: uuid.UUID) -> TelegramChat | None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.models import TelegramChat

    result = await session.execute(
        select(TelegramChat)
        .where(TelegramChat.id == chat_id)
        .options(selectinload(TelegramChat.access))
    )
    return result.scalar_one_or_none()


def _handle_stale_job(
    *,
    job: ForwardingJob,
    rule: ForwardingRule,
    source_chat: TelegramChat,
    destination_id: uuid.UUID,
) -> str | None:
    """A job created under an older rule version. Returns a skip reason, or None
    to deliver it under the original idempotency key."""
    if destination_id not in {d.chat_id for d in rule.destinations}:
        return reasons.DESTINATION_REMOVED

    from app.services.dispatch import filter_config_for

    config = filter_config_for(rule)
    if config.keyword_include or config.keyword_exclude:
        # The original message text is not retained (we never store message
        # content), so a keyword filter added after this job was queued cannot be
        # re-evaluated. Skip rather than deliver something the customer may have
        # since decided to exclude.
        return reasons.FILTERED_AFTER_EDIT

    probe = InboundMessage(
        source=chat_repo.to_ref(source_chat),
        message_ids=list(job.source_message_ids),
        media_type=MediaType.text,
        text="",
    )
    decision = evaluate(probe, config)
    if not decision.passed and decision.reason_code != reasons.FILTERED_MEDIA_TYPE:
        return decision.reason_code
    return None


async def _handle_failure(
    session: AsyncSession,
    *,
    job: ForwardingJob,
    rule_id: uuid.UUID,
    destination_id: uuid.UUID,
    exc: BaseException,
    connection: TelegramConnection,
    max_attempts: int,
) -> DeliveryOutcome:
    classified = classify_error(exc)
    settings = get_settings()

    # Authorization failures pause the whole connection immediately.
    if classified.error_class is ErrorClass.AUTH:
        await safety.pause_connection(
            session, connection=connection, reason_code=reasons.AUTH_PAUSE
        )
        await job_repo.finish(
            session,
            job=job,
            status=JobStatus.failed,
            error_class=classified.error_class.value,
            error_code=classified.code,
        )
        await _record_failure(
            session, job, rule_id, destination_id, classified.code, EventOutcome.paused
        )
        return DeliveryOutcome(JobStatus.failed, classified.code)

    # Permission and permanent-content failures are never retried.
    if not classified.retryable:
        await job_repo.finish(
            session,
            job=job,
            status=JobStatus.skipped,
            error_class=classified.error_class.value,
            error_code=classified.code,
        )
        await _record_failure(
            session, job, rule_id, destination_id, classified.code, EventOutcome.skipped
        )
        return DeliveryOutcome(JobStatus.skipped, classified.code)

    # Rate limits: obey Telegram's number exactly.
    if classified.error_class is ErrorClass.RATE_LIMIT:
        wait = classified.retry_after_s if classified.retry_after_s is not None else 60.0
        if wait >= settings.flood_wait_pause_threshold_s:
            await safety.pause_rule(session, rule_id=rule_id, reason_code=reasons.FLOOD_WAIT_PAUSE)
            await job_repo.reschedule(
                session,
                job=job,
                delay_s=wait,
                error_class=classified.error_class.value,
                error_code=classified.code,
            )
            await _record_failure(
                session, job, rule_id, destination_id, reasons.FLOOD_WAIT_PAUSE, EventOutcome.paused
            )
            return DeliveryOutcome(JobStatus.pending, reasons.FLOOD_WAIT_PAUSE, wait)

        await job_repo.reschedule(
            session,
            job=job,
            delay_s=wait,
            error_class=classified.error_class.value,
            error_code=classified.code,
        )
        await _record_failure(
            session, job, rule_id, destination_id, classified.code, EventOutcome.retry_scheduled
        )
        return DeliveryOutcome(JobStatus.pending, classified.code, wait)

    # TRANSIENT / UNKNOWN: bounded retries, then a visible dead letter.
    if job.attempt_count + 1 >= max_attempts:
        await job_repo.finish(
            session,
            job=job,
            status=JobStatus.dead_letter,
            error_class=classified.error_class.value,
            error_code=classified.code,
        )
        await _record_failure(
            session,
            job,
            rule_id,
            destination_id,
            reasons.MAX_ATTEMPTS_EXCEEDED,
            EventOutcome.failed,
        )
        await safety.note_failure(session, rule_id=rule_id, connection=connection)
        return DeliveryOutcome(JobStatus.dead_letter, reasons.MAX_ATTEMPTS_EXCEEDED)

    delay = backoff_seconds(job.attempt_count)
    await job_repo.reschedule(
        session,
        job=job,
        delay_s=delay,
        error_class=classified.error_class.value,
        error_code=classified.code,
    )
    await _record_failure(
        session, job, rule_id, destination_id, reasons.RETRYING, EventOutcome.retry_scheduled
    )
    return DeliveryOutcome(JobStatus.pending, reasons.RETRYING, delay)


async def _record_failure(
    session: AsyncSession,
    job: ForwardingJob,
    rule_id: uuid.UUID,
    destination_id: uuid.UUID | None,
    reason_code: str,
    outcome: EventOutcome,
) -> None:
    await event_repo.record(
        session,
        rule_id=rule_id,
        connection_id=job.connection_id,
        job_id=job.id,
        outcome=outcome,
        reason_code=reason_code,
        source_chat_id=job.source_chat_id,
        source_message_ids=job.source_message_ids,
        destination_chat_id=destination_id,
        attempt=job.attempt_count,
    )


async def _terminal(
    session: AsyncSession,
    job: ForwardingJob,
    status: JobStatus,
    reason_code: str,
    *,
    rule_id: uuid.UUID | None = None,
    destination_id: uuid.UUID | None = None,
) -> DeliveryOutcome:
    await job_repo.finish(session, job=job, status=status, error_code=reason_code)
    if rule_id is not None:
        await event_repo.record(
            session,
            rule_id=rule_id,
            connection_id=job.connection_id,
            job_id=job.id,
            outcome=EventOutcome.skipped,
            reason_code=reason_code,
            source_chat_id=job.source_chat_id,
            source_message_ids=job.source_message_ids,
            destination_chat_id=destination_id,
            attempt=job.attempt_count,
        )
    return DeliveryOutcome(status, reason_code)
