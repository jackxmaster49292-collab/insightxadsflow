"""Compose, queue and deliver a broadcast.

A broadcast is the customer's own message posted to groups they have chosen. It
is not forwarding: nothing is copied out of another chat, so there is no source
peer and no content-protection question to answer.

What it deliberately shares with forwarding is every property that makes
delivery trustworthy, because none of those are specific to forwarding:

* eligibility is revalidated immediately before each send, and **fails closed**;
* a Telegram-supplied wait is obeyed in full, never shortened by backoff;
* permission failures are never retried — they are a fact, not a hiccup;
* an ambiguous Bot API timeout becomes ``needs_attention`` rather than a retry,
  because posting the same ad twice in a group is worse than a visible gap.

What it will not do: post to a group the connection has not joined. Targets come
from synchronized membership, and the pre-send check is the second gate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import (
    AccessReport,
    AmbiguousDeliveryError,
    TelegramAdapter,
    TextEntity,
)
from app.adapters.errors import ErrorClass, classify_error
from app.config import get_settings
from app.db.models import (
    Broadcast,
    BroadcastMedia,
    BroadcastStatus,
    BroadcastTarget,
    ConnectionStatus,
    EventOutcome,
    JobStatus,
    TelegramChat,
    TelegramConnection,
    User,
)
from app.domain import reasons
from app.repositories import broadcasts as broadcast_repo
from app.repositories import chats as chat_repo
from app.repositories import events as event_repo
from app.services import safety
from app.services import users as user_service
from app.services.delivery import DeliveryOutcome, backoff_seconds

log = structlog.get_logger(__name__)


class BroadcastValidationError(Exception):
    """Rejected before anything is queued, with a sentence for the customer."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --------------------------------------------------------------------------- #
# Composing and queueing
# --------------------------------------------------------------------------- #
def validate(broadcast: Broadcast, *, target_count: int) -> None:
    """Everything that must hold before a broadcast may be queued."""
    settings = get_settings()

    has_text = bool(broadcast.body_text.strip())
    has_media = broadcast.media_kind is not BroadcastMedia.none and broadcast.media_bytes
    if not has_text and not has_media:
        raise BroadcastValidationError("Write a message, or attach an image, before sending.")

    # Telegram's own limits. Checking here means the customer is told while they
    # can still edit, rather than watching every delivery fail one by one.
    limit = settings.max_broadcast_caption_len if has_media else settings.max_broadcast_text_len
    if len(broadcast.body_text) > limit:
        kind = "caption" if has_media else "message"
        raise BroadcastValidationError(
            f"Telegram limits a {kind} to {limit} characters and yours is "
            f"{len(broadcast.body_text)}. Shorten it by {len(broadcast.body_text) - limit}."
        )

    if target_count == 0:
        raise BroadcastValidationError("Choose at least one group to post to.")

    if target_count > settings.max_broadcast_targets:
        raise BroadcastValidationError(
            f"This broadcast has {target_count} groups and the limit is "
            f"{settings.max_broadcast_targets}. Split it into more than one broadcast."
        )

    # delay_ms multiplies by the number of groups, so a generous pause across a
    # large list can push the tail days out. Say so now, with the number.
    spread_s = (broadcast.delay_ms / 1000) * max(0, target_count - 1)
    if spread_s > settings.max_rule_spread_s:
        raise BroadcastValidationError(
            f"A {broadcast.delay_ms} ms pause across {target_count} groups would take "
            f"{_humanize(spread_s)} to finish, which is longer than the "
            f"{_humanize(settings.max_rule_spread_s)} limit. Lower the pause."
        )


async def queue(
    session: AsyncSession,
    *,
    broadcast: Broadcast,
    start_at: datetime | None = None,
) -> int:
    """Validate, then hand the targets to the worker. Returns the count queued."""
    target_count = len(await broadcast_repo.target_chat_ids(session, broadcast_id=broadcast.id))
    validate(broadcast, target_count=target_count)

    broadcast.status = BroadcastStatus.sending
    broadcast.started_at = datetime.now(UTC)
    broadcast.completed_at = None
    broadcast.paused_reason_code = None
    queued = await broadcast_repo.schedule_targets(session, broadcast=broadcast, start_at=start_at)

    # The one counter an operator can use to spot an account behaving unlike the
    # others, without reading anything it sends.
    owner = await session.get(User, broadcast.user_id)
    if owner is not None:
        owner.broadcasts_sent += 1
    log.info(
        "broadcast_queued",
        broadcast_id=str(broadcast.id),
        targets=queued,
        delay_ms=broadcast.delay_ms,
    )
    return queued


