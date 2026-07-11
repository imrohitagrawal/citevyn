"""swap chunks.embedding to pgvector vector(1536) + HNSW; stamp IndexVersion

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-11 00:00:00

Makes vector retrieval real on Postgres (issue #51). Two changes:

1. ``chunks.embedding`` becomes a pgvector ``vector(1536)`` column with an HNSW
   index using ``vector_cosine_ops`` (matches the ``<=>`` cosine operator the
   retriever emits). 1536 is the largest recommended Gemini Matryoshka size under
   pgvector's 2000-dim index limit, and MUST equal ``Settings.embedding_dim``.
2. ``index_versions`` gains ``embedding_provider`` / ``embedding_model`` /
   ``embedding_dim`` columns (the Tier 3 guardrail): the provenance of the embedder
   that built each index, so a future query-time embedder can be checked against
   the model that built the active index.

Dialect handling
----------------
The pgvector column, extension, and index are **Postgres-only**; on SQLite (the
hermetic test engine) ``chunks.embedding`` stays a ``LargeBinary`` blob and this
migration touches only the portable ``index_versions`` columns. The hermetic suite
uses ``Base.metadata.create_all`` rather than alembic, so the SQLite branch is a
belt-and-braces guard.

Data note
---------
Existing ``embedding`` values are stub/pickle bytes that are meaningless as
vectors, so the upgrade drops and re-adds the column (NULL for existing rows)
rather than attempting an impossible ``bytea → vector`` cast. Real vectors are
produced by re-ingesting under the Gemini embedder. The retriever short-circuits
on ``embedding IS NULL``, so a partially-backfilled index degrades gracefully.

Downgrade
---------
Reverses the schema shape: drops the HNSW index, converts ``chunks.embedding`` back
to ``bytea`` (data lost — acceptable, the retriever tolerates NULL), and drops the
three ``index_versions`` columns. The ``vector`` extension is intentionally left
installed (dropping a shared extension is riskier than leaving it, and it does not
affect the restored ``bytea`` schema).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The pgvector column dimension. MUST match ``Settings.embedding_dim`` (1536).
# Alembic migrations are immutable DDL snapshots, so this is a literal here; a
# future dimension change ships as a new migration, not an edit to this one.
_EMBEDDING_DIM = 1536


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # Existing bytea (pickle) data cannot be cast to vector; drop + re-add.
        op.drop_column("chunks", "embedding")
        op.execute(f"ALTER TABLE chunks ADD COLUMN embedding vector({_EMBEDDING_DIM})")
        # HNSW + cosine ops: matches the retriever's ``<=>`` operator. Built on an
        # (initially) empty/NULL column, so this is cheap at migration time.
        op.execute(
            "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
            "USING hnsw (embedding vector_cosine_ops)"
        )

    # Portable on both backends.
    op.add_column(
        "index_versions",
        sa.Column("embedding_provider", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "index_versions",
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "index_versions",
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_column("index_versions", "embedding_dim")
    op.drop_column("index_versions", "embedding_model")
    op.drop_column("index_versions", "embedding_provider")

    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
        op.drop_column("chunks", "embedding")
        # Restore the pre-0004 portable blob column (bytea).
        op.add_column(
            "chunks",
            sa.Column("embedding", sa.LargeBinary(), nullable=True),
        )
        # The ``vector`` extension is left installed on purpose (see module docstring).
