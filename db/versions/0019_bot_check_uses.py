"""add bot_check_uses — single-use proof-of-work solutions (ADR-0005 Phase 5)

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-26 00:00:00

Additive only
-------------
* ``bot_check_uses``: the challenge hash of every proof-of-work solution already
  used to sign up or request a magic link, so one solution cannot be replayed.
  Indexed on ``expires_at`` so expired rows can be deleted cheaply.

Nothing writes or reads the table unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_check_uses",
        sa.Column("challenge", sa.String(64), primary_key=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bot_check_uses_expires_at", "bot_check_uses", ["expires_at"])


def downgrade() -> None:
    op.drop_table("bot_check_uses")
