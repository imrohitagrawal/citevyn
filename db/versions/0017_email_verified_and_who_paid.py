"""add users.email_verified_at and who paid on provider_calls (ADR-0005 Phase 4B)

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-26 00:00:00

Additive only
-------------
* ``users.email_verified_at``: when the account proved it controls its address.
  No backfill: an existing account is stamped on its next magic-link redeem or
  verified OAuth sign-in.
* ``provider_calls.user_id``: the account a paid model call was made for. NULL
  for ingest, eval and anonymous calls. No foreign key: spend rows are a record
  of money already spent and must outlive the account.
* ``provider_calls.paid_by``: ``platform`` (our key) or ``user`` (BYOK, Phase 8).
  A plain string, not a native enum (AGENTS.md). Existing rows are ``platform``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("provider_calls", sa.Column("user_id", sa.String(128), nullable=True))
    op.add_column(
        "provider_calls",
        sa.Column("paid_by", sa.String(16), nullable=False, server_default="platform"),
    )


def downgrade() -> None:
    op.drop_column("provider_calls", "paid_by")
    op.drop_column("provider_calls", "user_id")
    op.drop_column("users", "email_verified_at")
