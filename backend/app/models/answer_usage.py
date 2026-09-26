"""One row per answered question that used plan allowance (ADR-0005 §2, Quota).

The allowance counts ANSWERS, not model calls (``provider_calls``): a cache hit
is a cited answer that makes no model call, and a refusal can make several. A row
is written in the same transaction as the answer, so a request that fails records
nothing. Refusals, no-answers, greetings and errors never get a row.

``paid_by`` is ``platform`` or ``user`` (BYOK, Phase 8); only ``platform`` rows
count against the allowance. ``channel`` is ``chat`` or, from Phase 6, ``mcp``;
both share one allowance. Plain strings, not native enums (AGENTS.md).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class AnswerUsage(Base):
    __tablename__ = "answer_usage"

    usage_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_by: Mapped[str] = mapped_column(String(16), nullable=False, default="platform")
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="chat")
    # The answer this use paid for. No foreign key: history can be closed or
    # removed, and the count of what was used must not change with it.
    message_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # The hot query: "this account's answers since <start>".
        Index("ix_answer_usage_user_occurred", "user_id", "occurred_at"),
    )
