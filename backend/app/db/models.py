"""ORM models. Mirrors docs/DATABASE.md exactly.

Two structural rules are load-bearing and appear throughout:

* A chat is keyed ``(connection_id, peer_type, peer_id)``. Telegram's peer docs
  state the 64-bit id sequences of users, chats and channels *overlap*, so
  ``peer_id`` alone is never a key.
* ``access_hash`` is per-account, so it is stored per connection, encrypted, and
  never leaves the adapter layer.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_pk


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class ConnectionKind(enum.StrEnum):
    bot = "bot"
    user = "user"


class ConnectionStatus(enum.StrEnum):
    pending = "pending"
    awaiting_code = "awaiting_code"
    awaiting_2fa = "awaiting_2fa"
    active = "active"
    error = "error"
    paused_safety = "paused_safety"
    disconnected = "disconnected"


#: In-progress connection attempts. Used by a partial unique index so the
#: database — not just the service layer — prevents duplicate simultaneous
#: connection attempts for one user.
IN_PROGRESS_CONNECTION_STATUSES = ("pending", "awaiting_code", "awaiting_2fa")


class PeerType(enum.StrEnum):
    user = "user"
    chat = "chat"
    channel = "channel"


class ChatKind(enum.StrEnum):
    private = "private"
    group = "group"
    supergroup = "supergroup"
    channel = "channel"
    other = "other"


class RuleStatus(enum.StrEnum):
    draft = "draft"
    active = "active"
    paused = "paused"
    error = "error"
    disconnected = "disconnected"


class ForwardMode(enum.StrEnum):
    forward = "forward"
    copy = "copy"


class KeywordMatchMode(enum.StrEnum):
    substring = "substring"
    word = "word"


class JobStatus(enum.StrEnum):
    pending = "pending"
    leased = "leased"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    needs_attention = "needs_attention"
    dead_letter = "dead_letter"


TERMINAL_JOB_STATUSES = (
    JobStatus.succeeded,
    JobStatus.skipped,
    JobStatus.dead_letter,
)


class EventOutcome(enum.StrEnum):
    forwarded = "forwarded"
    skipped = "skipped"
    failed = "failed"
    retry_scheduled = "retry_scheduled"
    paused = "paused"


class BroadcastStatus(enum.StrEnum):
    """A broadcast is composed, then queued, then drained.

    ``draft`` exists because composing happens across several Telegram messages
    (text, then image, then group selection) and must survive the bot restarting
    halfway through.
    """

    draft = "draft"
    scheduled = "scheduled"
    sending = "sending"
    paused = "paused"
    completed = "completed"
    cancelled = "cancelled"


class BroadcastMedia(enum.StrEnum):
    none = "none"
    photo = "photo"


class ControlTaskKind(enum.StrEnum):
    sync_chats = "sync_chats"
    health_check = "health_check"
    check_chat_access = "check_chat_access"
    retry_failed = "retry_failed"
    activate_rule = "activate_rule"
    disconnect = "disconnect"


class ControlTaskStatus(enum.StrEnum):
    pending = "pending"
    leased = "leased"
    succeeded = "succeeded"
    failed = "failed"


def _enum(py_enum: type[enum.Enum], name: str) -> Enum:
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    #: False means suspended. Every delivery path re-checks it, so suspending
    #: takes effect on work already queued rather than only on new work.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Shown to the suspended person, so being cut off is not a silent mystery.
    suspended_reason: Mapped[str | None] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    #: Set when the account is reached through the Telegram control panel.
    #: Nullable so password accounts keep working unchanged.
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    #: When this person accepted the terms. NULL means they have not, and the
    #: panel shows them nothing but the terms screen until they do.
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Bumped on every broadcast queued. The one number an operator needs to
    #: spot an account behaving unlike the others, without reading its content.
    broadcasts_sent: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    #: When this person asked to be told the publisher side had opened. Kept as
    #: a timestamp rather than a flag so the operator can see *demand over
    #: time*, which is the only thing that would justify building it.
    publisher_interest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Nullable: a Telegram-only account never sets a password.
    password_hash: Mapped[str | None] = mapped_column(Text)


class AppSession(Base):
    """Server-side session so revocation is real, not advisory."""

    __tablename__ = "app_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: SHA-256 of the cookie value. The raw token is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    __table_args__ = (Index("ix_app_sessions_user_id_expires_at", "user_id", "expires_at"),)


class AppSetting(Base):
    __tablename__ = "app_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    notification_prefs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# Telegram connections
# --------------------------------------------------------------------------- #
class TelegramConnection(Base, TimestampMixin):
    __tablename__ = "telegram_connections"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ConnectionKind] = mapped_column(
        _enum(ConnectionKind, "connection_kind"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[ConnectionStatus] = mapped_column(
        _enum(ConnectionStatus, "connection_status"),
        default=ConnectionStatus.pending,
        nullable=False,
    )

    # Sealed secrets. Excluded from every response model (asserted by test).
    bot_token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    bot_token_wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    bot_token_key_version: Mapped[int | None] = mapped_column(Integer)

    telegram_account_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    #: Telegram Premium on this account. Only a premium account may send custom
    #: emoji, so an ad written with them renders as fallback characters without
    #: it — which is worth saying before the ad goes out, not after.
    is_premium: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    #: When Telegram last told us the line above. NULL means never, and a
    #: never-checked connection must not be reported as *not* premium — a
    #: default is not a finding, and claiming one is how a Premium account got
    #: told it was not.
    premium_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: SHA-256 of the E.164 phone. The raw phone number is never stored.
    phone_hash: Mapped[str | None] = mapped_column(String(64))

    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message_safe: Mapped[str | None] = mapped_column(Text)
    consecutive_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    chats: Mapped[list[TelegramChat]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "kind",
            "telegram_account_id",
            name="uq_telegram_connections_user_id_kind_telegram_account_id",
        ),
        Index("ix_telegram_connections_user_id", "user_id"),
        Index(
            "uq_telegram_connections_one_in_progress_per_user",
            "user_id",
            unique=True,
            postgresql_where=(
                # Prevents duplicate simultaneous connection attempts in the DB.
                f"status IN {IN_PROGRESS_CONNECTION_STATUSES}"
            ),
        ),
    )


class TelegramSession(Base):
    """MTProto session material (Telethon StringSession), envelope-encrypted.

    Nothing is written to a session file on disk.
    """

    __tablename__ = "telegram_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    session_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramChat(Base, TimestampMixin):
    __tablename__ = "telegram_chats"

    id: Mapped[uuid.UUID] = uuid_pk()
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), nullable=False
    )
    peer_type: Mapped[PeerType] = mapped_column(_enum(PeerType, "peer_type"), nullable=False)
    peer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    access_hash_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_hash_wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_hash_key_version: Mapped[int | None] = mapped_column(Integer)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    chat_kind: Mapped[ChatKind] = mapped_column(_enum(ChatKind, "chat_kind"), nullable=False)
    is_public: Mapped[bool | None] = mapped_column(Boolean)
    has_protected_content: Mapped[bool | None] = mapped_column(Boolean)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message_safe: Mapped[str | None] = mapped_column(Text)

    connection: Mapped[TelegramConnection] = relationship(back_populates="chats")
    access: Mapped[ConnectionChatAccess | None] = relationship(
        back_populates="chat", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        # The overlapping-id-sequence rule, made structural.
        UniqueConstraint(
            "connection_id", "peer_type", "peer_id", name="uq_telegram_chats_connection_peer"
        ),
        Index("ix_telegram_chats_connection_id_is_active", "connection_id", "is_active"),
    )


class ConnectionChatAccess(Base):
    """Eligibility snapshot, kept separate so re-checks do not churn chat metadata."""

    __tablename__ = "connection_chat_access"

    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), primary_key=True
    )
    can_read_source: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_post_destination: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_reason_code: Mapped[str] = mapped_column(String(48), default="unknown", nullable=False)
    destination_reason_code: Mapped[str] = mapped_column(
        String(48), default="unknown", nullable=False
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    check_source: Mapped[str] = mapped_column(String(24), default="sync", nullable=False)

    chat: Mapped[TelegramChat] = relationship(back_populates="access")


class ConnectionUpdateState(Base):
    """Durable intake cursor, so a restart resumes rather than replays."""

    __tablename__ = "connection_update_state"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), primary_key=True
    )
    bot_update_offset: Mapped[int | None] = mapped_column(BigInteger)
    mtproto_pts: Mapped[int | None] = mapped_column(Integer)
    mtproto_qts: Mapped[int | None] = mapped_column(Integer)
    mtproto_date: Mapped[int | None] = mapped_column(Integer)
    mtproto_seq: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SourceCursor(Base):
    """Second dedupe layer, per source chat."""

    __tablename__ = "source_cursors"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), primary_key=True
    )
    last_processed_message_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# Forwarding
# --------------------------------------------------------------------------- #
class ForwardingRule(Base, TimestampMixin):
    __tablename__ = "forwarding_rules"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[RuleStatus] = mapped_column(
        _enum(RuleStatus, "rule_status"), default=RuleStatus.draft, nullable=False
    )
    #: Bumped on every edit. Deliberately *not* part of the idempotency key.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    forward_mode: Mapped[ForwardMode] = mapped_column(
        _enum(ForwardMode, "forward_mode"), default=ForwardMode.forward, nullable=False
    )
    delay_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    keyword_include: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}", nullable=False
    )
    keyword_exclude: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}", nullable=False
    )
    keyword_match_mode: Mapped[KeywordMatchMode] = mapped_column(
        _enum(KeywordMatchMode, "keyword_match_mode"),
        default=KeywordMatchMode.substring,
        nullable=False,
    )
    media_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}", nullable=False
    )
    preserve_links: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    preserve_caption: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    paused_reason_code: Mapped[str | None] = mapped_column(String(48))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sources: Mapped[list[ForwardingRuleSource]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    destinations: Mapped[list[ForwardingRuleDestination]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("delay_ms >= 0 AND delay_ms <= 3600000", name="delay_ms_bounded"),
        CheckConstraint("max_attempts >= 1 AND max_attempts <= 20", name="max_attempts_bounded"),
        Index("ix_forwarding_rules_user_id", "user_id"),
        Index("ix_forwarding_rules_status", "status"),
    )


class ForwardingRuleSource(Base):
    __tablename__ = "forwarding_rule_sources"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("forwarding_rules.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), primary_key=True
    )


class ForwardingRuleDestination(Base):
    __tablename__ = "forwarding_rule_destinations"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("forwarding_rules.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ForwardingJob(Base, TimestampMixin):
    __tablename__ = "forwarding_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("forwarding_rules.id", ondelete="CASCADE"), nullable=False
    )
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), nullable=False
    )
    source_chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), nullable=False
    )
    #: An array so an album is one job, delivered with the plural forward call.
    source_message_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    destination_chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), default=JobStatus.pending, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: MTProto server-side dedupe token. Persisted so a retry after an ambiguous
    #: timeout reuses the exact same value and is therefore idempotent.
    mtproto_random_id: Mapped[int | None] = mapped_column(BigInteger)
    destination_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_error_class: Mapped[str | None] = mapped_column(String(32))
    last_error_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index(
            "ix_forwarding_jobs_claim",
            "not_before",
            postgresql_where="status = 'pending'",
        ),
        Index(
            "ix_forwarding_jobs_reclaim",
            "lease_expires_at",
            postgresql_where="status = 'leased'",
        ),
        Index("ix_forwarding_jobs_rule_id_status", "rule_id", "status"),
    )


class ForwardingEvent(Base):
    """Append-only durable history. ``detail_safe`` is redacted at write time.

    One feed for both pipelines. A row belongs to a rule *or* a broadcast, never
    both and never neither — the check constraint makes that structural, so the
    activity screen can render any row without guessing where it came from.
    """

    __tablename__ = "forwarding_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: Nullable since broadcasts joined this table; see the check constraint.
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("forwarding_rules.id", ondelete="CASCADE")
    )
    broadcast_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("broadcasts.id", ondelete="CASCADE")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("forwarding_jobs.id", ondelete="SET NULL")
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), nullable=False
    )
    source_chat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="SET NULL")
    )
    source_message_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), default=list, server_default="{}", nullable=False
    )
    destination_chat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="SET NULL")
    )
    outcome: Mapped[EventOutcome] = mapped_column(
        _enum(EventOutcome, "event_outcome"), nullable=False
    )
    reason_code: Mapped[str] = mapped_column(String(48), nullable=False)
    detail_safe: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "(rule_id IS NULL) <> (broadcast_id IS NULL)",
            name="event_belongs_to_exactly_one_pipeline",
        ),
        Index("ix_forwarding_events_rule_id_occurred_at", "rule_id", "occurred_at"),
        Index("ix_forwarding_events_broadcast_id_occurred_at", "broadcast_id", "occurred_at"),
        Index("ix_forwarding_events_connection_id_occurred_at", "connection_id", "occurred_at"),
    )


# --------------------------------------------------------------------------- #
# Broadcasts
# --------------------------------------------------------------------------- #
class Broadcast(Base, TimestampMixin):
    """Your own message, posted to groups you have chosen.

    Distinct from a forwarding rule in one way that matters: the content is
    authored here rather than copied from a source chat, so there is no source
    peer, no album, and no content-protection question. Everything else —
    per-destination durability, pacing, retry classification, obeying a flood
    wait — is the same machinery, because those properties are not specific to
    forwarding.

    Targets are groups the connected account is already a member of. There is no
    discovery, no invite, and no way to post somewhere you have not joined.
    """

    __tablename__ = "broadcasts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[BroadcastStatus] = mapped_column(
        _enum(BroadcastStatus, "broadcast_status"), default=BroadcastStatus.draft, nullable=False
    )

    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: The formatting the customer typed — bold, links, premium emoji — exactly
    #: as Telegram described it, so each delivery reproduces the original rather
    #: than a plain-text approximation.
    #:
    #: Stored as data rather than as Markdown: round-tripping through markup
    #: mangles any message containing a literal asterisk or underscore, and
    #: markup cannot express a custom emoji at all.
    body_entities: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    media_kind: Mapped[BroadcastMedia] = mapped_column(
        _enum(BroadcastMedia, "broadcast_media"), default=BroadcastMedia.none, nullable=False
    )
    #: The image bytes, not a Telegram file_id.
    #:
    #: A file_id is scoped to the bot that received it, so the admin bot's id is
    #: meaningless to the connection that does the sending. The bytes are fetched
    #: once at compose time and re-uploaded per delivery. Capped by
    #: ``max_broadcast_media_bytes``.
    media_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    media_filename: Mapped[str | None] = mapped_column(String(128))

    #: Pause between two deliveries, so one broadcast does not arrive as a burst.
    delay_ms: Mapped[int] = mapped_column(Integer, default=3000, nullable=False)
    #: Seconds between rounds. NULL means post once and stop, which stays the
    #: default — repeating is something the customer turns on deliberately.
    repeat_every_s: Mapped[int | None] = mapped_column(Integer)
    #: Rounds finished so far. Shown rather than hidden: "sent 14 times" is the
    #: number that tells someone their ad has been running longer than intended.
    repeat_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    #: When the next round starts. Derived from the last one finishing, stored
    #: so the panel can show it without recomputing.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_reason_code: Mapped[str | None] = mapped_column(String(48))

    targets: Mapped[list[BroadcastTarget]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("delay_ms >= 0 AND delay_ms <= 3600000", name="broadcast_delay_bounded"),
        Index("ix_broadcasts_user_id", "user_id"),
        Index("ix_broadcasts_status", "status"),
    )


class BroadcastTarget(Base, TimestampMixin):
    """One group, one delivery, one durable row.

    Carries its own lease and attempt count for the same reason forwarding jobs
    do: a worker that dies mid-broadcast must not lose the remaining groups, and
    a restart must not re-send to groups already delivered.
    """

    __tablename__ = "broadcast_targets"

    id: Mapped[uuid.UUID] = uuid_pk()
    broadcast_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("broadcasts.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Same vocabulary as a forwarding job, so one activity screen renders both.
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), default=JobStatus.pending, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: MTProto server-side dedupe token, persisted so a retry after an ambiguous
    #: timeout reuses the same value and is therefore idempotent.
    mtproto_random_id: Mapped[int | None] = mapped_column(BigInteger)
    destination_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_error_class: Mapped[str | None] = mapped_column(String(32))
    last_error_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        # One row per (broadcast, group). This is the idempotency guarantee:
        # enqueueing twice cannot produce two deliveries to the same group.
        UniqueConstraint("broadcast_id", "chat_id", name="uq_broadcast_targets_broadcast_chat"),
        Index("ix_broadcast_targets_claim", "not_before", postgresql_where="status = 'pending'"),
        Index(
            "ix_broadcast_targets_reclaim",
            "lease_expires_at",
            postgresql_where="status = 'leased'",
        ),
        Index("ix_broadcast_targets_broadcast_id_status", "broadcast_id", "status"),
    )


# --------------------------------------------------------------------------- #
# Auto-reply
# --------------------------------------------------------------------------- #
class AutoReply(Base, TimestampMixin):
    """An answer for people who message the connected account first.

    Strictly reactive. It has no recipient list and no way to acquire one: the
    only thing that can trigger it is an incoming private message, so the account
    never opens a conversation. That is the whole point of tying it to the
    broadcast connection — someone sees an ad in a group, messages the account,
    and gets an answer. Nothing is ever sent to someone who did not write first.
    """

    __tablename__ = "auto_replies"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: One reply per connection — the same account that broadcasts.
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Seconds before the same person may be answered again. Not a rate limit for
    #: our benefit — it is what keeps a reply from becoming repeat messaging.
    cooldown_s: Mapped[int] = mapped_column(Integer, default=86_400, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        CheckConstraint("cooldown_s >= 60", name="auto_reply_cooldown_floor"),
        Index("ix_auto_replies_user_id", "user_id"),
    )


class AutoReplyLog(Base):
    """Who has already been answered, and when.

    Persisted rather than cached: after a restart, an empty cache would answer
    everyone a second time.
    """

    __tablename__ = "auto_reply_log"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), primary_key=True
    )
    #: The Telegram user who wrote in. Paired with peer_type for the same reason
    #: chats are: the id sequences overlap.
    peer_type: Mapped[PeerType] = mapped_column(
        _enum(PeerType, "peer_type"), primary_key=True, default=PeerType.user
    )
    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    replied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reply_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (Index("ix_auto_reply_log_replied_at", "replied_at"),)


# --------------------------------------------------------------------------- #
# Control plane
# --------------------------------------------------------------------------- #
class ControlTask(Base, TimestampMixin):
    """Durable background command, so the API can return 202 without blocking.

    Same claim/lease mechanism as forwarding jobs — see ADR-015.
    """

    __tablename__ = "control_tasks"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ControlTaskKind] = mapped_column(
        _enum(ControlTaskKind, "control_task_kind"), nullable=False
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE")
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("forwarding_rules.id", ondelete="CASCADE")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[ControlTaskStatus] = mapped_column(
        _enum(ControlTaskStatus, "control_task_status"),
        default=ControlTaskStatus.pending,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_control_tasks_claim", "not_before", postgresql_where="status = 'pending'"),
        Index("ix_control_tasks_reclaim", "lease_expires_at", postgresql_where="status = 'leased'"),
    )


class AdminNotification(Base):
    """Outbox for alerts pushed to the Telegram control panel.

    The worker writes rows; the admin bot drains and sends them. Going through
    the database rather than calling the Bot API from the worker means an alert
    survives a bot restart and cannot be lost in a crash — the same durability
    rule the forwarding pipeline follows.
    """

    __tablename__ = "admin_notifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("forwarding_rules.id", ondelete="CASCADE")
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE")
    )
    #: Collapses a storm of identical alerts into one message.
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    send_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("ix_admin_notifications_unsent", "created_at", postgresql_where="sent_at IS NULL"),
    )


class IdempotencyKey(Base):
    """HTTP-layer replay protection for activate/pause/resume/retry."""

    __tablename__ = "idempotency_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), default="in_progress", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PanelEmoji(Base):
    """The panel's premium icons: one row per unicode emoji the panel uses.

    Extracted by an operator from a connected account's emoji search, because
    custom emoji ids are Telegram documents — there is nothing to hardcode.
    Presence of rows is the on/off switch: rows exist, the panel renders its
    icons as custom emoji; table empty, it renders plain unicode. Whether the
    custom emoji actually *display* depends on the bot having a Fragment
    username — Telegram's restriction, not ours — so rendering degrades to the
    embedded fallback emoji rather than failing.
    """

    __tablename__ = "panel_emoji"

    emoticon: Mapped[str] = mapped_column(String(16), primary_key=True)
    custom_emoji_id: Mapped[str] = mapped_column(String(32), nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PanelButton(Base):
    """An operator's custom label for one panel button.

    Keyed by the built-in default text, because that is the one stable identity
    a button has — callback data carries per-object ids, and screens are built
    in forty places. A row overrides the default wherever that button appears;
    no row means the built-in label, which is a default, not a fallback: the
    built-ins are the product's own wording, not an error state.
    """

    __tablename__ = "panel_buttons"

    default_text: Mapped[str] = mapped_column(String(64), primary_key=True)
    custom_text: Mapped[str] = mapped_column(String(64), nullable=False)
    #: A custom emoji drawn before the label. Separate from ``custom_text``
    #: because Telegram draws it from its own field: pasted into the text it
    #: would render as the digits of the id.
    icon_custom_emoji_id: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(48), nullable=False)
    object_id: Mapped[str | None] = mapped_column(String(64))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_audit_events_user_id_created_at", "user_id", "created_at"),)
