"""add memberships and stripe_events — entitlement (ADR-0005 Phase 4)

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-26 00:00:00

Additive only
-------------
* ``memberships``: one row per Stripe subscription; an account is Pro if any of
  its rows is paid. Written only by verified Stripe webhooks, read on every
  protected request. ``stripe_subscription_id`` is unique and required;
  ``account_id`` and ``stripe_customer_id`` are indexed.
* ``stripe_events``: every webhook event id once: idempotency, and the audit log
  of membership changes.

Plain string columns, not native enums (AGENTS.md). Nothing reads either table
unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # GUID() mirrored, not imported: the migration stays a frozen snapshot (0010).
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )
    op.create_table(
        "memberships",
        sa.Column("membership_id", uuid_type, primary_key=True),
        sa.Column(
            "account_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("billing_interval", sa.String(8), nullable=True),
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=False, unique=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False),
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memberships_account_id", "memberships", ["account_id"])
    op.create_index("ix_memberships_stripe_customer_id", "memberships", ["stripe_customer_id"])
    op.create_table(
        "stripe_events",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("event_created", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stripe_events")
    op.drop_table("memberships")
