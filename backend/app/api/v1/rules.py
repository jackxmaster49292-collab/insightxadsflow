from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import (
    CurrentUser,
    SessionDep,
    enforce_csrf,
    idempotent,
    rate_limit,
    store_idempotent_result,
)
from app.api.errors import ApiError, not_found
from app.db.models import ForwardingRule, JobStatus
from app.domain import reasons
from app.repositories import chats as chat_repo
from app.repositories import connections as connection_repo
from app.repositories import events as event_repo
from app.repositories import jobs as job_repo
from app.repositories import rules as rule_repo
from app.schemas import (
    AcceptedResponse,
    EventResponse,
    RuleChatSummary,
    RuleListItem,
    RuleResponse,
    RuleWriteRequest,
)
from app.services import rules as rule_service

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/forwarding-rules", tags=["rules"], dependencies=[Depends(enforce_csrf)])


def _summary(chat, *, as_destination: bool) -> RuleChatSummary:  # type: ignore[no-untyped-def]
    access = chat.access
    if as_destination:
        eligible = bool(access and access.can_post_destination)
        reason = access.destination_reason_code if access else reasons.UNKNOWN
    else:
        eligible = bool(access and access.can_read_source)
        reason = access.source_reason_code if access else reasons.UNKNOWN
    return RuleChatSummary(
        id=chat.id,
        title=chat.title,
        peer_id=str(chat.peer_id),
        peer_type=chat.peer_type.value,
        eligible=eligible,
        reason_code=reason,
    )


def _filter_summary(rule: ForwardingRule) -> str:
    parts: list[str] = []
    if rule.media_types:
        parts.append(f"{len(rule.media_types)} media type(s)")
    if rule.keyword_include:
        parts.append(f"{len(rule.keyword_include)} required keyword(s)")
    if rule.keyword_exclude:
        parts.append(f"{len(rule.keyword_exclude)} excluded keyword(s)")
    return ", ".join(parts) if parts else "No filters"


async def _detail(session, rule: ForwardingRule) -> RuleResponse:  # type: ignore[no-untyped-def]
    """Built explicitly rather than via ``model_validate``: the response's
    ``sources``/``destinations`` are chat summaries, not the ORM join rows that
    share those attribute names."""
    sources = await rule_repo.source_chats(session, rule=rule)
    destinations = await rule_repo.destination_chats(session, rule=rule)
    return RuleResponse(
        id=rule.id,
        name=rule.name,
        connection_id=rule.connection_id,
        status=rule.status.value,
        version=rule.version,
        forward_mode=rule.forward_mode.value,
        delay_ms=rule.delay_ms,
        keyword_include=list(rule.keyword_include or []),
        keyword_exclude=list(rule.keyword_exclude or []),
        keyword_match_mode=rule.keyword_match_mode.value,
        media_types=list(rule.media_types or []),
        preserve_links=rule.preserve_links,
        preserve_caption=rule.preserve_caption,
        paused_reason_code=rule.paused_reason_code,
        paused_reason_text=(
            reasons.describe(rule.paused_reason_code) if rule.paused_reason_code else None
        ),
        last_activity_at=rule.last_activity_at,
        created_at=rule.created_at,
        sources=[_summary(c, as_destination=False) for c in sources],
        destinations=[_summary(c, as_destination=True) for c in destinations],
        preview=await rule_service.preview_for(session, rule=rule),
    )


async def _load(session, user, rule_id: uuid.UUID) -> ForwardingRule:  # type: ignore[no-untyped-def]
    rule = await rule_repo.get(session, user_id=user.id, rule_id=rule_id)
    if rule is None:
        raise not_found("forwarding rule")
    return rule


async def _load_connection(session, user, connection_id: uuid.UUID):  # type: ignore[no-untyped-def]
    connection = await connection_repo.get(session, user_id=user.id, connection_id=connection_id)
    if connection is None:
        raise not_found("connection")
    return connection


