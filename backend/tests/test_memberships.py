"""Memberships from Stripe events, and deciding Pro (ADR-0005 §2, Phase 4).

Driven below the HTTP layer: ``apply_event`` takes an event whose signature has
ALREADY been checked, plus a ``fetch_subscription`` callable. Here that callable
is a fake Stripe (``LIVE``): the state each subscription is REALLY in.

The design these tests pin (rewritten after review, see the module docstring):
an event is only a SIGNAL. The stored state comes from the live subscription, so
delivery order cannot matter. Stripe's docs say not to use ``event.created`` to
order events; the first version did, and review showed a paying user dropping to
Free on a same-second reorder, and a stale grace leaving a later failure with 0
days.

Locked decisions pinned here: 7-day grace from the first time the subscription is
seen past due, cancel at the end of the period, idempotent per event id, and a
second subscription's events never override the account's paid one.
Each test says which change turns it red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.billing.memberships import apply_event, is_pro
from app.billing.stripe_client import StripeError
from app.models import Membership, StripeEvent, User
from app.models.enums import UserRole

ACCOUNT = "usr_" + "a" * 32
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T0 = int(NOW.timestamp())
LIVE: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def _clear_live() -> None:
    LIVE.clear()


def _live(
    sub_id: str = "sub_1",
    *,
    status: str = "active",
    account: str | None = ACCOUNT,
    cancel_at_period_end: bool = False,
    cancel_at: int | None = None,
    item_period_end: bool = False,
    customer: str = "cus_1",
) -> dict[str, Any]:
    """Set what Stripe says this subscription is, now."""
    sub: dict[str, Any] = {
        "id": sub_id,
        "object": "subscription",
        "customer": customer,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "cancel_at": cancel_at,
        "metadata": {"account_id": account} if account else {},
        "items": {"data": [{"price": {"recurring": {"interval": "month"}}}]},
    }
    if item_period_end:
        sub["items"]["data"][0]["current_period_end"] = T0 + 30 * 86400
    else:
        sub["current_period_end"] = T0 + 30 * 86400
    LIVE[sub_id] = sub
    return sub


async def _fetch(sub_id: str) -> dict[str, Any]:
    if sub_id not in LIVE:
        raise StripeError("Stripe returned 404")
    return LIVE[sub_id]


def _sub_event(
    event_id: str,
    sub_id: str = "sub_1",
    kind: str = "customer.subscription.updated",
    payload_status: str = "active",
) -> dict[str, Any]:
    """A subscription event. Its payload is deliberately NOT what gets stored."""
    return {
        "id": event_id,
        "type": kind,
        "created": T0,
        "data": {"object": {"id": sub_id, "object": "subscription", "status": payload_status}},
    }


def _invoice_event(event_id: str, kind: str, sub_id: str | None = "sub_1") -> dict[str, Any]:
    inv: dict[str, Any] = {"object": "invoice", "customer": "cus_1"}
    if sub_id:
        inv["subscription"] = sub_id
    return {"id": event_id, "type": kind, "created": T0, "data": {"object": inv}}


def _utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes on reload; every stored value is UTC."""
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


async def _user(session: Any, user_id: str = ACCOUNT) -> None:
    session.add(User(user_id=user_id, role=UserRole.demo_user, created_at=NOW))
    await session.flush()


async def _membership(session: Any) -> Membership | None:
    return (
        await session.execute(select(Membership).where(Membership.account_id == ACCOUNT))
    ).scalar_one_or_none()


async def _apply(session: Any, event: dict[str, Any], now: datetime = NOW) -> str:
    return await apply_event(session, event, fetch_subscription=_fetch, grace_days=7, now=now)


# ---------------------------------------------------------------------------
# is_pro
# ---------------------------------------------------------------------------


