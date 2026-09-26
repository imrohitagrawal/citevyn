"""Shareable answers (ADR-0005 §4, Phase 6C).

"A frozen copy of the answer, its citations and the crawl date; revocable."
The question, answer and citations are COPIED into the row when the answer is
shared, so the public page never changes afterwards, even if the conversation is
deleted. ``share_id`` is the unguessable part of the public link
(``/s/<share_id>``, 128 random bits). It is stored as it is, not hashed: the
page it opens is public by design, and the owner must be able to copy the link
again. Revoking sets ``revoked_at``; the link then answers 404.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class SharedAnswer(Base):
    __tablename__ = "shared_answers"

    share_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The answer it was copied from. SET NULL: the copy outlives the conversation.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("messages.message_id", ondelete="SET NULL"), nullable=True, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    docs_as_of: Mapped[str | None] = mapped_column(String(10), nullable=True)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