def _to_input(payload: RuleWriteRequest) -> rule_service.RuleInput:
    return rule_service.RuleInput(
        name=payload.name,
        connection_id=payload.connection_id,
        source_chat_ids=payload.source_chat_ids,
        destination_chat_ids=payload.destination_chat_ids,
        forward_mode=payload.forward_mode,
        delay_ms=payload.delay_ms,
        keyword_include=payload.keyword_include,
        keyword_exclude=payload.keyword_exclude,
        keyword_match_mode=payload.keyword_match_mode,
        media_types=payload.media_types,
        preserve_links=payload.preserve_links,
        preserve_caption=payload.preserve_caption,
        allow_source_as_destination=payload.allow_source_as_destination,
    )


@router.get("")
async def list_rules(user: CurrentUser, session: SessionDep) -> list[RuleListItem]:
    rows = await rule_repo.list_for_user(session, user_id=user.id)
    items: list[RuleListItem] = []
    for rule in rows:
        sources = await rule_repo.source_chats(session, rule=rule)
        items.append(
            RuleListItem(
                id=rule.id,
                name=rule.name,
                status=rule.status.value,
                connection_id=rule.connection_id,
                destination_count=len(rule.destinations),
                source_titles=[c.title for c in sources],
                filter_summary=_filter_summary(rule),
                last_activity_at=rule.last_activity_at,
                paused_reason_text=(
                    reasons.describe(rule.paused_reason_code) if rule.paused_reason_code else None
                ),
            )
        )
    return items


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[rate_limit("rule_create")])
async def create_rule(
    payload: RuleWriteRequest, user: CurrentUser, session: SessionDep
) -> RuleResponse:
    connection = await _load_connection(session, user, payload.connection_id)
    rule = await rule_service.create(
        session, user_id=user.id, connection=connection, payload=_to_input(payload)
    )
    await event_repo.audit(
        session, user_id=user.id, action="rule.create", object_type="rule", object_id=str(rule.id)
    )
    return await _detail(session, rule)


@router.get("/{rule_id}")
async def get_rule(rule_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> RuleResponse:
    return await _detail(session, await _load(session, user, rule_id))


@router.patch("/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID, payload: RuleWriteRequest, user: CurrentUser, session: SessionDep
) -> RuleResponse:
    rule = await _load(session, user, rule_id)
    connection = await _load_connection(session, user, payload.connection_id)
    if connection.id != rule.connection_id:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "connection_change_not_supported",
            "A rule cannot be moved to a different Telegram connection. Create a new rule instead.",
        )
    rule = await rule_service.update(
        session, user_id=user.id, rule=rule, connection=connection, payload=_to_input(payload)
    )
    await event_repo.audit(
        session,
        user_id=user.id,
        action="rule.update",
        object_type="rule",
        object_id=str(rule.id),
        payload={"version": rule.version},
    )
    return await _detail(session, rule)


@router.post(
    "/{rule_id}/activate",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[rate_limit("control")],
)
async def activate_rule(
    rule_id: uuid.UUID, request: Request, user: CurrentUser, session: SessionDep
) -> AcceptedResponse:
    rule = await _load(session, user, rule_id)
    key, stored = await idempotent(
        request, session, user, payload={"id": str(rule_id)}, required=True
    )
    if stored is not None:
        return AcceptedResponse(**stored)

    await rule_service.activate(session, user_id=user.id, rule=rule)
    await event_repo.audit(
        session, user_id=user.id, action="rule.activate", object_type="rule", object_id=str(rule.id)
    )
    body = AcceptedResponse(message="Rule activated. New messages will be forwarded.")
    await store_idempotent_result(
        session, user_id=user.id, key=key, status_code=202, body=body.model_dump()
    )
    return body


@router.post(
    "/{rule_id}/pause", status_code=status.HTTP_202_ACCEPTED, dependencies=[rate_limit("control")]
)
async def pause_rule(
    rule_id: uuid.UUID, request: Request, user: CurrentUser, session: SessionDep
) -> AcceptedResponse:
    rule = await _load(session, user, rule_id)
    key, stored = await idempotent(
        request, session, user, payload={"id": str(rule_id)}, required=True
    )
    if stored is not None:
        return AcceptedResponse(**stored)

    await rule_service.pause(session, rule=rule, reason_code="paused_by_customer")
    await event_repo.audit(
        session, user_id=user.id, action="rule.pause", object_type="rule", object_id=str(rule.id)
    )
    body = AcceptedResponse(message="Rule paused. No new messages will be forwarded.")
    await store_idempotent_result(
        session, user_id=user.id, key=key, status_code=202, body=body.model_dump()
    )
    return body


