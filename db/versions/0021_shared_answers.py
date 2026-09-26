"""add shared_answers — shareable answers (ADR-0005 Phase 6C)

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-26 00:00:00

Additive only
-------------
* ``shared_answers``: one row per shared answer, a frozen copy of the question,
  answer, citations and "docs as of" date. ``share_id`` (primary key) is the
  random part of the public link. ``user_id`` cascades with the account;
  ``message_id`` is SET NULL, so the copy outlives the conversation.
  ``revoked_at`` revokes without losing the row.

Plain string columns, not native enums (AGENTS.md). Nothing writes or reads the
table unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # GUID() mirrored, not imported: the migration stays a frozen snapshot (0010).
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )
    op.create_table(
        "shared_answers",
        sa.Column("share_id", sa.String(32), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            uuid_type,
            sa.ForeignKey("messages.message_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("docs_as_of", sa.String(10), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_shared_answers_user_id", "shared_answers", ["user_id"])
    op.create_index("ix_shared_answers_message_id", "shared_answers", ["message_id"])


def downgrade() -> None:
    op.drop_table("shared_answers")
