"""What users tell us about answers (ADR-0005 §6 trust must-haves).

Two tables, both written only by the caller about their OWN conversations:

* :class:`AnswerFeedback` — thumbs up/down on an answer. A thumbs-down may carry
  a ``reason`` ("report wrong answer") and a free-text ``comment``. One row per
  (answer, user): a second vote replaces the first.
* :class:`SourceRequest` — "request this source", usually from a refusal. This
  is the gap log: what users asked for that the corpus does not cover.

``rating``, ``reason`` and ``status`` are plain strings, not native database
enums (AGENTS.md: a value set that may grow must not need a migration). The API
layer validates them against a fixed set.

Free text (``comment``, ``note``, ``question``) is shown only to operators through
the admin routes and is never logged.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class AnswerFeedback(Base):
    __tablename__ = "answer_feedback"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_answer_feedback_message_user"),
    )

    feedback_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("messages.message_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[str] = mapped_column(String(8), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceRequest(Base):
    __tablename__ = "source_requests"

    request_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    # The answer (usually a refusal) the request came from, if any. SET NULL, not
    # CASCADE: the request is still a useful gap signal after the chat is gone.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("messages.message_id", ondelete="SET NULL"), nullable=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    requested_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["AnswerFeedback", "SourceRequest"]