async def pause(session: AsyncSession, *, broadcast: Broadcast, reason_code: str) -> None:
    """Stop feeding the worker. Targets already leased finish; nothing new starts."""
    broadcast.status = BroadcastStatus.paused
    broadcast.paused_reason_code = reason_code


async def cancel(session: AsyncSession, *, broadcast: Broadcast) -> int:
    """Cancelling stops what has not been sent. It cannot unsend anything."""
    broadcast.status = BroadcastStatus.cancelled
    broadcast.completed_at = datetime.now(UTC)
    return await broadcast_repo.cancel_pending(session, broadcast_id=broadcast.id)


async def retry_unfinished(session: AsyncSession, *, broadcast: Broadcast) -> int:
    """Queue another attempt for groups that did not receive the message.

    Reopening the broadcast is part of retrying, not a separate step: a target
    put back to ``pending`` under a ``completed`` broadcast is skipped by
    ``execute_target``, which would make Retry silently do nothing. Groups that
    already succeeded are untouched — a retry must never post twice.
    """
    requeued = await broadcast_repo.requeue_failed(session, broadcast_id=broadcast.id)
    if not requeued:
        return 0
    broadcast.status = BroadcastStatus.sending
    broadcast.completed_at = None
    broadcast.paused_reason_code = None
    await broadcast_repo.schedule_targets(session, broadcast=broadcast)
    return requeued


def estimated_duration_s(delay_ms: int, target_count: int) -> float:
    return (delay_ms / 1000) * max(0, target_count - 1)


def _humanize(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86_400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86_400:.1f} days"


