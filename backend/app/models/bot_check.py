"""Proof-of-work solutions already used (ADR-0005 Phase 5, the sign-up bot check).

Each solved challenge pays for ONE attempt to sign up or request a magic link,
whether or not the attempt then succeeds: the row is committed on its own before
the request runs. (Leaving it unused after a failed attempt let one solve probe
"is this email registered?" without limit; found in review.) ``expires_at`` lets
old rows be deleted a minute after expiry; an expired challenge is refused by its
own signed expiry.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BotCheckUse(Base):
    __tablename__ = "bot_check_uses"

    # The challenge hash (SHA-256 hex) the solution answered.
    challenge: Mapped[str] = mapped_column(String(64), primary_key=True)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_bot_check_uses_expires_at", "expires_at"),)