def _m(**kw: Any) -> Membership:
    base: dict[str, Any] = {"account_id": ACCOUNT, "status": "active", "grace_until": None}
    base.update(kw)
    return Membership(**base)


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"status": "active"}, True),
        ({"status": "trialing"}, True),
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
# The live state is what is stored
# ---------------------------------------------------------------------------


async def test_a_subscription_event_stores_the_live_state(session: Any) -> None:
    """Turns red if: the account, status, interval, period end or Stripe ids are
    not taken from the live subscription."""
    await _user(session)
    _live()
    event = _sub_event("evt_1", kind="customer.subscription.created")
    assert await _apply(session, event) == "applied"
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


async def test_the_payload_is_a_signal_not_the_truth(session: Any) -> None:
    """An event whose payload says one thing while Stripe says another: Stripe
    wins. Turns red if: the payload's status is stored."""
    await _user(session)
    _live(status="active")
    await _apply(session, _sub_event("evt_1", payload_status="incomplete"))
    m = await _membership(session)
    assert m is not None and m.status == "active"


async def test_delivery_order_cannot_drop_a_paying_user(session: Any) -> None:
    """The review's case A: 'updated(active)' then a late 'created(incomplete)'
    with the same timestamp. Both now read the live subscription, which is active.
    Turns red if: any event's payload decides the state."""
    await _user(session)
    _live(status="active")
    await _apply(session, _sub_event("evt_upd", payload_status="active"))
    late = _sub_event("evt_crt", kind="customer.subscription.created", payload_status="incomplete")
    await _apply(session, late)
    m = await _membership(session)
    assert m is not None and m.status == "active" and is_pro(m, NOW)


async def test_the_period_end_is_read_from_the_item_in_newer_api_versions(session: Any) -> None:
    """Turns red if: only the subscription-level field is read."""
    await _user(session)
    _live(item_period_end=True)
    await _apply(session, _sub_event("evt_1"))
    m = await _membership(session)
    assert m is not None and m.current_period_end is not None


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"cancel_at_period_end": True}, True),  # classic billing mode
        ({"cancel_at": T0 + 30 * 86400}, True),  # flexible mode, Checkout's default
        ({}, False),
    ],
)
async def test_a_pending_cancellation_is_seen_in_both_billing_modes(
    session: Any, kw: dict[str, Any], expected: bool
) -> None:
    """Stripe: flexible-mode subscriptions signal it with ``cancel_at``. Turns red
    if: only ``cancel_at_period_end`` is read."""
    await _user(session)
    _live(**kw)
    await _apply(session, _sub_event("evt_1"))
    m = await _membership(session)
    assert m is not None and m.cancel_at_period_end is expected and is_pro(m, NOW)


async def test_cancel_at_period_end_stays_pro_until_the_subscription_ends(session: Any) -> None:
    """Locked: cancel at period end. Turns red if: cancelling revokes Pro at once,
    or the final deletion does not."""
    await _user(session)
    _live(cancel_at_period_end=True)
    await _apply(session, _sub_event("evt_1"))
    m = await _membership(session)
    assert m is not None and is_pro(m, NOW)
    _live(status="canceled")
    await _apply(session, _sub_event("evt_2", kind="customer.subscription.deleted"))
    m = await _membership(session)
    assert m is not None and not is_pro(m, NOW)


# ---------------------------------------------------------------------------
# Grace
# ---------------------------------------------------------------------------


async def test_grace_starts_when_the_subscription_is_first_seen_past_due(session: Any) -> None:
    """Locked: 7 days, then the free tier; a retry does not restart the clock.
    Turns red if: the grace is not set, is the wrong length, restarts, or
    outlives 7 days."""
    await _user(session)
    _live(status="active")
    await _apply(session, _sub_event("evt_1"))
    _live(status="past_due")
    await _apply(session, _invoice_event("evt_2", "invoice.payment_failed"))
    m = await _membership(session)
    assert m is not None and _utc(m.grace_until) == NOW + timedelta(days=7)
    later = NOW + timedelta(days=2)
    await _apply(session, _invoice_event("evt_3", "invoice.payment_failed"), now=later)
    m = await _membership(session)
    assert m is not None and _utc(m.grace_until) == NOW + timedelta(days=7)
    assert is_pro(m, NOW + timedelta(days=6, hours=23))
    assert not is_pro(m, NOW + timedelta(days=7, seconds=1))


