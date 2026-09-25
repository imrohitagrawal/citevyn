"""add documents.content_as_of — the "docs as of" date (ADR-0005 §6)

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-26 00:00:00

Additive only
-------------
One nullable DATE column on ``documents``: the day this repo's copy of the
source was last updated (``SourceSpec.content_as_of``), stamped at ingest so it
travels with the indexed text. It is what answers and citations show as "docs as
of <date>".

``last_fetched_at`` is not that date: it is the ingest run's clock, and every
deploy re-ingests, so it would say "today" about months-old content. Rows
ingested before this migration read NULL, and the UI shows no date for them
rather than inventing one; the next ingest fills them.

Batch mode on both sides so the SQLite test engine can drop the column on
downgrade the same way Postgres does.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(sa.Column("content_as_of", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("content_as_of")
