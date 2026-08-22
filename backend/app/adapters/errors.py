"""Provider-agnostic error taxonomy.

Every provider exception lands in exactly one :class:`ErrorClass`. Unmapped
errors become ``UNKNOWN`` and are treated conservatively — limited retries then
``needs_attention`` — never retried forever.

Classification is done on exception *class name* and on Telegram's stable error
*code strings* rather than on ``isinstance`` checks, so a Telethon or aiogram
upgrade that moves a class cannot silently reclassify an error as retryable.

100% branch coverage is required here (docs/TESTING.md §5).
"""

from __future__ import annotations

import asyncio
import enum
import re
from dataclasses import dataclass

from app.security.redaction import scrub_text


class ErrorClass(enum.StrEnum):
    TRANSIENT = "TRANSIENT"
    RATE_LIMIT = "RATE_LIMIT"
    AUTH = "AUTH"
    PERMISSION = "PERMISSION"
    PERMANENT_CONTENT = "PERMANENT_CONTENT"
    UNKNOWN = "UNKNOWN"


#: Classes that must never be retried by the worker.
NON_RETRYABLE: frozenset[ErrorClass] = frozenset(
    {ErrorClass.AUTH, ErrorClass.PERMISSION, ErrorClass.PERMANENT_CONTENT}
)


@dataclass(frozen=True, slots=True)
class ClassifiedError:
    error_class: ErrorClass
    code: str
    #: Provider-specified wait. When present it is obeyed *in full* — backoff
    #: never shortens it.
    retry_after_s: float | None
    safe_message: str

    @property
    def retryable(self) -> bool:
        return self.error_class not in NON_RETRYABLE


class AdapterError(Exception):
    """Raised by adapters when they can classify a failure themselves."""

    def __init__(self, code: str, error_class: ErrorClass, *, retry_after_s: float | None = None):
        super().__init__(code)
        self.code = code
        self.error_class = error_class
        self.retry_after_s = retry_after_s


_BY_EXCEPTION_NAME: dict[str, tuple[ErrorClass, str]] = {
    # --- Telethon (MTProto) ---
    "FloodWaitError": (ErrorClass.RATE_LIMIT, "flood_wait"),
    "FloodPremiumWaitError": (ErrorClass.RATE_LIMIT, "flood_wait"),
    "SlowModeWaitError": (ErrorClass.RATE_LIMIT, "slowmode_wait"),
    "ServerError": (ErrorClass.TRANSIENT, "server_error"),
    "RpcCallFailError": (ErrorClass.TRANSIENT, "rpc_call_fail"),
    "TimedOutError": (ErrorClass.TRANSIENT, "timeout"),
    "AuthKeyUnregisteredError": (ErrorClass.AUTH, "auth_key_unregistered"),
    "AuthKeyDuplicatedError": (ErrorClass.AUTH, "auth_key_duplicated"),
    "SessionRevokedError": (ErrorClass.AUTH, "session_revoked"),
    "SessionExpiredError": (ErrorClass.AUTH, "session_expired"),
    "UserDeactivatedError": (ErrorClass.AUTH, "user_deactivated"),
    "UserDeactivatedBanError": (ErrorClass.AUTH, "user_deactivated_ban"),
    "UnauthorizedError": (ErrorClass.AUTH, "unauthorized"),
    "ChatWriteForbiddenError": (ErrorClass.PERMISSION, "write_forbidden"),
    "ChatAdminRequiredError": (ErrorClass.PERMISSION, "admin_required"),
    "ChatSendMediaForbiddenError": (ErrorClass.PERMISSION, "send_media_forbidden"),
    "UserBannedInChannelError": (ErrorClass.PERMISSION, "banned"),
    "ChannelPrivateError": (ErrorClass.PERMISSION, "channel_private"),
    "PeerIdInvalidError": (ErrorClass.PERMISSION, "peer_invalid"),
    # A basic group upgraded to a supergroup gets a NEW id; the old peer is dead.
    # Permanent, because retrying a dead identifier can never succeed.
    "ChatMigratedError": (ErrorClass.PERMISSION, "chat_migrated"),
    "ChannelMigratedError": (ErrorClass.PERMISSION, "chat_migrated"),
    "ChatForwardsRestrictedError": (ErrorClass.PERMANENT_CONTENT, "protected_content"),
    "MessageIdInvalidError": (ErrorClass.PERMANENT_CONTENT, "message_unavailable"),
    "MessageDeleteForbiddenError": (ErrorClass.PERMANENT_CONTENT, "message_unavailable"),
    "MediaEmptyError": (ErrorClass.PERMANENT_CONTENT, "media_unavailable"),
    "MessageTooLongError": (ErrorClass.PERMANENT_CONTENT, "message_too_long"),
    # --- aiogram (Bot API) ---
    "TelegramRetryAfter": (ErrorClass.RATE_LIMIT, "flood_wait"),
    "TelegramUnauthorizedError": (ErrorClass.AUTH, "unauthorized"),
    "TelegramForbiddenError": (ErrorClass.PERMISSION, "write_forbidden"),
    "TelegramConflictError": (ErrorClass.TRANSIENT, "getupdates_conflict"),
    "TelegramServerError": (ErrorClass.TRANSIENT, "server_error"),
    "TelegramNetworkError": (ErrorClass.TRANSIENT, "network_error"),
    "RestartingTelegram": (ErrorClass.TRANSIENT, "server_restarting"),
    "TelegramEntityTooLarge": (ErrorClass.PERMANENT_CONTENT, "entity_too_large"),
    "TelegramNotFound": (ErrorClass.PERMANENT_CONTENT, "message_unavailable"),
    # --- stdlib ---
    "TimeoutError": (ErrorClass.TRANSIENT, "timeout"),
    "ConnectionError": (ErrorClass.TRANSIENT, "network_error"),
    "ConnectionResetError": (ErrorClass.TRANSIENT, "network_error"),
    "ClientConnectorError": (ErrorClass.TRANSIENT, "network_error"),
    "ConnectTimeout": (ErrorClass.TRANSIENT, "timeout"),
    "ReadTimeout": (ErrorClass.TRANSIENT, "timeout"),
    "OSError": (ErrorClass.TRANSIENT, "network_error"),
}

