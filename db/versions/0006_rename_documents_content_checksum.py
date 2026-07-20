"""rename documents.content_checksum -> documents.identity_checksum

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20 00:00:00

``documents.content_checksum`` never hashed the document's content: the
ingestion runner computes it as ``sha256(source_name + title)``. So it
changed when a source was *retitled* and did NOT change when the prose was
edited — the exact inverse of what the name promised (issue #163). The real
content fingerprint is ``app.worker.cli._content_version_hash`` (stamped on
``index_versions.source_version_hash``, which feeds the answer-cache key);
per-chunk content hashes live on ``chunks.content_checksum`` and are genuine.

This is a pure rename: same type (``String(128)``), same nullability, same
values. No data migration is needed because the semantics of the stored value
are unchanged — only the name now tells the truth. ``chunks.content_checksum``
is deliberately left alone; it really is a content hash.

Dialect handling
----------------
SQLite cannot ``ALTER TABLE ... RENAME COLUMN`` through Alembic's plain
``alter_column`` (it has no generic ALTER), so this uses
``batch_alter_table``. Batch mode is a pass-through on Postgres (it emits a
native ``ALTER TABLE ... RENAME COLUMN``) and recreates-and-copies on SQLite,
so one code path covers both backends.

Downgrade
---------
Symmetric: renames the column back to ``content_checksum``. Values survive in
both directions, so the migration is fully reversible with no data loss.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.alter_column(
            "content_checksum",
            new_column_name="identity_checksum",
            existing_type=sa.String(length=128),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.alter_column(
            "identity_checksum",
            new_column_name="content_checksum",
            existing_type=sa.String(length=128),
            existing_nullable=False,
        )
