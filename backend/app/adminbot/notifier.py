"""Push alerts to the Telegram control panel.

The worker writes rows into ``admin_notifications``; this drains them and sends.
Going through the database rather than calling the Bot API from the worker means
an alert survives a bot restart — the same durability rule the forwarding
pipeline follows.

This is the main advantage of the Telegram surface over the web panel: a paused
rule reaches the operator instead of waiting to be noticed.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from aiogram import Bot
from sqlalchemy import select

from app.adminbot import premium_icons
from app.db.models import User
from app.db.session import session_scope
from app.repositories import admins as admin_repo
from app.security.redaction import scrub_text

log = structlog.get_logger(__name__)

POLL_INTERVAL_S = 5


def _escape(text: str) -> str:
    from app.adminbot.views import escape

    return escape(text)


async def drain_once(bot: Bot) -> int:
    """Send every pending alert. Returns how many were delivered."""
    sent = 0
    async with session_scope() as session:
        pending = await admin_repo.claim_unsent(session)
        for notification in pending:
            user = (
                await session.execute(select(User).where(User.id == notification.user_id))
            ).scalar_one_or_none()

            if user is None or user.telegram_user_id is None:
                # Nowhere to deliver — a password-only account. Mark it done
                # rather than retrying forever.
                await admin_repo.mark_sent(session, notification_id=notification.id)
                continue

            body = scrub_text(notification.body)
            text = f"⚠️ *{_escape(notification.title)}*\n\n{_escape(body)}"

            chat_id = user.telegram_user_id

            async def send(styled: str, _markup: object, chat_id: int = chat_id) -> None:
                await bot.send_message(chat_id, styled, parse_mode="MarkdownV2")

            try:
                # Through the same transform as every panel screen. Sending
                # here directly was the one path that did not, so an operator
                # with premium icons everywhere still got a plain ⚠️ on the
                # one message that arrives unasked.
                await premium_icons.deliver(send, text)
            except Exception as exc:
                # A blocked bot or deleted chat must not stall the outbox; the
                # attempt counter retires it after a few tries.
                await admin_repo.mark_attempt(session, notification_id=notification.id)
                log.warning(
                    "admin_notification_failed",
                    notification_id=str(notification.id),
                    error=exc,
                )
                continue

            await admin_repo.mark_sent(session, notification_id=notification.id)
            sent += 1
    return sent


async def run(bot: Bot, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            delivered = await drain_once(bot)
            if delivered:
                log.info("admin_notifications_sent", count=delivered)
        except Exception as exc:
            log.error("notifier_loop_failed", error=exc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
