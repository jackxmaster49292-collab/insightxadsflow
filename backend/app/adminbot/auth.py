"""Allowlist enforcement for the Telegram control panel.

Anyone on Telegram can find and message a bot. This middleware is the single
gate: it runs before **every** handler — messages and button callbacks alike —
so a handler cannot forget the check. Forgetting it once would let a stranger
control someone else's forwarding rules.

The allowlist is empty by default, so a misconfigured deployment is locked
rather than open.
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

log = structlog.get_logger(__name__)

DENIED_TEXT = (
    "This bot is a private control panel and your Telegram account is not authorized to use it."
)


class AdminOnlyMiddleware(BaseMiddleware):
    """Rejects non-allowlisted senders and injects the resolved app user."""

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
        if not settings.is_admin(sender.id):
            log.warning("admin_access_denied", telegram_user_id=sender.id)
            async with session_scope() as session:
                await event_repo.audit(
                    session,
                    user_id=None,
                    action="admin.access_denied",
                    object_type="telegram_user",
                    object_id=str(sender.id),
                )
            await _reject(event)
            return None

        async with session_scope() as session:
            user = await admin_repo.upsert_admin(
                session, telegram_user_id=sender.id, username=sender.username
            )
            data["user_id"] = user.id
            data["user_email"] = user.email

        return await handler(event, data)


async def _reject(event: TelegramObject) -> None:
    # Same response either way: do not reveal whether the panel exists or who
    # owns it.
    if isinstance(event, CallbackQuery):
        await event.answer(DENIED_TEXT, show_alert=True)
    elif isinstance(event, Message):
        await event.answer(DENIED_TEXT)
