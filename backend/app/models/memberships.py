"""Memberships and the Stripe event log (ADR-0005 §2, entitlement).

* :class:`Membership` — what an ACCOUNT has paid for. Written only by verified
  Stripe webhooks (``app.billing.memberships.apply_event``); read on every
  protected request to decide the tier. A request never asks Stripe.
  ``account_id`` is a user id today; ADR-0005 designs it to become an
  organisation later without changing this table's meaning.
* :class:`StripeEvent` — every webhook event id, recorded once. It makes the
  webhook idempotent (a redelivery changes nothing) and is the audit log of
  every membership change: what arrived, when, for whom, and what it did.

``status``, ``plan``, ``billing_interval`` and ``outcome`` are plain strings (AGENTS.md):
Stripe adds statuses over time and a new one must not need a migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class Membership(Base):
    __tablename__ = "memberships"

    membership_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, unique=True
    )
    plan: Mapped[str] = mapped_column(String(16), nullable=False, default="pro")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    billing_interval: Mapped[str | None] = mapped_column(String(8), nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set on the first failed payment; the account stays Pro until it passes.
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stripe's ``created`` (unix seconds) of the newest subscription event applied.
    # An older event arriving late is ignored rather than overwriting newer state.
    last_event_created: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StripeEvent(Base):
    __tablename__ = "stripe_events"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_created: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # No foreign key: an event can name an account we do not (yet) know.
    account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["Membership", "StripeEvent"]
