"""Retry classification. A silent bug here either spams Telegram or drops
messages, so this module carries a 100% coverage requirement."""

from __future__ import annotations

import asyncio

import pytest

from app.adapters.errors import (
    NON_RETRYABLE,
    AdapterError,
    ErrorClass,
    classify_error,
    safe_detail,
)


class FloodWaitError(Exception):
    """Shaped like Telethon's."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"A wait of {seconds} seconds is required (caused by ForwardMessages)")
        self.seconds = seconds


class TelegramRetryAfter(Exception):
    """Shaped like aiogram's."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Too Many Requests: retry after {retry_after}")
        self.retry_after = retry_after


class TelegramBadRequest(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ChatWriteForbiddenError(Exception):
    pass


class AuthKeyUnregisteredError(Exception):
    pass


class ChatForwardsRestrictedError(Exception):
    pass


class HttpStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_flood_wait_is_rate_limited_and_keeps_the_exact_duration():
    result = classify_error(FloodWaitError(42))
    assert result.error_class is ErrorClass.RATE_LIMIT
    assert result.retry_after_s == 42.0
    assert result.retryable


def test_bot_api_retry_after_is_rate_limited():
    result = classify_error(TelegramRetryAfter(17))
    assert result.error_class is ErrorClass.RATE_LIMIT
    assert result.retry_after_s == 17.0


def test_retry_after_is_recovered_from_the_message_when_no_attribute_exists():
    result = classify_error(Exception("FLOOD_WAIT_120"))
    assert result.error_class is ErrorClass.RATE_LIMIT
    assert result.retry_after_s == 120.0


@pytest.mark.parametrize(
    "exc,expected",
    [
        (AuthKeyUnregisteredError(), ErrorClass.AUTH),
        (ChatWriteForbiddenError(), ErrorClass.PERMISSION),
        (ChatForwardsRestrictedError(), ErrorClass.PERMANENT_CONTENT),
        (TimeoutError(), ErrorClass.TRANSIENT),
        (ConnectionResetError(), ErrorClass.TRANSIENT),
    ],
)
def test_exception_class_names_map_to_the_right_class(exc, expected):
    assert classify_error(exc).error_class is expected


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Bad Request: CHAT_WRITE_FORBIDDEN", ErrorClass.PERMISSION),
        ("Bad Request: CHAT_ADMIN_REQUIRED", ErrorClass.PERMISSION),
        ("Bad Request: USER_BANNED_IN_CHANNEL", ErrorClass.PERMISSION),
        ("Bad Request: CHANNEL_PRIVATE", ErrorClass.PERMISSION),
        ("Bad Request: MESSAGE_ID_INVALID", ErrorClass.PERMANENT_CONTENT),
        ("Bad Request: CHAT_FORWARDS_RESTRICTED", ErrorClass.PERMANENT_CONTENT),
        ("Bad Request: MESSAGE_CANT_BE_FORWARDED", ErrorClass.PERMANENT_CONTENT),
        ("Bad Request: MEDIA_EMPTY", ErrorClass.PERMANENT_CONTENT),
        ("Unauthorized: ACCESS_TOKEN_INVALID", ErrorClass.AUTH),
        ("Internal Server Error: INTERNAL_SERVER_ERROR", ErrorClass.TRANSIENT),
        ("Bad Request: TOPIC_CLOSED", ErrorClass.PERMISSION),
    ],
)
def test_telegram_error_tokens_in_the_message_are_classified(message, expected):
    assert classify_error(TelegramBadRequest(message)).error_class is expected


@pytest.mark.parametrize(
    "status,expected",
    [
        (429, ErrorClass.RATE_LIMIT),
        (401, ErrorClass.AUTH),
        (403, ErrorClass.PERMISSION),
        (500, ErrorClass.TRANSIENT),
        (503, ErrorClass.TRANSIENT),
    ],
)
def test_http_status_is_the_last_structured_signal(status, expected):
    assert classify_error(HttpStatusError(status)).error_class is expected


def test_unmapped_errors_are_unknown_not_retry_forever():
    result = classify_error(ValueError("something we have never seen"))
    assert result.error_class is ErrorClass.UNKNOWN
    assert result.code == "unknown:ValueError"
    # UNKNOWN is retryable but bounded by max_attempts, never infinite.
    assert result.retryable


def test_adapter_error_short_circuits_classification():
    result = classify_error(AdapterError("protected_content", ErrorClass.PERMANENT_CONTENT))
    assert result.error_class is ErrorClass.PERMANENT_CONTENT
    assert result.code == "protected_content"
    assert not result.retryable


def test_adapter_error_can_carry_a_wait():
    result = classify_error(AdapterError("flood_wait", ErrorClass.RATE_LIMIT, retry_after_s=9))
    assert result.retry_after_s == 9


def test_cancellation_is_transient():
    assert classify_error(asyncio.CancelledError()).error_class is ErrorClass.TRANSIENT


def test_auth_permission_and_content_are_never_retried():
    assert {
        ErrorClass.AUTH,
        ErrorClass.PERMISSION,
        ErrorClass.PERMANENT_CONTENT,
    } == NON_RETRYABLE
    for cls in NON_RETRYABLE:
        assert not classify_error(AdapterError("x", cls)).retryable


def test_subclasses_resolve_through_the_mro():
    class MyFloodWaitError(FloodWaitError):
        pass

    assert classify_error(MyFloodWaitError(5)).error_class is ErrorClass.RATE_LIMIT


def test_safe_detail_never_leaks_the_provider_message():
    secret = "987654321:AAHsecretTokenValueThatIsLongEnough1"
    detail = safe_detail(Exception(f"failed with token {secret}"))
    assert secret not in detail
    assert "code:" in detail


def test_negative_retry_after_is_ignored():
    class Weird(Exception):
        retry_after = -5

    # A negative wait is nonsense; it must not become a negative sleep.
    result = classify_error(Weird("FLOOD_WAIT"))
    assert result.retry_after_s is None or result.retry_after_s >= 0
