"""add magic_link_tokens — emailed single-use sign-in links (ADR-0004 PR 14)

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-02 00:00:00

Additive only
-------------
One new table with one foreign key (``users``), one index, no changes to any
existing table. Safe on a live database; the downgrade is a clean drop.

``user_id`` is ``ondelete="CASCADE"``, verified explicitly at review time
per the plan (this codebase has had the CASCADE/RESTRICT direction wrong
before -- migration 0008 exists to flip ``sessions.user_id``): a pending
sign-in link is a credential FOR a user and must not survive, or block
deleting, that user. The index on ``user_id`` backs the delete-prior-tokens
statement every ``POST /v1/auth/magic-link/request`` runs.

No ``used_at``/soft-delete column: the confirm route hard-deletes the row on
a successful claim (``DELETE ... RETURNING``), same as ``oauth_nonces``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )

    op.create_table(
        "magic_link_tokens",
        sa.Column("token_id", uuid_type, primary_key=True, nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_magic_link_tokens_user_id", "magic_link_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_magic_link_tokens_user_id", table_name="magic_link_tokens")
    op.drop_table("magic_link_tokens")
