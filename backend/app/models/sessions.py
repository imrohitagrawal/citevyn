"""Session table.

Holds bounded conversation sessions. ``current_product_area`` and
``summary`` are denormalized cache fields updated as messages flow
through, used to keep follow-up context from drifting across product
areas.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import GUID, Base

if TYPE_CHECKING:
    from app.models.messages import Message


class Session(Base):
    __tablename__ = "sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # CASCADE, not RESTRICT (migration 0008, ADR-0004 PR 5): deleting a user
    # deletes their sessions rather than raising IntegrityError forever.
    # audit_events.user_id stays SET NULL by deliberate asymmetry — see the
    # migration docstring.
    user_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_product_area: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    messages: Mapped[list[Message]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="raise",
    )
