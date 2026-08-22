"""broadcasts and auto-reply

Adds the two features that are not forwarding:

* **broadcasts** — the customer's own message, posted to groups they choose.
  ``broadcast_targets`` carries a lease and an attempt count for the same reason
  ``forwarding_jobs`` does: a worker that dies mid-broadcast must resume rather
  than restart.
* **auto-reply** — an answer for people who message the connected account first.
  ``auto_reply_log`` is a table rather than a cache because an empty cache after
  a restart would answer everyone a second time.

``forwarding_events.rule_id`` becomes nullable so both pipelines share one
activity feed. A check constraint keeps every row attached to exactly one of the
two, so nothing can be rendered without knowing where it came from.

Revision ID: eac10ca4eb8a
Revises: 293e62a48ff8
Create Date: 2026-08-22 22:25:07.335500
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "eac10ca4eb8a"
down_revision: str | None = "293e62a48ff8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Types the initial migration already created. Referencing them with
# create_type=False is load-bearing: autogenerate emits a plain sa.Enum, which
# issues an unconditional CREATE TYPE and fails with "type already exists".
PEER_TYPE = postgresql.ENUM("user", "chat", "channel", name="peer_type", create_type=False)
JOB_STATUS = postgresql.ENUM(
    "pending",
    "leased",
    "succeeded",
    "failed",
    "skipped",
    "needs_attention",
    "dead_letter",
    name="job_status",
    create_type=False,
)

#: Introduced here, so this migration owns creating and dropping them.
NEW_ENUMS = ("broadcast_status", "broadcast_media")


def upgrade() -> None:
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "scheduled",
                "sending",
                "paused",
                "completed",
                "cancelled",
                name="broadcast_status",
            ),
            nullable=False,
        ),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("media_kind", sa.Enum("none", "photo", name="broadcast_media"), nullable=False),
        sa.Column("media_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("media_filename", sa.String(length=128), nullable=True),
        sa.Column("delay_ms", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_reason_code", sa.String(length=48), nullable=True),
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
            name=op.f("ck_broadcasts_broadcast_delay_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_broadcasts_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_broadcasts_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broadcasts")),
    )
    op.create_index("ix_broadcasts_status", "broadcasts", ["status"], unique=False)
    op.create_index("ix_broadcasts_user_id", "broadcasts", ["user_id"], unique=False)

    op.create_table(
        "broadcast_targets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("broadcast_id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", JOB_STATUS, nullable=False),
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
            ["broadcast_id"],
            ["broadcasts.id"],
            name=op.f("fk_broadcast_targets_broadcast_id_broadcasts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_broadcast_targets_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broadcast_targets")),
        # One row per (broadcast, group): enqueueing twice cannot produce two
        # deliveries to the same group.
        sa.UniqueConstraint("broadcast_id", "chat_id", name="uq_broadcast_targets_broadcast_chat"),
    )
    op.create_index(
        "ix_broadcast_targets_broadcast_id_status",
        "broadcast_targets",
        ["broadcast_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_broadcast_targets_claim",
        "broadcast_targets",
        ["not_before"],
        unique=False,
        postgresql_where="status = 'pending'",
    )
    op.create_index(
        "ix_broadcast_targets_reclaim",
        "broadcast_targets",
        ["lease_expires_at"],
        unique=False,
        postgresql_where="status = 'leased'",
    )

    op.create_table(
        "auto_replies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("cooldown_s", sa.Integer(), nullable=False),
        sa.Column("sent_count", sa.Integer(), nullable=False),
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
            "cooldown_s >= 60", name=op.f("ck_auto_replies_auto_reply_cooldown_floor")
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_auto_replies_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auto_replies_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auto_replies")),
        sa.UniqueConstraint("connection_id", name=op.f("uq_auto_replies_connection_id")),
    )
    op.create_index("ix_auto_replies_user_id", "auto_replies", ["user_id"], unique=False)

    op.create_table(
        "auto_reply_log",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("peer_type", PEER_TYPE, nullable=False),
        sa.Column("peer_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "replied_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reply_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["telegram_connections.id"],
            name=op.f("fk_auto_reply_log_connection_id_telegram_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "connection_id", "peer_type", "peer_id", name=op.f("pk_auto_reply_log")
        ),
    )
    op.create_index("ix_auto_reply_log_replied_at", "auto_reply_log", ["replied_at"], unique=False)

    # One activity feed for both pipelines.
    op.add_column("forwarding_events", sa.Column("broadcast_id", sa.Uuid(), nullable=True))
    op.alter_column("forwarding_events", "rule_id", existing_type=sa.UUID(), nullable=True)
    op.create_index(
        "ix_forwarding_events_broadcast_id_occurred_at",
        "forwarding_events",
        ["broadcast_id", "occurred_at"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_forwarding_events_broadcast_id_broadcasts"),
        "forwarding_events",
        "broadcasts",
        ["broadcast_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        op.f("ck_forwarding_events_event_belongs_to_exactly_one_pipeline"),
        "forwarding_events",
        "(rule_id IS NULL) <> (broadcast_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_forwarding_events_event_belongs_to_exactly_one_pipeline"),
        "forwarding_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_forwarding_events_broadcast_id_broadcasts"),
        "forwarding_events",
        type_="foreignkey",
    )
    op.drop_index("ix_forwarding_events_broadcast_id_occurred_at", table_name="forwarding_events")
    # Rows created by a broadcast have no rule_id, so restoring NOT NULL would
    # fail on them. They cannot be reattached to anything, and the column they
    # depend on is going away with them.
    op.execute("DELETE FROM forwarding_events WHERE rule_id IS NULL")
    op.alter_column("forwarding_events", "rule_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("forwarding_events", "broadcast_id")

    op.drop_index("ix_auto_reply_log_replied_at", table_name="auto_reply_log")
    op.drop_table("auto_reply_log")
    op.drop_index("ix_auto_replies_user_id", table_name="auto_replies")
    op.drop_table("auto_replies")

    op.drop_index(
        "ix_broadcast_targets_reclaim",
        table_name="broadcast_targets",
        postgresql_where="status = 'leased'",
    )
    op.drop_index(
        "ix_broadcast_targets_claim",
        table_name="broadcast_targets",
        postgresql_where="status = 'pending'",
    )
    op.drop_index("ix_broadcast_targets_broadcast_id_status", table_name="broadcast_targets")
    op.drop_table("broadcast_targets")
    op.drop_index("ix_broadcasts_user_id", table_name="broadcasts")
    op.drop_index("ix_broadcasts_status", table_name="broadcasts")
    op.drop_table("broadcasts")

    # Autogenerate does not drop the enum types it created, which makes a
    # downgrade/upgrade round-trip fail on the second CREATE TYPE. Only the two
    # introduced here: peer_type and job_status belong to the initial migration
    # and are still in use by other tables.
    for enum_name in NEW_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
