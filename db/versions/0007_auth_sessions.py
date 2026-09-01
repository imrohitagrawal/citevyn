"""add auth_sessions — persistent per-visitor cookie identity (ADR-0004 PR 3)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-01 00:00:00

Backs the cookie described in ``docs/ADR/0004-user-accounts.md``: every
visitor, logged in or not, resolves to exactly one principal so ownership of
a session/message can be checked (``app.api.routes.sessions
._get_session_or_404``, ADR-0004 PR 1). Before this table existed, every
visitor shared the one constant ``demo_user`` principal.

Additive only
-------------
A new table, one foreign key to the pre-existing ``users`` table, no changes
to any existing table. Safe on a live database; the downgrade is a clean
drop. Nothing reads this table before the code that writes it
(``app.core.auth_sessions``) ships in the same PR, so migration and deploy
order do not matter — unlike ``0006``, which had to move together with its
application code.

``user_id`` FK is ``ondelete="CASCADE"``, not ``RESTRICT``: an auth session
is a credential FOR a user, not an independent record, so it must not block
deleting the user it authenticates. See the ``AuthSession`` model docstring.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # GUID() renders as native UUID on Postgres and CHAR(36) on SQLite; mirror
    # that here rather than importing the app model, so the migration stays a
    # frozen DDL snapshot that a later model edit cannot retroactively change
    # (same reasoning as 0005's provider_calls.call_id).
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )

    op.create_table(
        "auth_sessions",
        sa.Column("auth_session_id", uuid_type, primary_key=True, nullable=False),
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


def downgrade() -> None:
    op.drop_table("auth_sessions")
