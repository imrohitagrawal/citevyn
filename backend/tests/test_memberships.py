"""Applying Stripe events to memberships, and deciding Pro (ADR-0005 §2, Phase 4).

Driven below the HTTP layer: ``apply_event`` takes an event that has ALREADY been
signature-checked. The webhook route's own tests cover the signature, the setting
and the HTTP shape.

Locked decisions pinned here: 7-day grace on a failed payment, cancel at the end
of the period, and a redelivered or late event never overwrites newer state.
Each test says which change turns it red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.billing.memberships import apply_event, is_pro
from app.models import Membership, StripeEvent, User
from app.models.enums import UserRole

ACCOUNT = "usr_" + "a" * 32
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T0 = int(NOW.timestamp())


def _utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes on reload; every stored value is UTC."""
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


async def _user(session: Any, user_id: str = ACCOUNT) -> None:
    session.add(User(user_id=user_id, role=UserRole.demo_user, created_at=NOW))
    await session.flush()


def _sub_event(
    event_id: str,
    *,
    kind: str = "customer.subscription.updated",
    status: str = "active",
    created: int = T0,
    cancel_at_period_end: bool = False,
    account: str | None = ACCOUNT,
    period_end: int = T0 + 30 * 86400,
    item_period_end: bool = False,
) -> dict[str, Any]:
    sub: dict[str, Any] = {
        "id": "sub_1",
        "object": "subscription",
        "customer": "cus_1",
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "metadata": {"account_id": account} if account else {},
        "items": {"data": [{"price": {"recurring": {"interval": "month"}}}]},
    }
    # Newer Stripe API versions carry the period end on the item, not the subscription.
    if item_period_end:
        sub["items"]["data"][0]["current_period_end"] = period_end
    else:
        sub["current_period_end"] = period_end
    return {"id": event_id, "type": kind, "created": created, "data": {"object": sub}}


def _invoice_event(event_id: str, kind: str, created: int = T0) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": kind,
        "created": created,
        "data": {"object": {"object": "invoice", "customer": "cus_1", "subscription": "sub_1"}},
    }


async def _membership(session: Any) -> Membership | None:
    return (
        await session.execute(select(Membership).where(Membership.account_id == ACCOUNT))
    ).scalar_one_or_none()


async def _apply(session: Any, event: dict[str, Any], now: datetime = NOW) -> str:
    return await apply_event(session, event, grace_days=7, now=now)


# ---------------------------------------------------------------------------
# is_pro
# ---------------------------------------------------------------------------


def _m(**kw: Any) -> Membership:
    base: dict[str, Any] = {
        "account_id": ACCOUNT,
        "status": "active",
        "cancel_at_period_end": False,
        "grace_until": None,
    }
    base.update(kw)
    return Membership(**base)


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"status": "active"}, True),
        ({"status": "trialing"}, True),
        ({"status": "active", "cancel_at_period_end": True}, True),  # paid to period end
        ({"status": "past_due", "grace_until": NOW + timedelta(days=1)}, True),
        ({"status": "past_due", "grace_until": NOW - timedelta(seconds=1)}, False),
        ({"status": "past_due", "grace_until": None}, False),
        ({"status": "unpaid"}, False),
        ({"status": "canceled"}, False),
        ({"status": "incomplete"}, False),
        ({"status": "incomplete_expired"}, False),
        ({"status": "paused"}, False),
        ({"status": "some_future_status"}, False),  # unknown means NOT paid
    ],
)
def test_is_pro(kw: dict[str, Any], expected: bool) -> None:
    """Turns red if: any status is mapped wrongly, the grace window is ignored or
    unbounded, or an unknown status grants Pro."""
    assert is_pro(_m(**kw), NOW) is expected


def test_no_membership_is_not_pro() -> None:
    assert is_pro(None, NOW) is False


# ---------------------------------------------------------------------------
# apply_event
# ---------------------------------------------------------------------------


