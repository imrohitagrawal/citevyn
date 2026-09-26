"""add api_keys — per-user API keys (ADR-0005 Phase 6A)

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-26 00:00:00

Additive only
-------------
* ``api_keys``: one row per key. ``key_id`` (primary key) finds the row; only
  the SHA-256 of the secret is stored. ``user_id`` cascades with the account and
  is indexed. ``revoked_at`` revokes without losing the audit trail.

Plain string columns, not native enums (AGENTS.md). Nothing writes or reads the
table unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("key_id", sa.String(32), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])


def downgrade() -> None:
    op.drop_table("api_keys")
