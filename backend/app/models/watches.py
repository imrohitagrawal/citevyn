"""Pro watches (ADR-0005 §5, Phase 7C): "watch a specific flag, model, command
or page and get an alert when it changes".

``kind`` is a plain string (AGENTS.md): ``page`` (``value`` is a watched page's
URL from ``app/watch/pages.py``) or ``term`` (``value`` is text looked for,
case-insensitively, in a change's added and removed lines). One row per
(account, kind, value).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class Watch(Base):
    __tablename__ = "watches"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "value", name="uq_watches_user_kind_value"),
    )

    watch_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