# --------------------------------------------------------------------------- #
# Delivering one target
# --------------------------------------------------------------------------- #
async def execute_target(
    session: AsyncSession,
    *,
    target: BroadcastTarget,
    adapter: TelegramAdapter,
    connection: TelegramConnection,
) -> DeliveryOutcome:
    """Post one broadcast to one group."""
    settings = get_settings()

    broadcast = await broadcast_repo.get_unscoped_for_worker(
        session, broadcast_id=target.broadcast_id
    )
    if broadcast is None:
        return await _terminal(session, target, JobStatus.skipped, reasons.BROADCAST_INACTIVE)

    # --- guards that must hold at delivery time, not just at queue time -----
    # An account suspended mid-broadcast has targets already queued; they must
    # stop, which is why this is checked here and not only at queue time.
    if not await user_service.is_active(session, broadcast.user_id):
        return await _terminal(
            session,
            target,
            JobStatus.skipped,
            reasons.ACCOUNT_SUSPENDED,
            broadcast_id=broadcast.id,
        )

    if connection.status is not ConnectionStatus.active:
        return await _terminal(
            session,
            target,
            JobStatus.skipped,
            reasons.CONNECTION_DISCONNECTED,
            broadcast_id=broadcast.id,
        )

    if broadcast.status is not BroadcastStatus.sending:
        return await _terminal(
            session,
            target,
            JobStatus.skipped,
            reasons.BROADCAST_INACTIVE,
            broadcast_id=broadcast.id,
        )

    chat = await _load_chat(session, target.chat_id)
    if chat is None:
        return await _terminal(
            session,
            target,
            JobStatus.skipped,
            reasons.DESTINATION_REMOVED,
            broadcast_id=broadcast.id,
        )

    has_media = broadcast.media_kind is not BroadcastMedia.none and broadcast.media_bytes
    if not broadcast.body_text.strip() and not has_media:
        return await _terminal(
            session,
            target,
            JobStatus.skipped,
            reasons.BROADCAST_EMPTY,
            broadcast_id=broadcast.id,
            destination_id=chat.id,
        )

    # --- revalidate authorization; fail closed on uncertainty ---------------
    destination_ref = chat_repo.to_ref(chat)
    access: AccessReport | None
    try:
        access = await adapter.check_destination_access(destination_ref)
    except Exception as exc:  # a check that errors is not a confirmation
        classified = classify_error(exc)
        log.warning(
            "broadcast_destination_check_failed", target_id=str(target.id), code=classified.code
        )
        access = None

    if access is None or not access.allowed:
        reason = reasons.DESTINATION_NOT_ELIGIBLE if access is None else access.reason_code
        await chat_repo.set_access(
            session,
            chat=chat,
            can_read_source=bool(chat.access.can_read_source if chat.access else False),
            source_reason_code=(chat.access.source_reason_code if chat.access else reasons.UNKNOWN),
            can_post_destination=False,
            destination_reason_code=reason,
            check_source="pre_delivery",
        )
        return await _terminal(
            session,
            target,
            JobStatus.skipped,
            reason,
            broadcast_id=broadcast.id,
            destination_id=chat.id,
        )

    # --- deliver ------------------------------------------------------------
    try:
        entities = [TextEntity.from_json(e) for e in broadcast.body_entities]
        if has_media:
            receipt = await adapter.send_photo(
                destination_ref,
                bytes(broadcast.media_bytes or b""),
                caption=broadcast.body_text,
                caption_entities=entities,
                filename=broadcast.media_filename or "image.jpg",
                random_id=target.mtproto_random_id,
            )
        else:
            receipt = await adapter.send_text(
                destination_ref,
                broadcast.body_text,
                entities=entities,
                random_id=target.mtproto_random_id,
            )
    except AmbiguousDeliveryError:
        # No idempotency token on this provider: refuse to guess. Posting the
        # same ad twice into a group is worse than one visible gap.
        await broadcast_repo.finish(
            session,
            target=target,
            status=JobStatus.needs_attention,
            error_class=ErrorClass.UNKNOWN.value,
            error_code=reasons.AMBIGUOUS_TIMEOUT,
        )
        await event_repo.record(
            session,
            broadcast_id=broadcast.id,
            connection_id=connection.id,
            outcome=EventOutcome.failed,
            reason_code=reasons.AMBIGUOUS_TIMEOUT,
            destination_chat_id=chat.id,
            attempt=target.attempt_count,
        )
        return DeliveryOutcome(JobStatus.needs_attention, reasons.AMBIGUOUS_TIMEOUT)
    except Exception as exc:
        return await _handle_failure(
            session,
            target=target,
            broadcast=broadcast,
            destination_id=chat.id,
            exc=exc,
            connection=connection,
            max_attempts=settings.max_attempts,
        )

    await broadcast_repo.finish(
        session,
        target=target,
        status=JobStatus.succeeded,
        destination_message_id=receipt.destination_message_id,
    )
    await event_repo.record(
        session,
        broadcast_id=broadcast.id,
        connection_id=connection.id,
        outcome=EventOutcome.forwarded,
        reason_code=reasons.BROADCAST_POSTED,
        destination_chat_id=chat.id,
        attempt=target.attempt_count,
    )
    connection.consecutive_failure_count = 0
    await settle(session, broadcast=broadcast)
    return DeliveryOutcome(JobStatus.succeeded, reasons.BROADCAST_POSTED)


async def settle(session: AsyncSession, *, broadcast: Broadcast) -> bool:
    """Mark the broadcast complete once nothing is left to deliver."""
    if broadcast.status is not BroadcastStatus.sending:
        return False
    remaining = await broadcast_repo.remaining_count(session, broadcast_id=broadcast.id)
    if remaining:
        return False
    broadcast.status = BroadcastStatus.completed
    broadcast.completed_at = datetime.now(UTC)
    return True


async def _load_chat(session: AsyncSession, chat_id: uuid.UUID) -> TelegramChat | None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(TelegramChat)
        .where(TelegramChat.id == chat_id)
        .options(selectinload(TelegramChat.access))
    )
    return result.scalar_one_or_none()


