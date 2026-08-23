"""Telegram control-panel handlers.

The bot *is* the admin surface: connecting an account, composing an ad, writing
an auto-reply and managing forwarding rules all happen here. There is no web
panel to fall back to.

Every handler runs behind :class:`~app.adminbot.auth.AdminOnlyMiddleware` and
receives the resolved ``user_id`` in ``data``. Handlers use the same
``user_id``-scoped repositories as the HTTP API, so a bot handler cannot reach
another account's data even if the allowlist were somehow bypassed — the
isolation is structural on both surfaces.

Control commands enqueue durable work exactly as the API does; nothing here
blocks on Telegram I/O.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.adminbot import secrets, views
from app.adminbot.states import (
    ComposeAd,
    ComposeRule,
    ConnectAccount,
    ConnectBot,
    EditAutoReply,
)
from app.config import get_settings
from app.db.models import (
    BroadcastMedia,
    BroadcastStatus,
    ConnectionStatus,
    ControlTaskKind,
    JobStatus,
)
from app.db.session import session_scope
from app.repositories import autoreply as autoreply_repo
from app.repositories import broadcasts as broadcast_repo
from app.repositories import chats as chat_repo
from app.repositories import connections as connection_repo
from app.repositories import events as event_repo
from app.repositories import jobs as job_repo
from app.repositories import rules as rule_repo
from app.repositories import users as user_repo
from app.services import broadcast as broadcast_service
from app.services import connections as connection_service
from app.services import rules as rule_service
from app.services import users as user_service

log = structlog.get_logger(__name__)
router = Router(name="adminbot")

PARSE_MODE = "MarkdownV2"

#: Which flow the group picker should return to, kept in FSM data so one picker
#: implementation serves both ads and rules.
PICK_TARGET = "pick_target"

PICK_HINT = "Tap to select. Only groups this account can post in are listed."

#: Rules can also target a channel, so the hint differs.
PICK_HINT_RULE = "Tap to select. Only chats this account can post in are listed."


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
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


async def _ask(message: Message, text: str) -> None:
    """Prompt for the next step of a flow, as a fresh message."""
    await message.answer(text, parse_mode=PARSE_MODE)


async def _send(message: Message, screen: views.Screen) -> None:
    await message.answer(screen.text, reply_markup=screen.keyboard, parse_mode=PARSE_MODE)


async def _home_screen(user_id: uuid.UUID, *, is_operator: bool = False) -> views.Screen:
    async with session_scope() as session:
        connections = await connection_repo.list_for_user(session, user_id=user_id)
        rules = await rule_repo.list_for_user(session, user_id=user_id)
        broadcasts = await broadcast_repo.list_for_user(session, user_id=user_id)
        counts = await event_repo.summary(session, user_id=user_id, period_hours=24)
    return views.home(
        connections=connections,
        rules=rules,
        broadcasts=broadcasts,
        counts=counts,
        is_operator=is_operator,
    )


async def _go_home(
    target: Message | CallbackQuery, user_id: uuid.UUID, *, is_operator: bool = False
) -> None:
    screen = await _home_screen(user_id, is_operator=is_operator)
    if isinstance(target, Message):
        await _send(target, screen)
    else:
        await _render(target, screen)


async def _require_operator(query: CallbackQuery, is_operator: bool) -> bool:
    """Second gate on operator-only screens.

    The button is hidden for everyone else, but hiding a control is
    presentation, not authorization — a callback can be replayed by anyone who
    has seen it.
    """
    if is_operator:
        return True
    await query.answer("That is not available on your account.", show_alert=True)
    return False


async def _active_connection(session, user_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """The connection ads and auto-reply use.

    A single choice rather than a picker: the customer's mental model is one
    account that posts and answers, and tying the two features to the same
    connection is the point — an ad brings people to that account, and the
    auto-reply answers them there.
    """
    connections = await connection_repo.list_for_user(session, user_id=user_id)
    active = [c for c in connections if c.status is ConnectionStatus.active]
    return active[0] if active else (connections[0] if connections else None)


def _entities_of(message: Message) -> list[dict[str, Any]]:
    """The formatting Telegram attached to what the customer typed.

    Taken as data rather than re-parsed from the text. Telegram already knows
    where the bold starts and which emoji is a premium one; re-deriving that
    from Markdown would both lose the custom emoji and corrupt any message
    containing a literal asterisk.
    """
    entities = message.entities or message.caption_entities or []
    return [
        {
            "type": entity.type,
            "offset": entity.offset,
            "length": entity.length,
            **({"url": entity.url} if entity.url else {}),
            **({"custom_emoji_id": entity.custom_emoji_id} if entity.custom_emoji_id else {}),
            **({"language": entity.language} if entity.language else {}),
        }
        for entity in entities
    ]


def _describe(exc: BaseException) -> str:
    """Turn a provider exception into one escaped, customer-facing sentence."""
    from app.adapters.errors import classify_error
    from app.domain import reasons

    return views.escape(reasons.describe(classify_error(exc).code))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def start(
    message: Message,
    user_id: uuid.UUID,
    state: FSMContext,
    is_operator: bool = False,
    **_extra: Any,
) -> None:
    await state.clear()
    await _go_home(message, user_id, is_operator=is_operator)


@router.message(Command("panel", "home", "status"))
async def panel(
    message: Message,
    user_id: uuid.UUID,
    state: FSMContext,
    is_operator: bool = False,
    **_extra: Any,
) -> None:
    await state.clear()
    await _go_home(message, user_id, is_operator=is_operator)


@router.callback_query(F.data == "terms:accept")
async def accept_terms(
    query: CallbackQuery, user_id: uuid.UUID, is_operator: bool = False, **_extra: Any
) -> None:
    async with session_scope() as session:
        user = await user_repo.get_by_id(session, user_id)
        if user is None:
            await query.answer("Send /start to begin.", show_alert=True)
            return
        await user_service.accept_terms(session, user=user)
        await event_repo.audit(
            session,
            user_id=user_id,
            action="user.accept_terms",
            object_type="user",
            object_id=str(user_id),
        )

    await _render(query, await _home_screen(user_id, is_operator=is_operator))
    await query.answer("Welcome.")


@router.message(Command("cancel"))
async def cancel_flow(
    message: Message,
    user_id: uuid.UUID,
    state: FSMContext,
    is_operator: bool = False,
    **_extra: Any,
) -> None:
    await state.clear()
    await _go_home(message, user_id, is_operator=is_operator)


@router.message(Command("ads"))
async def ads_command(message: Message, user_id: uuid.UUID, **_extra: Any) -> None:
    await _send(message, await _ads_screen(user_id, page=0))


@router.message(Command("rules"))
async def rules_command(message: Message, user_id: uuid.UUID, **_extra: Any) -> None:
    await _send(message, await _rules_screen(user_id, page=0))


@router.message(Command("help"))
async def help_command(message: Message, **_extra: Any) -> None:
    await message.answer(
        "*InsightAdFlow*\n\n"
        "/panel — open the control panel\n"
        "/ads — your ads\n"
        "/rules — forwarding rules\n"
        "/cancel — abandon whatever you are in the middle of\n"
        "/help — this message\n\n"
        "*What it does*\n"
        "• *Ads* — write your own message and post it to groups you choose\n"
        "• *Auto\\-reply* — answer people who message your account first\n"
        "• *Forwarding* — copy new messages from one chat into others\n\n"
        "Everything posts only to groups your connected account has already "
        "joined\\. Nothing here joins groups, collects members, or messages "
        "people who have not written to you\\.",
        parse_mode=PARSE_MODE,
    )


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "noop")
async def noop(query: CallbackQuery, **_extra: Any) -> None:
    """The page indicator is a button because Telegram has no plain label."""
    await query.answer()


@router.callback_query(F.data == "nav:home")
async def nav_home(
    query: CallbackQuery,
    user_id: uuid.UUID,
    state: FSMContext,
    is_operator: bool = False,
    **_extra: Any,
) -> None:
    await state.clear()
    await _render(query, await _home_screen(user_id, is_operator=is_operator))
    await query.answer()


def _page_from(data: str, index: int = 2) -> int:
    parts = data.split(":")
    return int(parts[index]) if len(parts) > index and parts[index].isdigit() else 0


@router.callback_query(F.data.startswith("nav:rules"))
async def nav_rules(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    await _render(query, await _rules_screen(user_id, page=_page_from(query.data or "")))
    await query.answer()


@router.callback_query(F.data.startswith("nav:ads"))
async def nav_ads(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    await _render(query, await _ads_screen(user_id, page=_page_from(query.data or "")))
    await query.answer()


@router.callback_query(F.data == "nav:conns")
async def nav_connections(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    async with session_scope() as session:
        connections = await connection_repo.list_for_user(session, user_id=user_id)
    await _render(query, views.connections_list(connections=connections))
    await query.answer()


@router.callback_query(F.data.startswith("nav:chats"))
async def nav_chats(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    async with session_scope() as session:
        everything = await chat_repo.list_filtered(session, user_id=user_id, limit=1000)
    groups = [c for c in everything if c.chat_kind.value in AD_CHAT_KINDS]
    await _render(
        query,
        views.chats_list(
            chats=groups,
            page=_page_from(query.data or ""),
            other_count=len(everything) - len(groups),
        ),
    )
    await query.answer()


@router.callback_query(F.data == "nav:activity")
async def nav_activity(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    async with session_scope() as session:
        events = await event_repo.list_recent_for_user(session, user_id=user_id, limit=12)
    await _render(query, views.activity(events=events))
    await query.answer()


async def _ads_screen(user_id: uuid.UUID, *, page: int) -> views.Screen:
    async with session_scope() as session:
        broadcasts = await broadcast_repo.list_for_user(session, user_id=user_id)
        connection = await _active_connection(session, user_id)
    return views.ads_list(broadcasts=broadcasts, page=page, can_create=connection is not None)


async def _rules_screen(user_id: uuid.UUID, *, page: int) -> views.Screen:
    async with session_scope() as session:
        rules = await rule_repo.list_for_user(session, user_id=user_id)
        connection = await _active_connection(session, user_id)
    return views.rules_list(rules=rules, page=page, can_create=connection is not None)


# --------------------------------------------------------------------------- #
# Connecting a bot
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "add:bot")
async def add_bot(query: CallbackQuery, state: FSMContext, **_extra: Any) -> None:
    await state.clear()
    await state.set_state(ConnectBot.label)
    if isinstance(query.message, Message):
        await _ask(
            query.message,
            "🤖 *Add a bot*\n\nWhat should I call it? A short name for your own "
            "reference, like `Sales bot`\\.\n\n/cancel to stop\\.",
        )
    await query.answer()


@router.message(ConnectBot.label)
async def connect_bot_label(message: Message, state: FSMContext, **_extra: Any) -> None:
    label = (message.text or "").strip()
    if not label:
        await _ask(message, "Send a name, or /cancel\\.")
        return
    await state.update_data(label=label[:120])
    await state.set_state(ConnectBot.token)
    await _ask(
        message,
        "Now send the bot token from @BotFather\\.\n\n"
        f"{secrets.WARNING}\n\n"
        "It must be a *different* bot from this control panel — Telegram allows "
        "only one program to receive a given bot's updates, and two would fight\\.",
    )


@router.message(ConnectBot.token)
async def connect_bot_token(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    # Read and delete in one step; the value never reaches state or a log.
    token = await secrets.consume(message)
    data = await state.get_data()
    await state.clear()

    if ":" not in token or len(token) < 20:
        await _ask(
            message,
            "That does not look like a bot token — they look like "
            "`1234567890:AA…`\\. Tap *Add bot* to try again\\.",
        )
        await _go_home(message, user_id)
        return

    async with session_scope() as session:
        try:
            connection = await connection_service.create_bot_connection(
                session, user_id=user_id, label=data.get("label", "Bot"), bot_token=token
            )
        except connection_service.DuplicateConnectionAttempt:
            await _ask(
                message,
                "Another connection attempt is already in progress\\. Finish or cancel it first\\.",
            )
            await _go_home(message, user_id)
            return
        except connection_service.TooManyConnections as exc:
            await _ask(message, views.escape(exc.message))
            await _go_home(message, user_id)
            return

        try:
            await connection_service.verify_bot_connection(session, connection=connection)
        except Exception as exc:
            connection.status = ConnectionStatus.error
            await _ask(message, f"Telegram rejected that token\\.\n\n_{_describe(exc)}_")
            await _go_home(message, user_id)
            return

        await event_repo.audit(
            session,
            user_id=user_id,
            action="connection.create",
            object_type="connection",
            object_id=str(connection.id),
            payload={"via": "telegram", "kind": "bot"},
        )

    await _ask(
        message,
        "✅ Bot connected\\.\n\nNext: open *Accounts*, pick it, and tap "
        "*Sync groups* so I can see where it is allowed to post\\.",
    )
    await _go_home(message, user_id)


# --------------------------------------------------------------------------- #
# Connecting an account (phone → code → 2FA)
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "add:user")
async def add_account(query: CallbackQuery, state: FSMContext, **_extra: Any) -> None:
    """Phone, then the code Telegram sends, then 2FA if the account has it.

    The limit is stated before anything is typed rather than after a code has
    been burned: Telegram cancels any login code it sees an account send inside
    a chat, so this cannot connect the account that is driving the bot.
    """
    await state.clear()
    await state.set_state(ConnectAccount.label)
    if isinstance(query.message, Message):
        await _ask(
            query.message,
            "👤 *Add a Telegram account*\n\n"
            "⚠️ *Read this first\\.* Telegram cancels any login "
            "code it sees an account send inside a chat\\. So this works only "
            "when the account you are connecting is *not* the one you are "
            "messaging me from\\.\n\n"
            "Connecting your own account this way will fail with *the code was "
            "previously shared*, however carefully you type it\\.\n\n"
            "What should I call this connection? A short name for your own "
            "reference, like `Sales account`\\.\n\n/cancel to stop\\.",
        )
    await query.answer()


@router.message(ConnectAccount.label)
async def account_label(message: Message, state: FSMContext, **_extra: Any) -> None:
    label = (message.text or "").strip()
    if not label:
        await _ask(message, "Send a name, or /cancel\\.")
        return
    await state.update_data(label=label[:120])
    await state.set_state(ConnectAccount.phone)
    await _ask(
        message,
        "Send the phone number of that Telegram account, with the country "
        "code:\n`\\+919876543210`\n\n" + secrets.WARNING,
    )


@router.message(ConnectAccount.phone)
async def account_phone(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    phone = await secrets.consume(message)
    normalized = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if not normalized.startswith("+") or len(normalized) < 8:
        await _ask(
            message,
            "That does not look like a phone number\\. Include the country code, "
            "like `\\+919876543210`\\.",
        )
        return

    data = await state.get_data()
    async with session_scope() as session:
        try:
            connection = await connection_service.start_user_connection(
                session, user_id=user_id, label=data.get("label", "Account"), phone=normalized
            )
        except connection_service.DuplicateConnectionAttempt:
            await state.clear()
            await _ask(
                message,
                "Another connection attempt is already in progress\\. Finish or cancel it first\\.",
            )
            await _go_home(message, user_id)
            return
        except connection_service.TooManyConnections as exc:
            await state.clear()
            await _ask(message, views.escape(exc.message))
            await _go_home(message, user_id)
            return
        except RuntimeError as exc:
            # Missing TELEGRAM_API_ID / TELEGRAM_API_HASH says exactly what to do.
            await state.clear()
            await _ask(message, f"Cannot start sign\\-in\\.\n\n_{views.escape(str(exc))}_")
            await _go_home(message, user_id)
            return
        except Exception as exc:
            await state.clear()
            await _ask(message, f"Telegram refused to send the code\\.\n\n_{_describe(exc)}_")
            await _go_home(message, user_id)
            return

        connection_id = connection.id

    await state.update_data(connection_id=str(connection_id))
    await state.set_state(ConnectAccount.code)
    await _ask(
        message,
        "📲 Telegram has sent a login code to that account\\.\n\n"
        "Send it here — but *put a space or a dash between the digits*, like "
        "`1 2 3 4 5` or `1\\-2\\-3\\-4\\-5`\\.\n\n"
        "Telegram cancels a login code it sees posted as plain digits in a chat\\. "
        "That protection is on your side, so work with it rather than around "
        "it\\.\n\n" + secrets.WARNING,
    )


@router.message(ConnectAccount.code)
async def account_code(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    raw = await secrets.consume(message)
    code = "".join(ch for ch in raw if ch.isdigit())
    data = await state.get_data()
    connection_id = views.as_uuid(data.get("connection_id"))
    if connection_id is None:
        await state.clear()
        await _ask(message, "That sign\\-in expired\\. Start again from *Accounts*\\.")
        await _go_home(message, user_id)
        return

    if not code:
        await _ask(message, "Send the digits of the code, or /cancel\\.")
        return

    async with session_scope() as session:
        connection = await connection_repo.get(
            session, user_id=user_id, connection_id=connection_id
        )
        if connection is None:
            await state.clear()
            await _ask(message, "That connection is gone\\. Start again from *Accounts*\\.")
            await _go_home(message, user_id)
            return

        try:
            status = await connection_service.verify_user_code(
                session, connection=connection, code=code
            )
        except connection_service.ConnectionNotReady:
            await state.clear()
            await _ask(
                message,
                "That sign\\-in is no longer in progress — the panel may have "
                "restarted\\. Start again from *Accounts*\\.",
            )
            await _go_home(message, user_id)
            return
        except Exception as exc:
            await _ask(
                message,
                f"That code was not accepted\\.\n\n_{_describe(exc)}_\n\n"
                "Send it again, or /cancel\\.",
            )
            return

    if status is ConnectionStatus.awaiting_2fa:
        await state.set_state(ConnectAccount.password)
        await _ask(
            message,
            "🔐 This account has two\\-step verification\\.\n\nSend the password\\.\n\n"
            + secrets.WARNING
            + "\n\nIt is used once to finish signing in and is never stored\\.",
        )
        return

    await state.clear()
    await _finish_account(message, user_id, connection_id)


@router.message(ConnectAccount.password)
async def account_password(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    password = await secrets.consume(message)
    data = await state.get_data()
    connection_id = views.as_uuid(data.get("connection_id"))
    if connection_id is None or not password:
        await state.clear()
        await _ask(message, "That sign\\-in expired\\. Start again from *Accounts*\\.")
        await _go_home(message, user_id)
        return

    async with session_scope() as session:
        connection = await connection_repo.get(
            session, user_id=user_id, connection_id=connection_id
        )
        if connection is None:
            await state.clear()
            await _ask(message, "That connection is gone\\. Start again from *Accounts*\\.")
            await _go_home(message, user_id)
            return
        try:
            await connection_service.verify_user_2fa(
                session, connection=connection, password=password
            )
        except connection_service.ConnectionNotReady:
            await state.clear()
            await _ask(
                message,
                "That sign\\-in is no longer in progress\\. Start again from *Accounts*\\.",
            )
            await _go_home(message, user_id)
            return
        except Exception as exc:
            await _ask(
                message,
                f"That password was not accepted\\.\n\n_{_describe(exc)}_\n\n"
                "Send it again, or /cancel\\.",
            )
            return

    await state.clear()
    await _finish_account(message, user_id, connection_id)


async def _finish_account(message: Message, user_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Signed in. Queue a group sync so the panel is useful straight away."""
    async with session_scope() as session:
        await job_repo.enqueue_control(
            session,
            user_id=user_id,
            kind=ControlTaskKind.sync_chats,
            connection_id=connection_id,
        )
        await event_repo.audit(
            session,
            user_id=user_id,
            action="connection.create",
            object_type="connection",
            object_id=str(connection_id),
            payload={"via": "telegram", "kind": "user"},
        )

    await _ask(
        message,
        "✅ Account connected\\.\n\nI am reading the groups it has already joined "
        "— that takes a few seconds\\. Then you can post an ad\\.",
    )
    await _go_home(message, user_id)


