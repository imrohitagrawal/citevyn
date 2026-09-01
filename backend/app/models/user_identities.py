"""External login identity table (ADR-0004 PR 12).

Backs OAuth login (GitHub + Google), described in
``docs/ADR/0004-user-accounts.md``: resolving "who is this caller" from a
provider callback goes through this table's ``(provider,
provider_account_id)`` unique pair — never by matching the provider's email
against ``users.email`` — because auto-linking by email would let anyone who
controls a matching email on a third-party provider take over an existing
password account. See ``app.api.routes.oauth`` for the resolution order this
table exists to make structural.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class UserIdentity(Base):
    __tablename__ = "user_identities"
    # Declared at the ORM level too (not just migration 0010's inline
    # constraint) so the hermetic SQLite test suite -- which creates its
    # schema from this metadata via Base.metadata.create_all, not alembic --
    # actually enforces the same uniqueness Postgres does. Without this, the
    # concurrent-first-time-login race in app.api.routes.oauth's
    # IntegrityError handling would be untestable outside a live Postgres.
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_account_id", name="uq_user_identities_provider_account"
        ),
    )

    identity_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # Plain string values "github" / "google" -- NOT a DB enum. See the
    # migration docstring: a native Postgres enum needs a guarded
    # ``ALTER TYPE ... ADD VALUE`` migration to add a member later, which is
    # avoided here since new providers may be added.
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # The provider's stable subject id (GitHub's numeric user id as a
    # string; Google's OIDC `sub`) -- never the provider's email.
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # CASCADE, not RESTRICT: an identity link is a credential FOR a user, not
    # an independent record. Mirrors ``AuthSession.user_id``.
    user_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["UserIdentity"]
