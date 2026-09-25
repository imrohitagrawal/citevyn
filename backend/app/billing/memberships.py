"""Memberships from verified Stripe events, and the one "is this account Pro?" rule.

ADR-0005 §2: entitlement lives in OUR table, written only here, from webhook
events whose signature has already been checked. Every protected request reads
the table (``app.access.deps.resolve_tier``); nothing ever asks Stripe mid-request.

Locked decisions (owner, 2026-09-25) implemented here:

* **Cancel at period end.** Stripe keeps the subscription ``active`` with
  ``cancel_at_period_end`` until the period ends, then sends
  ``customer.subscription.deleted``. So a cancelled account stays Pro for what it
  paid for, and no clock of ours has to decide when that is.
* **7-day grace** on a failed payment, then back to the free tier. The first
  ``invoice.payment_failed`` starts the grace; retries do not restart it; a paid
  invoice ends it.
* **Idempotent and order-safe.** Each event id is applied once (``stripe_events``
  is the record, and the audit log). Each membership keeps the newest event time it
  has applied, and an older event arriving late is logged as ``stale``, not
  applied: Stripe does not guarantee delivery order.

An event naming an account we do not have creates nothing (``unmatched``): the
account id comes from subscription metadata WE set at checkout, so a subscription
without it is not ours to grant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, StripeEvent, User

# Statuses that mean "paid up". Anything else, including a status Stripe adds in
# future, is NOT Pro: an unknown state must fail closed.
_PAID = frozenset({"active", "trialing"})

_SUBSCRIPTION_EVENTS = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)
_FAILED_PAYMENT = "invoice.payment_failed"
_PAID_INVOICE = frozenset({"invoice.paid", "invoice.payment_succeeded"})


def is_pro(membership: Membership | None, now: datetime) -> bool:
    """The one rule for "this account has Pro" (read by ``resolve_tier``)."""
    if membership is None:
        return False
    if membership.status in _PAID:
        return True
    grace = membership.grace_until
    if membership.status == "past_due" and grace is not None:
        return _aware(grace) > now
    return False


def _aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes even for timezone=True columns; every value
    # this module writes is UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_dict(value: object) -> dict[str, Any]:
    """``value`` if it is a JSON object, else an empty one: event payloads are
    untrusted shapes, and a missing or odd field must not raise."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _obj(event: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(_as_dict(event.get("data")).get("object"))


def _first_item(sub: dict[str, Any]) -> dict[str, Any]:
    items: object = _as_dict(sub.get("items")).get("data")
    return _as_dict(cast(list[object], items)[0]) if isinstance(items, list) and items else {}


def _period_end(sub: dict[str, Any]) -> datetime | None:
    # Older API versions put it on the subscription; newer ones on each item.
    raw: object = sub.get("current_period_end")
    if raw is None:
        raw = _first_item(sub).get("current_period_end")
    return datetime.fromtimestamp(int(raw), UTC) if isinstance(raw, int | float) else None


def _interval(sub: dict[str, Any]) -> str | None:
    recurring = _as_dict(_as_dict(_first_item(sub).get("price")).get("recurring"))
    value: object = recurring.get("interval")
    return value if isinstance(value, str) else None


def _invoice_subscription(inv: dict[str, Any]) -> str | None:
    # Older API versions: invoice.subscription; newer: invoice.parent.subscription_details.
    sub: object = inv.get("subscription")
    if isinstance(sub, str):
        return sub
    details = _as_dict(_as_dict(inv.get("parent")).get("subscription_details"))
    value: object = details.get("subscription")
    return value if isinstance(value, str) else None


async def _apply_subscription(
    db: AsyncSession, event: dict[str, Any], created: int, now: datetime
) -> tuple[str, str | None]:
    sub = _obj(event)
    account: object = _as_dict(sub.get("metadata")).get("account_id")
    if not isinstance(account, str):
        return "unmatched", None
    if await db.get(User, account) is None:
        return "unmatched", account
    m = (
        await db.execute(select(Membership).where(Membership.account_id == account))
    ).scalar_one_or_none()
    if m is not None and created < m.last_event_created:
        return "stale", account
    if m is None:
        m = Membership(account_id=account, plan="pro", created_at=now, grace_until=None)
        db.add(m)
    deleted = event.get("type") == "customer.subscription.deleted"
    m.status = "canceled" if deleted else str(sub.get("status") or "incomplete")
    m.billing_interval = _interval(sub)
    m.stripe_subscription_id = sub.get("id") if isinstance(sub.get("id"), str) else None
    m.stripe_customer_id = sub.get("customer") if isinstance(sub.get("customer"), str) else None
    m.current_period_end = _period_end(sub)
    m.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    m.last_event_created = created
    m.updated_at = now
    return "applied", account


async def _apply_invoice(
    db: AsyncSession, event: dict[str, Any], created: int, now: datetime, grace_days: int
) -> tuple[str, str | None]:
    inv = _obj(event)
    sub_id = _invoice_subscription(inv)
    stmt = select(Membership)
    if sub_id is not None:
        stmt = stmt.where(Membership.stripe_subscription_id == sub_id)
    elif isinstance(inv.get("customer"), str):
        stmt = stmt.where(Membership.stripe_customer_id == inv["customer"])
    else:
        return "unmatched", None
    m = (await db.execute(stmt)).scalars().first()
    if m is None:
        return "unmatched", None
    if created < m.last_event_created:
        return "stale", m.account_id
    if event.get("type") == _FAILED_PAYMENT:
        if m.grace_until is None:  # a retry does not restart the clock
            m.grace_until = now + timedelta(days=grace_days)
    else:
        m.grace_until = None
    m.last_event_created = created
    m.updated_at = now
    return "applied", m.account_id


async def apply_event(
    db: AsyncSession, event: dict[str, Any], *, grace_days: int, now: datetime
) -> str:
    """Apply one signature-verified event; return its outcome.

    ``duplicate`` (already seen, nothing done), ``applied``, ``stale`` (older than
    what the membership already reflects), ``unmatched`` (names no account or
    subscription we hold) or ``ignored`` (a type we do not act on). Flushes but
    does not commit: the caller owns the transaction.
    """
    event_id = str(event.get("id") or "")
    if not event_id:
        return "ignored"
    if await db.get(StripeEvent, event_id) is not None:
        return "duplicate"
    kind = str(event.get("type") or "")
    created = int(event.get("created") or 0)
    if kind in _SUBSCRIPTION_EVENTS:
        outcome, account = await _apply_subscription(db, event, created, now)
    elif kind == _FAILED_PAYMENT or kind in _PAID_INVOICE:
        outcome, account = await _apply_invoice(db, event, created, now, grace_days)
    else:
        outcome, account = "ignored", None
    db.add(
        StripeEvent(
            event_id=event_id,
            event_type=kind[:128],
            event_created=created,
            account_id=account,
            outcome=outcome,
            received_at=now,
        )
    )
    await db.flush()
    return outcome


async def membership_for(db: AsyncSession, account_id: str) -> Membership | None:
    return (
        await db.execute(select(Membership).where(Membership.account_id == account_id))
    ).scalar_one_or_none()


__all__ = ["apply_event", "is_pro", "membership_for"]