@router.post(
    "/{rule_id}/resume", status_code=status.HTTP_202_ACCEPTED, dependencies=[rate_limit("control")]
)
async def resume_rule(
    rule_id: uuid.UUID, request: Request, user: CurrentUser, session: SessionDep
) -> AcceptedResponse:
    rule = await _load(session, user, rule_id)
    key, stored = await idempotent(
        request, session, user, payload={"id": str(rule_id)}, required=True
    )
    if stored is not None:
        return AcceptedResponse(**stored)

    await rule_service.resume(session, user_id=user.id, rule=rule)
    await event_repo.audit(
        session, user_id=user.id, action="rule.resume", object_type="rule", object_id=str(rule.id)
    )
    body = AcceptedResponse(message="Rule resumed.")
    await store_idempotent_result(
        session, user_id=user.id, key=key, status_code=202, body=body.model_dump()
    )
    return body


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    rule = await _load(session, user, rule_id)
    await job_repo.cancel_pending_for_rule(session, rule_id=rule.id)
    await event_repo.audit(
        session, user_id=user.id, action="rule.delete", object_type="rule", object_id=str(rule.id)
    )
    await session.delete(rule)


@router.get("/{rule_id}/events")
async def list_rule_events(
    rule_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    outcome: str | None = None,
    destination_chat_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[EventResponse]:
    await _load(session, user, rule_id)
    rows = await event_repo.list_for_rule(
        session,
        user_id=user.id,
        rule_id=rule_id,
        outcome=outcome,
        destination_chat_id=destination_chat_id,
        limit=limit,
        offset=offset,
    )
    return [_event_response(row) for row in rows]


def _event_response(row) -> EventResponse:  # type: ignore[no-untyped-def]
    payload = EventResponse.model_validate(row)
    payload.reason_text = reasons.describe(row.reason_code)
    return payload


@router.post(
    "/{rule_id}/retry-failed",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[rate_limit("control")],
)
async def retry_failed(
    rule_id: uuid.UUID, request: Request, user: CurrentUser, session: SessionDep
) -> AcceptedResponse:
    """Requeues only failed / needs-attention work. Successes are never replayed."""
    rule = await _load(session, user, rule_id)
    key, stored = await idempotent(
        request, session, user, payload={"id": str(rule_id)}, required=True
    )
    if stored is not None:
        return AcceptedResponse(**stored)

    requeued = await job_repo.requeue_failed(session, rule_id=rule.id)
    await event_repo.audit(
        session,
        user_id=user.id,
        action="rule.retry_failed",
        object_type="rule",
        object_id=str(rule.id),
        payload={"requeued": requeued},
    )
    body = AcceptedResponse(message=f"{requeued} failed destination(s) queued for retry.")
    await store_idempotent_result(
        session, user_id=user.id, key=key, status_code=202, body=body.model_dump()
    )
    return body


@router.get("/{rule_id}/jobs")
async def list_rule_jobs(
    rule_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[dict[str, Any]]:
    """Per-destination status, which is what the Rule Detail page renders."""
    await _load(session, user, rule_id)
    rows = await job_repo.get_for_rule(
        session,
        rule_id=rule_id,
        statuses=[
            JobStatus.pending,
            JobStatus.leased,
            JobStatus.failed,
            JobStatus.needs_attention,
            JobStatus.dead_letter,
            JobStatus.succeeded,
            JobStatus.skipped,
        ],
    )
    chats = {
        c.id: c
        for c in await chat_repo.get_many(
            session, user_id=user.id, chat_ids=[r.destination_chat_id for r in rows]
        )
    }
    return [
        {
            "id": str(row.id),
            "destination_chat_id": str(row.destination_chat_id),
            "destination_title": (
                chats[row.destination_chat_id].title
                if row.destination_chat_id in chats
                else "Unknown chat"
            ),
            "status": row.status.value,
            "attempt_count": row.attempt_count,
            "last_error_code": row.last_error_code,
            "last_error_text": (
                reasons.describe(row.last_error_code) if row.last_error_code else None
            ),
            "source_message_ids": [str(i) for i in row.source_message_ids],
            "updated_at": row.updated_at.isoformat(),
        }
        for row in rows
    ]
