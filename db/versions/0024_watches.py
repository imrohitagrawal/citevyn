"""add doc_changes.lines and watches — Pro watch alerts (ADR-0005 Phase 7C)

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-27 00:00:00

Additive only
-------------
* ``doc_changes.lines`` (JSON, nullable): the added and removed lines of a
  change (bounded), so a term watch can match. NULL on existing rows.
* ``watches``: one row per (account, kind, value), unique; ``user_id``
  cascades with the account and is indexed.

Nothing writes or reads these unless the access model is on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # GUID() mirrored, not imported: the migration stays a frozen snapshot (0010).
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )
    op.add_column("doc_changes", sa.Column("lines", sa.JSON(), nullable=True))
    op.create_table(
        "watches",
        sa.Column("watch_id", uuid_type, primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "kind", "value", name="uq_watches_user_kind_value"),
    )
    op.create_index("ix_watches_user_id", "watches", ["user_id"])


def downgrade() -> None:
    op.drop_table("watches")
    op.drop_column("doc_changes", "lines")
