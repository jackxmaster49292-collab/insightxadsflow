"""The gate every update passes through.

Anyone on Telegram can find and message a bot, so this middleware is the whole
access model for this surface. It runs before **every** handler — messages and
button callbacks alike — so a handler cannot forget the check. Forgetting it
once would let a stranger drive someone else's account.

It answers four questions in order, and each has a different failure:

1. **Are they allowed in at all?** In ``closed`` mode only the operator ids are.
   In ``open`` mode anyone is, which is a deliberate deployment choice.
2. **Are they suspended?** They are told, with the reason, rather than being
   silently ignored.
3. **Have they accepted the terms?** Until they do, the only screen that
   resolves is the terms screen — and the accept button itself, or the gate
   would be impossible to pass.
4. **Are they flooding the bot?** An open bot is reachable by anyone, so a
   per-person throttle keeps one client from occupying the panel.

Authorization is decided here and nowhere else; handlers receive a resolved
``user_id`` and use ``user_id``-scoped repositories from there on.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from app.config import get_settings
from app.db.session import session_scope
from app.repositories import admins as admin_repo
from app.repositories import events as event_repo
from app.security.ratelimit import check_rate_limit

log = structlog.get_logger(__name__)

DENIED_TEXT = (
    "This bot is a private control panel and your Telegram account is not authorized to use it."
)

THROTTLED_TEXT = "You are sending requests too quickly. Wait a moment and try again."

#: Callbacks that must work before the terms are accepted, or the gate could
#: never be passed.
TERMS_EXEMPT = frozenset({"terms:accept", "terms:show"})

#: Rate-limit bucket for bot updates. Generous — this is protection against a
#: stuck client or a script, not a restriction on ordinary use. Tapping through
#: the panel produces a handful of updates a second at most.
THROTTLE_BUCKET = "bot_update"


def suspended_text(reason: str | None) -> str:
    base = "Your access to this bot has been suspended."
    return f"{base}\n\nReason: {reason}" if reason else base


class AccessMiddleware(BaseMiddleware):
    """Resolves the caller, enforces access, and injects the app user."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        sender: TgUser | None = data.get("event_from_user")
        if sender is None:
            return None

        settings = get_settings()
        # The settings file, and nothing else. Operator access is not something
        # the panel can hand out: on an open deployment anyone gets an account
        # by messaging the bot, so the list of people who can act on every
        # account stays a deployment decision, made outside the product.
        is_operator = settings.is_admin(sender.id)

        # --- 1. allowed in at all? -----------------------------------------
        if not settings.open_access and not is_operator:
            log.warning("access_denied", telegram_user_id=sender.id)
            # Written in its own transaction: returning below discards the
            # request session, and a denied-access record must survive the
            # rejection that caused it.
            async with session_scope() as session:
                await event_repo.audit(
                    session,
                    user_id=None,
                    action="admin.access_denied",
                    object_type="telegram_user",
                    object_id=str(sender.id),
                )
            await _reply(event, DENIED_TEXT)
            return None

        # --- 2. throttle ---------------------------------------------------
        verdict = await check_rate_limit(THROTTLE_BUCKET, str(sender.id))
        if not verdict.allowed:
            log.info("bot_update_throttled", telegram_user_id=sender.id)
            await _reply(event, THROTTLED_TEXT, alert=True)
            return None

        async with session_scope() as session:
            user = await admin_repo.upsert_user(
                session, telegram_user_id=sender.id, username=sender.username
            )

            # --- 3. suspended? ---------------------------------------------
            if not user.is_active:
                await _reply(event, suspended_text(user.suspended_reason), alert=True)
                return None

            data["user_id"] = user.id
            data["user_email"] = user.email
            data["is_operator"] = is_operator
            data["terms_accepted"] = user.terms_accepted_at is not None

        # --- 4. terms ------------------------------------------------------
        # Enforced here rather than in each handler, for the same reason the
        # access check is: a handler can forget, and forgetting once would let
        # someone use the tool without ever seeing what they are responsible
        # for. The accept button is exempt, or the gate could never be passed.
        if not data["terms_accepted"] and not _is_terms_step(event):
            await _show_terms(event)
            return None

        return await handler(event, data)


def _is_terms_step(event: TelegramObject) -> bool:
    """Is this the accept button, or /start showing the terms?"""
    if isinstance(event, CallbackQuery):
        return (event.data or "") in TERMS_EXEMPT
    return False


async def _show_terms(event: TelegramObject) -> None:
    """Send the terms screen instead of whatever was asked for."""
    # Imported here rather than at module scope: views imports the ORM models,
    # and a top-level import would make this middleware part of that cycle.
    from app.adminbot.views import terms

    screen = terms()
    if isinstance(event, CallbackQuery):
        await event.answer()
        message = event.message
        if isinstance(message, Message):
            await message.answer(screen.text, reply_markup=screen.keyboard, parse_mode="MarkdownV2")
    elif isinstance(event, Message):
        await event.answer(screen.text, reply_markup=screen.keyboard, parse_mode="MarkdownV2")


async def _reply(event: TelegramObject, text: str, *, alert: bool = True) -> None:
    # Same response either way: do not reveal whether the panel exists or who
    # owns it.
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=alert)
    elif isinstance(event, Message):
        await event.answer(text)
