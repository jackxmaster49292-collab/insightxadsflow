"""What each connection type can actually do.

This is a product statement, not an implementation detail, so it lives in one
place and every adapter — including the mock — reports the same thing. The UI
renders it verbatim on the Connections page so the customer always knows what
the active connection can and cannot reach.
"""

from __future__ import annotations

from app.adapters.base import Capabilities

MAX_BOT_DOWNLOAD_BYTES = 20 * 1024 * 1024

BOT_NOTES = [
    "A bot cannot list its own chats. Add the bot to a chat and send a message there, "
    "and the chat will appear after the next synchronization.",
    "A bot receives all messages from channels where it is a member.",
    "In groups the bot must be an administrator (or have privacy mode disabled) to read "
    "messages — otherwise it only sees commands and replies.",
    "Bots can download files up to 20 MB.",
]

USER_NOTES = [
    "This connection acts as your Telegram account and can read any chat it has joined, "
    "including channels you only subscribe to.",
    "Telegram may restrict the account if it is used abusively. Prefer a bot connection "
    "wherever one is sufficient.",
]


def capabilities_for(kind: str) -> Capabilities:
    if kind == "user":
        return Capabilities(
            can_read_subscribed_channels=True,
            can_read_group_messages=True,
            can_read_history=True,
            max_download_bytes=None,
            notes=list(USER_NOTES),
        )
    return Capabilities(
        can_read_subscribed_channels=False,
        can_read_group_messages=False,
        can_read_history=False,
        max_download_bytes=MAX_BOT_DOWNLOAD_BYTES,
        notes=list(BOT_NOTES),
    )
