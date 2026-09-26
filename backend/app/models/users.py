"""User table for Slice 2, extended by ADR-0004 PR 5 with identity columns.

The MVP supports two roles returned by ``app.core.security``:
``demo_user`` and ``admin``. We persist those identifiers here so that
``sessions`` and ``audit_events`` can reference them via foreign key
with cascade behavior, instead of holding an unconstrained string.

``email``/``password_hash`` (migration ``0008``) are both nullable because
one ``users`` row shape serves both principals in the ADR-0004 single-
principal design: the anonymous ``anon_<uuid4hex>`` row PR 3 mints has
neither, and a registered ``usr_<uuid4hex>`` row (PR 6) has both. ``role``
stays the DB-enum authorization tier (``demo_user``/``admin``); it is
unrelated to whether the row is anonymous or registered, which is expressed
by whether ``email``/``password_hash`` are set — no third enum value is
added for it (native-Postgres-enum migration trap, see ADR-0004).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, StrEnumType
from app.models.enums import UserRole

if TYPE_CHECKING:
    pass


class User(Base):
    __tablename__ = "users"
    # Same name and shape as migration 0008's ``op.create_index("ix_users_email",
    # "users", ["email"], unique=True)``, so the hermetic ``create_all`` schema
    # refuses a duplicate email exactly as production does (#455). A bare
    # ``unique=True`` on the column would create an UNNAMED constraint instead,
    # which the ORM-to-migration parity guard reports as drift.
    __table_args__ = (Index("ix_users_email", "email", unique=True),)

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[UserRole] = mapped_column(
        StrEnumType(UserRole),
        default=UserRole.demo_user,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Unique via ``ix_users_email`` in ``__table_args__``; NULL for anonymous
    # principals, which is not compared as equal to itself under SQL
    # uniqueness, so any number of anonymous rows may coexist.
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Argon2id PHC string (``app.core.passwords``). NULL for anonymous
    # principals and, later, OAuth-only accounts (PR 12).
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # When the account proved it controls ``email`` (ADR-0005 §1): it redeemed a
    # magic link, or an OAuth provider reported that same address as verified.
    # Password sign-up proves nothing, so it leaves this NULL. The free trial is
    # granted once per verified account. Set once, never moved; ``email`` has no
    # change path today, and one added later must clear this in the same write.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
