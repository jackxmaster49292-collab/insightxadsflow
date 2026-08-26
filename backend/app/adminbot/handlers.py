"""Telegram control-panel handlers.

The bot *is* the admin surface: connecting an account, composing an ad, writing
and an auto-reply all happen here. There is no web
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

import contextlib
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.adminbot import icon_setup, premium_icons, secrets, views
from app.adminbot.states import (
    ComposeAd,
    ConnectAccount,
    ConnectBot,
    EditAutoReply,
    EditButton,
    IconSetup,
    SetArchive,
)
from app.config import get_settings
from app.db.models import (
    BroadcastMedia,
    BroadcastStatus,
    ConnectionKind,
    ConnectionStatus,
    ControlTaskKind,
)
from app.db.session import session_scope
from app.repositories import autoreply as autoreply_repo
from app.repositories import broadcasts as broadcast_repo
from app.repositories import chats as chat_repo
from app.repositories import connections as connection_repo
from app.repositories import events as event_repo
from app.repositories import jobs as job_repo
from app.repositories import panel_buttons as panel_buttons_repo
from app.repositories import panel_emoji as panel_emoji_repo
from app.repositories import rules as rule_repo
from app.repositories import users as user_repo
from app.services import archive as archive_service
from app.services import broadcast as broadcast_service
from app.services import connections as connection_service
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
async def _deliver(send, text: str, markup=None) -> None:  # type: ignore[no-untyped-def]
    """Every panel message goes out through here — see ``premium_icons``.

    Kept as a name of its own because it is called from forty places and the
    tests reach for it, but the behaviour lives beside the transform it wraps:
    alerts are sent from a different module entirely and must upgrade the same
    way, and two copies of a fallback is one copy that eventually differs.
    """
    await premium_icons.deliver(send, text, markup)


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
                bot, chat_id = target.bot, target.from_user.id

                async def send_fresh(text: str, markup) -> None:  # type: ignore[no-untyped-def]
                    await bot.send_message(
                        chat_id, text, reply_markup=markup, parse_mode=PARSE_MODE
                    )

                await _deliver(send_fresh, screen.text, screen.keyboard)
            return

        async def send_edit(text: str, markup) -> None:  # type: ignore[no-untyped-def]
            await message.edit_text(text, reply_markup=markup, parse_mode=PARSE_MODE)

        try:
            await _deliver(send_edit, screen.text, screen.keyboard)
        except TelegramBadRequest as exc:
            # Tapping Refresh twice produces an identical message; Telegram
            # rejects that edit and it is not an error worth surfacing.
            if "message is not modified" not in str(exc):
                raise
    else:

        async def send_answer(text: str, markup) -> None:  # type: ignore[no-untyped-def]
            await target.answer(text, reply_markup=markup, parse_mode=PARSE_MODE)

        await _deliver(send_answer, screen.text, screen.keyboard)


async def _ask(message: Message, text: str) -> None:
    """Prompt for the next step of a flow, as a fresh message."""

    async def send(styled: str, _markup) -> None:  # type: ignore[no-untyped-def]
        await message.answer(styled, parse_mode=PARSE_MODE)

    await _deliver(send, text)


async def _send(message: Message, screen: views.Screen) -> None:
    async def send(text: str, markup) -> None:  # type: ignore[no-untyped-def]
        await message.answer(text, reply_markup=markup, parse_mode=PARSE_MODE)

    await _deliver(send, screen.text, screen.keyboard)


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


def _merge_entities(
    from_message: list[dict[str, Any]],
    from_markup: list[dict[str, Any]],
    original: str,
) -> list[dict[str, Any]]:
    """Entities Telegram supplied, plus the ones written as markup.

    Telegram's offsets describe the text *as sent*, so they only survive
    untouched when no markup was rewritten — rewriting shifts every offset
    after it. Rather than re-deriving offsets that Telegram alone can compute
    correctly, markup and keyboard-inserted formatting are treated as an
    either/or: if the message contained markup, its entities win. Mixing the
    two in one message is the one case this does not serve, and silently
    misplacing bold text would be worse than not serving it.
    """
    if not from_markup:
        return from_message
    if premium_icons.CUSTOM_EMOJI_MARKUP.search(original):
        return from_markup
    return from_message


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


async def _archive_screen(user_id: uuid.UUID, *, page: int) -> views.Screen:
    async with session_scope() as session:
        chats = await _postable_chats(session, user_id, groups_only=True)
        setting = await archive_service.any_operator_setting(session)
        title = None
        if setting is not None and setting.archive_bot_chat_id is not None:
            # No title to show: this chat is one the bot was added to, not one
            # the account synced, so its id is the only name we honestly have.
            title = f"chat {setting.archive_bot_chat_id} (via the bot)"
        else:
            chat = await archive_service.archive_chat(session, user_id=user_id)
            title = chat.title if chat else None
    return views.archive_settings(
        current=title,
        chats=chats,
        page=page,
        all_users=setting.archive_all_users if setting else True,
    )


@router.callback_query(F.data.startswith("nav:arch"))
async def nav_archive(
    query: CallbackQuery, user_id: uuid.UUID, is_operator: bool = False, **_extra: Any
) -> None:
    if not await _require_operator(query, is_operator):
        return
    await _render(query, await _archive_screen(user_id, page=_page_from(query.data or "")))
    await query.answer()


# Narrow, non-overlapping filters. ``startswith("arch:")`` also swallowed
# ``arch:bot``, whose own handler is registered later and so never ran — the
# button reported "that group is not in your list" because this handler read
# "bot" as a chat id.
@router.callback_query(
    F.data.startswith("arch:s:") | (F.data == "arch:off") | (F.data == "arch:scope")
)
async def set_archive(
    query: CallbackQuery, user_id: uuid.UUID, is_operator: bool = False, **_extra: Any
) -> None:
    if not await _require_operator(query, is_operator):
        return
    parts = (query.data or "").split(":")
    verb = parts[1] if len(parts) > 1 else ""

    async with session_scope() as session:
        setting = await user_repo.get_settings_row(session, user_id=user_id)
        if setting is None:
            from app.db.models import AppSetting

            setting = AppSetting(user_id=user_id)
            session.add(setting)

        if verb == "scope":
            setting.archive_all_users = not setting.archive_all_users
            notice = (
                "Copying everyone's ads."
                if setting.archive_all_users
                else "Copying only accounts you switch on."
            )
        elif verb == "off":
            setting.archive_chat_id = None
            setting.archive_bot_chat_id = None
            notice = "Archive off."
        else:
            chat_id = views.as_uuid(parts[2] if len(parts) > 2 else None)
            # Resolved through the user-scoped repository, so an id alone is
            # never enough to point someone's archive at another account's chat.
            chat = (
                await chat_repo.get(session, user_id=user_id, chat_id=chat_id) if chat_id else None
            )
            if chat is None:
                await query.answer("That group is not in your list.", show_alert=True)
                return
            setting.archive_chat_id = chat.id
            # One destination at a time: two would double every copy.
            setting.archive_bot_chat_id = None
            notice = f"Copies go to {chat.title}."

    await _render(query, await _archive_screen(user_id, page=0))
    await query.answer(notice)


@router.callback_query(F.data == "arch:bot")
async def archive_by_bot(
    query: CallbackQuery,
    user_id: uuid.UUID,
    state: FSMContext,
    is_operator: bool = False,
    **_extra: Any,
) -> None:
    if not await _require_operator(query, is_operator):
        return
    await state.clear()
    await state.set_state(SetArchive.chat)
    if isinstance(query.message, Message):
        await _ask(
            query.message,
            "Add me to the group as an *admin* with permission to post, then "
            "send me its chat id — it looks like `\\-1001234567890`\\.\n\n"
            "Or just forward me any message from that group and I will read "
            "the id off it\\.\n\n/cancel to stop\\.",
        )
    await query.answer()


@router.message(SetArchive.chat)
async def archive_chat_given(
    message: Message,
    user_id: uuid.UUID,
    state: FSMContext,
    is_operator: bool = False,
    **_extra: Any,
) -> None:
    # A conversation state is not authorization: someone could be left in this
    # state by a change of operator list, and the check belongs here anyway.
    if not is_operator:
        await state.clear()
        await _go_home(message, user_id)
        return
    chat_id = _forwarded_chat_id(message)
    if chat_id is None:
        raw = (message.text or "").strip()
        try:
            chat_id = int(raw)
        except ValueError:
            await _ask(
                message,
                "That is not a chat id\\. Send the number \\(like "
                "`\\-1001234567890`\\) or forward a message from the group\\.",
            )
            return

    # Proved rather than assumed: the bot posts a line into the chat now, so a
    # missing invite or a missing permission is discovered here instead of
    # silently swallowing every archive from now on.
    from app.adapters.base import ChatRef, PeerKind
    from app.services import archive as archive_service

    bot_adapter = archive_service.bot_adapter()
    if bot_adapter is None:
        await _ask(message, "This deployment has no bot token configured\\.")
        return

    kind = PeerKind.channel if str(chat_id).startswith("-100") else PeerKind.chat
    try:
        await bot_adapter.send_text(
            ChatRef(kind, chat_id),
            "📁 Archive set up. Copies of every ad will arrive here.",
        )
    except Exception as exc:
        await _ask(
            message,
            "I could not post there\\.\n\n"
            f"_{views.escape(_describe(exc))}_\n\n"
            "Add me to the group as an admin who may post, then send the id "
            "again\\.",
        )
        return

    async with session_scope() as session:
        setting = await user_repo.get_settings_row(session, user_id=user_id)
        if setting is None:
            from app.db.models import AppSetting

            setting = AppSetting(user_id=user_id)
            session.add(setting)
        setting.archive_bot_chat_id = chat_id
        setting.archive_chat_id = None

    await state.clear()
    await _send(message, await _archive_screen(user_id, page=0))


def _forwarded_chat_id(message: Message) -> int | None:
    """The chat a message was forwarded from, when Telegram says so.

    Only a forward from a *chat* counts: a forward from a person carries their
    id, and pointing an archive at a private conversation is not what anyone
    means by "the group I added the bot to".
    """
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    return int(chat.id) if chat is not None else None


@router.callback_query(F.data == "nav:about")
async def nav_about(query: CallbackQuery, **_extra: Any) -> None:
    await _render(query, views.about(links=get_settings().public_links))
    await query.answer()


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


@router.message(Command("help"))
async def help_command(message: Message, **_extra: Any) -> None:
    await message.answer(
        "*InsightAdFlow*\n\n"
        "/start — open the panel\n"
        "/ads — your ads\n"
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


async def _fill_page_details(session, user_id: uuid.UUID, chats) -> None:  # type: ignore[no-untyped-def]
    """Learn the bio of the private chats about to be shown, and only those.

    Bounded to one page on purpose. Fetching all 237 would take two minutes and
    hammer Telegram's most rate-limited lookup for chats nobody is looking at;
    a page is six, costs three seconds once, and is cached from then on.

    Public chats are skipped — Telegram unfurls their links itself, so there is
    nothing here they need.
    """
    import asyncio

    from app.db.models import ConnectionKind, ConnectionStatus
    from app.services import connections as connection_service

    wanted = [
        c
        for c in chats
        if not c.username and c.details_synced_at is None and c.chat_kind.value != "private"
    ]
    if not wanted:
        return

    connections = await connection_repo.list_for_user(session, user_id=user_id)
    live = [
        c
        for c in connections
        if c.kind is ConnectionKind.user and c.status is ConnectionStatus.active
    ]
    if not live:
        return
    adapter = await connection_service.adapter_for(session, live[0])

    for chat in wanted:
        try:
            details = await adapter.chat_details(chat_repo.to_ref(chat))
        except Exception as exc:
            # One unreadable chat must not cost the page its other five.
            log.warning("chat_details_failed", chat_id=str(chat.id), error=str(exc))
            continue
        await chat_repo.set_details(session, chat=chat, details=details)
        await asyncio.sleep(0.5)


async def _dead_screen(user_id: uuid.UUID, *, page: int) -> views.Screen:
    async with session_scope() as session:
        refusing = await chat_repo.refusing(session, user_id=user_id)
    return views.dead_groups(chats=refusing, page=page, temporary=chat_repo.TEMPORARY_REFUSALS)


@router.callback_query(F.data.startswith("nav:dead"))
async def nav_dead_groups(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    await _render(query, await _dead_screen(user_id, page=_page_from(query.data or "")))
    await query.answer()


@router.callback_query(F.data.startswith("dead:"))
async def drop_dead_groups(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    """Take refusing groups out of the ads. Never out of the group itself."""
    parts = (query.data or "").split(":")
    verb = parts[1] if len(parts) > 1 else ""

    async with session_scope() as session:
        refusing = await chat_repo.refusing(session, user_id=user_id)
        if verb == "all":
            # Only the lasting refusals. Sweeping up a slow-mode wait would
            # remove a group that was going to work again in a minute.
            chat_ids = [
                c.id
                for c in refusing
                if (c.access.destination_reason_code if c.access else "unknown")
                not in chat_repo.TEMPORARY_REFUSALS
            ]
        else:
            wanted = views.as_uuid(parts[2] if len(parts) > 2 else None)
            # Resolved against the refusing list, not taken on trust: an id
            # alone must never reach another account's chat.
            chat_ids = [c.id for c in refusing if c.id == wanted]

        if not chat_ids:
            await query.answer("Nothing to remove.", show_alert=True)
            return

        dropped = await broadcast_repo.drop_chats(session, user_id=user_id, chat_ids=chat_ids)
        await event_repo.audit(
            session,
            user_id=user_id,
            action="ads.drop_groups",
            object_type="telegram_chat",
            payload={"groups": len(chat_ids), "targets_removed": dropped},
        )

    await _render(query, await _dead_screen(user_id, page=0))
    await query.answer(f"Removed {len(chat_ids)} group(s) from your ads. You are still a member.")


@router.callback_query(F.data.startswith("nav:chats"))
async def nav_chats(query: CallbackQuery, user_id: uuid.UUID, **_extra: Any) -> None:
    async with session_scope() as session:
        everything = await chat_repo.list_filtered(session, user_id=user_id, limit=1000)
        refusing = await chat_repo.refusing(session, user_id=user_id)
    groups = [c for c in everything if c.chat_kind.value in AD_CHAT_KINDS]
    await _render(
        query,
        views.chats_list(
            chats=groups,
            page=_page_from(query.data or ""),
            other_count=len(everything) - len(groups),
            refusing_count=len(refusing),
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
        counts = await broadcast_repo.counts_for(session, broadcast_ids=[b.id for b in broadcasts])
    return views.ads_list(
        broadcasts=broadcasts,
        page=page,
        can_create=connection is not None,
        counts_by_id=counts,
    )


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
        counts = await event_repo.summary_for_connection(session, connection_id=connection.id)
        broadcasts = await broadcast_repo.list_for_user(session, user_id=user_id)
        screen = views.connection_detail(
            connection=connection,
            chat_count=len(chats),
            syncing=running is not None,
            counts=counts,
            ads=sum(1 for b in broadcasts if b.connection_id == connection.id),
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
        # Two ways to put a premium emoji in an ad, and both end as the same
        # stored entities. Inserting the emoji from your own keyboard is the
        # easy one and needs nothing here. Writing
        # ``![🔥](tg://emoji?id=123)`` is the other, and it works for ids from
        # a pack this account does not own — which is the only way to use one
        # you were simply given.
        broadcast.body_text, from_markup = premium_icons.parse_entities(body)
        broadcast.body_entities = _merge_entities(_entities_of(message), from_markup, body)

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
    floor_ms = get_settings().min_broadcast_delay_ms
    if not floor_ms / 1000 <= seconds <= 3600:
        await _ask(
            message,
            f"Use a value between {views.escape(f'{floor_ms / 1000:g}')} and "
            "3600 seconds\\.\n\n"
            "Below that the gain is a few seconds across the whole round, and "
            "the risk is *your* account being read as a flood\\.",
        )
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


#: ``90m``, ``6h``, ``1h 30m``, ``45 minutes``. A bare number is hours, because
#: that is what the prompt asks for and what most repeats are.
#: Longest alternative first. Ordered the other way, ``m`` matches the start of
#: "minutes" and the leftover "inutes" makes the whole thing unparseable.
_INTERVAL_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(hours|hour|hrs|hr|h|minutes|minute|mins|min|m)?")


def _parse_interval(text: str) -> int | None:
    """Seconds, or None if it is not a time anyone meant.

    Deliberately strict about what it accepts: an interval misread by a factor
    of sixty is an ad posting every minute instead of every hour, from the
    customer's own account.
    """
    cleaned = text.strip().lower()
    if not cleaned:
        return None

    total = 0.0
    consumed = 0
    for match in _INTERVAL_PART.finditer(cleaned):
        if match.start() != consumed and cleaned[consumed : match.start()].strip():
            return None  # something between the numbers that is not a unit
        amount = float(match.group(1))
        unit = match.group(2) or "h"
        total += amount * (60 if unit.startswith("m") else 3600)
        consumed = match.end()
    if consumed == 0 or cleaned[consumed:].strip():
        return None
    # ``inf`` and ``nan`` cannot reach here through the digits-only pattern, but
    # a long enough number still can, and a timestamp overflow is a crash rather
    # than a message anyone can act on.
    if not math.isfinite(total) or total < 0:
        return None
    return int(total)


@router.message(ComposeAd.schedule)
async def ad_schedule(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    from app.domain import when as when_domain

    raw = (message.text or "").strip()
    async with session_scope() as session:
        setting = await user_repo.get_settings_row(session, user_id=user_id)
        tz_name = setting.timezone if setting else "UTC"

    if raw.lower() in ("now", "0"):
        start_at = None
    elif when_domain.is_a_zone(raw):
        # They answered with a timezone instead of a time, which is what
        # someone does when the confirmed time came back wrong.
        async with session_scope() as session:
            setting = await user_repo.get_settings_row(session, user_id=user_id)
            if setting is not None:
                setting.timezone = raw
        await _ask(
            message,
            f"Timezone set to `{views.escape(raw)}`\\. Now send the time\\.",
        )
        return
    else:
        start_at = when_domain.parse_when(raw, now=datetime.now(UTC), tz=when_domain.zone(tz_name))
        if start_at is None:
            await _ask(
                message,
                "That is not a time I can read\\. Try `21:30`, `2h`, `1d`, or "
                "`now`\\.\n\n"
                "If the times keep coming back wrong, send your timezone "
                "instead — like `Asia/Kolkata`\\.",
            )
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
        broadcast.scheduled_for = start_at

    await state.set_state(None)
    await _send(message, await _compose_screen(user_id, broadcast_id))


@router.message(ComposeAd.repeat)
async def ad_repeat(message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any) -> None:
    seconds = _parse_interval(message.text or "")
    if seconds is None:
        await _ask(
            message,
            "Send a time like `90m`, `6h` or `1h 30m` — or `0` to post once\\.",
        )
        return

    if seconds > _MAX_REPEAT_HOURS * 3600:
        await _ask(
            message,
            f"The longest repeat is {_MAX_REPEAT_HOURS // 24} days\\. "
            "Past that it is not really a schedule\\.",
        )
        return

    settings = get_settings()
    if seconds and seconds < settings.min_broadcast_repeat_s:
        await _ask(
            message,
            f"The shortest repeat is "
            f"{views.escape(views.interval_label(settings.min_broadcast_repeat_s))}\\. "
            "The same message arriving in the same group more often than that is "
            "what gets an account reported and banned — and it is your account, "
            "not this bot's\\.",
        )
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
        broadcast.repeat_every_s = seconds or None

    await state.set_state(None)
    await _send(message, await _compose_screen(user_id, broadcast_id))


async def _timezone_of(session, user_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """The clock a start time should be shown in.

    Without this every scheduled time renders in UTC, which for most people is
    a plausible-looking number that is hours wrong — the failure mode a start
    time can least afford.
    """
    from app.domain import when as when_domain

    setting = await user_repo.get_settings_row(session, user_id=user_id)
    return when_domain.zone(setting.timezone if setting else "UTC")


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
            tz=await _timezone_of(session, user_id),
        )


#: A month. Past this the repeat is not a schedule any more, and a number large
#: enough to overflow a timestamp is a crash rather than a message.
_MAX_REPEAT_HOURS = 720

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
    "sched": (
        ComposeAd.schedule,
        "When should this ad start?\n\n"
        "`21:30` — at that time, tonight or tomorrow\n"
        "`2h` — in two hours\n"
        "`1d` — this time tomorrow\n"
        "`now` — no waiting\n\n"
        "I will read a clock time in your timezone and show you the answer "
        "both ways, so a wrong timezone is obvious\\.",
    ),
    "repeat": (
        ComposeAd.repeat,
        "How often should this ad be posted again?\n\n"
        "`6h` — four times a day\n"
        "`90m` — every hour and a half\n"
        "`1h 30m` — the same thing\n"
        "`0` — post it once and stop\n\n"
        "A bare number means hours\\. The clock starts when a round *finishes*, "
        "so the gap is measured from the last group receiving it\\.\n\n"
        "The shortest allowed is 1 hour\\. The same message arriving in the same "
        "group more often than that is what gets an account reported — and it is "
        "your account, not this bot's\\.",
    ),
}


# Excludes ``ad:new`` explicitly. It only worked because that handler happens
# to be registered first, and registration order is not a thing to depend on.
@router.callback_query(F.data.startswith("ad:") & (F.data != "ad:new"))
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
        premium_checked = bool(owner_connection and owner_connection.premium_checked_at)

        if action == "confirm":
            await _render(
                query,
                views.ad_confirm(
                    broadcast=broadcast,
                    target_count=len(target_ids),
                    estimate_s=estimate,
                    account_is_premium=is_premium,
                    premium_checked=premium_checked,
                ),
            )
            await query.answer()
            return

        if action == "edit":
            # Paused first, deliberately. Changing the wording or the groups of
            # an ad while the worker is mid-round means some groups get the old
            # version and some the new, with no way to tell which got which.
            if broadcast.status is BroadcastStatus.sending:
                from app.domain import reasons

                await broadcast_service.pause(
                    session, broadcast=broadcast, reason_code=reasons.BROADCAST_BEING_EDITED
                )
            await state.clear()
            await state.update_data(broadcast_id=str(broadcast.id))
            await _render(
                query,
                views.ad_compose(
                    broadcast=broadcast,
                    target_count=len(target_ids),
                    estimate_s=estimate,
                    account_is_premium=is_premium,
                    premium_checked=premium_checked,
                ),
            )
            await query.answer("Paused while you edit.")
            return

        if action == "speed":
            await _render(query, views.ad_speed(broadcast=broadcast, target_count=len(target_ids)))
            await query.answer()
            return

        if action == "spd":
            parts = (query.data or "").split(":")
            index = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
            if not 0 <= index < len(views.SPEED_PRESETS):
                await query.answer("Unknown speed.", show_alert=True)
                return
            label, broadcast.delay_ms = views.SPEED_PRESETS[index]
            await _render(
                query,
                views.ad_compose(
                    broadcast=broadcast,
                    target_count=len(target_ids),
                    estimate_s=broadcast_service.estimated_duration_s(
                        broadcast.delay_ms, len(target_ids)
                    ),
                    account_is_premium=is_premium,
                    premium_checked=premium_checked,
                ),
            )
            await query.answer(label.split(" ", 1)[-1])
            return

        if action == "back":
            await _render(
                query,
                views.ad_compose(
                    broadcast=broadcast,
                    target_count=len(target_ids),
                    estimate_s=estimate,
                    account_is_premium=is_premium,
                    premium_checked=premium_checked,
                ),
            )
            await query.answer()
            return

        if action == "askdel":
            await _render(query, views.confirm_delete_ad(broadcast=broadcast))
            await query.answer()
            return

        if action == "del":
            # Checked again here, not only by the button being hidden: a
            # callback can be replayed, and deleting a running ad would drop
            # rows the worker is holding leases on.
            if broadcast.status in (BroadcastStatus.sending, BroadcastStatus.paused):
                await query.answer("Stop it first, then delete.", show_alert=True)
                return
            await event_repo.audit(
                session,
                user_id=user_id,
                action="broadcast.delete",
                object_type="broadcast",
                object_id=str(broadcast.id),
                payload={"name": broadcast.name},
            )
            await broadcast_repo.remove(session, broadcast=broadcast)
            await _render(query, await _ads_screen(user_id, page=0))
            await query.answer("Deleted.")
            return

        if action == "groups":
            page = 0
            parts = (query.data or "").split(":")
            if len(parts) > 3 and parts[3].isdigit():
                page = int(parts[3])
            rows = await broadcast_repo.targets_with_chats(session, broadcast_id=broadcast.id)
            await _render(query, views.ad_group_report(broadcast=broadcast, rows=rows, page=page))
            await query.answer()
            return

        if action == "events":
            events = await event_repo.list_for_broadcast(
                session, user_id=user_id, broadcast_id=broadcast.id, limit=12
            )
            titles = await broadcast_repo.chat_titles(session, broadcast_id=broadcast.id)
            await _render(
                query,
                views.activity(events=events, titles=titles, back=f"ad:{broadcast.id}"),
            )
            await query.answer()
            return

        if action == "discard":
            await broadcast_repo.discard_drafts(session, user_id=user_id)
            await state.clear()
            await _render(query, await _ads_screen(user_id, page=0))
            await query.answer("Discarded.")
            return

        if action == "send":
            # Sending a finished ad again means running it again: every target
            # is reopened, exactly as a repeat round reopens them. Without this
            # there is nothing pending, queue() schedules nothing, and the ad
            # sits in "sending" forever with no work that could ever settle it.
            if broadcast.status in (BroadcastStatus.completed, BroadcastStatus.cancelled):
                await broadcast_repo.reopen_for_repeat(
                    session, broadcast=broadcast, start_at=broadcast_repo.now()
                )
            # A start time turns Send into "put it in the queue for later".
            # The ad is validated *now* either way — finding out at 6am that it
            # had no groups would be the worst possible moment.
            if broadcast.scheduled_for and broadcast.scheduled_for > datetime.now(UTC):
                try:
                    broadcast_service.validate(
                        broadcast,
                        target_count=len(target_ids),
                    )
                except broadcast_service.BroadcastValidationError as exc:
                    await query.answer(exc.message, show_alert=True)
                    return
                broadcast.status = BroadcastStatus.scheduled
                notice = "Scheduled."
            else:
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
                session,
                broadcast=broadcast,
                reason_code=reasons.BROADCAST_PAUSED_BY_CUSTOMER,
            )
            notice = "Paused. Groups already posted to stay posted."

        elif action == "resume":
            broadcast.status = BroadcastStatus.sending
            broadcast.paused_reason_code = None
            # Only pending targets are re-timed, so groups already posted to in
            # this round are not posted to twice.
            await broadcast_repo.schedule_targets(session, broadcast=broadcast)
            notice = "Resumed."

        elif action == "cancel":
            # Only the account route needs the account's adapter, and building
            # an MTProto client costs a connection — so it is built when it is
            # the courier and not otherwise.
            courier = None
            where = await archive_service.destination_for(session, user_id=user_id)
            if where is not None and not where.via_bot and owner_connection is not None:
                with contextlib.suppress(Exception):
                    courier = await connection_service.adapter_for(session, owner_connection)
            stopped = await broadcast_service.cancel(session, broadcast=broadcast, adapter=courier)
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
                premium_checked=premium_checked,
                tz=await _timezone_of(session, user_id),
            )
        else:
            screen = views.ad_detail(
                broadcast=broadcast,
                counts=await broadcast_repo.status_counts(session, broadcast_id=broadcast.id),
                target_count=len(target_ids),
                reason_counts=await broadcast_repo.reason_counts(
                    session, broadcast_id=broadcast.id
                ),
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

    ``groups_only`` is what an ad uses — a private chat or a channel is never
    an ad destination.
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
) -> None:
    """Load the selectable groups and remember their order.

    The order is stored because buttons address a group by index — see
    ``views.group_picker``. Resolving an index against a *different* ordering
    would toggle the wrong group, so the list the keyboard was built from is the
    list a callback resolves against.
    """
    async with session_scope() as session:
        chats = await _postable_chats(session, user_id, groups_only=True)

        if broadcast_id is None:
            await query.answer("That ad no longer exists.", show_alert=True)
            return
        selected = set(await broadcast_repo.target_chat_ids(session, broadcast_id=broadcast_id))
        done = f"ad:{broadcast_id}"

    await state.set_state(ComposeAd.picking)
    await state.set_data(
        {
            "broadcast_id": str(broadcast_id) if broadcast_id else None,
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

    done = f"ad:{data.get('broadcast_id')}:save"
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
# Excludes ``rule:new``, for the same reason as ``ad:new`` above.
# --------------------------------------------------------------------------- #
# Premium icons (operators only)
# --------------------------------------------------------------------------- #
async def _emoji_status_screen(user_id: uuid.UUID) -> views.Screen:
    async with session_scope() as session:
        extracted = await panel_emoji_repo.get_map(session)
        connections = await connection_repo.list_for_user(session, user_id=user_id)
    has_user = any(
        c.kind is ConnectionKind.user and c.status is ConnectionStatus.active for c in connections
    )
    return views.premium_icons_status(
        extracted=extracted,
        live=premium_icons.enabled(),
        suspended=premium_icons.suspended(),
        has_user_connection=has_user,
    )


@router.callback_query(F.data.startswith("op:emoji"))
async def op_emoji(
    query: CallbackQuery,
    user_id: uuid.UUID,
    state: FSMContext,
    is_operator: bool = False,
    **_extra: Any,
) -> None:
    if not await _require_operator(query, is_operator):
        return
    action = (query.data or "").split(":")[2] if (query.data or "").count(":") >= 2 else None

    if action == "off":
        async with session_scope() as session:
            cleared = await panel_emoji_repo.clear(session)
            await event_repo.audit(
                session,
                user_id=user_id,
                action="panel_emoji.clear",
                object_type="panel_emoji",
                payload={"cleared": cleared},
            )
        premium_icons.set_map({})
        await _render(query, await _emoji_status_screen(user_id))
        await query.answer("Plain icons.")
        return

    if action == "send":
        await state.clear()
        await state.set_state(IconSetup.collect)
        if isinstance(query.message, Message):
            await _ask(
                query.message,
                "Send me the premium emoji you want the panel to use — one "
                "message, as many as you like\\. I read the ids straight from "
                "the message; nothing connects and nothing logs in\\. "
                "/cancel when done\\.",
            )
        await query.answer()
        return

    if action == "lib":
        # op:emoji:lib:<kind>:<page>
        parts = (query.data or "").split(":")
        kind = parts[3] if len(parts) > 3 else "all"
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        async with session_scope() as session:
            extracted = await panel_emoji_repo.get_map(session)
        await _render(query, views.emoji_library(extracted=extracted, page=page, kind=kind))
        await query.answer()
        return

    if action in ("one", "ask", "del"):
        # op:emoji:<action>:<kind>:<emoji> — the emoji last, because it is the
        # only part that may contain anything, and splitting from the left
        # stops at it.
        parts = (query.data or "").split(":", 4)
        kind = parts[3] if len(parts) > 3 else "all"
        emoticon = parts[4] if len(parts) > 4 else ""
        # Callback data is not trustworthy input: an id set for a character no
        # screen draws would sit in the table for ever, matching nothing.
        alphabet = views.library_alphabet(kind)
        if emoticon not in alphabet:
            await query.answer("Unknown emoji.", show_alert=True)
            return
        page = alphabet.index(emoticon) // views.LIBRARY_PAGE_SIZE

        if action == "ask":
            await state.clear()
            await state.set_state(IconSetup.one)
            await state.update_data(emoticon=emoticon, emoji_kind=kind)
            if isinstance(query.message, Message):
                await _ask(
                    query.message,
                    f"Send the premium emoji to draw in place of {views.escape(emoticon)} "
                    "— or just its id as digits\\. /cancel to leave it alone\\.",
                )
            await query.answer()
            return

        async with session_scope() as session:
            mapping = await panel_emoji_repo.get_map(session)
            if action == "del" and mapping.pop(emoticon, None) is not None:
                await panel_emoji_repo.replace(session, mapping=mapping)
                await event_repo.audit(
                    session,
                    user_id=user_id,
                    action="panel_emoji.unset",
                    object_type="panel_emoji",
                    object_id=emoticon,
                )
                premium_icons.set_map(mapping)
        await _render(
            query,
            views.emoji_one(
                emoticon=emoticon, custom_id=mapping.get(emoticon), page=page, kind=kind
            ),
        )
        await query.answer("Plain again." if action == "del" else None)
        return

    if action == "run":
        # Inline rather than queued, like the sign-in flow: a burst of small
        # searches through the operator's own connection, with the operator
        # watching. One search per icon the panel draws.
        async with session_scope() as session:
            connections = await connection_repo.list_for_user(session, user_id=user_id)
            candidates = [
                c
                for c in connections
                if c.kind is ConnectionKind.user and c.status is ConnectionStatus.active
            ]
            if not candidates:
                await query.answer(
                    "Connect a Telegram account first — the Bot API cannot search emoji.",
                    show_alert=True,
                )
                return
            adapter = await connection_service.adapter_for(session, candidates[0])

        try:
            mapping = await icon_setup.fetch_icons(adapter)
        except Exception as exc:
            log.warning("panel_emoji_extraction_failed", error=str(exc))
            await query.answer(f"Extraction failed: {_describe(exc)}"[:180], show_alert=True)
            return

        async with session_scope() as session:
            await panel_emoji_repo.replace(session, mapping=mapping)
            await event_repo.audit(
                session,
                user_id=user_id,
                action="panel_emoji.extract",
                object_type="panel_emoji",
                payload={"matched": len(mapping), "of": len(views.panel_emoji())},
            )
        premium_icons.set_map(mapping)
        await _render(query, await _emoji_status_screen(user_id))
        await query.answer(f"Matched {len(mapping)} of {len(views.panel_emoji())} icons.")
        return

    await _render(query, await _emoji_status_screen(user_id))
    await query.answer()


def _custom_emoji_pairs(message: Message) -> dict[str, str]:
    """(character → custom_emoji_id) for every premium emoji in a message.

    Offsets are UTF-16 code units — Telegram's counting, not Python's — and an
    emoji is itself a surrogate pair there, so the text is sliced in UTF-16
    bytes rather than by Python index. Getting this wrong maps the *wrong
    character* to an id, which would draw someone's flame on the ✅ icon.
    """
    pairs: dict[str, str] = {}
    text = message.text or message.caption or ""
    raw = text.encode("utf-16-le")
    for entity in message.entities or message.caption_entities or []:
        if entity.type == "custom_emoji" and entity.custom_emoji_id:
            char = raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
            pairs[char] = entity.custom_emoji_id
    return pairs


def _typed_emoji_pairs(text: str) -> dict[str, str]:
    """``🔥 5368324170671202286`` and ``![🔥](tg://emoji?id=…)``, per line.

    Typing the pair is the only route for an id from a pack this account does
    not own — an id alone cannot say *which* panel icon it replaces, so the
    plain emoji has to come with it.
    """
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        for match in premium_icons.CUSTOM_EMOJI_MARKUP.finditer(line):
            pairs[match.group(1)] = match.group(2)
        rest = premium_icons.CUSTOM_EMOJI_MARKUP.sub("", line).strip()
        found = premium_icons.BARE_EMOJI_ID.search(rest)
        if found:
            emoticon = rest.replace(found.group(1), "").strip()
            if emoticon:
                pairs[emoticon] = found.group(1)
    return pairs


def _one_icon_id(message: Message) -> str | None:
    """The one custom-emoji id in a message, however it was sent.

    Three spellings, because all three reach this bot in practice: a premium
    emoji picked from the keyboard (an entity), Telegram's markup form pasted
    from somewhere, or the bare digits. Used where the panel already knows
    *what* the id is for, so the id may arrive alone.
    """
    sent = _custom_emoji_pairs(message)
    if sent:
        return next(iter(sent.values()))
    text = (message.text or message.caption or "").strip()
    as_markup = premium_icons.CUSTOM_EMOJI_MARKUP.search(text)
    if as_markup:
        return as_markup.group(2)
    bare = premium_icons.BARE_EMOJI_ID.search(text)
    return bare.group(1) if bare else None


@router.message(IconSetup.one)
async def icon_one(message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any) -> None:
    data = await state.get_data()
    emoticon = data.get("emoticon")
    stored_kind = data.get("emoji_kind")
    kind = stored_kind if isinstance(stored_kind, str) else "all"
    alphabet = views.library_alphabet(kind)
    if not isinstance(emoticon, str) or emoticon not in alphabet:
        await state.clear()
        await _go_home(message, user_id)
        return

    custom_id = _one_icon_id(message)
    if custom_id is None:
        await _ask(
            message,
            "Nothing usable in that\\. Send the premium emoji itself — from "
            "the *animated* rows of your keyboard, since a plain one carries "
            "no id — or paste the id as digits\\. /cancel to leave it alone\\.",
        )
        return

    async with session_scope() as session:
        mapping = await panel_emoji_repo.get_map(session)
        mapping[emoticon] = custom_id
        await panel_emoji_repo.replace(session, mapping=mapping)
        await event_repo.audit(
            session,
            user_id=user_id,
            action="panel_emoji.set",
            object_type="panel_emoji",
            object_id=emoticon,
            payload={"total": len(mapping)},
        )
    premium_icons.set_map(mapping)

    await state.clear()
    await _send(
        message,
        views.emoji_one(
            emoticon=emoticon,
            custom_id=custom_id,
            page=alphabet.index(emoticon) // views.LIBRARY_PAGE_SIZE,
            kind=kind,
        ),
    )


@router.message(IconSetup.collect)
async def icon_collect(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    pairs = _custom_emoji_pairs(message)
    pairs.update(_typed_emoji_pairs(message.text or message.caption or ""))
    if not pairs:
        await _ask(
            message,
            "Nothing usable in that message\\. Two ways to send one:\n\n"
            "• pick the emoji from the *animated* rows of your keyboard — a "
            "plain keyboard emoji carries no id\\.\n"
            "• or type the pair: `🔥 5368324170671202286`\n\n"
            "Send more, or /cancel\\.",
        )
        return

    async with session_scope() as session:
        merged = await panel_emoji_repo.get_map(session)
        merged.update(pairs)
        await panel_emoji_repo.replace(session, mapping=merged)
        await event_repo.audit(
            session,
            user_id=user_id,
            action="panel_emoji.collect",
            object_type="panel_emoji",
            payload={"added": len(pairs), "total": len(merged)},
        )
    premium_icons.set_map(merged)

    await _ask(
        message,
        f"Got {len(pairs)} — {len(merged)} icons mapped now\\. Send more, or /cancel to finish\\.",
    )


def _inferred_icon(text: str, emoji_map: dict[str, str]) -> str | None:
    """The icon the automatic pass would give a button with this label.

    Same rule as the renderer's: the longest mapped emoji the label starts
    with. Shown on the icon screen so an operator can see what they would be
    overriding before they override it.
    """
    lead = next((e for e in sorted(emoji_map, key=len, reverse=True) if text.startswith(e)), None)
    return emoji_map[lead] if lead else None


async def _reload_button_look(session) -> None:  # type: ignore[no-untyped-def]
    """Push labels, icons and colours into the renderer together.

    One call, because they are one thing from the screen's point of view and
    three separate loads is three chances to reload two of them.
    """
    premium_icons.set_labels(
        await panel_buttons_repo.get_map(session),
        await panel_buttons_repo.get_icons(session),
        await panel_buttons_repo.get_styles(session),
    )


@router.callback_query(F.data.startswith("op:btn"))
async def op_buttons(
    query: CallbackQuery,
    user_id: uuid.UUID,
    state: FSMContext,
    is_operator: bool = False,
    **_extra: Any,
) -> None:
    if not await _require_operator(query, is_operator):
        return
    parts = (query.data or "").split(":")

    if len(parts) >= 4 and parts[2] == "pick" and parts[3].isdigit():
        index = int(parts[3])
        if index >= len(views.RENAMEABLE_BUTTONS):
            await query.answer("Unknown button.", show_alert=True)
            return
        default = views.RENAMEABLE_BUTTONS[index]
        await state.clear()
        await state.set_state(EditButton.text)
        await state.update_data(button_index=index)
        if isinstance(query.message, Message):
            await _ask(
                query.message,
                f"New label for *{views.escape(default)}* — up to 32 characters\\. "
                "Send `-` for the built\\-in label\\.",
            )
        await query.answer()
        return

    if len(parts) >= 4 and parts[2] == "col" and parts[3].isdigit():
        index = int(parts[3])
        if index >= len(views.RENAMEABLE_BUTTONS):
            await query.answer("Unknown button.", show_alert=True)
            return
        default = views.RENAMEABLE_BUTTONS[index]
        async with session_scope() as session:
            styles = await panel_buttons_repo.get_styles(session)
        await _render(
            query,
            views.button_style_picker(
                default_text=default, index=index, current=styles.get(default)
            ),
        )
        await query.answer()
        return

    if len(parts) >= 4 and parts[2] in ("ico", "ask", "auto") and parts[3].isdigit():
        index = int(parts[3])
        if index >= len(views.RENAMEABLE_BUTTONS):
            await query.answer("Unknown button.", show_alert=True)
            return
        default = views.RENAMEABLE_BUTTONS[index]

        if parts[2] == "ask":
            await state.clear()
            await state.set_state(EditButton.icon)
            await state.update_data(button_index=index)
            if isinstance(query.message, Message):
                await _ask(
                    query.message,
                    f"Send the icon for *{views.escape(default)}* — the premium "
                    "emoji itself, or its id as digits\\. The button keeps its "
                    "words; only the picture in front of them changes\\.",
                )
            await query.answer()
            return

        async with session_scope() as session:
            if parts[2] == "auto":
                await panel_buttons_repo.set_icon(session, default_text=default, icon_id=None)
                await event_repo.audit(
                    session,
                    user_id=user_id,
                    action="panel_button.icon",
                    object_type="panel_button",
                    object_id=default,
                    payload={"icon": None},
                )
                await _reload_button_look(session)
            icons = await panel_buttons_repo.get_icons(session)
            emoji_map = await panel_emoji_repo.get_map(session)
        await _render(
            query,
            views.button_icon_picker(
                default_text=default,
                index=index,
                current=icons.get(default),
                inherited=_inferred_icon(default, emoji_map),
            ),
        )
        await query.answer("Automatic again." if parts[2] == "auto" else None)
        return

    if len(parts) >= 5 and parts[2] == "sty":
        index = int(parts[3]) if parts[3].isdigit() else -1
        choice = int(parts[4]) if parts[4].isdigit() else -1
        if not (0 <= index < len(views.RENAMEABLE_BUTTONS)) or not (
            0 <= choice < len(views.BUTTON_STYLES)
        ):
            await query.answer("Unknown colour.", show_alert=True)
            return
        default = views.RENAMEABLE_BUTTONS[index]
        label, value = views.BUTTON_STYLES[choice]
        async with session_scope() as session:
            await panel_buttons_repo.set_style(session, default_text=default, style=value)
            await event_repo.audit(
                session,
                user_id=user_id,
                action="panel_button.style",
                object_type="panel_button",
                object_id=default,
                payload={"style": value},
            )
            await _reload_button_look(session)
            styles = await panel_buttons_repo.get_styles(session)
        await _render(
            query,
            views.button_style_picker(
                default_text=default, index=index, current=styles.get(default)
            ),
        )
        await query.answer(label)
        return

    page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
    async with session_scope() as session:
        custom = await panel_buttons_repo.get_map(session)
    await _render(query, views.panel_buttons_list(custom=custom, page=page))
    await query.answer()


@router.message(EditButton.icon)
async def button_icon(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    data = await state.get_data()
    index = data.get("button_index")
    if not isinstance(index, int) or index >= len(views.RENAMEABLE_BUTTONS):
        await state.clear()
        await _go_home(message, user_id)
        return
    default = views.RENAMEABLE_BUTTONS[index]

    icon_id = _one_icon_id(message)
    if icon_id is None:
        await _ask(
            message,
            "Nothing usable in that\\. Send the premium emoji itself — from "
            "the *animated* rows of your keyboard — or paste its id as "
            "digits\\. /cancel to leave the button alone\\.",
        )
        return

    async with session_scope() as session:
        await panel_buttons_repo.set_icon(session, default_text=default, icon_id=icon_id)
        await event_repo.audit(
            session,
            user_id=user_id,
            action="panel_button.icon",
            object_type="panel_button",
            object_id=default,
            payload={"icon": icon_id},
        )
        await _reload_button_look(session)
        emoji_map = await panel_emoji_repo.get_map(session)

    await state.clear()
    await _send(
        message,
        views.button_icon_picker(
            default_text=default,
            index=index,
            current=icon_id,
            inherited=_inferred_icon(default, emoji_map),
        ),
    )


@router.message(EditButton.text)
async def button_label(
    message: Message, user_id: uuid.UUID, state: FSMContext, **_extra: Any
) -> None:
    data = await state.get_data()
    index = data.get("button_index")
    if not isinstance(index, int) or index >= len(views.RENAMEABLE_BUTTONS):
        await state.clear()
        await _go_home(message, user_id)
        return
    default = views.RENAMEABLE_BUTTONS[index]

    label = (message.text or "").strip()

    # An id sent here is meant as the button's *icon*, not as its words. Left
    # in the label it renders as eighteen digits, which is exactly what the
    # operator saw and reported.
    icon_id: str | None = None
    sent_emoji = _custom_emoji_pairs(message)
    if sent_emoji:
        emoticon, icon_id = next(iter(sent_emoji.items()))
        label = label.replace(emoticon, "").strip()
    else:
        as_markup = premium_icons.CUSTOM_EMOJI_MARKUP.search(label)
        bare = premium_icons.BARE_EMOJI_ID.search(label)
        if as_markup:
            icon_id = as_markup.group(2)
            label = premium_icons.CUSTOM_EMOJI_MARKUP.sub("", label).strip()
        elif bare:
            icon_id = bare.group(1)
            label = label.replace(bare.group(1), "").strip()

    if icon_id and not label:
        await _ask(
            message,
            "That is an icon with no words\\. Telegram needs text on a button, "
            "so send them together — `5368324170671202286 Ads`\\.",
        )
        return
    if label != "-" and not (1 <= len(label) <= 32):
        await _ask(message, "Between 1 and 32 characters, or `-` to reset\\.")
        return
    if "\n" in label:
        await _ask(message, "One line — a button has no second one\\.")
        return

    async with session_scope() as session:
        if label == "-":
            await panel_buttons_repo.reset_label(session, default_text=default)
        else:
            await panel_buttons_repo.set_label(
                session, default_text=default, custom_text=label, icon_id=icon_id
            )
        await event_repo.audit(
            session,
            user_id=user_id,
            action="panel_button.rename",
            object_type="panel_button",
            object_id=default,
            payload={"custom": None if label == "-" else label},
        )
        await _reload_button_look(session)
        custom = await panel_buttons_repo.get_map(session)

    await state.clear()
    await _send(
        message, views.panel_buttons_list(custom=custom, page=index // views.BUTTONS_PAGE_SIZE)
    )


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

        if action in ("groups", "gall", "gchan", "ggrp"):
            kind = {"gchan": "channels", "ggrp": "groups"}.get(action, "all")
            page = 0
            parts = (query.data or "").split(":")
            if len(parts) > 3 and parts[3].isdigit():
                page = int(parts[3])
            chats = await chat_repo.list_filtered(session, user_id=target.id, limit=2000)
            await _fill_page_details(
                session, target.id, views.page_of_chats(chats=chats, page=page, kind=kind)
            )
            await _render(query, views.user_groups(user=target, chats=chats, page=page, kind=kind))
            await query.answer()
            return

        if action in ("arcon", "arcoff"):
            # A decision about this one account, which outranks the default in
            # both directions — see ``archive_service._is_archived``.
            target.archive_ads = action == "arcon"
            await event_repo.audit(
                session,
                user_id=user_id,
                action="user.archive_ads",
                object_type="user",
                object_id=str(target.id),
                payload={"archive_ads": target.archive_ads},
            )
            notice = (
                "Their ads will be copied to your archive."
                if target.archive_ads
                else "Their ads will not be copied."
            )

        elif action == "arcauto":
            # Back to following the deployment default, which is different from
            # being switched off: flipping the default moves this account again.
            target.archive_ads = None
            notice = "Following the default."

        elif action == "asksus":
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
        setting = await archive_service.any_operator_setting(session)
        screen = views.user_detail(
            user=target,
            activity=activity,
            archive_default_on=bool(setting and setting.archive_all_users),
        )

    await _render(query, screen)
    await query.answer(notice)


@router.callback_query()
async def unknown_callback(query: CallbackQuery, **_extra: Any) -> None:
    await query.answer("That button is no longer valid. Tap /panel to reopen.", show_alert=True)
