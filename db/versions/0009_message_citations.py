"""add messages.citations — persisted citation snapshot for history resume (ADR-0004 PR 10)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-01 00:00:00

Backs the "resumed conversation renders its source cards" requirement from
``docs/ADR/0004-user-accounts.md`` PR 10. Before this column existed, the
ONLY place a citation's wire shape (``source_name``/``title``/``url``/
``chunk_id``/``marker``) was captured was the live response returned at ask
time — nothing persisted it for later retrieval.

``retrieved_evidence`` looked like a substitute at first glance (it does
carry ``chunk_id`` and ``used_in_answer``), but it is NOT a reliable source
for reconstructing history: a cache-hit answer (``Orchestrator.
_respond_cache_hit``) persists its user/assistant messages with
``evidence=[]`` — zero ``retrieved_evidence`` rows — because the whole point
of a cache hit is skipping retrieval. Reconstructing citations from
``retrieved_evidence`` would silently show NO sources for any historical
message that happened to be served from cache, which is exactly the
"looks like coverage, isn't" failure shape ``docs/BACKLOG.md``'s #170 entry
warns about elsewhere in this codebase. ``app.answer.orchestrator.
_persist_messages`` already receives the fully-resolved ``citations: list[
Citation]`` (marker included) on EVERY response path — including cache hits,
which carry ``cached.citations`` forward — so persisting that value directly
is both simpler and strictly correct where a rank-reconstruction approach
would not be.

Additive only
-------------
One new nullable column on ``messages``, no changes to any existing table or
column. ``NULL`` for every pre-existing row (they have no captured
citations, which is exactly the previous behavior — nothing is invented for
them) and for user messages / any assistant message with no citations
(a no-answer or unsupported refusal). Safe on a live database; the
downgrade is a clean column drop.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(sa.Column("citations", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("citations")
