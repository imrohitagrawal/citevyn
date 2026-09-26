"""Weekly digest sends (ADR-0005 §5, Phase 7B).

One row per (account, week) the digest was sent, so a re-run of the sender in
the same week never mails anyone twice (the unique constraint is the guard).
``token_hash`` is the SHA-256 of that email's one-click unsubscribe token
(``app.core.token_secrets``); the raw token is only in the email.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class DigestSend(Base):
    __tablename__ = "digest_sends"
    __table_args__ = (UniqueConstraint("user_id", "week_start", name="uq_digest_sends_user_week"),)

    send_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
