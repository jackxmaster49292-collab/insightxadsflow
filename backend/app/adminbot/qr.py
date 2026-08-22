"""Rendering a QR sign-in as an image, and waiting for it to be scanned.

Why the panel prefers this over asking for a login code: Telegram cancels any
login code it sees an account send inside a Telegram chat. Typing the code into
a bot therefore burns it, and the sign-in fails with "the code was previously
shared by your account" even though the digits were right. That protection is
deliberate and is not worked around here — a QR is simply the sign-in that has
no code to leak.

The wait runs as a background task because Telegram's tokens expire in well
under a minute, so the flow is: show, wait, refresh, show again. Blocking the
handler for that would freeze the whole panel.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import uuid

import segno
import structlog
from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.adminbot import views
from app.db.models import ConnectionStatus
from app.db.session import session_scope
from app.repositories import connections as connection_repo
from app.services import connections as connection_service

log = structlog.get_logger(__name__)

#: How long to wait on one token before refreshing it. Telegram expires QR
#: tokens quickly; a short wait plus a refresh is what keeps the code on screen
#: valid rather than silently dead.
WAIT_SLICE_S = 25.0

#: How long to keep offering fresh codes before giving up on the whole attempt.
#: Long enough to find your phone, short enough not to hold a client open all day.
TOTAL_WAIT_S = 300.0

PARSE_MODE = "MarkdownV2"


def render(url: str) -> bytes:
    """A QR image for a ``tg://login?token=…`` URL.

    Scaled up and given a quiet border because it is going to be scanned off one
    phone screen by another camera, which is the least forgiving case.
    """
    buffer = io.BytesIO()
    segno.make(url, error="m").save(buffer, kind="png", scale=8, border=3)
    return buffer.getvalue()


def caption(*, attempt: int = 1) -> str:
    lines = [
        "📷 *Scan this with the Telegram app*",
        "",
        "On the phone with the account you want to connect:",
        "",
        "*Settings* → *Devices* → *Link Desktop Device*",
        "",
        "Then point it at this code\\.",
        "",
        "_Nothing secret is typed anywhere, so Telegram has no login code to "
        "cancel — which is why this works and typing a code into a chat does "
        "not\\._",
    ]
    if attempt > 1:
        lines.insert(1, f"\n_Code {attempt} — the previous one expired\\._")
    return "\n".join(lines)


def cancel_keyboard(connection_id: uuid.UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Cancel", callback_data=f"conn:{connection_id}:abandon")]
        ]
    )


async def send_code(
    bot: Bot,
    chat_id: int,
    url: str,
    *,
    connection_id: uuid.UUID,
    attempt: int = 1,
) -> Message:
    return await bot.send_photo(
        chat_id,
        BufferedInputFile(render(url), filename="login-qr.png"),
        caption=caption(attempt=attempt),
        parse_mode=PARSE_MODE,
        reply_markup=cancel_keyboard(connection_id),
    )


async def watch(
    bot: Bot,
    *,
    chat_id: int,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> None:
    """Wait for the scan, refreshing the code as tokens expire.

    Runs detached from the handler. Every exit path tells the customer what
    happened — a QR that silently stops working is worse than no QR.
    """
    attempt = 1
    deadline = TOTAL_WAIT_S
    last_message = None

    try:
        while deadline > 0:
            async with session_scope() as session:
                connection = await connection_repo.get(
                    session, user_id=user_id, connection_id=connection_id
                )
                if connection is None:
                    return  # cancelled from the panel
                try:
                    status = await connection_service.await_qr_scan(
                        session, connection=connection, timeout_s=min(WAIT_SLICE_S, deadline)
                    )
                except connection_service.QrExpired:
                    status = None
                except connection_service.ConnectionNotReady:
                    return
                except Exception as exc:
                    from app.adapters.errors import classify_error
                    from app.domain import reasons

                    code = classify_error(exc).code
                    log.warning("qr_login_failed", connection_id=str(connection_id), code=code)
                    await bot.send_message(
                        chat_id,
                        f"Sign\\-in failed\\.\n\n_{views.escape(reasons.describe(code))}_",
                        parse_mode=PARSE_MODE,
                    )
                    await connection_service.abandon(session, connection=connection)
                    return

            if status is ConnectionStatus.active:
                await _finish(bot, chat_id, user_id, connection_id)
                return

            if status is ConnectionStatus.awaiting_2fa:
                await bot.send_message(
                    chat_id,
                    "🔐 Scanned\\. This account has two\\-step verification — send "
                    "the password to finish\\.\n\n"
                    "It is used once and never stored\\. Your message is deleted "
                    "as soon as it is read\\.",
                    parse_mode=PARSE_MODE,
                )
                return

            # Token expired: issue a fresh one rather than making them start over.
            deadline -= WAIT_SLICE_S
            if deadline <= 0:
                break
            attempt += 1
            try:
                qr = await connection_service.refresh_qr(connection_id)
            except connection_service.ConnectionNotReady:
                return
            with contextlib.suppress(Exception):
                if last_message is not None:
                    await last_message.delete()
            last_message = await send_code(
                bot, chat_id, qr.url, connection_id=connection_id, attempt=attempt
            )

        await bot.send_message(
            chat_id,
            "The sign\\-in timed out — nobody scanned the code\\.\n\n"
            "Open *Accounts* and start again when you are ready\\.",
            parse_mode=PARSE_MODE,
        )
        async with session_scope() as session:
            connection = await connection_repo.get(
                session, user_id=user_id, connection_id=connection_id
            )
            if connection is not None and connection.status is not ConnectionStatus.active:
                await connection_service.abandon(session, connection=connection)

    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        raise
    except Exception as exc:  # pragma: no cover - defensive
        log.error("qr_watch_failed", connection_id=str(connection_id), error=exc)


async def _finish(bot: Bot, chat_id: int, user_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Signed in. Queue a group sync so the panel is useful straight away."""
    from app.db.models import ControlTaskKind
    from app.repositories import events as event_repo
    from app.repositories import jobs as job_repo

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
            payload={"via": "telegram", "kind": "user", "method": "qr"},
        )

    await bot.send_message(
        chat_id,
        "✅ Account connected\\.\n\nI am reading the groups it has already "
        "joined — that takes a few seconds\\. Then you can post an ad\\.",
        parse_mode=PARSE_MODE,
    )
