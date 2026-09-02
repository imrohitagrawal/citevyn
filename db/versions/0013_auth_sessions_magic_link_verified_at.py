"""add auth_sessions.magic_link_verified_at — same-session step-up (ADR-0004 PR 15, #293)

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-02 00:00:00

Additive only
-------------
One nullable column on ``auth_sessions``. Stamped ONLY on a session minted by
a redeemed magic link (``POST /v1/auth/magic-link/confirm``); NULL for every
password, OAuth and anonymous session. ``POST /v1/auth/me/password`` may skip
the current-password check when the CALLER'S OWN session carries a stamp
younger than ``CITEVYN_PASSWORD_STEP_UP_WINDOW_SECONDS``, and clears it on
use (one shot). The stamp is a server-held fact about the session -- never
something the request body can assert -- which is the whole point of the
column (issue #293).

Batch mode on both sides so the SQLite test engine can drop the column on
downgrade the same way Postgres does.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("auth_sessions") as batch_op:
        batch_op.add_column(
            sa.Column("magic_link_verified_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("auth_sessions") as batch_op:
        batch_op.drop_column("magic_link_verified_at")
