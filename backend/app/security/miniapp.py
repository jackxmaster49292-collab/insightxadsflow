"""Telegram Mini App ``initData`` verification.

This is how a Mini App proves *which Telegram user* opened it. Telegram signs the
launch payload with a key derived from the bot token, so the check is a real
cryptographic proof — not "trust the user_id the client sent".

Per Telegram's Mini Apps documentation:

    secret_key       = HMAC_SHA256(<bot_token>, "WebAppData")
    data_check_string = every field except `hash`, sorted alphabetically,
                        joined as "key=value" with \\n between them
    valid            = hex(HMAC_SHA256(data_check_string, secret_key)) == hash

``auth_date`` is checked too, so a captured launch payload cannot be replayed
forever.

This module replaces password authentication for the Telegram surface, so it is
security-critical and carries a 100% coverage requirement alongside
``classify_error`` and the redaction filter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class InitDataError(Exception):
    """initData could not be verified. Never carries the raw payload."""


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    telegram_user_id: int
    username: str | None
    first_name: str | None
    auth_date: int


def _secret_key(bot_token: str) -> bytes:
    # Note the argument order: the *constant* is the HMAC key here, and the bot
    # token is the message. Reversing it silently accepts nothing.
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def verify_init_data(
    init_data: str,
    *,
    bot_token: str,
    max_age_s: int = 86_400,
    now: float | None = None,
) -> TelegramIdentity:
    """Validate a raw ``Telegram.WebApp.initData`` query string.

    Raises :class:`InitDataError` on any problem. Returns the verified identity.
    """
    if not init_data:
        raise InitDataError("Empty initData")
    if not bot_token:
        raise InitDataError("No admin bot token configured")

    # keep_blank_values so a present-but-empty field still participates in the
    # signature exactly as Telegram computed it.
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("initData has no hash")

    # `signature` is for third-party verification and is excluded from the
    # bot-side data-check-string.
    pairs.pop("signature", None)

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    expected = hmac.new(
        _secret_key(bot_token), data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("initData signature mismatch")

    raw_auth_date = pairs.get("auth_date")
    if not raw_auth_date:
        raise InitDataError("initData has no auth_date")
    try:
        auth_date = int(raw_auth_date)
    except ValueError as exc:
        raise InitDataError("initData auth_date is not an integer") from exc

    current = time.time() if now is None else now
    age = current - auth_date
    if age > max_age_s:
        raise InitDataError("initData has expired")
    # A launch stamped in the future means a clock problem or a forged payload.
    if age < -300:
        raise InitDataError("initData auth_date is in the future")

    raw_user = pairs.get("user")
    if not raw_user:
        raise InitDataError("initData has no user")
    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise InitDataError("initData user is not valid JSON") from exc

    telegram_user_id = user.get("id")
    if not isinstance(telegram_user_id, int):
        raise InitDataError("initData user has no numeric id")

    return TelegramIdentity(
        telegram_user_id=telegram_user_id,
        username=user.get("username"),
        first_name=user.get("first_name"),
        auth_date=auth_date,
    )


def build_init_data(payload: dict[str, str], *, bot_token: str) -> str:
    """Sign a payload the way Telegram would. Test helper only."""
    from urllib.parse import urlencode

    data_check_string = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
    signature = hmac.new(
        _secret_key(bot_token), data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode({**payload, "hash": signature})
