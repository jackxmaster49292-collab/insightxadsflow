"""Telegram control-panel handlers.

Every handler runs behind :class:`~app.adminbot.auth.AdminOnlyMiddleware`, and
receives the resolved ``user_id`` in ``data``. Handlers use the same
``user_id``-scoped repositories as the HTTP API, so a bot handler cannot reach
another account's data even if the allowlist were somehow bypassed — the
isolation is structural in both surfaces.

Control commands enqueue durable work exactly like the API does; nothing here
blocks on Telegram I/O.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.adminbot import views
from app.config import get_settings
from app.db.models import ControlTaskKind, JobStatus
from app.db.session import session_scope
from app.repositories import connections as connection_repo
from app.repositories import events as event_repo
from app.repositories import jobs as job_repo
from app.repositories import rules as rule_repo
from app.services import rules as rule_service

log = structlog.get_logger(__name__)
router = Router(name="adminbot")

PARSE_MODE = "MarkdownV2"


async def _render(target: Message | CallbackQuery, screen: views.Screen) -> None:
    """Edit the existing panel message instead of sending a new one.

    Telegram rejects an edit that would not change anything, which is a normal
    outcome when someone taps Refresh twice — that specific error is ignored.
    """
    if isinstance(target, CallbackQuery):
        message = target.message
        # Telegram hands back an InaccessibleMessage for anything older than
        # ~48 hours, and that type cannot be edited. Send a fresh panel instead
        # of letting an old message break the button.
        if not isinstance(message, Message):
            if target.from_user and target.bot:
                await target.bot.send_message(
                    target.from_user.id,
                    screen.text,
                    reply_markup=screen.keyboard,
                    parse_mode=PARSE_MODE,
                )
            return
        try:
            await message.edit_text(
                screen.text, reply_markup=screen.keyboard, parse_mode=PARSE_MODE
            )
        except TelegramBadRequest as exc:
            # Tapping Refresh twice produces an identical message; Telegram
            # rejects that edit and it is not an error worth surfacing.
            if "message is not modified" not in str(exc):
                raise
    else:
        await target.answer(screen.text, reply_markup=screen.keyboard, parse_mode=PARSE_MODE)


async def _home_screen(user_id: uuid.UUID) -> views.Screen:
    settings = get_settings()
    async with session_scope() as session:
        connections = await connection_repo.list_for_user(session, user_id=user_id)
        rules = await rule_repo.list_for_user(session, user_id=user_id)
        counts = await event_repo.summary(session, user_id=user_id, period_hours=24)
    return views.home(
        connections=connections,
        rules=rules,
        counts=counts,
        miniapp_url=settings.miniapp_url,
    )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def start(message: Message, user_id: uuid.UUID, **_extra: Any) -> None:
    await _render(message, await _home_screen(user_id))


@router.message(Command("panel", "home", "status"))
async def panel(message: Message, user_id: uuid.UUID, **_extra: Any) -> None:
    await _render(message, await _home_screen(user_id))


@router.message(Command("rules"))
async def rules_command(message: Message, user_id: uuid.UUID, **_extra: Any) -> None:
    settings = get_settings()
    async with session_scope() as session:
        rules = await rule_repo.list_for_user(session, user_id=user_id)
    await _render(message, views.rules_list(rules=rules, page=0, miniapp_url=settings.miniapp_url))


@router.message(Command("help"))
async def help_command(message: Message, **_extra: Any) -> None:
    await message.answer(
        "*Insight Store — control panel*\n\n"
        "/panel — open the panel\n"
        "/rules — list forwarding rules\n"
        "/help — this message\n\n"
        "Connecting a bot or an account, and creating or editing rules, happens in "
        "the full panel \\(the ⚙️ button\\)\\. Credentials go over HTTPS there and are "
        "never typed into this chat\\.",
        parse_mode=PARSE_MODE,
    )


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "nav:home")
async def nav_home(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    await _render(query, await _home_screen(user_id))
    await query.answer()


@router.callback_query(F.data.startswith("nav:rules"))
async def nav_rules(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    settings = get_settings()
    # "nav:rules:2" — the page is the third segment.
    parts = (query.data or "").split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    async with session_scope() as session:
        rules = await rule_repo.list_for_user(session, user_id=user_id)
    await _render(query, views.rules_list(rules=rules, page=page, miniapp_url=settings.miniapp_url))
    await query.answer()


@router.callback_query(F.data == "nav:conns")
async def nav_connections(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    settings = get_settings()
    async with session_scope() as session:
        connections = await connection_repo.list_for_user(session, user_id=user_id)
    await _render(
        query, views.connections_list(connections=connections, miniapp_url=settings.miniapp_url)
    )
    await query.answer()


@router.callback_query(F.data.startswith("nav:chats"))
async def nav_chats(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    from app.repositories import chats as chat_repo

    parts = (query.data or "").split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    async with session_scope() as session:
        chats = await chat_repo.list_filtered(session, user_id=user_id, limit=200)
    await _render(query, views.chats_list(chats=chats, page=page))
    await query.answer()


@router.callback_query(F.data == "nav:activity")
async def nav_activity(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    async with session_scope() as session:
        events = await event_repo.list_recent_for_user(session, user_id=user_id, limit=12)
    await _render(query, views.activity(events=events))
    await query.answer()


# --------------------------------------------------------------------------- #
# Rule actions
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith("rule:"))
async def rule_actions(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    settings = get_settings()
    _kind, ident, action = views.parse_callback(query.data or "")
    rule_id = views.as_uuid(ident)
    if rule_id is None:
        await query.answer("Unknown rule.", show_alert=True)
        return

    async with session_scope() as session:
        # user_id-scoped: another admin's rule simply does not resolve.
        rule = await rule_repo.get(session, user_id=user_id, rule_id=rule_id)
        if rule is None:
            await query.answer("That rule no longer exists.", show_alert=True)
            return

        notice: str | None = None
        if action == "pause":
            await rule_service.pause(session, rule=rule, reason_code="paused_by_customer")
            await event_repo.audit(
                session,
                user_id=user_id,
                action="rule.pause",
                object_type="rule",
                object_id=str(rule.id),
                payload={"via": "telegram"},
            )
            notice = "Rule paused."
        elif action == "resume":
            try:
                await rule_service.resume(session, user_id=user_id, rule=rule)
                notice = "Rule resumed."
            except rule_service.RuleValidationError as exc:
                notice = exc.message
            else:
                await event_repo.audit(
                    session,
                    user_id=user_id,
                    action="rule.resume",
                    object_type="rule",
                    object_id=str(rule.id),
                    payload={"via": "telegram"},
                )
        elif action == "retry":
            requeued = await job_repo.requeue_failed(session, rule_id=rule.id)
            await event_repo.audit(
                session,
                user_id=user_id,
                action="rule.retry_failed",
                object_type="rule",
                object_id=str(rule.id),
                payload={"via": "telegram", "requeued": requeued},
            )
            notice = f"{requeued} failed destination(s) queued for retry."
        elif action == "events":
            events = await event_repo.list_for_rule(
                session, user_id=user_id, rule_id=rule.id, limit=12
            )
            await _render(query, views.activity(events=events, back=f"rule:{rule.id}"))
            await query.answer()
            return

        sources = await rule_repo.source_chats(session, rule=rule)
        destinations = await rule_repo.destination_chats(session, rule=rule)
        jobs = await job_repo.get_for_rule(
            session,
            rule_id=rule.id,
            statuses=[
                JobStatus.pending,
                JobStatus.succeeded,
                JobStatus.failed,
                JobStatus.skipped,
                JobStatus.needs_attention,
                JobStatus.dead_letter,
            ],
        )
        preview = await rule_service.preview_for(session, rule=rule)
        screen = views.rule_detail(
            rule=rule,
            source_titles=[c.title for c in sources],
            destination_count=len(destinations),
            job_counts=views.job_counts([j.status for j in jobs]),
            preview=preview,
            miniapp_url=settings.miniapp_url,
        )

    await _render(query, screen)
    await query.answer(notice or "")


# --------------------------------------------------------------------------- #
# Connection actions
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith("conn:"))
async def connection_actions(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    settings = get_settings()
    _kind, ident, action = views.parse_callback(query.data or "")
    connection_id = views.as_uuid(ident)
    if connection_id is None:
        await query.answer("Unknown connection.", show_alert=True)
        return

    async with session_scope() as session:
        connection = await connection_repo.get(
            session, user_id=user_id, connection_id=connection_id
        )
        if connection is None:
            await query.answer("That connection no longer exists.", show_alert=True)
            return

        notice = ""
        if action == "sync":
            existing = await job_repo.pending_control_for(
                session,
                user_id=user_id,
                kind=ControlTaskKind.sync_chats,
                connection_id=connection.id,
            )
            if existing is not None:
                notice = "A synchronization is already running."
            else:
                # Queued, not executed here: the bot never blocks on Telegram.
                await job_repo.enqueue_control(
                    session,
                    user_id=user_id,
                    kind=ControlTaskKind.sync_chats,
                    connection_id=connection.id,
                )
                notice = "Synchronization queued."
            await event_repo.audit(
                session,
                user_id=user_id,
                action="connection.sync",
                object_type="connection",
                object_id=str(connection.id),
                payload={"via": "telegram"},
            )

        connections = await connection_repo.list_for_user(session, user_id=user_id)
        screen = views.connections_list(connections=connections, miniapp_url=settings.miniapp_url)

    await _render(query, screen)
    await query.answer(notice)


@router.callback_query()
async def unknown_callback(query: CallbackQuery, **_extra: Any) -> None:
    await query.answer("That button is no longer valid. Tap /panel to reopen.", show_alert=True)
