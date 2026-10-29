"""add deleted_accounts — what is kept after an account is deleted (#511)

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-28 00:00:00

Additive only
-------------
* ``deleted_accounts``: one row per deleted address, keyed by the SHA-256 hex
  of the normalised address (never the address itself), holding the trial
  answers it had used, so deleting an account cannot hand the same address a
  fresh free trial. No foreign key: the account it describes is gone.

Nothing writes it except the operator's ``delete-account`` command.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deleted_accounts",
        sa.Column("email_hash", sa.String(64), primary_key=True),
        sa.Column("trial_answers_used", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("deleted_accounts")
