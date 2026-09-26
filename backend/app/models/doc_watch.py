"""Docs-watcher storage (ADR-0005 §5, Phase 7A).

``doc_snapshots``: the LATEST text of each watched page (one row per URL), so
the next run has something to compare with. ``doc_changes``: one row per real
change, with a small section summary; this is what digests and watch alerts
(Phase 7B/7C) read. Separate from the answer corpus: nothing here is embedded
or answers questions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class DocSnapshot(Base):
    __tablename__ = "doc_snapshots"

    url: Mapped[str] = mapped_column(String(512), primary_key=True)
    product_area: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Why the last check failed (never page text); NULL after a good fetch.
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)


class DocChange(Base):
    __tablename__ = "doc_changes"

    change_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    product_area: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    old_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    new_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # The added and removed lines (bounded), so a term watch (Phase 7C) can
    # tell whether "--permission-mode" changed. NULL on rows from before 0024.
    lines: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
