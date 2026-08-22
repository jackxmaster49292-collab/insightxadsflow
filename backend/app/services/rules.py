"""Forwarding rule creation, validation, and lifecycle.

Validation is where most of the safety boundary is enforced, so each rejection
has a specific code the UI can explain rather than a generic 400.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    ForwardingRule,
    ForwardMode,
    KeywordMatchMode,
    RuleStatus,
    TelegramChat,
    TelegramConnection,
)
from app.domain import reasons
from app.domain.preview import build_preview
from app.repositories import chats as chat_repo
from app.repositories import jobs as job_repo
from app.repositories import rules as rule_repo

log = structlog.get_logger(__name__)


class RuleValidationError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class RuleInput:
    name: str
    connection_id: uuid.UUID
    source_chat_ids: list[uuid.UUID]
    destination_chat_ids: list[uuid.UUID]
    forward_mode: str = "forward"
    delay_ms: int = 0
    keyword_include: list[str] | None = None
    keyword_exclude: list[str] | None = None
    keyword_match_mode: str = "substring"
    media_types: list[str] | None = None
    preserve_links: bool = True
    preserve_caption: bool = True
    allow_source_as_destination: bool = False


async def validate(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    connection: TelegramConnection,
    payload: RuleInput,
) -> tuple[list[TelegramChat], list[TelegramChat]]:
    settings = get_settings()

    if not payload.source_chat_ids:
        raise RuleValidationError("no_sources", "Select at least one source chat.")
    if not payload.destination_chat_ids:
        raise RuleValidationError("no_destinations", "Select at least one destination chat.")
    if len(payload.destination_chat_ids) > settings.max_destinations_per_rule:
        raise RuleValidationError(
            "too_many_destinations",
            f"A rule can target at most {settings.max_destinations_per_rule} destinations. "
            "This is an operational safety control, not a plan limit — raise "
            "MAX_DESTINATIONS_PER_RULE, or split the rule.",
            {"selected": len(payload.destination_chat_ids)},
        )
    if len(payload.source_chat_ids) > settings.max_sources_per_rule:
        raise RuleValidationError(
            "too_many_sources",
            f"A rule can read from at most {settings.max_sources_per_rule} sources.",
            {"selected": len(payload.source_chat_ids)},
        )
    if not 0 <= payload.delay_ms <= settings.max_rule_delay_ms:
        raise RuleValidationError(
            "delay_out_of_bounds",
            f"The delay must be between 0 and {settings.max_rule_delay_ms} milliseconds.",
        )

    # The delay is *per destination*, so it multiplies by their count. Without
    # this, "1 hour" across 500 destinations would schedule the last delivery
    # three weeks out — almost certainly not what was meant.
    spread_s = payload.delay_ms * len(payload.destination_chat_ids) / 1000
    if spread_s > settings.max_rule_spread_s:
        raise RuleValidationError(
            "delay_spread_too_long",
            f"A {payload.delay_ms} ms delay across "
            f"{len(payload.destination_chat_ids)} destinations would take "
            f"{spread_s / 3600:.1f} hours to finish delivering one message. "
            f"The limit is {settings.max_rule_spread_s / 3600:.0f} hours — "
            "lower the delay or split the rule.",
            {"spread_seconds": int(spread_s), "delay_ms": payload.delay_ms},
        )

    overlap = set(payload.source_chat_ids) & set(payload.destination_chat_ids)
    if overlap and not payload.allow_source_as_destination:
        raise RuleValidationError(
            "source_is_also_destination",
            "A chat is listed as both a source and a destination, which would forward "
            "messages back into the same chat. Confirm explicitly if this is intended.",
            {"chat_ids": [str(c) for c in overlap]},
        )

    sources = await chat_repo.get_many(session, user_id=user_id, chat_ids=payload.source_chat_ids)
    destinations = await chat_repo.get_many(
        session, user_id=user_id, chat_ids=payload.destination_chat_ids
    )

    if len(sources) != len(set(payload.source_chat_ids)):
        raise RuleValidationError("unknown_source_chat", "One or more source chats were not found.")
    if len(destinations) != len(set(payload.destination_chat_ids)):
        raise RuleValidationError(
            "unknown_destination_chat", "One or more destination chats were not found."
        )

    for chat in [*sources, *destinations]:
        if chat.connection_id != connection.id:
            raise RuleValidationError(
                "chat_from_other_connection",
                "Every source and destination must belong to the same Telegram connection.",
                {"chat_id": str(chat.id)},
            )

    for chat in sources:
        if chat.has_protected_content:
            # Refused in both forward and copy mode, deliberately.
            raise RuleValidationError(
                "protected_source",
                reasons.describe(reasons.PROTECTED_CONTENT),
                {"chat_id": str(chat.id)},
            )
        if chat.access is None or not chat.access.can_read_source:
            raise RuleValidationError(
                "source_not_eligible",
                "The connection cannot read messages from this chat.",
                {
                    "chat_id": str(chat.id),
                    "reason_code": chat.access.source_reason_code
                    if chat.access
                    else reasons.UNKNOWN,
                },
            )

    for chat in destinations:
        if chat.access is None or not chat.access.can_post_destination:
            raise RuleValidationError(
                "destination_not_eligible",
                "The connection cannot post in this chat.",
                {
                    "chat_id": str(chat.id),
                    "reason_code": (
                        chat.access.destination_reason_code if chat.access else reasons.UNKNOWN
                    ),
                },
            )

    return sources, destinations


async def create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    connection: TelegramConnection,
    payload: RuleInput,
) -> ForwardingRule:
    await validate(session, user_id=user_id, connection=connection, payload=payload)

    rule = ForwardingRule(
        user_id=user_id,
        connection_id=connection.id,
        name=payload.name,
        status=RuleStatus.draft,
        version=1,
        forward_mode=ForwardMode(payload.forward_mode),
        delay_ms=payload.delay_ms,
        keyword_include=payload.keyword_include or [],
        keyword_exclude=payload.keyword_exclude or [],
        keyword_match_mode=KeywordMatchMode(payload.keyword_match_mode),
        media_types=payload.media_types or [],
        preserve_links=payload.preserve_links,
        preserve_caption=payload.preserve_caption,
        max_attempts=get_settings().max_attempts,
    )
    session.add(rule)
    await session.flush()

    await rule_repo.replace_sources(session, rule=rule, chat_ids=payload.source_chat_ids)
    await rule_repo.replace_destinations(session, rule=rule, chat_ids=payload.destination_chat_ids)
    return await rule_repo.reload(session, rule)


async def update(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    rule: ForwardingRule,
    connection: TelegramConnection,
    payload: RuleInput,
) -> ForwardingRule:
    await validate(session, user_id=user_id, connection=connection, payload=payload)

    rule.name = payload.name
    rule.forward_mode = ForwardMode(payload.forward_mode)
    rule.delay_ms = payload.delay_ms
    rule.keyword_include = payload.keyword_include or []
    rule.keyword_exclude = payload.keyword_exclude or []
    rule.keyword_match_mode = KeywordMatchMode(payload.keyword_match_mode)
    rule.media_types = payload.media_types or []
    rule.preserve_links = payload.preserve_links
    rule.preserve_caption = payload.preserve_caption
    # Bumping the version is what lets queued jobs detect that they are stale.
    rule.version += 1

    await rule_repo.replace_sources(session, rule=rule, chat_ids=payload.source_chat_ids)
    await rule_repo.replace_destinations(session, rule=rule, chat_ids=payload.destination_chat_ids)
    return await rule_repo.reload(session, rule)


async def activate(
    session: AsyncSession, *, user_id: uuid.UUID, rule: ForwardingRule
) -> ForwardingRule:
    sources = await rule_repo.source_chats(session, rule=rule)
    destinations = await rule_repo.destination_chats(session, rule=rule)

    eligible_sources = [
        c for c in sources if c.access is not None and c.access.can_read_source and c.is_active
    ]
    eligible_destinations = [
        d
        for d in destinations
        if d.access is not None and d.access.can_post_destination and d.is_active
    ]
    if not eligible_sources or not eligible_destinations:
        raise RuleValidationError(
            "not_activatable",
            "A rule needs at least one eligible source and one eligible destination "
            "before it can be activated.",
        )

    rule.status = RuleStatus.active
    rule.paused_reason_code = None
    return rule


async def pause(session: AsyncSession, *, rule: ForwardingRule, reason_code: str) -> ForwardingRule:
    rule.status = RuleStatus.paused
    rule.paused_reason_code = reason_code
    # No new jobs are created for a paused rule; queued ones stop too.
    await job_repo.cancel_pending_for_rule(session, rule_id=rule.id)
    return rule


async def resume(
    session: AsyncSession, *, user_id: uuid.UUID, rule: ForwardingRule
) -> ForwardingRule:
    return await activate(session, user_id=user_id, rule=rule)


async def preview_for(session: AsyncSession, *, rule: ForwardingRule) -> str:
    sources = await rule_repo.source_chats(session, rule=rule)
    destinations = await rule_repo.destination_chats(session, rule=rule)
    return build_preview(
        source_titles=[c.title for c in sources],
        destination_titles=[c.title for c in destinations],
        forward_mode=rule.forward_mode.value,
        delay_ms=rule.delay_ms,
        has_filters=bool(rule.keyword_include or rule.keyword_exclude or rule.media_types),
    )
