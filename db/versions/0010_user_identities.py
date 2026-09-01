"""add user_identities — provider-linked login identities (ADR-0004 PR 12)

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-01 00:00:00

Backs OAuth login (GitHub + Google), described in
``docs/ADR/0004-user-accounts.md``: a ``users`` row can be reached via zero,
one, or two external identities, resolved by the exact ``(provider,
provider_account_id)`` pair rather than by email — never auto-linking by
email is the account-takeover guard this table exists to make structural,
not just a code-review convention.

Additive only
-------------
A new table, one foreign key to the pre-existing ``users`` table, no changes
to any existing table. Safe on a live database; the downgrade is a clean
drop.

``provider`` is a plain ``String(32)``, deliberately NOT a native Postgres
ENUM (contrast ``UserRole``/``AuditAction``, promoted in migration ``0002``):
adding a new provider later must not need its own guarded
``ALTER TYPE ... ADD VALUE`` migration.

``user_id`` FK is ``ondelete="CASCADE"``, matching ``auth_sessions.user_id``
(migration ``0007``): an identity link is a credential FOR a user, not an
independent record, so it must not block deleting the user it authenticates.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # GUID() renders as native UUID on Postgres and CHAR(36) on SQLite; mirror
    # that here rather than importing the app model, so the migration stays a
    # frozen DDL snapshot that a later model edit cannot retroactively change
    # (same reasoning as 0007's auth_sessions.auth_session_id).
    uuid_type: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)
    )

    # The unique constraint is declared INLINE on create_table, not via a
    # separate create_unique_constraint() call afterward -- SQLite (the
    # hermetic test dialect) has no ALTER-based constraint support at all,
    # only the batch/copy-and-move workaround, which create_table's own
    # inline constraint list sidesteps entirely since there is no existing
    # table to alter.
    op.create_table(
        "user_identities",
        sa.Column("identity_id", uuid_type, primary_key=True, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_account_id", sa.String(255), nullable=False),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "provider", "provider_account_id", name="uq_user_identities_provider_account"
        ),
    )
    op.create_index(
        "ix_user_identities_user_id",
        "user_identities",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_table("user_identities")
