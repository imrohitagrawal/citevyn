"""add answer_usage — the answer allowance (ADR-0005 Phase 4B-2)

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-26 00:00:00

Additive only
-------------
* ``answer_usage``: one row per answered question that used plan allowance.
  ``user_id`` cascades with the account; ``message_id`` has no foreign key (the
  count must not change when history is closed). Indexed on
  ``(user_id, occurred_at)`` for "this account's answers since <start>".

Plain string columns, not native enums (AGENTS.md). Nothing writes or reads the
table unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # GUID() mirrored, not imported: the migration stays a frozen snapshot (0010).
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )
    op.create_table(
        "answer_usage",
        sa.Column("usage_id", uuid_type, primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_by", sa.String(16), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("message_id", uuid_type, nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_answer_usage_user_occurred", "answer_usage", ["user_id", "occurred_at"])


def downgrade() -> None:
    op.drop_table("answer_usage")
