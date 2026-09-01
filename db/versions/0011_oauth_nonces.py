"""add oauth_nonces — state/PKCE anchor for OAuth login (ADR-0004 PR 12)

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-01 00:00:00

DB-backed state/PKCE mechanism (not Redis, which is optional infra in this
codebase — see ``app.api.routes.oauth`` module docstring for the full
rationale). ``nonce_id`` round-trips through the browser as the ``state``
query parameter — a lookup key, not a credential, same pattern as
``auth_sessions.auth_session_id``.

Additive only
-------------
A new table, two foreign keys (``auth_sessions``, transitively ``users`` via
that FK's own cascade), no changes to any existing table. Safe on a live
database; the downgrade is a clean drop.

``auth_session_id`` is nullable + ``ondelete="CASCADE"``: a nonce is a
short-lived (5 minute TTL) artifact of one browser's in-flight OAuth attempt,
not an independent record — it must not survive, or block deleting, the
session it is bound to.

No ``used_at``/soft-delete column: the callback route hard-deletes the row on
successful validation (see the route's docstring), so "single-use" collapses
to one failure mode ("the row is gone") instead of two.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )

    op.create_table(
        "oauth_nonces",
        sa.Column("nonce_id", uuid_type, primary_key=True, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column(
            "auth_session_id",
            uuid_type,
            sa.ForeignKey("auth_sessions.auth_session_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("return_intent", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("oauth_nonces")