async def _handle_failure(
    session: AsyncSession,
    *,
    target: BroadcastTarget,
    broadcast: Broadcast,
    destination_id: uuid.UUID,
    exc: BaseException,
    connection: TelegramConnection,
    max_attempts: int,
) -> DeliveryOutcome:
    """Same taxonomy as forwarding: retry, skip, or pause — never guess."""
    classified = classify_error(exc)
    settings = get_settings()

    # Authorization failures pause the whole connection immediately.
    if classified.error_class is ErrorClass.AUTH:
        await safety.pause_connection(
            session, connection=connection, reason_code=reasons.AUTH_PAUSE
        )
        await broadcast_repo.finish(
            session,
            target=target,
            status=JobStatus.failed,
            error_class=classified.error_class.value,
            error_code=classified.code,
        )
        await _record(
            session,
            broadcast,
            connection,
            destination_id,
            target,
            classified.code,
            EventOutcome.paused,
        )
        return DeliveryOutcome(JobStatus.failed, classified.code)

    # Permission and permanent-content failures are never retried: "you cannot
    # post in this group" does not become true by asking again.
    if not classified.retryable:
        await broadcast_repo.finish(
            session,
            target=target,
            status=JobStatus.skipped,
            error_class=classified.error_class.value,
            error_code=classified.code,
        )
        await _record(
            session,
            broadcast,
            connection,
            destination_id,
            target,
            classified.code,
            EventOutcome.skipped,
        )
        await settle(session, broadcast=broadcast)
        return DeliveryOutcome(JobStatus.skipped, classified.code)

    # Rate limits: obey Telegram's number exactly. Slow mode lands here too, and
    # is the normal case in a group that has it enabled.
    if classified.error_class is ErrorClass.RATE_LIMIT:
        wait = classified.retry_after_s if classified.retry_after_s is not None else 60.0
        if wait >= settings.flood_wait_pause_threshold_s:
            await pause(session, broadcast=broadcast, reason_code=reasons.FLOOD_WAIT_PAUSE)
            await broadcast_repo.reschedule(
                session,
                target=target,
                delay_s=wait,
                error_class=classified.error_class.value,
                error_code=classified.code,
            )
            await _record(
                session,
                broadcast,
                connection,
                destination_id,
                target,
                reasons.FLOOD_WAIT_PAUSE,
                EventOutcome.paused,
            )
            return DeliveryOutcome(JobStatus.pending, reasons.FLOOD_WAIT_PAUSE, wait)

        await broadcast_repo.reschedule(
            session,
            target=target,
            delay_s=wait,
            error_class=classified.error_class.value,
            error_code=classified.code,
        )
        await _record(
            session,
            broadcast,
            connection,
            destination_id,
            target,
            classified.code,
            EventOutcome.retry_scheduled,
        )
        return DeliveryOutcome(JobStatus.pending, classified.code, wait)

    # TRANSIENT / UNKNOWN: bounded retries, then a visible dead letter.
    if target.attempt_count + 1 >= max_attempts:
        await broadcast_repo.finish(
            session,
            target=target,
            status=JobStatus.dead_letter,
            error_class=classified.error_class.value,
            error_code=classified.code,
        )
        await _record(
            session,
            broadcast,
            connection,
            destination_id,
            target,
            reasons.MAX_ATTEMPTS_EXCEEDED,
            EventOutcome.failed,
        )
        await safety.note_connection_failure(session, connection=connection)
        await settle(session, broadcast=broadcast)
        return DeliveryOutcome(JobStatus.dead_letter, reasons.MAX_ATTEMPTS_EXCEEDED)

    delay = backoff_seconds(target.attempt_count)
    await broadcast_repo.reschedule(
        session,
        target=target,
        delay_s=delay,
        error_class=classified.error_class.value,
        error_code=classified.code,
    )
    await _record(
        session,
        broadcast,
        connection,
        destination_id,
        target,
        reasons.RETRYING,
        EventOutcome.retry_scheduled,
    )
    return DeliveryOutcome(JobStatus.pending, reasons.RETRYING, delay)


async def _record(
    session: AsyncSession,
    broadcast: Broadcast,
    connection: TelegramConnection,
    destination_id: uuid.UUID | None,
    target: BroadcastTarget,
    reason_code: str,
    outcome: EventOutcome,
) -> None:
    await event_repo.record(
        session,
        broadcast_id=broadcast.id,
        connection_id=connection.id,
        outcome=outcome,
        reason_code=reason_code,
        destination_chat_id=destination_id,
        attempt=target.attempt_count,
    )


async def _terminal(
    session: AsyncSession,
    target: BroadcastTarget,
    status: JobStatus,
    reason_code: str,
    *,
    broadcast_id: uuid.UUID | None = None,
    destination_id: uuid.UUID | None = None,
) -> DeliveryOutcome:
    await broadcast_repo.finish(session, target=target, status=status, error_code=reason_code)
    if broadcast_id is not None:
        broadcast = await broadcast_repo.get_unscoped_for_worker(session, broadcast_id=broadcast_id)
        if broadcast is not None:
            await event_repo.record(
                session,
                broadcast_id=broadcast_id,
                connection_id=broadcast.connection_id,
                outcome=EventOutcome.skipped,
                reason_code=reason_code,
                destination_chat_id=destination_id,
                attempt=target.attempt_count,
            )
            await settle(session, broadcast=broadcast)
    return DeliveryOutcome(status, reason_code)