# --------------------------------------------------------------------------- #
# Connection actions
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith("conn:"))
async def connection_actions(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
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
                notice = "A sync is already running."
            else:
                # Queued, not executed here: the bot never blocks on Telegram.
                await job_repo.enqueue_control(
                    session,
                    user_id=user_id,
                    kind=ControlTaskKind.sync_chats,
                    connection_id=connection.id,
                )
                notice = "Sync queued — check back in a few seconds."
            await event_repo.audit(
                session,
                user_id=user_id,
                action="connection.sync",
                object_type="connection",
                object_id=str(connection.id),
                payload={"via": "telegram"},
            )

        elif action == "health":
            await job_repo.enqueue_control(
                session,
                user_id=user_id,
                kind=ControlTaskKind.health_check,
                connection_id=connection.id,
            )
            notice = "Health check queued."

        elif action == "abandon":
            # No confirmation: there is nothing here to lose, and the customer
            # is usually stuck precisely because this row exists.
            await connection_service.abandon(session, connection=connection)
            connections = await connection_repo.list_for_user(session, user_id=user_id)
            await _render(query, views.connections_list(connections=connections))
            await query.answer("Sign-in cleared. You can start again.")
            return

        elif action == "askdel":
            await _render(query, views.confirm_disconnect(connection=connection))
            await query.answer()
            return

        elif action == "delete":
            await job_repo.enqueue_control(
                session,
                user_id=user_id,
                kind=ControlTaskKind.disconnect,
                connection_id=connection.id,
                payload={"revoke": True},
            )
            await event_repo.audit(
                session,
                user_id=user_id,
                action="connection.disconnect",
                object_type="connection",
                object_id=str(connection.id),
                payload={"via": "telegram"},
            )
            connections = await connection_repo.list_for_user(session, user_id=user_id)
            await _render(query, views.connections_list(connections=connections))
            await query.answer("Disconnecting.")
            return

        chats = await chat_repo.list_filtered(
            session, user_id=user_id, connection_id=connection.id, limit=1000
        )
        running = await job_repo.pending_control_for(
            session,
            user_id=user_id,
            kind=ControlTaskKind.sync_chats,
            connection_id=connection.id,
        )
        screen = views.connection_detail(
            connection=connection, chat_count=len(chats), syncing=running is not None
        )

    await _render(query, screen)
    await query.answer(notice)


# --------------------------------------------------------------------------- #
# Ads
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "ad:new")
async def ad_new(query: CallbackQuery, state: FSMContext, **_extra: Any) -> None:
    await state.clear()
    await state.set_state(ComposeAd.name)
    if isinstance(query.message, Message):
        await _ask(
            query.message,
            "📣 *New ad*\n\nGive it a name for your own reference, like "
            "`October offer`\\.\n\n/cancel to stop\\.",
        )
    await query.answer()


