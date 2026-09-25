"""add answer_feedback and source_requests — user signals (ADR-0005 §6)

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-26 00:00:00

Additive only
-------------
Two new tables, written only by a caller about their own conversations:

* ``answer_feedback``: thumbs up/down on an answer, with an optional reason and
  comment ("report wrong answer"). Unique on (message_id, user_id): a second
  vote replaces the first.
* ``source_requests``: "request this source", the gap log. ``message_id`` is
  SET NULL on delete so the gap signal outlives the chat.

``rating``, ``reason`` and ``status`` are plain strings, not native enums
(AGENTS.md), so a new value needs no ``ALTER TYPE``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # uuid_type renders as native UUID on Postgres and CHAR(36) on SQLite; mirrored
    # here rather than importing the app model, so the migration stays a frozen
    # DDL snapshot (the 0007 / 0010 convention).
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )
    op.create_table(
        "answer_feedback",
        sa.Column("feedback_id", uuid_type, primary_key=True),
        sa.Column(
            "message_id",
            uuid_type,
            sa.ForeignKey("messages.message_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rating", sa.String(8), nullable=False),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("message_id", "user_id", name="uq_answer_feedback_message_user"),
    )
    op.create_table(
        "source_requests",
        sa.Column("request_id", uuid_type, primary_key=True),
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
        sa.Column("requested_url", sa.String(2048), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("source_requests")
    op.drop_table("answer_feedback")
