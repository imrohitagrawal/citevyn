"""Watch alert sends (ADR-0005 §5, Phase 7C-2).

One row per (account, change) alerted, unique on both, so a change is never
alerted to the same account twice. One email can cover several changes: its
rows share ``email_id``, and ``token_hash`` (the SHA-256 of that email's
one-click unsubscribe token; the raw token is only in the email).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class AlertSend(Base):
    __tablename__ = "alert_sends"
    __table_args__ = (UniqueConstraint("user_id", "change_id", name="uq_alert_sends_user_change"),)

    alert_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    email_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    change_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("doc_changes.change_id", ondelete="CASCADE"), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