@router.message(ComposeAd.name)
async def ad_name(message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any) -> None:
    name = (message.text or "").strip()
    if not name:
        await _ask(message, "Send a name, or /cancel\\.")
        return

    settings = get_settings()
    async with session_scope() as session:
        connection = await _active_connection(session, user_id)
        if connection is None:
            await state.clear()
            await _ask(message, "Connect an account first\\.")
            await _go_home(message, user_id)
            return
        # One draft at a time: two half-written ads carrying the same buttons
        # would be impossible to tell apart in a chat.
        await broadcast_repo.discard_drafts(session, user_id=user_id)
        broadcast = await broadcast_repo.create(
            session,
            user_id=user_id,
            connection_id=connection.id,
            name=name,
            delay_ms=settings.broadcast_default_delay_ms,
        )
        broadcast_id = broadcast.id

    await state.set_state(ComposeAd.text)
    await state.update_data(broadcast_id=str(broadcast_id))
    await _ask(
        message,
        "Now send the message you want posted — plain text, exactly as it should "
        "appear\\.\n\nYou can add an image afterwards\\.",
    )


@router.message(ComposeAd.text)
async def ad_text(message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any) -> None:
    body = (message.text or message.caption or "").strip()
    if not body:
        await _ask(message, "Send the text of your ad, or /cancel\\.")
        return

    data = await state.get_data()
    broadcast_id = views.as_uuid(data.get("broadcast_id"))
    if broadcast_id is None:
        await state.clear()
        await _ask(message, "That ad is gone\\. Start again from *Ads*\\.")
        await _go_home(message, user_id)
        return

    async with session_scope() as session:
        broadcast = await broadcast_repo.get(session, user_id=user_id, broadcast_id=broadcast_id)
        if broadcast is None:
            await state.clear()
            await _ask(message, "That ad is gone\\. Start again from *Ads*\\.")
            await _go_home(message, user_id)
            return
        broadcast.body_text = body
        broadcast.body_entities = _entities_of(message)

    await state.set_state(None)
    await _send(message, await _compose_screen(user_id, broadcast_id))