async def test_grace_starts_even_if_the_invoice_event_never_arrives(session: Any) -> None:
    """The review's case B: the subscription turns past_due and that is the
    only event we get. Turns red if: grace depends on the invoice event."""
    await _user(session)
    _live(status="past_due")
    await _apply(session, _sub_event("evt_1"))
    m = await _membership(session)
    assert m is not None and _utc(m.grace_until) == NOW + timedelta(days=7) and is_pro(m, NOW)


async def test_recovery_clears_grace_so_a_later_failure_gets_a_fresh_seven_days(
    session: Any,
) -> None:
    """The review's case C: without this, a stale grace from months ago meant a
    new failure got 0 days. Turns red if: grace survives a return to active."""
    await _user(session)
    _live(status="past_due")
    await _apply(session, _sub_event("evt_1"))
    _live(status="active")
    await _apply(session, _invoice_event("evt_2", "invoice.paid"))
    m = await _membership(session)
    assert m is not None and m.grace_until is None
    months_later = NOW + timedelta(days=60)
    _live(status="past_due")
    await _apply(session, _invoice_event("evt_3", "invoice.payment_failed"), now=months_later)
    m = await _membership(session)
    assert m is not None and _utc(m.grace_until) == months_later + timedelta(days=7)


# ---------------------------------------------------------------------------
# Two subscriptions on one account
# ---------------------------------------------------------------------------


async def test_a_dead_subscription_never_overrides_the_paid_one(session: Any) -> None:
    """The review's case D (rated a blocker): an account pays a second checkout
    (two tabs), then the FIRST one's deletion arrives. Turns red if: it replaces
    the paid subscription, leaving a paying customer on Free."""
    await _user(session)
    _live("sub_1", status="past_due")
    await _apply(session, _sub_event("evt_1", "sub_1"))
    _live("sub_2", status="active")
    await _apply(session, _sub_event("evt_2", "sub_2", kind="customer.subscription.created"))
    _live("sub_1", status="canceled")
    deleted = _sub_event("evt_3", "sub_1", kind="customer.subscription.deleted")
    assert await _apply(session, deleted) == "superseded"
    m = await _membership(session)
    assert m is not None and m.stripe_subscription_id == "sub_2" and is_pro(m, NOW)


async def test_a_newly_paid_subscription_replaces_a_dead_one(session: Any) -> None:
    """Partner: after a lapse, subscribing again must take over.
    Turns red if: the account stays tied to its old, dead subscription."""
    await _user(session)
    _live("sub_1", status="canceled")
    await _apply(session, _sub_event("evt_1", "sub_1"))
    _live("sub_2", status="active")
    await _apply(session, _sub_event("evt_2", "sub_2", kind="customer.subscription.created"))
    m = await _membership(session)
    assert m is not None and m.stripe_subscription_id == "sub_2" and is_pro(m, NOW)


# ---------------------------------------------------------------------------
# Idempotency, the audit log, and what is not ours
# ---------------------------------------------------------------------------


async def test_the_same_event_twice_changes_nothing(session: Any) -> None:
    """Idempotency. Turns red if: a redelivery is processed again."""
    await _user(session)
    _live(status="active")
    await _apply(session, _sub_event("evt_1"))
    _live(status="canceled")
    assert await _apply(session, _sub_event("evt_1")) == "duplicate"
    m = await _membership(session)
    assert m is not None and m.status == "active"


