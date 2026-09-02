"""Auth session table (ADR-0004 PR 3).

Backs the persistent per-visitor cookie identity described in
``docs/ADR/0004-user-accounts.md``: every request resolves to exactly one
principal, logged in or not, so a session/message cannot be read by anyone
but the principal that owns it.

The cookie carries ``<auth_session_id>.<secret>`` (see
``app.core.auth_sessions``). Only ``secret_hash`` — the SHA-256 digest of the
secret — is stored, never the secret itself: a leaked database row (or a
backup, or a careless log line) is not enough to forge a session, because
recovering the secret from its hash is infeasible. ``auth_session_id`` alone
is a lookup key, not a credential.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    auth_session_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # SHA-256 hex digest (64 chars) of the cookie's secret half. Never the
    # secret itself — see the module docstring.
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # CASCADE, not RESTRICT: an auth session is a credential FOR a user, not
    # an independent record. A deleted user's login cookies must stop
    # resolving to anything rather than blocking the deletion (contrast
    # ``sessions.user_id``, whose PR 5 migration moves it the other way,
    # RESTRICT -> CASCADE, for the same reason).
    user_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # ADR-0004 PR 15 (#293): set ONLY when this session was minted by a
    # redeemed magic link; NULL for password/OAuth/anonymous sessions.
    # ``POST /v1/auth/me/password`` may skip the current-password check while
    # this is younger than ``password_step_up_window_seconds`` and clears it
    # on use. A server-held fact about the session, never a body field.
    magic_link_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["AuthSession"]
