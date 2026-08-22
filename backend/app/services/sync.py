"""Chat synchronization and eligibility checks.

Appearing in a discovery list never confers eligibility: source and destination
access are checked explicitly, recorded with a reason code, and revalidated again
immediately before every delivery.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import AccessReport, ChatRef, TelegramAdapter
from app.adapters.errors import classify_error
from app.db.models import ChatKind, TelegramChat, TelegramConnection
from app.domain import reasons
from app.repositories import chats as chat_repo

log = structlog.get_logger(__name__)

#: Chat kinds that can never serve as a forwarding destination in this product.
NON_DESTINATION_KINDS = {ChatKind.private}


@dataclass
class SyncReport:
    discovered: int = 0
    source_eligible: int = 0
    destination_eligible: int = 0
    deactivated: int = 0
    errors: int = 0


async def synchronize(
    session: AsyncSession,
    *,
    connection: TelegramConnection,
    adapter: TelegramAdapter,
) -> SyncReport:
    report = SyncReport()
    seen: set[tuple[str, int]] = set()

    discovered_chats = await adapter.list_available_chats()
    for discovered in discovered_chats:
        chat = await chat_repo.upsert_discovered(
            session, connection_id=connection.id, discovered=discovered
        )
        seen.add((chat.peer_type.value, chat.peer_id))
        report.discovered += 1

        await _check_and_store(session, chat=chat, adapter=adapter, report=report)

    report.deactivated = await chat_repo.deactivate_missing(
        session, connection_id=connection.id, seen=seen
    )
    return report


async def recheck_chat(
    session: AsyncSession, *, chat: TelegramChat, adapter: TelegramAdapter
) -> SyncReport:
    report = SyncReport(discovered=1)
    await _check_and_store(
        session, chat=chat, adapter=adapter, report=report, check_source="explicit_check"
    )
    return report


async def _check_and_store(
    session: AsyncSession,
    *,
    chat: TelegramChat,
    adapter: TelegramAdapter,
    report: SyncReport,
    check_source: str = "sync",
) -> None:
    ref: ChatRef = chat_repo.to_ref(chat)

    try:
        source_report = await adapter.check_source_access(ref)
    except Exception as exc:
        # A check that errors is not a confirmation — fail closed.
        classified = classify_error(exc)
        source_report = AccessReport.denied(classified.code)
        report.errors += 1
        chat.last_error_code = classified.code
        chat.last_error_message_safe = classified.safe_message

    if chat.chat_kind in NON_DESTINATION_KINDS:
        destination_allowed, destination_reason = False, reasons.NOT_A_DESTINATION_TYPE
    else:
        try:
            destination_report = await adapter.check_destination_access(ref)
            destination_allowed = destination_report.allowed
            destination_reason = destination_report.reason_code
        except Exception as exc:
            classified = classify_error(exc)
            destination_allowed, destination_reason = False, classified.code
            report.errors += 1
            chat.last_error_code = classified.code
            chat.last_error_message_safe = classified.safe_message

    # A chat with content protection can never be a source: we refuse to relay
    # from it in both forward and copy mode.
    source_allowed = bool(source_report.allowed) and not chat.has_protected_content
    source_reason = (
        reasons.PROTECTED_CONTENT if chat.has_protected_content else str(source_report.reason_code)
    )

    if source_allowed:
        report.source_eligible += 1
    if destination_allowed:
        report.destination_eligible += 1

    await chat_repo.set_access(
        session,
        chat=chat,
        can_read_source=source_allowed,
        source_reason_code=source_reason,
        can_post_destination=destination_allowed,
        destination_reason_code=destination_reason,
        check_source=check_source,
    )


async def revalidate_for_rule(
    session: AsyncSession,
    *,
    chat_ids: list[uuid.UUID],
    adapter: TelegramAdapter,
    user_id: uuid.UUID,
) -> dict[uuid.UUID, str]:
    """Returns ``{chat_id: reason_code}`` for chats that failed revalidation."""
    failures: dict[uuid.UUID, str] = {}
    chats = await chat_repo.get_many(session, user_id=user_id, chat_ids=chat_ids)
    for chat in chats:
        report = SyncReport()
        await _check_and_store(
            session, chat=chat, adapter=adapter, report=report, check_source="pre_delivery"
        )
        if report.destination_eligible == 0 and chat.access is not None:
            failures[chat.id] = chat.access.destination_reason_code
    return failures