async def test_every_event_is_logged_once_with_its_outcome(session: Any) -> None:
    """The audit log of membership changes. Turns red if: an event is not
    recorded, or is recorded twice."""
    await _user(session)
    _live()
    await _apply(session, _sub_event("evt_1"))
    await _apply(session, _sub_event("evt_1"))
    other = {"id": "evt_x", "type": "customer.created", "created": T0, "data": {"object": {}}}
    await _apply(session, other)
    rows = (
        (await session.execute(select(StripeEvent).order_by(StripeEvent.event_id))).scalars().all()
    )
    assert [(r.event_id, r.outcome, r.account_id) for r in rows] == [
        ("evt_1", "applied", ACCOUNT),
        ("evt_x", "ignored", None),
    ]


async def test_a_subscription_for_an_unknown_account_is_logged_not_applied(session: Any) -> None:
    """The account comes from metadata WE set at checkout. A subscription naming
    an account we do not have, or none, grants nothing. Turns red if: it creates a
    membership, or crashes."""
    _live("sub_1", account="usr_" + "f" * 32)
    assert await _apply(session, _sub_event("evt_1", "sub_1")) == "unmatched"
    _live("sub_2", account=None)
    assert await _apply(session, _sub_event("evt_2", "sub_2")) == "unmatched"
    assert await _membership(session) is None


async def test_an_invoice_in_the_newer_api_shape_is_matched(session: Any) -> None:
    """Newer Stripe API versions put the subscription under
    ``parent.subscription_details``. Turns red if: only the old field is read."""
    await _user(session)
    _live()
    event = _invoice_event("evt_1", "invoice.paid", sub_id=None)
    event["data"]["object"]["parent"] = {"subscription_details": {"subscription": "sub_1"}}
    assert await _apply(session, event) == "applied"


async def test_an_invoice_without_a_subscription_is_unmatched(session: Any) -> None:
    """A one-off invoice is not a membership matter. Turns red if: it crashes or
    matches something."""
    event = _invoice_event("evt_1", "invoice.paid", sub_id=None)
    assert await _apply(session, event) == "unmatched"


async def test_an_event_without_an_id_is_ignored_and_not_logged(session: Any) -> None:
    """Turns red if: an id-less event is applied or logged under an empty key."""
    assert await _apply(session, {"type": "customer.subscription.updated", "data": {}}) == "ignored"
    assert (await session.execute(select(StripeEvent))).scalars().all() == []


async def test_a_malformed_created_field_does_not_crash(session: Any) -> None:
    """Only Stripe can sign, but a bad field must not become an endless 500.
    Turns red if: ``created`` is parsed unguarded."""
    event = {"id": "evt_1", "type": "customer.created", "created": "not-a-number", "data": {}}
    assert await _apply(session, event) == "ignored"


async def test_stripe_unreachable_records_nothing_so_the_retry_can_apply(session: Any) -> None:
    """If the live fetch fails, the event must NOT be logged as seen, or Stripe's
    retry would be ignored as a duplicate. Turns red if: the event is logged
    before the fetch succeeds."""
    await _user(session)
    with pytest.raises(StripeError):
        await _apply(session, _sub_event("evt_1", "sub_missing"))
    assert (await session.execute(select(StripeEvent))).scalars().all() == []


async def test_a_past_due_second_subscription_takes_over_from_a_lapsed_one(session: Any) -> None:
    """Only a PAID current subscription is protected. If the current one has
    lapsed, a different subscription going past due must be tracked, so its 7-day
    grace applies. Turns red if: every different, unpaid subscription is
    'superseded' regardless of whether the current one is still paid."""
    await _user(session)
    _live("sub_1", status="canceled")
    await _apply(session, _sub_event("evt_1", "sub_1"))
    _live("sub_2", status="past_due")
    assert await _apply(session, _sub_event("evt_2", "sub_2")) == "applied"
    m = await _membership(session)
    assert m is not None and m.stripe_subscription_id == "sub_2" and is_pro(m, NOW)
