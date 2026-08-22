"""Handling credentials that arrive as Telegram messages.

The panel asks for bot tokens, phone numbers, login codes and 2FA passwords in
the chat, because that is where the panel is. Telegram stores chat history on
its servers, so a message containing a secret is a copy of that secret sitting
somewhere we do not control.

Nothing here can undo that. What it does is narrow the window and make the
tradeoff visible:

* the customer's message is deleted the moment it is read, on both sides;
* the prompt says plainly that the value will be deleted and why;
* the value is passed straight to the service layer and never echoed back,
  never written to FSM state, and never logged — the redaction filter is the
  backstop, not the plan.

Deletion is best-effort by definition: Telegram refuses to delete another
account's message after 48 hours, and a network failure can lose the call. A
failure to delete is logged without the value and never blocks the flow.
"""

from __future__ import annotations

import contextlib

import structlog
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

log = structlog.get_logger(__name__)

#: Shown before asking for anything sensitive. Says what will happen, rather
#: than reassuring the customer that it is safe — it is a real tradeoff.
WARNING = (
    "⚠️ Telegram keeps chat history on its servers\\. Your next message will be "
    "deleted from this chat straight away, but treat anything typed here as "
    "having been on Telegram's servers for a moment\\."
)


async def consume(message: Message) -> str:
    """Read a secret out of a message and delete the message.

    Returns the raw text. The caller must pass it directly to the service that
    needs it and let it go out of scope — no state, no logs, no echo.
    """
    value = (message.text or "").strip()
    await erase(message)
    return value


async def erase(message: Message) -> None:
    """Delete a message, tolerating every reason Telegram might refuse.

    Deletion is not allowed to fail the flow: a customer who cannot finish
    connecting because a delete call timed out is worse off than one whose
    message lingered a few seconds.
    """
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        # Older than 48h, already gone, or the bot lacks the right in this chat.
        log.info("secret_message_not_deleted", reason=type(exc).__name__)
    except Exception as exc:  # pragma: no cover - transport failures
        log.warning("secret_message_delete_failed", error=exc)


async def erase_later(message: Message | None) -> None:
    """Delete a message we sent, ignoring anything that goes wrong."""
    if message is None:
        return
    with contextlib.suppress(Exception):
        await message.delete()
