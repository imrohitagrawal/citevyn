"""OAuth state/PKCE nonce table (ADR-0004 PR 12).

The CSRF/replay guard for ``GET /v1/auth/oauth/{provider}/callback`` (see
``app.api.routes.oauth``). ``nonce_id`` is the value sent to the provider as
``state`` and round-tripped back on the callback -- a lookup key, not a
credential, same pattern as ``AuthSession.auth_session_id``.

Single-use: the callback route deletes the row immediately on successful
validation. There is no ``used_at``/soft-delete column on purpose -- "the row
is gone" is the only signal a replayed ``state`` needs to fail, rather than
two overlapping failure modes (used vs. expired) that could disagree.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class OAuthNonce(Base):
    __tablename__ = "oauth_nonces"

    nonce_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # A GitHub-flow state must not validate a Google callback.
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # PKCE verifier -- stored server-side only, never leaves the backend
    # (only its S256 challenge is sent to the provider).
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    # Nullable + CASCADE: a nonce is a short-lived artifact of one browser's
    # in-flight attempt, not an independent record. Binding to the CURRENT
    # request's auth_session_id at callback time is what "state bound to
    # session" concretely means -- see the route's docstring.
    auth_session_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("auth_sessions.auth_session_id", ondelete="CASCADE"),
        nullable=True,
    )
    # "login" (oauth_start, PR 12) or "connect" (oauth_connect_start, ADR-0004
    # PR 13). Compared EXACTLY at callback; an unknown value fails closed.
    # A plain String(16), not an enum, so adding a third intent still needs
    # no migration -- which is exactly why PR 13 needed none.
    return_intent: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["OAuthNonce"]