async def test_a_subscription_event_creates_the_membership(session: Any) -> None:
    """Turns red if: the account, status, interval, period end or Stripe ids are
    not recorded."""
    await _user(session)
    assert (
        await _apply(session, _sub_event("evt_1", kind="customer.subscription.created"))
        == "applied"
    )
    m = await _membership(session)
    assert m is not None
    assert (m.status, m.billing_interval, m.stripe_subscription_id, m.stripe_customer_id) == (
        "active",
        "month",
        "sub_1",
        "cus_1",
    )
    assert _utc(m.current_period_end) == datetime.fromtimestamp(T0 + 30 * 86400, UTC)
    assert is_pro(m, NOW)


async def test_the_period_end_is_read_from_the_item_in_newer_api_versions(session: Any) -> None:
    """Turns red if: only the subscription-level field is read."""
    await _user(session)
    await _apply(session, _sub_event("evt_1", item_period_end=True))
    m = await _membership(session)
    assert m is not None and m.current_period_end is not None


async def test_the_same_event_twice_changes_nothing(session: Any) -> None:
    """Idempotency. Turns red if: a redelivery is processed again."""
    await _user(session)
    await _apply(session, _sub_event("evt_1"))
    assert await _apply(session, _sub_event("evt_1", status="canceled")) == "duplicate"
    m = await _membership(session)
    assert m is not None and m.status == "active"


async def test_a_late_older_event_does_not_overwrite_newer_state(session: Any) -> None:
    """Stripe does not guarantee order. Turns red if: an event created BEFORE the
    one already applied overwrites it."""
    await _user(session)
    await _apply(session, _sub_event("evt_new", status="canceled", created=T0 + 60))
    assert await _apply(session, _sub_event("evt_old", status="active", created=T0)) == "stale"
    m = await _membership(session)
    assert m is not None and m.status == "canceled"


async def test_cancel_at_period_end_stays_pro_until_the_subscription_ends(session: Any) -> None:
    """Locked: cancel at period end. Turns red if: cancelling revokes Pro at once,
    or the final deletion does not."""
    await _user(session)
    await _apply(session, _sub_event("evt_1", cancel_at_period_end=True))
    m = await _membership(session)
    assert m is not None and m.cancel_at_period_end and is_pro(m, NOW)
    await _apply(
        session,
        _sub_event(
            "evt_2", kind="customer.subscription.deleted", status="canceled", created=T0 + 1
        ),
    )
    m = await _membership(session)
    assert m is not None and not is_pro(m, NOW)


async def test_a_failed_payment_starts_a_seven_day_grace(session: Any) -> None:
    """Locked: 7-day grace, then back to the free tier. Turns red if: the grace is
    not set, is the wrong length, restarts on every retry, or outlives 7 days."""
    await _user(session)
    await _apply(session, _sub_event("evt_1"))
    await _apply(session, _sub_event("evt_2", status="past_due", created=T0 + 1))
    await _apply(session, _invoice_event("evt_3", "invoice.payment_failed", T0 + 2))
    m = await _membership(session)
    assert m is not None and _utc(m.grace_until) == NOW + timedelta(days=7)
    # A retry failing two days later does NOT restart the clock.
    later = NOW + timedelta(days=2)
    await _apply(session, _invoice_event("evt_4", "invoice.payment_failed", T0 + 3), now=later)
    m = await _membership(session)
    assert m is not None and _utc(m.grace_until) == NOW + timedelta(days=7)
    assert is_pro(m, NOW + timedelta(days=6, hours=23))
    assert not is_pro(m, NOW + timedelta(days=7, seconds=1))


async def test_a_successful_payment_ends_the_grace(session: Any) -> None:
    """Turns red if: grace outlives the payment that fixed it (a LATER failure
    would then get less than 7 days)."""
    await _user(session)
    await _apply(session, _sub_event("evt_1"))
    await _apply(session, _invoice_event("evt_2", "invoice.payment_failed", T0 + 1))
    await _apply(session, _invoice_event("evt_3", "invoice.paid", T0 + 2))
    m = await _membership(session)
    assert m is not None and m.grace_until is None