@router.message(ComposeAd.media, F.photo)
async def ad_media(message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any) -> None:
    """Store the image *bytes*, not Telegram's file id.

    A ``file_id`` is scoped to the bot that received it, so the one this panel
    sees is meaningless to the account that will do the posting. The bytes are
    fetched once here and re-uploaded on each delivery.
    """
    settings = get_settings()
    data = await state.get_data()
    broadcast_id = views.as_uuid(data.get("broadcast_id"))
    # photo[] is ordered smallest to largest; the last entry is full resolution.
    photo = message.photo[-1] if message.photo else None
    if photo is None or broadcast_id is None or message.bot is None:
        await _ask(message, "Send a photo, or /cancel\\.")
        return

    limit = settings.max_broadcast_media_bytes
    if (photo.file_size or 0) > limit:
        await _ask(
            message,
            f"That image is {(photo.file_size or 0) // 1024} kB and the limit is "
            f"{limit // 1024} kB\\. Send a smaller one\\.",
        )
        return

    buffer = await message.bot.download(photo.file_id)
    raw = buffer.read() if buffer is not None else b""
    if not raw or len(raw) > limit:
        await _ask(message, "That image could not be used\\. Try a smaller one\\.")
        return

    caption = (message.caption or "").strip()

    async with session_scope() as session:
        broadcast = await broadcast_repo.get(session, user_id=user_id, broadcast_id=broadcast_id)
        if broadcast is None:
            await state.clear()
            await _ask(message, "That ad is gone\\. Start again from *Ads*\\.")
            await _go_home(message, user_id)
            return
        broadcast.media_bytes = raw
        broadcast.media_kind = BroadcastMedia.photo
        broadcast.media_filename = "ad.jpg"
        if caption:
            broadcast.body_text = caption
            broadcast.body_entities = _entities_of(message)

    await state.set_state(None)
    await _send(message, await _compose_screen(user_id, broadcast_id))


