"""Per-user API keys (ADR-0005 §4, Phase 6A).

"Shown once, stored only as a hash, scoped, revocable." The key the user holds
is ``cvk_<key_id>_<secret>``: ``key_id`` finds the row, and only the SHA-256 of
``secret`` is stored (``app.core.token_secrets``), so a leaked row cannot be
replayed. ``scope`` is a plain string (AGENTS.md); today only ``mcp`` exists.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="mcp")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
