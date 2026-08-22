from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Request, status

from app.adapters.factory import build_adapter
from app.api.deps import (
    CurrentUser,
    SessionDep,
    client_ip,
    enforce_csrf,
    idempotent,
    rate_limit,
    store_idempotent_result,
)
from app.api.errors import ApiError, not_found
from app.db.models import ConnectionKind, ConnectionStatus, ControlTaskKind
from app.repositories import connections as connection_repo
from app.repositories import events as event_repo
from app.repositories import jobs as job_repo
from app.schemas import (
    AcceptedResponse,
    CapabilitiesResponse,
    ConnectionResponse,
    CreateBotConnectionRequest,
    DisconnectRequest,
    StartUserConnectionRequest,
    TwoFactorRequest,
    VerifyCodeRequest,
)
from app.security.auth import hash_ip
from app.services import connections as connection_service

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/telegram", tags=["connections"], dependencies=[Depends(enforce_csrf)])


def _to_response(connection, *, with_capabilities: bool = False) -> ConnectionResponse:  # type: ignore[no-untyped-def]
    payload = ConnectionResponse.model_validate(connection)
    if with_capabilities:
        caps = build_adapter(connection).capabilities()
        payload.capabilities = CapabilitiesResponse(
            can_read_subscribed_channels=caps.can_read_subscribed_channels,
            can_read_group_messages=caps.can_read_group_messages,
            can_read_history=caps.can_read_history,
            max_download_bytes=caps.max_download_bytes,
            notes=caps.notes,
        )
    return payload


async def _load(session, user, connection_id: uuid.UUID):  # type: ignore[no-untyped-def]
    connection = await connection_repo.get(session, user_id=user.id, connection_id=connection_id)
    if connection is None:
        raise not_found("connection")
    return connection


@router.get("/connections")
async def list_connections(user: CurrentUser, session: SessionDep) -> list[ConnectionResponse]:
    rows = await connection_repo.list_for_user(session, user_id=user.id)
    return [_to_response(row) for row in rows]


@router.get("/connections/{connection_id}")
async def get_connection(
    connection_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> ConnectionResponse:
    return _to_response(await _load(session, user, connection_id), with_capabilities=True)


@router.post(
    "/connections/bot",
    status_code=status.HTTP_201_CREATED,
    dependencies=[rate_limit("connection_create")],
)
async def create_bot_connection(
    payload: CreateBotConnectionRequest,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> ConnectionResponse:
    key, stored = await idempotent(
        request, session, user, payload={"label": payload.label}, required=True
    )
    if stored is not None:
        return ConnectionResponse.model_validate(stored)

    try:
        connection = await connection_service.create_bot_connection(
            session, user_id=user.id, label=payload.label, bot_token=payload.bot_token
        )
        # Verified immediately so the customer learns straight away whether the
        # token works — this is one getMe call, not a forwarding operation.
        await connection_service.verify_bot_connection(session, connection=connection)
    except connection_service.DuplicateConnectionAttempt as exc:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "connection_attempt_in_progress",
            "Another connection attempt is already in progress. Finish or cancel it first.",
        ) from exc

    await event_repo.audit(
        session,
        user_id=user.id,
        action="connection.create",
        object_type="connection",
        object_id=str(connection.id),
        ip_hash=hash_ip(client_ip(request)),
        payload={"kind": "bot"},
    )
    body = _to_response(connection, with_capabilities=True)
    await store_idempotent_result(
        session, user_id=user.id, key=key, status_code=201, body=body.model_dump()
    )
    return body


@router.post(
    "/connections/user/start",
    status_code=status.HTTP_201_CREATED,
    dependencies=[rate_limit("connection_create")],
)
async def start_user_connection(
    payload: StartUserConnectionRequest,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> ConnectionResponse:
    try:
        connection = await connection_service.start_user_connection(
            session, user_id=user.id, label=payload.label, phone=payload.phone
        )
    except connection_service.DuplicateConnectionAttempt as exc:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "connection_attempt_in_progress",
            "Another connection attempt is already in progress. Finish or cancel it first.",
        ) from exc
    except RuntimeError as exc:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "mtproto_not_configured", str(exc)
        ) from exc

    await event_repo.audit(
        session,
        user_id=user.id,
        action="connection.user_start",
        object_type="connection",
        object_id=str(connection.id),
        ip_hash=hash_ip(client_ip(request)),
    )
    return _to_response(connection, with_capabilities=True)