@router.message(ComposeAd.media)
async def ad_media_wrong_type(message: Message, **_extra: Any) -> None:
    await _ask(
        message,
        "Send it as a *photo*, not as a file\\. Or /cancel to keep the ad text only\\.",
    )


@router.message(ComposeAd.delay)
async def ad_delay(message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any) -> None:
    raw = (message.text or "").strip().rstrip("s")
    try:
        seconds = float(raw)
    except ValueError:
        await _ask(message, "Send a number of seconds, like `3`\\.")
        return
    if not 0 <= seconds <= 3600:
        await _ask(message, "Use a value between 0 and 3600 seconds\\.")
        return

    data = await state.get_data()
    broadcast_id = views.as_uuid(data.get("broadcast_id"))
    if broadcast_id is None:
        await state.clear()
        await _go_home(message, user_id)
        return

    async with session_scope() as session:
        broadcast = await broadcast_repo.get(session, user_id=user_id, broadcast_id=broadcast_id)
        if broadcast is None:
            await state.clear()
            await _go_home(message, user_id)
            return
        broadcast.delay_ms = int(seconds * 1000)

    await state.set_state(None)
    await _send(message, await _compose_screen(user_id, broadcast_id))


async def _compose_screen(user_id: uuid.UUID, broadcast_id: uuid.UUID) -> views.Screen:
    async with session_scope() as session:
        broadcast = await broadcast_repo.get(session, user_id=user_id, broadcast_id=broadcast_id)
        if broadcast is None:
            return await _ads_screen(user_id, page=0)
        target_ids = await broadcast_repo.target_chat_ids(session, broadcast_id=broadcast.id)
        return views.ad_compose(
            broadcast=broadcast,
            target_count=len(target_ids),
            estimate_s=broadcast_service.estimated_duration_s(broadcast.delay_ms, len(target_ids)),
        )


#: Steps that simply ask for the next message, keyed by callback action.
_AD_PROMPTS = {
    "text": (ComposeAd.text, "Send the message you want posted\\."),
    "media": (
        ComposeAd.media,
        "Send the image as a *photo*\\. Any caption you add becomes the ad text\\."
        "\n\n/cancel to keep it text only\\.",
    ),
    "delay": (
        ComposeAd.delay,
        "How many seconds between groups? `3` is a sensible default — it keeps "
        "one ad comfortably inside Telegram's limits\\.",
    ),
}


