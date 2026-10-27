"""Deleted accounts: what is kept after an account is deleted on request (#511).

The privacy page promises to delete an account's data on request. The free
trial is once per address (ADR-0005 §1, ``docs/ACCESS_POLICY.md``), so deleting
the account must not hand the same address a fresh trial. One row per address
keeps exactly what that needs and nothing more:

* ``email_hash``: the SHA-256 hex of the normalised address (:func:`email_hash`),
  never the address itself.
* ``trial_answers_used``: the trial answers the deleted account(s) had used.
  A second deletion of the same address ADDS to it.
* ``deleted_at``: when the latest deletion happened.

Written by ``app.services.account_deletion``; read by
``app.access.quota.usage_for`` for the Free trial only.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def email_hash(email: str) -> str:
    """SHA-256 hex of ``email`` normalised the way sign-up stores it
    (``strip().lower()``, ``app.api.routes.auth.normalize_email``), so any case
    or surrounding spaces give the same hash."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


class DeletedAccount(Base):
    __tablename__ = "deleted_accounts"

    email_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    trial_answers_used: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["DeletedAccount", "email_hash"]