@router.post("/connections/user/verify")
async def verify_user_code(
    payload: VerifyCodeRequest, user: CurrentUser, session: SessionDep
) -> ConnectionResponse:
    connection = await _load(session, user, payload.connection_id)
    if connection.kind is not ConnectionKind.user:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST, "wrong_connection_kind", "Not a user connection."
        )
    if connection.status is not ConnectionStatus.awaiting_code:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "unexpected_state",
            f"This connection is '{connection.status.value}', not awaiting a login code.",
        )

    try:
        await connection_service.verify_user_code(session, connection=connection, code=payload.code)
    except connection_service.ConnectionNotReady as exc:
        raise ApiError(status.HTTP_409_CONFLICT, "login_not_started", str(exc)) from exc

    await event_repo.audit(
        session,
        user_id=user.id,
        action="connection.user_verify",
        object_type="connection",
        object_id=str(connection.id),
    )
    return _to_response(connection, with_capabilities=True)


@router.post("/connections/{connection_id}/2fa")
async def submit_2fa(
    connection_id: uuid.UUID,
    payload: TwoFactorRequest,
    user: CurrentUser,
    session: SessionDep,
) -> ConnectionResponse:
    connection = await _load(session, user, connection_id)
    if connection.status is not ConnectionStatus.awaiting_2fa:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "unexpected_state",
            "This connection is not awaiting a two-factor password.",
        )

    # payload.password is used once inside the service and never persisted.
    await connection_service.verify_user_2fa(
        session, connection=connection, password=payload.password
    )
    await event_repo.audit(
        session,
        user_id=user.id,
        action="connection.user_2fa",
        object_type="connection",
        object_id=str(connection.id),
    )
    return _to_response(connection, with_capabilities=True)


@router.post(
    "/connections/{connection_id}/sync",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[rate_limit("chat_sync")],
)
async def sync_chats(
    connection_id: uuid.UUID, request: Request, user: CurrentUser, session: SessionDep
) -> AcceptedResponse:
    """202 and returns. The API never waits for a Telegram round trip."""
    connection = await _load(session, user, connection_id)
    key, stored = await idempotent(request, session, user, payload={"id": str(connection_id)})
    if stored is not None:
        return AcceptedResponse(**stored)

    existing = await job_repo.pending_control_for(
        session, user_id=user.id, kind=ControlTaskKind.sync_chats, connection_id=connection.id
    )
    if existing is not None:
        return AcceptedResponse(
            task_id=existing.id, message="A synchronization is already in progress."
        )

    task = await job_repo.enqueue_control(
        session, user_id=user.id, kind=ControlTaskKind.sync_chats, connection_id=connection.id
    )
    await event_repo.audit(
        session,
        user_id=user.id,
        action="connection.sync",
        object_type="connection",
        object_id=str(connection.id),
    )
    body = AcceptedResponse(task_id=task.id, message="Synchronization queued.")
    await store_idempotent_result(
        session, user_id=user.id, key=key, status_code=202, body=body.model_dump()
    )
    return body


@router.post("/connections/{connection_id}/health-check", status_code=status.HTTP_202_ACCEPTED)
async def health_check(
    connection_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> AcceptedResponse:
    connection = await _load(session, user, connection_id)
    task = await job_repo.enqueue_control(
        session, user_id=user.id, kind=ControlTaskKind.health_check, connection_id=connection.id
    )
    return AcceptedResponse(task_id=task.id, message="Health check queued.")


@router.post("/connections/{connection_id}/disconnect", status_code=status.HTTP_202_ACCEPTED)
async def disconnect(
    connection_id: uuid.UUID,
    payload: DisconnectRequest,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> AcceptedResponse:
    connection = await _load(session, user, connection_id)
    key, stored = await idempotent(
        request,
        session,
        user,
        payload={"id": str(connection_id), "revoke": payload.revoke},
        required=True,
    )
    if stored is not None:
        return AcceptedResponse(**stored)

    task = await job_repo.enqueue_control(
        session,
        user_id=user.id,
        kind=ControlTaskKind.disconnect,
        connection_id=connection.id,
        payload={"revoke": payload.revoke},
    )
    await event_repo.audit(
        session,
        user_id=user.id,
        action="connection.disconnect",
        object_type="connection",
        object_id=str(connection.id),
        payload={"revoke": payload.revoke},
    )
    body = AcceptedResponse(
        task_id=task.id,
        message=(
            "Disconnect queued. The Telegram session will be revoked."
            if payload.revoke
            else "Disconnect queued."
        ),
    )
    await store_idempotent_result(
        session, user_id=user.id, key=key, status_code=202, body=body.model_dump()
    )
    return body