#: Telegram's stable uppercase error strings, matched against the message when
#: the exception class alone is not decisive (notably ``TelegramBadRequest``).
_BY_MESSAGE_TOKEN: tuple[tuple[str, ErrorClass, str], ...] = (
    ("CHAT_WRITE_FORBIDDEN", ErrorClass.PERMISSION, "write_forbidden"),
    ("CHAT_SEND_MEDIA_FORBIDDEN", ErrorClass.PERMISSION, "send_media_forbidden"),
    ("CHAT_ADMIN_REQUIRED", ErrorClass.PERMISSION, "admin_required"),
    ("NOT_ENOUGH_RIGHTS", ErrorClass.PERMISSION, "write_forbidden"),
    ("USER_BANNED_IN_CHANNEL", ErrorClass.PERMISSION, "banned"),
    ("CHANNEL_PRIVATE", ErrorClass.PERMISSION, "channel_private"),
    ("PEER_ID_INVALID", ErrorClass.PERMISSION, "peer_invalid"),
    ("CHAT_NOT_FOUND", ErrorClass.PERMISSION, "peer_invalid"),
    ("BOT_WAS_BLOCKED", ErrorClass.PERMISSION, "blocked"),
    ("USER_IS_BLOCKED", ErrorClass.PERMISSION, "blocked"),
    ("CHAT_FORWARDS_RESTRICTED", ErrorClass.PERMANENT_CONTENT, "protected_content"),
    ("MESSAGE_CANT_BE_FORWARDED", ErrorClass.PERMANENT_CONTENT, "protected_content"),
    ("MESSAGE_CANT_BE_COPIED", ErrorClass.PERMANENT_CONTENT, "uncopyable_message"),
    ("MESSAGE_ID_INVALID", ErrorClass.PERMANENT_CONTENT, "message_unavailable"),
    ("MESSAGE_TO_FORWARD_NOT_FOUND", ErrorClass.PERMANENT_CONTENT, "message_unavailable"),
    ("MESSAGE_TO_COPY_NOT_FOUND", ErrorClass.PERMANENT_CONTENT, "message_unavailable"),
    ("MSG_ID_INVALID", ErrorClass.PERMANENT_CONTENT, "message_unavailable"),
    ("MEDIA_EMPTY", ErrorClass.PERMANENT_CONTENT, "media_unavailable"),
    ("MEDIA_CAPTION_TOO_LONG", ErrorClass.PERMANENT_CONTENT, "caption_too_long"),
    ("MESSAGE_TOO_LONG", ErrorClass.PERMANENT_CONTENT, "message_too_long"),
    ("POLL_QUESTION_INVALID", ErrorClass.PERMANENT_CONTENT, "unsupported_message"),
    ("TOPIC_CLOSED", ErrorClass.PERMISSION, "topic_closed"),
    ("CHAT_MIGRATED", ErrorClass.PERMISSION, "chat_migrated"),
    # Bot API phrases it in prose rather than as a token.
    ("GROUP CHAT WAS UPGRADED", ErrorClass.PERMISSION, "chat_migrated"),
    ("AUTH_KEY_UNREGISTERED", ErrorClass.AUTH, "auth_key_unregistered"),
    ("SESSION_REVOKED", ErrorClass.AUTH, "session_revoked"),
    ("USER_DEACTIVATED", ErrorClass.AUTH, "user_deactivated"),
    ("ACCESS_TOKEN_INVALID", ErrorClass.AUTH, "unauthorized"),
    ("TOKEN_INVALID", ErrorClass.AUTH, "unauthorized"),
    ("UNAUTHORIZED", ErrorClass.AUTH, "unauthorized"),
    ("FLOOD_WAIT", ErrorClass.RATE_LIMIT, "flood_wait"),
    ("SLOWMODE_WAIT", ErrorClass.RATE_LIMIT, "slowmode_wait"),
    ("TOO MANY REQUESTS", ErrorClass.RATE_LIMIT, "flood_wait"),
    ("INTERNAL_SERVER_ERROR", ErrorClass.TRANSIENT, "server_error"),
    ("BAD GATEWAY", ErrorClass.TRANSIENT, "server_error"),
)

