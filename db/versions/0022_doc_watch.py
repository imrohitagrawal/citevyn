"""add doc_snapshots and doc_changes — the docs watcher (ADR-0005 Phase 7A)

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-27 00:00:00

Additive only
-------------
* ``doc_snapshots``: the latest text of each watched vendor page (primary key
  ``url``), so the next run can compare. ``last_error`` says why the last check
  failed (never page text).
* ``doc_changes``: one row per real change with a section summary (JSON),
  indexed by ``url`` and ``detected_at`` for digests and watch alerts.

Separate from the answer corpus: nothing here is embedded. Nothing writes or
reads these tables unless ``CITEVYN_DOCS_WATCH_ENABLED`` is on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # GUID() mirrored, not imported: the migration stays a frozen snapshot (0010).
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )
    op.create_table(
        "doc_snapshots",
        sa.Column("url", sa.String(512), primary_key=True),
        sa.Column("product_area", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(255), nullable=True),
    )
    op.create_table(
        "doc_changes",
        sa.Column("change_id", uuid_type, primary_key=True),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("product_area", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("old_sha256", sa.String(64), nullable=False),
        sa.Column("new_sha256", sa.String(64), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
    )
    op.create_index("ix_doc_changes_url", "doc_changes", ["url"])
    op.create_index("ix_doc_changes_detected_at", "doc_changes", ["detected_at"])


def downgrade() -> None:
    op.drop_table("doc_changes")
    op.drop_table("doc_snapshots")
