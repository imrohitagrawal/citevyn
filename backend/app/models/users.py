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

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, StrEnumType
from app.models.enums import UserRole

if TYPE_CHECKING:
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[UserRole] = mapped_column(
        StrEnumType(UserRole),
        default=UserRole.demo_user,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Unique (see migration 0008's ``ix_users_email``); NULL for anonymous
    # principals, which is not compared as equal to itself under SQL
    # uniqueness, so any number of anonymous rows may coexist.
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Argon2id PHC string (``app.core.passwords``). NULL for anonymous
    # principals and, later, OAuth-only accounts (PR 12).
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
