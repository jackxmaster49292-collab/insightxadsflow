from __future__ import annotations

import secrets

import structlog
from fastapi import APIRouter, Request, Response, status
from sqlalchemy import func, select

from app.api.deps import (
    CurrentUser,
    SessionDep,
    clear_session_cookies,
    client_ip,
    rate_limit,
    set_session_cookies,
)
from app.api.errors import ApiError
from app.config import get_settings
from app.db.models import ForwardingRule, RuleStatus, TelegramConnection
from app.db.session import session_scope
from app.repositories import admins as admin_repo
from app.repositories import events as event_repo
from app.repositories import users as user_repo
from app.schemas import LoginRequest, MeResponse, RegisterRequest, TelegramLoginRequest
from app.security import auth
from app.security.miniapp import InitDataError, verify_init_data

log = structlog.get_logger(__name__)
router = APIRouter(tags=["auth"])


@router.post(
    "/auth/register", status_code=status.HTTP_201_CREATED, dependencies=[rate_limit("register")]
)
async def register(
    payload: RegisterRequest, request: Request, response: Response, session: SessionDep
) -> MeResponse:
    if await user_repo.get_by_email(session, payload.email) is not None:
        # Same shape and timing as success would be ideal; at minimum, do not
        # confirm which half of the pair already exists.
        raise ApiError(
            status.HTTP_409_CONFLICT, "registration_failed", "This account could not be created."
        )

    user = await user_repo.create(
        session, email=payload.email, password=payload.password, timezone=payload.timezone
    )
    token, _ = await auth.create_session(
        session,
        user_id=user.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    set_session_cookies(response, token=token, csrf=secrets.token_urlsafe(24))
    await event_repo.audit(
        session, user_id=user.id, action="user.register", object_type="user", object_id=str(user.id)
    )
    return MeResponse(id=user.id, email=user.email, timezone=user.timezone)


@router.post("/auth/login", dependencies=[rate_limit("login")])
async def login(
    payload: LoginRequest, request: Request, response: Response, session: SessionDep
) -> MeResponse:
    user = await user_repo.get_by_email(session, payload.email)
    # Always run a verification so a missing account and a wrong password take
    # comparable time.
    # A Telegram-only admin has no password. Verify against a dummy hash so the
    # timing matches, then reject — never treat "no password" as "any password".
    stored = (user.password_hash if user else None) or (
        "$argon2id$v=19$m=65536,t=3,p=4$" + "A" * 22
    )
    valid = auth.verify_password(stored, payload.password) and bool(user and user.password_hash)
    if user is None or not valid or not user.is_active:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED, "invalid_credentials", "Incorrect email or password."
        )

    if user.password_hash and auth.needs_rehash(user.password_hash):
        user.password_hash = auth.hash_password(payload.password)

    token, _ = await auth.create_session(
        session,
        user_id=user.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    set_session_cookies(response, token=token, csrf=secrets.token_urlsafe(24))
    await event_repo.audit(
        session, user_id=user.id, action="user.login", object_type="user", object_id=str(user.id)
    )
    return MeResponse(id=user.id, email=user.email, timezone=user.timezone)


@router.post("/auth/telegram", dependencies=[rate_limit("login")])
async def telegram_login(
    payload: TelegramLoginRequest, request: Request, response: Response, session: SessionDep
) -> MeResponse:
    """Sign in from inside a Telegram Mini App.

    Telegram signs the launch payload with a key derived from the bot token, so
    this is a cryptographic proof of identity rather than a claim from the
    client. There is no password involved and none is created.
    """
    settings = get_settings()
    try:
        identity = verify_init_data(
            payload.init_data,
            bot_token=settings.admin_bot_token or "",
            max_age_s=settings.miniapp_max_age_s,
        )
    except InitDataError as exc:
        # The reason is logged, never returned: a precise message would help an
        # attacker tune a forgery.
        log.warning("miniapp_auth_rejected", reason=str(exc))
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_init_data",
            "This Telegram session could not be verified. Reopen the panel from the bot.",
        ) from exc

    # Verified identity is still not authorization. The allowlist decides, and it
    # is empty by default so a misconfigured deploy is locked, not open.
    if not settings.is_admin(identity.telegram_user_id):
        # Written in its own transaction: raising below rolls back the request
        # session, and a denied-access record must survive the rejection that
        # caused it.
        async with session_scope() as audit_session:
            await event_repo.audit(
                audit_session,
                user_id=None,
                action="admin.access_denied",
                object_type="telegram_user",
                object_id=str(identity.telegram_user_id),
                ip_hash=auth.hash_ip(client_ip(request)),
            )
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "not_an_admin",
            "This Telegram account is not authorized to use the control panel.",
        )

    user = await admin_repo.upsert_admin(
        session,
        telegram_user_id=identity.telegram_user_id,
        username=identity.username,
    )
    token, _ = await auth.create_session(
        session,
        user_id=user.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    set_session_cookies(response, token=token, csrf=secrets.token_urlsafe(24))
    await event_repo.audit(
        session,
        user_id=user.id,
        action="admin.telegram_login",
        object_type="user",
        object_id=str(user.id),
    )
    return MeResponse(id=user.id, email=user.email, timezone=user.timezone)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: SessionDep) -> Response:
    token = request.cookies.get(get_settings().cookie_name)
    if token:
        await auth.revoke_session(session, token)
    clear_session_cookies(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/auth/revoke-all", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_all(user: CurrentUser, response: Response, session: SessionDep) -> Response:
    await auth.revoke_all_sessions(session, user.id)
    await event_repo.audit(
        session, user_id=user.id, action="user.revoke_all_sessions", object_type="user"
    )
    clear_session_cookies(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me")
async def me(user: CurrentUser, session: SessionDep) -> MeResponse:
    connections = await session.execute(
        select(func.count())
        .select_from(TelegramConnection)
        .where(TelegramConnection.user_id == user.id)
    )
    active_rules = await session.execute(
        select(func.count())
        .select_from(ForwardingRule)
        .where(ForwardingRule.user_id == user.id, ForwardingRule.status == RuleStatus.active)
    )
    return MeResponse(
        id=user.id,
        email=user.email,
        timezone=user.timezone,
        connection_count=int(connections.scalar_one()),
        active_rule_count=int(active_rules.scalar_one()),
    )
