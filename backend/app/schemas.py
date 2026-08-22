"""API request/response models.

Two rules hold throughout:

* **No response model exposes a secret.** A reflection test asserts this against
  the denylist in ``app.security.redaction``.
* **Telegram identifiers are strings in JSON**, always paired with ``peer_type``,
  so no frontend ``Number`` rounding is possible and the overlapping-id-sequence
  rule stays visible at the edge.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.domain import reasons


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class RegisterRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=12, max_length=256)]
    timezone: str = "UTC"


class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=256)]


class MeResponse(ApiModel):
    id: uuid.UUID
    email: str
    timezone: str
    connection_count: int = 0
    active_rule_count: int = 0
    telegram_username: str | None = None


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
class CapabilitiesResponse(BaseModel):
    can_read_subscribed_channels: bool
    can_read_group_messages: bool
    can_read_history: bool
    max_download_bytes: int | None
    notes: list[str] = Field(default_factory=list)


class ConnectionResponse(ApiModel):
    id: uuid.UUID
    kind: Literal["bot", "user"]
    label: str
    status: str
    telegram_username: str | None = None
    telegram_account_id: str | None = None
    last_health_check_at: datetime | None = None
    last_successful_check_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message_safe: str | None = None
    created_at: datetime
    capabilities: CapabilitiesResponse | None = None

    @field_validator("telegram_account_id", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> str | None:
        return None if value is None else str(value)


class CreateBotConnectionRequest(BaseModel):
    label: Annotated[str, Field(min_length=1, max_length=120)]
    #: Accepted only in a request body over TLS. Never in a URL, never logged.
    bot_token: Annotated[str, Field(min_length=20, max_length=256)]


class StartUserConnectionRequest(BaseModel):
    label: Annotated[str, Field(min_length=1, max_length=120)]
    phone: Annotated[str, Field(pattern=r"^\+[1-9]\d{6,14}$")]


class VerifyCodeRequest(BaseModel):
    connection_id: uuid.UUID
    code: Annotated[str, Field(min_length=3, max_length=16)]


class TwoFactorRequest(BaseModel):
    #: Used once in memory to complete sign-in, then discarded.
    password: Annotated[str, Field(min_length=1, max_length=256)]


class DisconnectRequest(BaseModel):
    revoke: bool = False


# --------------------------------------------------------------------------- #
# Chats
# --------------------------------------------------------------------------- #
class ChatResponse(ApiModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    peer_type: str
    peer_id: str
    title: str
    username: str | None
    chat_kind: str
    is_public: bool | None
    has_protected_content: bool | None
    is_active: bool
    last_synced_at: datetime | None
    source_eligible: bool = False
    source_reason_code: str = reasons.UNKNOWN
    source_reason_text: str = ""
    destination_eligible: bool = False
    destination_reason_code: str = reasons.UNKNOWN
    destination_reason_text: str = ""

    @field_validator("peer_id", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> str:
        return str(value)


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
MEDIA_TYPE_CHOICES = (
    "text",
    "photo",
    "video",
    "document",
    "audio",
    "voice",
    "poll",
    "animation",
    "sticker",
)


class RuleWriteRequest(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    connection_id: uuid.UUID
    # Coarse guard only; the real operational bounds are checked in
    # app.services.rules against the configured limits.
    source_chat_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=1000)]
    destination_chat_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=2000)]
    forward_mode: Literal["forward", "copy"] = "forward"
    delay_ms: Annotated[int, Field(ge=0, le=3_600_000)] = 0
    keyword_include: list[Annotated[str, Field(max_length=128)]] = Field(default_factory=list)
    keyword_exclude: list[Annotated[str, Field(max_length=128)]] = Field(default_factory=list)
    keyword_match_mode: Literal["substring", "word"] = "substring"
    media_types: list[str] = Field(default_factory=list)
    preserve_links: bool = True
    preserve_caption: bool = True
    allow_source_as_destination: bool = False

    @field_validator("media_types")
    @classmethod
    def _known_media(cls, value: list[str]) -> list[str]:
        from app.domain.filters import normalize_media_types

        normalized = normalize_media_types(value)
        unknown = normalized - set(MEDIA_TYPE_CHOICES)
        if unknown:
            raise ValueError(f"Unsupported media types: {', '.join(sorted(unknown))}")
        return sorted(normalized)


class RuleChatSummary(BaseModel):
    id: uuid.UUID
    title: str
    peer_id: str
    peer_type: str
    eligible: bool
    reason_code: str


class RuleResponse(ApiModel):
    id: uuid.UUID
    name: str
    connection_id: uuid.UUID
    status: str
    version: int
    forward_mode: str
    delay_ms: int
    keyword_include: list[str]
    keyword_exclude: list[str]
    keyword_match_mode: str
    media_types: list[str]
    preserve_links: bool
    preserve_caption: bool
    paused_reason_code: str | None
    paused_reason_text: str | None = None
    last_activity_at: datetime | None
    created_at: datetime
    sources: list[RuleChatSummary] = Field(default_factory=list)
    destinations: list[RuleChatSummary] = Field(default_factory=list)
    preview: str = ""


class RuleListItem(ApiModel):
    id: uuid.UUID
    name: str
    status: str
    connection_id: uuid.UUID
    destination_count: int
    source_titles: list[str]
    filter_summary: str
    last_activity_at: datetime | None
    paused_reason_text: str | None = None


# --------------------------------------------------------------------------- #
# Events / status
# --------------------------------------------------------------------------- #
class EventResponse(ApiModel):
    id: uuid.UUID
    rule_id: uuid.UUID
    outcome: str
    reason_code: str
    reason_text: str = ""
    detail_safe: str | None
    attempt: int
    source_chat_id: uuid.UUID | None
    destination_chat_id: uuid.UUID | None
    source_message_ids: list[str] = Field(default_factory=list)
    occurred_at: datetime

    @field_validator("source_message_ids", mode="before")
    @classmethod
    def _stringify_ids(cls, value: Any) -> list[str]:
        return [str(v) for v in (value or [])]


class UsageSummaryResponse(BaseModel):
    """Operational counters. Explicitly not quota accounting — this product has
    no quotas."""

    period: str
    forwarded: int = 0
    skipped: int = 0
    failed: int = 0
    retry_scheduled: int = 0
    paused: int = 0


class AuditEventResponse(ApiModel):
    id: uuid.UUID
    action: str
    object_type: str
    object_id: str | None
    correlation_id: str | None
    created_at: datetime


class AcceptedResponse(BaseModel):
    """202 body for anything that enqueues background work."""

    status: Literal["accepted"] = "accepted"
    task_id: uuid.UUID | None = None
    message: str = "Queued. Refresh to see the result."


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool
    redis: bool
    telegram_provider: str
