"""add chunks.embedding column (pickle-backed portable blob)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-19 00:00:00

Adds a nullable ``embedding`` column to the ``chunks`` table. The
column is declared as ``LargeBinary`` (a portable blob type) so:

* On SQLite (hermetic test engine) it becomes a ``BLOB``.
* On Postgres it becomes ``bytea``.

Values are pickled ``list[float]`` payloads produced by
:class:`app.models.base.PickledEmbedding`. The decorator is dialect-
agnostic, so the same column declaration works on both backends
without any ``if dialect == "postgresql"`` guard.

Why not ``pgvector`` here?
--------------------------
The follow-up migration (``0004``) swaps the Postgres column to
``pgvector``'s ``vector(<dim>)`` and adds an ``ivfflat``/``hnsw``
index. That change needs three things this slice hasn't decided:

1. The embedding model + its fixed dimension (today the
   :class:`StubEmbedder` returns an arbitrary dim, configurable via
   ``CITEVYN_EMBEDDING_DIM``).
2. Whether to use ``ivfflat`` or ``hnsw`` and the index build
   parameters (``lists``, ``ef_construction``).
3. A backfill plan for existing rows whose ``embedding`` is
   ``NULL`` after this migration lands.

This migration is intentionally cheap (one ``ADD COLUMN`` on
both backends, ``NULL`` default) so it can land and be tested in
isolation before the heavier ``pgvector`` migration.

Downgrade
---------
``op.drop_column("chunks", "embedding")`` drops the column on both
backends. SQLite supports ``DROP COLUMN`` from 3.35 onwards (we
require 3.40+ in CI); Postgres has supported it since 9.6. Existing
rows lose their embedding, which is acceptable because the retriever
short-circuits on ``embedding IS NULL``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # LargeBinary is the portable binary type: BLOB on SQLite,
    # ``bytea`` on Postgres. The ORM column
    # (:class:`app.models.base.PickledEmbedding`) handles the
    # pickle round-trip transparently.
    op.add_column(
        "chunks",
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
    )
    # No index here — a future migration will add a pgvector index
    # for cosine distance lookups on Postgres.


def downgrade() -> None:
    op.drop_column("chunks", "embedding")