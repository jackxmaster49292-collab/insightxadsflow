"""open access: terms, suspension, user counters

The bot stops being single-operator. Accounts now need three things the
allowlist made unnecessary:

* ``terms_accepted_at`` — NULL means the account has not accepted, and the panel
  shows it nothing but the terms screen until it does. Existing accounts get
  NULL too, so the operator sees the terms once as well. That is deliberate:
  the statement applies to them the same way it applies to everyone else.
* ``suspended_at`` / ``suspended_reason`` — suspension is ``is_active = false``,
  which already existed; these say when and why, so being cut off is not a
  silent mystery to the person it happened to.
* ``broadcasts_sent`` — the one counter that lets an operator spot an account
  behaving unlike the others without reading anything it sends.

Revision ID: 16d1e7be2bd4
Revises: eac10ca4eb8a
Create Date: 2026-08-22 23:47:31.957770
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "16d1e7be2bd4"
down_revision: str | None = "eac10ca4eb8a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("suspended_reason", sa.String(length=200), nullable=True))
    op.add_column(
        "users", sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True)
    )
    # server_default is load-bearing: this column is NOT NULL and the table
    # already has rows, so adding it without one fails outright. It stays on the
    # column rather than being dropped afterwards, so an INSERT that omits the
    # counter still works.
    op.add_column(
        "users",
        sa.Column("broadcasts_sent", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "broadcasts_sent")
    op.drop_column("users", "terms_accepted_at")
    op.drop_column("users", "suspended_reason")
    op.drop_column("users", "suspended_at")
