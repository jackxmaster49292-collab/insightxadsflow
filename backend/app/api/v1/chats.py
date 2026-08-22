from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, SessionDep, enforce_csrf
from app.api.errors import not_found
from app.db.models import ControlTaskKind, TelegramChat
from app.domain import reasons
from app.repositories import chats as chat_repo
from app.repositories import jobs as job_repo
from app.schemas import AcceptedResponse, ChatResponse

router = APIRouter(prefix="/telegram", tags=["chats"])


def to_response(chat: TelegramChat) -> ChatResponse:
    access = chat.access
    payload = ChatResponse.model_validate(chat)
    payload.source_eligible = bool(access and access.can_read_source)
    payload.source_reason_code = access.source_reason_code if access else reasons.UNKNOWN
    payload.source_reason_text = reasons.describe(payload.source_reason_code)
    payload.destination_eligible = bool(access and access.can_post_destination)
    payload.destination_reason_code = access.destination_reason_code if access else reasons.UNKNOWN
    payload.destination_reason_text = reasons.describe(payload.destination_reason_code)
    return payload


@router.get("/chats")
async def list_chats(
    user: CurrentUser,
    session: SessionDep,
    connection_id: uuid.UUID | None = None,
    source_eligible: bool | None = None,
    destination_eligible: bool | None = None,
    type: str | None = Query(default=None, alias="type"),
    is_public: bool | None = None,
    is_active: bool | None = None,
    has_error: bool | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ChatResponse]:
    rows = await chat_repo.list_filtered(
        session,
        user_id=user.id,
        connection_id=connection_id,
        source_eligible=source_eligible,
        destination_eligible=destination_eligible,
        chat_kind=type,
        is_public=is_public,
        is_active=is_active,
        has_error=has_error,
        query=q,
        limit=limit,
        offset=offset,
    )
    return [to_response(row) for row in rows]


@router.get("/chats/{chat_id}")
async def get_chat(chat_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> ChatResponse:
    chat = await chat_repo.get(session, user_id=user.id, chat_id=chat_id)
    if chat is None:
        raise not_found("chat")
    return to_response(chat)


@router.post(
    "/chats/{chat_id}/check-access",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(enforce_csrf)],
)
async def check_access(
    chat_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> AcceptedResponse:
    chat = await chat_repo.get(session, user_id=user.id, chat_id=chat_id)
    if chat is None:
        raise not_found("chat")
    task = await job_repo.enqueue_control(
        session,
        user_id=user.id,
        kind=ControlTaskKind.check_chat_access,
        connection_id=chat.connection_id,
        payload={"chat_id": str(chat.id)},
    )
    return AcceptedResponse(task_id=task.id, message="Access check queued.")
