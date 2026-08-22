"""Structured-log redaction.

Applied *before* any sink. Two layers, because either alone is insufficient:

1. Key denylist  — catches ``{"bot_token": "..."}``.
2. Pattern scrub — catches a secret embedded in a free-text message or an
   exception string (Telethon exceptions can embed request parameters).

Message *content* is never logged at all; events store a length, a media type,
and a reason code instead. See docs/SECURITY.md §6.
"""

from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any, cast

REDACTED = "[REDACTED]"

#: Substring match against the lower-cased key. Deliberately broad.
DENYLISTED_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "bot_token",
        "session",
        "session_string",
        "access_hash",
        "password",
        "passwd",
        "secret",
        "code",
        "login_code",
        "phone",
        "authorization",
        "cookie",
        "set-cookie",
        "2fa",
        "two_factor",
        "dek",
        "kek",
        "encryption_kek",
        "api_hash",
        "ciphertext",
        "credential",
        # A DSN carries the password inside it, so the whole string is a secret.
        "dsn",
        "database_url",
        "redis_url",
        "connection_string",
        "conn_str",
    }
)

#: Keys that merely *contain* a denylisted word but are safe and useful to keep.
KEY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "reason_code",
        "error_code",
        "status_code",
        "last_error_code",
        "source_reason_code",
        "destination_reason_code",
        "paused_reason_code",
        "http_status_code",
        "phone_hash",
    }
)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Telegram bot token: <bot id>:<secret>. The secret is conventionally 35
    # characters, but pinning that exactly let longer tokens through — match
    # generously, since over-redacting a log line costs nothing.
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"),
    # E.164 phone numbers
    re.compile(r"(?<![\w.])\+\d{7,15}\b"),
    # Telethon StringSession blobs and other long opaque base64
    re.compile(r"\b1[A-Za-z0-9+/=_-]{80,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{60,}={0,2}\b"),
    # Bare login codes when adjacent to an obvious label
    re.compile(r"(?i)\b(login[_ ]?code|otp|2fa)\b\s*[:=]\s*\S+"),
)


def _key_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered in KEY_ALLOWLIST:
        return False
    return any(bad in lowered for bad in DENYLISTED_KEYS)


def scrub_text(value: str) -> str:
    """Remove secret-shaped substrings from free text."""
    for pattern in _PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a log-event payload."""
    if _depth > 8:
        return REDACTED
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _key_is_sensitive(key):
                out[key] = REDACTED
            else:
                out[key] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return type(value)(redact(v, _depth=_depth + 1) for v in value)
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, BaseException):
        # Never surface a raw provider exception string.
        return f"{type(value).__name__}: {scrub_text(str(value))}"
    return value


def redaction_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor entry point."""
    return cast("MutableMapping[str, Any]", redact(dict(event_dict)))