@router.callback_query(F.data.startswith("ad:"))
async def ad_actions(
    query: CallbackQuery, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    _kind, ident, action = views.parse_callback(query.data or "")
    broadcast_id = views.as_uuid(ident)
    if broadcast_id is None:
        await query.answer("Unknown ad.", show_alert=True)
        return

    if action in _AD_PROMPTS:
        next_state, prompt = _AD_PROMPTS[action]
        await state.clear()
        await state.set_state(next_state)
        await state.update_data(broadcast_id=str(broadcast_id))
        if isinstance(query.message, Message):
            await _ask(query.message, prompt)
        await query.answer()
        return

    if action == "pick":
        await _open_picker(query, user_id, state, broadcast_id=broadcast_id)
        return

    notice = ""
    if action == "save":
        kept = await _save_selection(user_id, state)
        await state.clear()
        notice = f"{kept} group(s) selected."

    async with session_scope() as session:
        broadcast = await broadcast_repo.get(session, user_id=user_id, broadcast_id=broadcast_id)
        if broadcast is None:
            await query.answer("That ad no longer exists.", show_alert=True)
            return

        target_ids = await broadcast_repo.target_chat_ids(session, broadcast_id=broadcast.id)
        estimate = broadcast_service.estimated_duration_s(broadcast.delay_ms, len(target_ids))
        owner_connection = await connection_repo.get(
            session, user_id=user_id, connection_id=broadcast.connection_id
        )
        is_premium = bool(owner_connection and owner_connection.is_premium)

        if action == "confirm":
            await _render(
                query,
                views.ad_confirm(
                    broadcast=broadcast,
                    target_count=len(target_ids),
                    estimate_s=estimate,
                    account_is_premium=is_premium,
                ),
            )
            await query.answer()
            return

        if action == "events":
            events = await event_repo.list_for_broadcast(
                session, user_id=user_id, broadcast_id=broadcast.id, limit=12
            )
            await _render(query, views.activity(events=events, back=f"ad:{broadcast.id}"))
            await query.answer()
            return

        if action == "discard":
            await broadcast_repo.discard_drafts(session, user_id=user_id)
            await state.clear()
            await _render(query, await _ads_screen(user_id, page=0))
            await query.answer("Discarded.")
            return

        if action == "send":
            try:
                queued = await broadcast_service.queue(session, broadcast=broadcast)
            except broadcast_service.BroadcastValidationError as exc:
                await query.answer(exc.message, show_alert=True)
                return
            await event_repo.audit(
                session,
                user_id=user_id,
                action="broadcast.send",
                object_type="broadcast",
                object_id=str(broadcast.id),
                payload={"via": "telegram", "targets": queued},
            )
            notice = f"Sending to {queued} groups."

        elif action == "pause":
            from app.domain import reasons

            await broadcast_service.pause(
                session, broadcast=broadcast, reason_code=reasons.BROADCAST_INACTIVE
            )
            notice = "Paused. Groups already posted to stay posted."

        elif action == "resume":
            broadcast.status = BroadcastStatus.sending
            broadcast.paused_reason_code = None
            await broadcast_repo.schedule_targets(session, broadcast=broadcast)
            notice = "Resumed."

        elif action == "cancel":
            stopped = await broadcast_service.cancel(session, broadcast=broadcast)
            await event_repo.audit(
                session,
                user_id=user_id,
                action="broadcast.cancel",
                object_type="broadcast",
                object_id=str(broadcast.id),
                payload={"via": "telegram", "stopped": stopped},
            )
            notice = f"Stopped. {stopped} group(s) will not be posted to."

        elif action == "retry":
            requeued = await broadcast_service.retry_unfinished(session, broadcast=broadcast)
            notice = f"{requeued} group(s) queued for another try."

        if broadcast.status is BroadcastStatus.draft:
            screen = views.ad_compose(
                broadcast=broadcast,
                target_count=len(target_ids),
                estimate_s=estimate,
                account_is_premium=is_premium,
            )
        else:
            screen = views.ad_detail(
                broadcast=broadcast,
                counts=await broadcast_repo.status_counts(session, broadcast_id=broadcast.id),
                target_count=len(target_ids),
            )

    await _render(query, screen)
    await query.answer(notice)


# --------------------------------------------------------------------------- #
# Group picker — shared by ads and rules
# --------------------------------------------------------------------------- #
#: What an ad may be posted to. A private chat is excluded further down the
#: stack as well — posting an ad into someone's DM is unsolicited messaging, and
#: `sync.NON_DESTINATION_KINDS` refuses it outright. Channels are excluded here
#: as a product choice: an ad is for groups, and a channel you own is better
#: posted to directly.
AD_CHAT_KINDS = {"group", "supergroup"}


async def _postable_chats(session, user_id: uuid.UUID, *, groups_only: bool = False):  # type: ignore[no-untyped-def]
    """Chats the connection can actually post in, in a stable order.

    Only postable ones are offered: listing one the account cannot write to
    invites selecting it and discovering the problem 300 deliveries later. The
    sort is fixed because picker buttons address a chat by its index.

    ``groups_only`` is what an ad uses. A forwarding rule keeps the wider set,
    because copying into a channel you run is a legitimate thing to want.
    """
    chats = await chat_repo.list_filtered(session, user_id=user_id, limit=1000)
    postable = [c for c in chats if c.access and c.access.can_post_destination]
    if groups_only:
        postable = [c for c in postable if c.chat_kind.value in AD_CHAT_KINDS]
    postable.sort(key=lambda c: (c.title.lower(), str(c.id)))
    return postable


def _picker_screen(chats, selected: set[uuid.UUID], page: int, done: str) -> views.Screen:  # type: ignore[no-untyped-def]
    return views.group_picker(
        chats=chats,
        selected=selected,
        page=page,
        title="Choose groups",
        hint=PICK_HINT,
        done_callback=done,
    )


async def _open_picker(
    query: CallbackQuery,
    user_id: uuid.UUID,
    state: FSMContext,
    *,
    broadcast_id: uuid.UUID | None = None,
    rule_id: uuid.UUID | None = None,
) -> None:
    """Load the selectable groups and remember their order.

    The order is stored because buttons address a group by index — see
    ``views.group_picker``. Resolving an index against a *different* ordering
    would toggle the wrong group, so the list the keyboard was built from is the
    list a callback resolves against.
    """
    async with session_scope() as session:
        chats = await _postable_chats(session, user_id, groups_only=broadcast_id is not None)

        if broadcast_id is not None:
            selected = set(await broadcast_repo.target_chat_ids(session, broadcast_id=broadcast_id))
            done = f"ad:{broadcast_id}"
        else:
            rule = await rule_repo.get(session, user_id=user_id, rule_id=rule_id or uuid.uuid4())
            selected = {d.chat_id for d in rule.destinations} if rule else set()
            done = f"rule:{rule_id}:save"

    await state.set_state(ComposeAd.picking if broadcast_id else ComposeRule.picking)
    await state.set_data(
        {
            PICK_TARGET: "ad" if broadcast_id is not None else "rule",
            "broadcast_id": str(broadcast_id) if broadcast_id else None,
            "rule_id": str(rule_id) if rule_id else None,
            "chat_ids": [str(c.id) for c in chats],
            "selected": sorted(str(c) for c in selected),
            "page": 0,
        }
    )
    await _render(query, _picker_screen(chats, selected, 0, done))
    await query.answer()


@router.callback_query(F.data.startswith(views.PICK))
async def picker_actions(
    query: CallbackQuery, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    data = await state.get_data()
    chat_ids: list[str] = data.get("chat_ids") or []
    if not chat_ids:
        await query.answer("This list expired. Open it again.", show_alert=True)
        return

    selected: set[str] = set(data.get("selected") or [])
    page = int(data.get("page") or 0)
    command = (query.data or "")[len(views.PICK) :]
    verb, argument = command[:1], command[1:]
    number = int(argument) if argument.isdigit() else None

    if verb == "t" and number is not None and 0 <= number < len(chat_ids):
        selected ^= {chat_ids[number]}
    elif verb == "p" and number is not None:
        page = number
    elif verb == "A":
        # Every group, not just the visible page. With 157 groups over 20 pages
        # the page-at-a-time button was twenty taps.
        selected |= set(chat_ids)
    elif verb == "a" and number is not None:
        page = number
        selected |= set(
            chat_ids[page * views.PICKER_PAGE_SIZE : (page + 1) * views.PICKER_PAGE_SIZE]
        )
    elif verb == "n":
        selected.clear()

    await state.update_data(selected=sorted(selected), page=page)

    async with session_scope() as session:
        chats = await chat_repo.get_many(
            session, user_id=user_id, chat_ids=[uuid.UUID(c) for c in chat_ids]
        )
    # Restore the order the keyboard was built from; get_many does not preserve it.
    position = {chat_id: index for index, chat_id in enumerate(chat_ids)}
    chats = sorted(chats, key=lambda c: position.get(str(c.id), len(chat_ids)))

    done = (
        f"ad:{data.get('broadcast_id')}:save"
        if data.get(PICK_TARGET) == "ad"
        else f"rule:{data.get('rule_id')}:save"
    )
    await _render(query, _picker_screen(chats, {uuid.UUID(c) for c in selected}, page, done))
    await query.answer()


async def _save_selection(user_id: uuid.UUID, state: FSMContext) -> int:
    """Persist the picker's result. Returns how many groups were kept."""
    data = await state.get_data()
    selected = [uuid.UUID(c) for c in (data.get("selected") or [])]
    broadcast_id = views.as_uuid(data.get("broadcast_id"))

    async with session_scope() as session:
        if broadcast_id is not None:
            broadcast = await broadcast_repo.get(
                session, user_id=user_id, broadcast_id=broadcast_id
            )
            if broadcast is None:
                return 0
            return await broadcast_repo.replace_targets(
                session, broadcast=broadcast, chat_ids=selected
            )

        rule_id = views.as_uuid(data.get("rule_id"))
        rule = await rule_repo.get(session, user_id=user_id, rule_id=rule_id or uuid.uuid4())
        if rule is None:
            return 0
        await rule_repo.replace_destinations(session, rule=rule, chat_ids=selected)
        return len(selected)


# --------------------------------------------------------------------------- #
# Auto-reply
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "nav:autoreply")
async def nav_autoreply(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    await _render(query, await _autoreply_screen(user_id))
    await query.answer()


async def _autoreply_screen(user_id: uuid.UUID) -> views.Screen:
    async with session_scope() as session:
        connection = await _active_connection(session, user_id)
        reply = (
            await autoreply_repo.get_for_connection(session, connection_id=connection.id)
            if connection is not None
            else None
        )
    return views.autoreply_screen(connection=connection, reply=reply)


@router.callback_query(F.data.startswith("ar:"))
async def autoreply_actions(
    query: CallbackQuery, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    action = (query.data or "").split(":", 1)[1]

    prompts = {
        "text": (
            EditAutoReply.text,
            "Send the reply people should receive when they message this "
            "account\\.\n\nIt goes only to people who write to you first\\.\n\n"
            "/cancel to stop\\.",
        ),
        "cooldown": (
            EditAutoReply.cooldown,
            "How many hours before the same person can be answered again?\n\n"
            "`24` is the default\\. This is what keeps the reply from turning "
            "into repeat messaging\\. Minimum is 1 hour\\.",
        ),
    }
    if action in prompts:
        next_state, prompt = prompts[action]
        await state.clear()
        await state.set_state(next_state)
        if isinstance(query.message, Message):
            await _ask(query.message, prompt)
        await query.answer()
        return

    async with session_scope() as session:
        connection = await _active_connection(session, user_id)
        if connection is None:
            await query.answer("Connect an account first.", show_alert=True)
            return
        try:
            await autoreply_repo.upsert(
                session, user_id=user_id, connection_id=connection.id, enabled=action == "on"
            )
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        await event_repo.audit(
            session,
            user_id=user_id,
            action=f"autoreply.{action}",
            object_type="connection",
            object_id=str(connection.id),
            payload={"via": "telegram"},
        )

    await _render(query, await _autoreply_screen(user_id))
    await query.answer("Auto-reply is on." if action == "on" else "Auto-reply is off.")


@router.message(EditAutoReply.text)
async def autoreply_text(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    body = (message.text or "").strip()
    if not body:
        await _ask(message, "Send the reply text, or /cancel\\.")
        return
    await state.clear()

    async with session_scope() as session:
        connection = await _active_connection(session, user_id)
        if connection is None:
            await _ask(message, "Connect an account first\\.")
            await _go_home(message, user_id)
            return
        await autoreply_repo.upsert(
            session, user_id=user_id, connection_id=connection.id, body_text=body[:4096]
        )

    await _send(message, await _autoreply_screen(user_id))


@router.message(EditAutoReply.cooldown)
async def autoreply_cooldown(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    raw = (message.text or "").strip().rstrip("h")
    try:
        hours = float(raw)
    except ValueError:
        await _ask(message, "Send a number of hours, like `24`\\.")
        return
    if hours < 1:
        await _ask(message, "Use at least 1 hour\\.")
        return
    await state.clear()

    async with session_scope() as session:
        connection = await _active_connection(session, user_id)
        if connection is None:
            await _go_home(message, user_id)
            return
        await autoreply_repo.upsert(
            session, user_id=user_id, connection_id=connection.id, cooldown_s=int(hours * 3600)
        )

    await _send(message, await _autoreply_screen(user_id))


# --------------------------------------------------------------------------- #
# Forwarding rules
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "rule:new")
async def rule_new(query: CallbackQuery, state: FSMContext, **_extra: Any) -> None:
    await state.clear()
    await state.set_state(ComposeRule.name)
    if isinstance(query.message, Message):
        await _ask(
            query.message,
            "📋 *New forwarding rule*\n\nGive it a name, like `Deals to partners`\\."
            "\n\n/cancel to stop\\.",
        )
    await query.answer()


@router.message(ComposeRule.name)
async def rule_name(message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any) -> None:
    name = (message.text or "").strip()
    if not name:
        await _ask(message, "Send a name, or /cancel\\.")
        return

    async with session_scope() as session:
        connection = await _active_connection(session, user_id)
        if connection is None:
            await state.clear()
            await _ask(message, "Connect an account first\\.")
            await _go_home(message, user_id)
            return
        chats = await chat_repo.list_filtered(session, user_id=user_id, limit=1000)
        readable = [c for c in chats if c.access and c.access.can_read_source]
        connection_id = connection.id

    if not readable:
        await state.clear()
        await _ask(
            message,
            "No chat is readable by this connection yet\\. Open *Accounts*, pick "
            "it, and tap *Sync groups* first\\.",
        )
        await _go_home(message, user_id)
        return

    readable.sort(key=lambda c: (c.title.lower(), str(c.id)))
    shown = readable[:30]
    await state.update_data(
        name=name[:120],
        connection_id=str(connection_id),
        source_ids=[str(c.id) for c in shown],
    )
    await state.set_state(ComposeRule.source)

    listing = "\n".join(
        f"`{index + 1}` — {views.escape(chat.title[:40])}" for index, chat in enumerate(shown)
    )
    await _ask(
        message, f"Which chat should messages be *copied from*?\n\n{listing}\n\nSend the number\\."
    )


@router.message(ComposeRule.source)
async def rule_source(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    raw = (message.text or "").strip()
    data = await state.get_data()
    source_ids: list[str] = data.get("source_ids") or []
    if not raw.isdigit() or not 1 <= int(raw) <= len(source_ids):
        await _ask(message, "Send one of the numbers listed above, or /cancel\\.")
        return

    source_id = uuid.UUID(source_ids[int(raw) - 1])
    connection_id = views.as_uuid(data.get("connection_id"))
    if connection_id is None:
        await state.clear()
        await _go_home(message, user_id)
        return

    async with session_scope() as session:
        connection = await connection_repo.get(
            session, user_id=user_id, connection_id=connection_id
        )
        if connection is None:
            await state.clear()
            await _go_home(message, user_id)
            return
        try:
            rule = await rule_service.create(
                session,
                user_id=user_id,
                connection=connection,
                payload=rule_service.RuleInput(
                    name=data.get("name", "Rule"),
                    connection_id=connection_id,
                    source_chat_ids=[source_id],
                    destination_chat_ids=[],
                ),
            )
        except rule_service.RuleValidationError as exc:
            await state.clear()
            await _ask(message, f"Cannot create that rule\\.\n\n_{views.escape(exc.message)}_")
            await _go_home(message, user_id)
            return
        rule_id = rule.id

    await state.clear()
    await _ask(
        message,
        "✅ Rule created as a draft\\.\n\nOpen it to choose the groups to copy "
        "into, then resume it\\.",
    )
    await _send(message, await _rules_screen(user_id, page=0))
    log.info("rule_created_via_bot", rule_id=str(rule_id))


@router.callback_query(F.data.startswith("rule:"))
async def rule_actions(
    query: CallbackQuery, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    _kind, ident, action = views.parse_callback(query.data or "")
    rule_id = views.as_uuid(ident)
    if rule_id is None:
        await query.answer("Unknown rule.", show_alert=True)
        return

    if action == "pick":
        await _open_picker(query, user_id, state, rule_id=rule_id)
        return

    notice: str | None = None
    if action == "save":
        kept = await _save_selection(user_id, state)
        await state.clear()
        notice = f"{kept} group(s) saved."

    async with session_scope() as session:
        # user_id-scoped: another admin's rule simply does not resolve.
        rule = await rule_repo.get(session, user_id=user_id, rule_id=rule_id)
        if rule is None:
            await query.answer("That rule no longer exists.", show_alert=True)
            return

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
        elif action == "askdel":
            await _render(query, views.confirm_delete_rule(rule=rule))
            await query.answer()
            return
        elif action == "delete":
            await event_repo.audit(
                session,
                user_id=user_id,
                action="rule.delete",
                object_type="rule",
                object_id=str(rule.id),
                payload={"via": "telegram"},
            )
            await session.delete(rule)
            await _render(query, await _rules_screen(user_id, page=0))
            await query.answer("Deleted.")
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
        )

    await _render(query, screen)
    await query.answer(notice or "")


# --------------------------------------------------------------------------- #
# Users (operators only)
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith("nav:users"))
async def nav_users(query: CallbackQuery, is_operator: bool = False, **_extra: Any) -> None:
    if not await _require_operator(query, is_operator):
        return
    await _render(query, await _users_screen(page=_page_from(query.data or "")))
    await query.answer()


async def _users_screen(*, page: int) -> views.Screen:
    async with session_scope() as session:
        users = await user_repo.list_all(session)
        totals = await user_repo.counts(session)
    return views.users_list(users=users, page=page, totals=totals)


@router.callback_query(F.data.startswith("usr:"))
async def user_actions(
    query: CallbackQuery, user_id: uuid.UUID, is_operator: bool = False, **_extra: Any
) -> None:
    if not await _require_operator(query, is_operator):
        return

    _kind, ident, action = views.parse_callback(query.data or "")
    target_id = views.as_uuid(ident)
    if target_id is None:
        await query.answer("Unknown account.", show_alert=True)
        return

    notice = ""
    async with session_scope() as session:
        target = await user_repo.get_by_id(session, target_id)
        if target is None:
            await query.answer("That account no longer exists.", show_alert=True)
            return

        if action == "asksus":
            if target.id == user_id:
                await query.answer("You cannot suspend yourself.", show_alert=True)
                return
            await _render(query, views.confirm_suspend(user=target))
            await query.answer()
            return

        if action == "sus":
            # Guarded again here, not only on the confirmation screen: the
            # callback can be replayed directly.
            if target.id == user_id:
                await query.answer("You cannot suspend yourself.", show_alert=True)
                return
            stopped = await user_service.suspend(session, user=target, by=user_id)
            notice = (
                f"Suspended. {stopped['rules']} rule(s) paused, "
                f"{stopped['targets']} queued delivery(ies) cancelled."
            )

        elif action == "allow":
            await user_service.reinstate(session, user=target, by=user_id)
            notice = "Reinstated. Their rules and ads stay paused until they restart them."

        activity = await user_repo.activity_for(session, user_id=target.id)
        screen = views.user_detail(user=target, activity=activity)

    await _render(query, screen)
    await query.answer(notice)


@router.callback_query()
async def unknown_callback(query: CallbackQuery, **_extra: Any) -> None:
    await query.answer("That button is no longer valid. Tap /panel to reopen.", show_alert=True)
