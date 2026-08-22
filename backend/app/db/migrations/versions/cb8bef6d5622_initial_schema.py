"""initial schema

Revision ID: cb8bef6d5622
Revises:
Create Date: 2026-08-22 12:31:14.513612
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "cb8bef6d5622"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # users.email is CITEXT so addresses are unique case-insensitively.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    # Used by the trigram index backing chat title search.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "app_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_app_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_app_sessions_token_hash")),
    )
    op.create_index(
        "ix_app_sessions_user_id_expires_at",
        "app_sessions",
        ["user_id", "expires_at"],
        unique=False,
    )
    op.create_table(
        "app_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("notification_prefs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_app_settings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_app_settings")),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("object_type", sa.String(length=48), nullable=False),
        sa.Column("object_id", sa.String(length=64), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_audit_events_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_user_id_created_at",
        "audit_events",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("endpoint", sa.String(length=160), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_idempotency_keys_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "key", name=op.f("pk_idempotency_keys")),
    )
    op.create_table(
        "telegram_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Enum("bot", "user", name="connection_kind"), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "awaiting_code",
                "awaiting_2fa",
                "active",
                "error",
                "paused_safety",
                "disconnected",
                name="connection_status",
            ),
            nullable=False,
        ),
        sa.Column("bot_token_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("bot_token_wrapped_dek", sa.LargeBinary(), nullable=True),
        sa.Column("bot_token_key_version", sa.Integer(), nullable=True),
        sa.Column("telegram_account_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_username", sa.String(length=64), nullable=True),
        sa.Column("phone_hash", sa.String(length=64), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_successful_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message_safe", sa.Text(), nullable=True),
        sa.Column("consecutive_failure_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_telegram_connections_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_connections")),
        sa.UniqueConstraint(
            "user_id",
            "kind",
            "telegram_account_id",
            name="uq_telegram_connections_user_id_kind_telegram_account_id",
        ),
    )
    op.create_index(
        "ix_telegram_connections_user_id", "telegram_connections", ["user_id"], unique=False
    )
    op.create_index(
        "uq_telegram_connections_one_in_progress_per_user",
        "telegram_connections",
        ["user_id"],
        unique=True,
        postgresql_where="status IN ('pending', 'awaiting_code', 'awaiting_2fa')",
    )
    op.create_table(
        "connection_update_state",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("bot_update_offset", sa.BigInteger(), nullable=True),
        sa.Column("mtproto_pts", sa.Integer(), nullable=True),
        sa.Column("mtproto_qts", sa.Integer(), nullable=True),
        sa.Column("mtproto_date", sa.Integer(), nullable=True),
        sa.Column("mtproto_seq", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_connection_update_state_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connection_id", name=op.f("pk_connection_update_state")),
    )
    op.create_table(
        "forwarding_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "active", "paused", "error", "disconnected", name="rule_status"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("forward_mode", sa.Enum("forward", "copy", name="forward_mode"), nullable=False),
        sa.Column("delay_ms", sa.Integer(), nullable=False),
        sa.Column("keyword_include", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("keyword_exclude", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column(
            "keyword_match_mode",
            sa.Enum("substring", "word", name="keyword_match_mode"),
            nullable=False,
        ),
        sa.Column("media_types", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("preserve_links", sa.Boolean(), nullable=False),
        sa.Column("preserve_caption", sa.Boolean(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("paused_reason_code", sa.String(length=48), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "delay_ms >= 0 AND delay_ms <= 3600000",
            name=op.f("ck_forwarding_rules_delay_ms_bounded"),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 20",
            name=op.f("ck_forwarding_rules_max_attempts_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_forwarding_rules_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_forwarding_rules_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forwarding_rules")),
    )
    op.create_index("ix_forwarding_rules_status", "forwarding_rules", ["status"], unique=False)
    op.create_index("ix_forwarding_rules_user_id", "forwarding_rules", ["user_id"], unique=False)
    op.create_table(
        "telegram_chats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column(
            "peer_type", sa.Enum("user", "chat", "channel", name="peer_type"), nullable=False
        ),
        sa.Column("peer_id", sa.BigInteger(), nullable=False),
        sa.Column("access_hash_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("access_hash_wrapped_dek", sa.LargeBinary(), nullable=True),
        sa.Column("access_hash_key_version", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column(
            "chat_kind",
            sa.Enum("private", "group", "supergroup", "channel", "other", name="chat_kind"),
            nullable=False,
        ),
        sa.Column("is_public", sa.Boolean(), nullable=True),
        sa.Column("has_protected_content", sa.Boolean(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message_safe", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_telegram_chats_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_chats")),
        sa.UniqueConstraint(
            "connection_id", "peer_type", "peer_id", name="uq_telegram_chats_connection_peer"
        ),
    )
    op.create_index(
        "ix_telegram_chats_connection_id_is_active",
        "telegram_chats",
        ["connection_id", "is_active"],
        unique=False,
    )
    op.create_table(
        "telegram_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("session_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_telegram_sessions_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_sessions")),
        sa.UniqueConstraint("connection_id", name=op.f("uq_telegram_sessions_connection_id")),
    )
    op.create_table(
        "connection_chat_access",
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.Column("can_read_source", sa.Boolean(), nullable=False),
        sa.Column("can_post_destination", sa.Boolean(), nullable=False),
        sa.Column("source_reason_code", sa.String(length=48), nullable=False),
        sa.Column("destination_reason_code", sa.String(length=48), nullable=False),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("check_source", sa.String(length=24), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_connection_chat_access_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chat_id", name=op.f("pk_connection_chat_access")),
    )
    op.create_table(
        "control_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "sync_chats",
                "health_check",
                "check_chat_access",
                "retry_failed",
                "activate_rule",
                "disconnect",
                name="control_task_kind",
            ),
            nullable=False,
        ),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("rule_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "leased", "succeeded", "failed", name="control_task_status"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "not_before",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_control_tasks_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["forwarding_rules.id"],
            name=op.f("fk_control_tasks_rule_id_forwarding_rules"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_control_tasks_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_tasks")),
    )
    op.create_index(
        "ix_control_tasks_claim",
        "control_tasks",
        ["not_before"],
        unique=False,
        postgresql_where="status = 'pending'",
    )
    op.create_index(
        "ix_control_tasks_reclaim",
        "control_tasks",
        ["lease_expires_at"],
        unique=False,
        postgresql_where="status = 'leased'",
    )
    op.create_table(
        "forwarding_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("source_chat_id", sa.Uuid(), nullable=False),
        sa.Column("source_message_ids", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("destination_chat_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "leased",
                "succeeded",
                "failed",
                "skipped",
                "needs_attention",
                "dead_letter",
                name="job_status",
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "not_before",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mtproto_random_id", sa.BigInteger(), nullable=True),
        sa.Column("destination_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error_class", sa.String(length=32), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_forwarding_jobs_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["destination_chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_forwarding_jobs_destination_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["forwarding_rules.id"],
            name=op.f("fk_forwarding_jobs_rule_id_forwarding_rules"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_forwarding_jobs_source_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forwarding_jobs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_forwarding_jobs_idempotency_key")),
    )
    op.create_index(
        "ix_forwarding_jobs_claim",
        "forwarding_jobs",
        ["not_before"],
        unique=False,
        postgresql_where="status = 'pending'",
    )
    op.create_index(
        "ix_forwarding_jobs_reclaim",
        "forwarding_jobs",
        ["lease_expires_at"],
        unique=False,
        postgresql_where="status = 'leased'",
    )
    op.create_index(
        "ix_forwarding_jobs_rule_id_status", "forwarding_jobs", ["rule_id", "status"], unique=False
    )
    op.create_table(
        "forwarding_rule_destinations",
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_forwarding_rule_destinations_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["forwarding_rules.id"],
            name=op.f("fk_forwarding_rule_destinations_rule_id_forwarding_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("rule_id", "chat_id", name=op.f("pk_forwarding_rule_destinations")),
    )
    op.create_table(
        "forwarding_rule_sources",
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_forwarding_rule_sources_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["forwarding_rules.id"],
            name=op.f("fk_forwarding_rule_sources_rule_id_forwarding_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("rule_id", "chat_id", name=op.f("pk_forwarding_rule_sources")),
    )
    op.create_table(
        "source_cursors",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.Column("last_processed_message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_source_cursors_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_source_cursors_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connection_id", "chat_id", name=op.f("pk_source_cursors")),
    )
    op.create_table(
        "forwarding_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("source_chat_id", sa.Uuid(), nullable=True),
        sa.Column(
            "source_message_ids", sa.ARRAY(sa.BigInteger()), server_default="{}", nullable=False
        ),
        sa.Column("destination_chat_id", sa.Uuid(), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                "forwarded", "skipped", "failed", "retry_scheduled", "paused", name="event_outcome"
            ),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(length=48), nullable=False),
        sa.Column("detail_safe", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_forwarding_events_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["destination_chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_forwarding_events_destination_chat_id_telegram_chats"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["forwarding_jobs.id"],
            name=op.f("fk_forwarding_events_job_id_forwarding_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["forwarding_rules.id"],
            name=op.f("fk_forwarding_events_rule_id_forwarding_rules"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_forwarding_events_source_chat_id_telegram_chats"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forwarding_events")),
    )
    op.create_index(
        "ix_forwarding_events_connection_id_occurred_at",
        "forwarding_events",
        ["connection_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_forwarding_events_rule_id_occurred_at",
        "forwarding_events",
        ["rule_id", "occurred_at"],
        unique=False,
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index("ix_forwarding_events_rule_id_occurred_at", table_name="forwarding_events")
    op.drop_index("ix_forwarding_events_connection_id_occurred_at", table_name="forwarding_events")
    op.drop_table("forwarding_events")
    op.drop_table("source_cursors")
    op.drop_table("forwarding_rule_sources")
    op.drop_table("forwarding_rule_destinations")
    op.drop_index("ix_forwarding_jobs_rule_id_status", table_name="forwarding_jobs")
    op.drop_index(
        "ix_forwarding_jobs_reclaim",
        table_name="forwarding_jobs",
        postgresql_where="status = 'leased'",
    )
    op.drop_index(
        "ix_forwarding_jobs_claim",
        table_name="forwarding_jobs",
        postgresql_where="status = 'pending'",
    )
    op.drop_table("forwarding_jobs")
    op.drop_index(
        "ix_control_tasks_reclaim", table_name="control_tasks", postgresql_where="status = 'leased'"
    )
    op.drop_index(
        "ix_control_tasks_claim", table_name="control_tasks", postgresql_where="status = 'pending'"
    )
    op.drop_table("control_tasks")
    op.drop_table("connection_chat_access")
    op.drop_table("telegram_sessions")
    op.drop_index("ix_telegram_chats_connection_id_is_active", table_name="telegram_chats")
    op.drop_table("telegram_chats")
    op.drop_index("ix_forwarding_rules_user_id", table_name="forwarding_rules")
    op.drop_index("ix_forwarding_rules_status", table_name="forwarding_rules")
    op.drop_table("forwarding_rules")
    op.drop_table("connection_update_state")
    op.drop_index(
        "uq_telegram_connections_one_in_progress_per_user",
        table_name="telegram_connections",
        postgresql_where="status IN ('pending', 'awaiting_code', 'awaiting_2fa')",
    )
    op.drop_index("ix_telegram_connections_user_id", table_name="telegram_connections")
    op.drop_table("telegram_connections")
    op.drop_table("idempotency_keys")
    op.drop_index("ix_audit_events_user_id_created_at", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("app_settings")
    op.drop_index("ix_app_sessions_user_id_expires_at", table_name="app_sessions")
    op.drop_table("app_sessions")
    op.drop_table("users")

    # Autogenerate does not drop the enum types it created, which makes a
    # downgrade/upgrade round-trip fail on the second CREATE TYPE.
    for enum_name in (
        "connection_kind",
        "connection_status",
        "peer_type",
        "chat_kind",
        "rule_status",
        "forward_mode",
        "keyword_match_mode",
        "job_status",
        "event_outcome",
        "control_task_kind",
        "control_task_status",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
