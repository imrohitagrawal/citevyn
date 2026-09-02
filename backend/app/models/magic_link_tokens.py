"""Magic-link login tokens (ADR-0004 PR 14).

One row per outstanding emailed sign-in link. ``token_id`` is the lookup half
of the emailed ``<token_id>.<secret>`` value -- a key, not a credential --
and ``secret_hash`` is the SHA-256 digest of the secret half
(``app.core.token_secrets``). The raw secret exists only inside the email.
Same shape as ``AuthSession``'s cookie credential, on purpose: an emailed
token is the sole proof of identity exactly the way the cookie is.

Single-use: ``POST /v1/auth/magic-link/confirm`` deletes the row atomically
(``DELETE ... RETURNING``) before logging anyone in, mirroring
``OAuthNonce``. No ``used_at``/soft-delete column, for the same reason as
there: "the row is gone" is the only failure signal a replay needs. A user
has at most one live row at a time -- issuing a new link deletes the user's
prior rows first, so an old unread email cannot stay redeemable after a
newer link was used.

No ``auth_session_id`` binding, deliberately (unlike ``OAuthNonce``): a magic
link is cross-device by design -- requested on a laptop, opened on a phone --
so the completing browser is NOT expected to be the one that asked.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class MagicLinkToken(Base):
    __tablename__ = "magic_link_tokens"

    token_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # SHA-256 hex digest (64 chars) of the emailed secret. Never the secret.
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # CASCADE, not RESTRICT: a pending sign-in link is a credential FOR a
    # user, not an independent record. Mirrors ``AuthSession.user_id`` and
    # ``UserIdentity.user_id``. Indexed: every request deletes by user_id.
    user_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["MagicLinkToken"]
