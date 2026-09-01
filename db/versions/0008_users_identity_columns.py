"""add users identity columns (email, password_hash); sessions FK RESTRICT -> CASCADE (ADR-0004 PR 5)

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-01 00:00:00

Backs the registered-account half of the single-principal design in
``docs/ADR/0004-user-accounts.md``. ``email``/``password_hash`` are nullable:
the anonymous ``anon_<uuid4hex>`` principal minted by PR 3 has neither, and
stays a first-class ``users`` row with both columns ``NULL``. A unique
constraint on ``email`` prevents two accounts claiming the same address;
NULL is not compared as equal to itself under SQL uniqueness on either
SQLite or Postgres, so any number of anonymous (``NULL`` email) rows can
coexist.

No code in this PR writes either column — that ships in PR 6
(``/v1/auth/register``). Additive-only for ``users``: two new nullable
columns, no backfill, safe on a live database.

Sessions FK: the one-way door
------------------------------
``sessions.user_id`` moves ``RESTRICT`` -> ``CASCADE`` so a user can ever be
deleted at all — today, ``ON DELETE RESTRICT`` means any user who has
chatted even once raises ``IntegrityError`` on deletion, forever. This half
of the migration is *itself* reversible (downgrade restores ``RESTRICT``),
but the downgrade is only safe **before PR 6 creates real accounts**. Once a
real user deletes their account and that CASCADE fires, the rows are gone;
no migration downgrade brings them back, because a schema rollback undoes
DDL, not data that a since-run CASCADE already erased. Verify this PR in
production before PR 6 ships (ADR-0004, "PR 5 is a one-way door").

``audit_events.user_id`` is untouched — it stays ``ON DELETE SET NULL`` so
the audit trail survives a deleted account, de-identified rather than
destroyed. That is a deliberate asymmetry with ``sessions``, not an
oversight: an audit row is evidence *about* the platform; a session is
evidence *belonging to* the user, so it is right that the user's deletion
choice erases the second and not the first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("email", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("password_hash", sa.String(255), nullable=True))
    # A separate statement, not `unique=True` on the column above: batch mode's
    # inline unique handling varies across SQLAlchemy versions between an
    # anonymous and a named constraint, and this PR needs a stable name
    # (`ix_users_email`) for the downgrade to drop deterministically.
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_constraint("fk_sessions_user", type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_sessions_user",
            "users",
            ["user_id"],
            ["user_id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_constraint("fk_sessions_user", type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_sessions_user",
            "users",
            ["user_id"],
            ["user_id"],
            ondelete="RESTRICT",
        )

    op.drop_index("ix_users_email", table_name="users")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("password_hash")
        batch_op.drop_column("email")
