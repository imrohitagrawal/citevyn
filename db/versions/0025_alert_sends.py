"""add alert_sends — watch alert emails (ADR-0005 Phase 7C-2)

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-27 00:00:00

Additive only
-------------
* ``alert_sends``: one row per (account, change) alerted, unique on both, so a
  change is never alerted to the same account twice. Rows of one email share
  ``email_id`` (indexed) and ``token_hash`` (that email's one-click unsubscribe
  token, hashed). Cascades with the account and with the change.

Nothing writes or reads it unless watch alerts are turned on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # GUID() mirrored, not imported: the migration stays a frozen snapshot (0010).
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )
    op.create_table(
        "alert_sends",
        sa.Column("alert_id", uuid_type, primary_key=True),
        sa.Column("email_id", uuid_type, nullable=False),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "change_id",
            uuid_type,
            sa.ForeignKey("doc_changes.change_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("user_id", "change_id", name="uq_alert_sends_user_change"),
    )
    op.create_index("ix_alert_sends_email_id", "alert_sends", ["email_id"])


def downgrade() -> None:
    op.drop_table("alert_sends")