_RETRY_AFTER_RE = re.compile(r"(?:retry[_ ]after|FLOOD_WAIT_|SLOWMODE_WAIT_)\D{0,4}(\d+)", re.I)

_SAFE_MESSAGES: dict[ErrorClass, str] = {
    ErrorClass.TRANSIENT: "A temporary problem occurred. This will be retried automatically.",
    ErrorClass.RATE_LIMIT: "Telegram asked us to slow down. Waiting before the next attempt.",
    ErrorClass.AUTH: "This connection is no longer authorized. Reconnect it to continue.",
    ErrorClass.PERMISSION: "The connection is not allowed to post in this chat.",
    ErrorClass.PERMANENT_CONTENT: "This message cannot be delivered and was skipped.",
    ErrorClass.UNKNOWN: "An unexpected problem occurred. This has been recorded for review.",
}


def _extract_retry_after(exc: BaseException) -> float | None:
    """Telegram's wait duration, from whichever attribute the library used."""
    for attribute in ("retry_after", "seconds", "timeout"):
        value = getattr(exc, attribute, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    match = _RETRY_AFTER_RE.search(str(exc))
    if match:
        return float(match.group(1))
    return None


def _status_code(exc: BaseException) -> int | None:
    for attribute in ("status_code", "code", "http_status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def _class_names(exc: BaseException) -> list[str]:
    return [klass.__name__ for klass in type(exc).__mro__ if klass is not object]


def classify_error(exc: BaseException) -> ClassifiedError:
    """Map any provider exception onto the taxonomy in docs/OPERATIONS.md §3."""
    # 1. Adapters that already know the answer.
    if isinstance(exc, AdapterError):
        return ClassifiedError(
            error_class=exc.error_class,
            code=exc.code,
            retry_after_s=exc.retry_after_s,
            safe_message=_SAFE_MESSAGES[exc.error_class],
        )

    if isinstance(exc, asyncio.CancelledError):
        return ClassifiedError(
            ErrorClass.TRANSIENT, "cancelled", None, _SAFE_MESSAGES[ErrorClass.TRANSIENT]
        )

    text = str(exc)
    upper = text.upper()

    # 2. Exception class name, walking the MRO so subclasses resolve.
    for name in _class_names(exc):
        mapped = _BY_EXCEPTION_NAME.get(name)
        if mapped is None:
            continue
        error_class, code = mapped
        retry_after = _extract_retry_after(exc) if error_class is ErrorClass.RATE_LIMIT else None
        return ClassifiedError(error_class, code, retry_after, _SAFE_MESSAGES[error_class])

    # 3. Telegram's stable error tokens in the message.
    for token, error_class, code in _BY_MESSAGE_TOKEN:
        if token in upper:
            retry_after = (
                _extract_retry_after(exc) if error_class is ErrorClass.RATE_LIMIT else None
            )
            return ClassifiedError(error_class, code, retry_after, _SAFE_MESSAGES[error_class])

    # 4. HTTP status as a last structured signal.
    status = _status_code(exc)
    if status == 429:
        return ClassifiedError(
            ErrorClass.RATE_LIMIT,
            "flood_wait",
            _extract_retry_after(exc),
            _SAFE_MESSAGES[ErrorClass.RATE_LIMIT],
        )
    if status == 401:
        return ClassifiedError(
            ErrorClass.AUTH, "unauthorized", None, _SAFE_MESSAGES[ErrorClass.AUTH]
        )
    if status == 403:
        return ClassifiedError(
            ErrorClass.PERMISSION, "write_forbidden", None, _SAFE_MESSAGES[ErrorClass.PERMISSION]
        )
    if status is not None and 500 <= status <= 599:
        return ClassifiedError(
            ErrorClass.TRANSIENT, "server_error", None, _SAFE_MESSAGES[ErrorClass.TRANSIENT]
        )

    # 5. Conservative default. Never "retry forever".
    return ClassifiedError(
        ErrorClass.UNKNOWN,
        f"unknown:{type(exc).__name__}",
        None,
        _SAFE_MESSAGES[ErrorClass.UNKNOWN],
    )


def safe_detail(exc: BaseException) -> str:
    """A customer-viewable detail string. Never the raw provider message."""
    classified = classify_error(exc)
    return f"{classified.safe_message} (code: {scrub_text(classified.code)})"
