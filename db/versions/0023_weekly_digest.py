"""add users.digest_opt_in_at and digest_sends — the weekly digest (ADR-0005 Phase 7B)

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-27 00:00:00

Additive only
-------------
* ``users.digest_opt_in_at`` (nullable): opt-in to the weekly "what changed"
  digest; NULL (every existing account) means no digest.
* ``digest_sends``: one row per (account, week) sent, unique on both, so a
  re-run never mails anyone twice; ``token_hash`` is the SHA-256 of that
  email's one-click unsubscribe token. ``user_id`` cascades with the account.

Nothing writes or reads these unless the digest is turned on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # GUID() mirrored, not imported: the migration stays a frozen snapshot (0010).
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )
    op.add_column("users", sa.Column("digest_opt_in_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "digest_sends",
        sa.Column("send_id", uuid_type, primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "week_start", name="uq_digest_sends_user_week"),
    )


def downgrade() -> None:
    op.drop_table("digest_sends")
    op.drop_column("users", "digest_opt_in_at")
