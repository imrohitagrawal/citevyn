"""add provider_calls — per-call cost metering (#153 Layer 1)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20 00:00:00

``RELEASE_PLAN.md`` §9 specifies a soft $5 / hard $10 daily limit, but nothing in
the system recorded what a provider call cost, so there was no data to enforce it
from: ``LLMResult`` carried token counts that were discarded as soon as the answer
was returned. This table is the metering substrate the budget is computed from.

Additive only
-------------
A new table with no foreign keys and no changes to existing tables, so the upgrade
is safe on a live database and the downgrade is a clean drop. Nothing reads the
table before the code that writes it ships, so migration and deploy order do not
matter.

``request_id`` is deliberately a plain string rather than a foreign key: ingest and
eval calls happen outside any HTTP request, and a spend row must never be blocked
(or cascade-deleted) by the lifecycle of a correlated record. Spend is a fact about
money that already left; it outlives the request that caused it.

Numeric, not float
------------------
``cost_usd`` is ``NUMERIC(14, 6)``. These values are summed across thousands of rows
and compared against a dollar threshold; binary floats accumulate error in exactly
that pattern, and a budget that trips a cent late is a bug that only appears in
production. 6 decimal places resolve a single cheap call (~$0.000015).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
# Imported EXPLICITLY. ``import sqlalchemy as sa`` does NOT pull in the dialect
# submodules, so ``sa.dialects.postgresql`` is an AttributeError in a fresh
# interpreter. It resolves today only because alembic eagerly loads every revision
# file and migration 0002 happens to import it — so squashing or deleting 0002
# would break every Postgres upgrade through this revision.
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # GUID() renders as native UUID on Postgres and CHAR(36) on SQLite; mirror that
    # here rather than importing the app model, so the migration stays a frozen DDL
    # snapshot that a later model edit cannot retroactively change.
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )

    op.create_table(
        "provider_calls",
        sa.Column("call_id", uuid_type, primary_key=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("call_site", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=False, server_default="0"),
        sa.Column("input_price_per_1m", sa.Numeric(12, 6), nullable=True),
        sa.Column("output_price_per_1m", sa.Numeric(12, 6), nullable=True),
        sa.Column("priced", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("tokens_estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("request_id", sa.String(64), nullable=True),
    )
    # The budget's hot query is "sum cost_usd since midnight UTC" and it runs before
    # every paid call, so it must not degrade into a full scan as the table grows.
    op.create_index("ix_provider_calls_occurred_at", "provider_calls", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_provider_calls_occurred_at", table_name="provider_calls")
    op.drop_table("provider_calls")