async def test_every_event_is_logged_once_with_its_outcome(session: Any) -> None:
    """The audit log of membership changes. Turns red if: an event is not
    recorded, or is recorded twice."""
    await _user(session)
    await _apply(session, _sub_event("evt_1"))
    await _apply(session, _sub_event("evt_1"))
    await _apply(
        session, {"id": "evt_x", "type": "customer.created", "created": T0, "data": {"object": {}}}
    )
    rows = (
        (await session.execute(select(StripeEvent).order_by(StripeEvent.event_id))).scalars().all()
    )
    assert [(r.event_id, r.outcome, r.account_id) for r in rows] == [
        ("evt_1", "applied", ACCOUNT),
        ("evt_x", "ignored", None),
    ]


async def test_an_event_for_an_unknown_account_is_logged_not_applied(session: Any) -> None:
    """A subscription whose metadata names no account we know must not create a
    membership for a stranger. Turns red if: it creates a row, or crashes."""
    assert await _apply(session, _sub_event("evt_1", account="usr_" + "f" * 32)) == "unmatched"
    assert await _apply(session, _sub_event("evt_2", account=None)) == "unmatched"
    assert await _membership(session) is None


async def test_an_invoice_for_an_unknown_subscription_is_unmatched(session: Any) -> None:
    """Turns red if: an invoice event for a subscription we do not hold crashes."""
    assert await _apply(session, _invoice_event("evt_1", "invoice.payment_failed")) == "unmatched"


async def test_an_invoice_in_the_newer_api_shape_is_matched_by_subscription(session: Any) -> None:
    """Newer Stripe API versions move the subscription id under
    ``parent.subscription_details``. Turns red if: only the old field is read."""
    await _user(session)
    await _apply(session, _sub_event("evt_1"))
    event = _invoice_event("evt_2", "invoice.payment_failed", T0 + 1)
    inv = event["data"]["object"]
    del inv["subscription"]
    inv["parent"] = {"subscription_details": {"subscription": "sub_1"}}
    assert await _apply(session, event) == "applied"


async def test_an_invoice_with_only_a_customer_is_matched_by_customer(session: Any) -> None:
    """Turns red if: the customer-id fallback is dropped."""
    await _user(session)
    await _apply(session, _sub_event("evt_1"))
    event = _invoice_event("evt_2", "invoice.payment_failed", T0 + 1)
    del event["data"]["object"]["subscription"]
    assert await _apply(session, event) == "applied"


async def test_an_invoice_naming_nothing_is_unmatched(session: Any) -> None:
    """Turns red if: an invoice with neither id crashes or matches something."""
    event = _invoice_event("evt_1", "invoice.payment_failed")
    event["data"]["object"] = {"object": "invoice"}
    assert await _apply(session, event) == "unmatched"


async def test_a_late_older_invoice_does_not_restart_grace(session: Any) -> None:
    """A failed-payment event delivered AFTER the newer 'paid' one must not put the
    account back into grace. Turns red if: invoices skip the order check."""
    await _user(session)
    await _apply(session, _sub_event("evt_1"))
    await _apply(session, _invoice_event("evt_paid", "invoice.paid", T0 + 10))
    assert (
        await _apply(session, _invoice_event("evt_old", "invoice.payment_failed", T0 + 5))
        == "stale"
    )
    m = await _membership(session)
    assert m is not None and m.grace_until is None


async def test_an_event_without_an_id_is_ignored_and_not_logged(session: Any) -> None:
    """Turns red if: an id-less event is applied or logged under an empty key."""
    assert await _apply(session, {"type": "customer.subscription.updated", "data": {}}) == "ignored"
    assert (await session.execute(select(StripeEvent))).scalars().all() == []


async def test_a_deletion_revokes_pro_whatever_status_the_object_carries(session: Any) -> None:
    """``customer.subscription.deleted`` means the subscription is over, even if
    its object still says 'active' (an immediate deletion, or a stale copy).
    Turns red if: the status is copied from the object on a deletion."""
    await _user(session)
    await _apply(session, _sub_event("evt_1"))
    await _apply(
        session,
        _sub_event("evt_2", kind="customer.subscription.deleted", status="active", created=T0 + 1),
    )
    m = await _membership(session)
    assert m is not None and m.status == "canceled" and not is_pro(m, NOW)
