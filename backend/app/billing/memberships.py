"""Memberships from verified Stripe events, and the one "is this account Pro?" rule.

ADR-0005 §2: entitlement lives in OUR table, written only here, from webhook
events whose signature has already been checked. Every protected request reads
the table (``app.access.deps.resolve_tier``); no user request ever asks Stripe.

Locked decisions (owner, 2026-09-25) implemented here:

* **Cancel at period end.** Stripe keeps the subscription ``active`` until the
  period ends, then sends ``customer.subscription.deleted``. So a cancelled
  account stays Pro for what it paid for, and no clock of ours decides when that
  is. The pending cancellation is shown from ``cancel_at_period_end`` (classic
  billing mode) or ``cancel_at`` (flexible mode, Checkout's default).
* **7-day grace** on a failed payment, then back to the free tier. The grace
  starts the first time the subscription is SEEN ``past_due`` (from any event), is
  not restarted while it stays past due, and is cleared whenever it is not, so a
  later failure always gets a fresh 7 days.

How events are applied (rewritten after review):

* **The event is a signal; the live subscription is the truth.** Stripe's docs:
  "Don't use ``created`` to determine event order ... use the API to retrieve any
  missing objects." The first version ordered events by ``created``, and review
  reproduced a paying user dropping to Free on a same-second reorder and a stale
  grace leaving a later failure with 0 days. Now every subscription or invoice
  event re-reads the subscription from Stripe (on the webhook path only) and
  stores what it says, so delivery order cannot matter.
* **Idempotent.** Each event id is applied once (``stripe_events``, which is also
  the audit log). A failed live fetch raises and logs nothing, so Stripe's retry
  is applied rather than dismissed as a duplicate.
* **One paid subscription per account wins.** An account can end up with two
  subscriptions (two checkout tabs). An event for a subscription that is not the
  account's current one never replaces a paid current one (``superseded``); a
  newly paid one does replace a lapsed one.
* **Only our own accounts.** The account id comes from subscription metadata WE
  set at checkout; a subscription naming no account we have grants nothing
  (``unmatched``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
_INVOICE_EVENTS = frozenset({"invoice.payment_failed", "invoice.paid", "invoice.payment_succeeded"})

FetchSubscription = Callable[[str], Awaitable[dict[str, Any]]]


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


def _subscription_id(kind: str, obj: dict[str, Any]) -> str | None:
    if kind in _SUBSCRIPTION_EVENTS:
        own: object = obj.get("id")
        return own if isinstance(own, str) else None
    # Invoices. Older API versions: invoice.subscription; newer (2025-03-31.basil):
    # invoice.parent.subscription_details.subscription.
    sub: object = obj.get("subscription")
    if isinstance(sub, str):
        return sub
    details = _as_dict(_as_dict(obj.get("parent")).get("subscription_details"))
    value: object = details.get("subscription")
    return value if isinstance(value, str) else None


def _store(m: Membership, sub: dict[str, Any], now: datetime, grace_days: int) -> None:
    m.status = str(sub.get("status") or "incomplete")
    m.billing_interval = _interval(sub)
    sub_id: object = sub.get("id")
    customer: object = sub.get("customer")
    m.stripe_subscription_id = sub_id if isinstance(sub_id, str) else None
    m.stripe_customer_id = customer if isinstance(customer, str) else None
    m.current_period_end = _period_end(sub)
    m.cancel_at_period_end = (
        bool(sub.get("cancel_at_period_end")) or sub.get("cancel_at") is not None
    )
    if m.status == "past_due":
        if m.grace_until is None:  # a retry does not restart the clock
            m.grace_until = now + timedelta(days=grace_days)
    else:
        m.grace_until = None  # recovered or ended: a later failure gets a fresh 7 days
    m.updated_at = now


async def _sync(
    db: AsyncSession,
    sub_id: str,
    fetch_subscription: FetchSubscription,
    now: datetime,
    grace_days: int,
) -> tuple[str, str | None]:
    sub = await fetch_subscription(sub_id)  # StripeError propagates: nothing logged
    account: object = _as_dict(sub.get("metadata")).get("account_id")
    if not isinstance(account, str):
        return "unmatched", None
    if await db.get(User, account) is None:
        return "unmatched", account
    m = await membership_for(db, account)
    if m is None:
        m = Membership(account_id=account, plan="pro", created_at=now, grace_until=None)
        db.add(m)
    elif (
        m.stripe_subscription_id not in (None, sub.get("id"))
        and is_pro(m, now)
        and sub.get("status") not in _PAID
    ):
        # A second subscription that is not paid must not replace the paid one.
        return "superseded", account
    _store(m, sub, now, grace_days)
    return "applied", account


async def apply_event(
    db: AsyncSession,
    event: dict[str, Any],
    *,
    fetch_subscription: FetchSubscription,
    grace_days: int,
    now: datetime,
) -> str:
    """Apply one signature-verified event; return its outcome.

    ``duplicate`` (already seen, nothing done), ``applied``, ``superseded`` (a
    second subscription's event that must not replace the paid one), ``unmatched``
    (names no subscription, or an account we do not have) or ``ignored`` (a type
    we do not act on). A failed live fetch raises and logs NOTHING, so Stripe's
    retry is not mistaken for a duplicate. Flushes; the caller commits.
    """
    event_id = str(event.get("id") or "")
    if not event_id:
        return "ignored"
    if await db.get(StripeEvent, event_id) is not None:
        return "duplicate"
    kind = str(event.get("type") or "")
    raw_created: object = event.get("created")
    created = int(raw_created) if isinstance(raw_created, int | float) else 0
    account: str | None = None
    if kind in _SUBSCRIPTION_EVENTS or kind in _INVOICE_EVENTS:
        sub_id = _subscription_id(kind, _obj(event))
        if sub_id is None:
            outcome = "unmatched"
        else:
            outcome, account = await _sync(db, sub_id, fetch_subscription, now, grace_days)
    else:
        outcome = "ignored"
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


__all__ = ["FetchSubscription", "apply_event", "is_pro", "membership_for"]
