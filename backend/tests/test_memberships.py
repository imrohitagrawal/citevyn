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

from app.billing.memberships import account_is_pro, apply_event, is_pro
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


async def _membership(session: Any, sub_id: str = "sub_1") -> Membership | None:
    """The row for ONE subscription (one row per Stripe subscription)."""
    return (
        await session.execute(select(Membership).where(Membership.stripe_subscription_id == sub_id))
    ).scalar_one_or_none()


async def _pro(session: Any, now: datetime = NOW) -> bool:
    """The account-level answer resolve_tier gives: Pro if ANY subscription is."""
    rows = (
        (await session.execute(select(Membership).where(Membership.account_id == ACCOUNT)))
        .scalars()
        .all()
    )
    return account_is_pro(list(rows), now)


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
# Two subscriptions on one account (one row per subscription)
# ---------------------------------------------------------------------------
#
# Review found the one-row-per-account design unsafe twice (cases D and E):
# whichever subscription had the latest event owned the row, so cancelling the
# OTHER one could leave a paying customer on Free. One row per subscription, and
# "Pro if any row is paid", removes the question entirely.


async def test_d_a_dead_first_subscription_does_not_hide_a_paid_second(session: Any) -> None:
    """Case D. Turns red if: the first subscription's deletion affects the second."""
    await _user(session)
    _live("sub_1", status="past_due")
    await _apply(session, _sub_event("evt_1", "sub_1"))
    _live("sub_2", status="active")
    await _apply(session, _sub_event("evt_2", "sub_2", kind="customer.subscription.created"))
    _live("sub_1", status="canceled")
    await _apply(session, _sub_event("evt_3", "sub_1", kind="customer.subscription.deleted"))
    assert await _pro(session)


async def test_e_cancelling_one_of_two_paid_subscriptions_keeps_pro(session: Any) -> None:
    """Case E (the verifier's blocker): two tabs, both paid; the customer cancels
    the one whose event came last. Turns red if: the account is not Pro while it
    still pays for the other."""
    await _user(session)
    _live("sub_1", status="active")
    _live("sub_2", status="active")
    await _apply(session, _sub_event("evt_1", "sub_2", kind="customer.subscription.created"))
    await _apply(session, _invoice_event("evt_2", "invoice.paid", "sub_1"))
    _live("sub_1", status="canceled")
    await _apply(session, _sub_event("evt_3", "sub_1", kind="customer.subscription.deleted"))
    assert await _pro(session)
    rows = (await session.execute(select(Membership))).scalars().all()
    assert sorted((r.stripe_subscription_id, r.status) for r in rows) == [
        ("sub_1", "canceled"),
        ("sub_2", "active"),
    ]  # partner: both are recorded, so double billing is visible


async def test_f_grace_belongs_to_its_subscription(session: Any) -> None:
    """Case F: an unrelated second subscription must not reset the first one's
    grace. Turns red if: grace is shared across subscriptions."""
    await _user(session)
    _live("sub_1", status="past_due")
    await _apply(session, _sub_event("evt_1", "sub_1"))
    day8 = NOW + timedelta(days=8)
    assert not await _pro(session, day8)
    _live("sub_2", status="incomplete")
    await _apply(
        session, _sub_event("evt_2", "sub_2", kind="customer.subscription.created"), now=day8
    )
    await _apply(session, _invoice_event("evt_3", "invoice.payment_failed", "sub_1"), now=day8)
    m1 = await _membership(session, "sub_1")
    assert m1 is not None and _utc(m1.grace_until) == NOW + timedelta(days=7)
    assert not await _pro(session, day8)


async def test_the_account_is_pro_when_any_subscription_is(session: Any) -> None:
    """Turns red if: account_is_pro requires ALL rows, or only looks at one."""
    assert account_is_pro([_m(status="canceled"), _m(status="active")], NOW)
    assert not account_is_pro([_m(status="canceled"), _m(status="incomplete")], NOW)
    assert not account_is_pro([], NOW)


async def test_the_subscription_is_read_again_after_taking_the_account_lock(session: Any) -> None:
    """Lost update: two handlers for one account must not let a slower, staler
    read win. The state stored is the one read AFTER the per-account lock (on
    Postgres, SELECT ... FOR UPDATE on the user row). Turns red if: the first,
    unlocked read is what gets stored.

    What this CANNOT see: whether FOR UPDATE is emitted. SQLite ignores it, so
    deleting ``.with_for_update()`` leaves this green; only a Postgres run with
    two real connections would catch that."""
    await _user(session)
    reads: list[str] = []

    async def changing(sub_id: str) -> dict[str, Any]:
        reads.append(sub_id)
        _live(sub_id, status="past_due" if len(reads) == 1 else "active")
        return LIVE[sub_id]

    await apply_event(
        session, _sub_event("evt_1"), fetch_subscription=changing, grace_days=7, now=NOW
    )
    m = await _membership(session)
    assert reads == ["sub_1", "sub_1"]
    assert m is not None and m.status == "active" and m.grace_until is None


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
        (await session.execute(select(StripeEvent).order_by(StripeEvent.stripe_event_id)))
        .scalars()
        .all()
    )
    assert [(r.stripe_event_id, r.outcome, r.account_id) for r in rows] == [
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


async def test_a_past_due_second_subscription_keeps_pro_during_its_grace(session: Any) -> None:
    """A lapsed first subscription and a second one past due: the second's own
    7-day grace keeps the account Pro. Turns red if: a lapsed row hides a row in
    grace."""
    await _user(session)
    _live("sub_1", status="canceled")
    await _apply(session, _sub_event("evt_1", "sub_1"))
    _live("sub_2", status="past_due")
    assert await _apply(session, _sub_event("evt_2", "sub_2")) == "applied"
    m2 = await _membership(session, "sub_2")
    assert m2 is not None and _utc(m2.grace_until) == NOW + timedelta(days=7)
    assert await _pro(session)


# ---------------------------------------------------------------------------
# v4: bind once, then the status always follows Stripe (cases G, H, H2)
# ---------------------------------------------------------------------------
#
# v3 re-derived the account from subscription METADATA on every event. Metadata
# can be edited (Dashboard or API), so a subscription whose metadata stopped naming
# one of our accounts was skipped, its row stayed 'active' forever (H: Pro without
# payment), and a moved subscription left the paying account on Free (G). v4 binds
# a subscription to an account ONCE, at row creation, and after that looks the row
# up by subscription id first and always stores Stripe's status.

OTHER = "usr_" + "b" * 32


async def test_h_cleared_metadata_cannot_freeze_a_subscription_as_paid(session: Any) -> None:
    """Case H (blocker): metadata cleared, then the subscription deleted. Turns red
    if: the existing row is skipped as 'unmatched' and stays active."""
    await _user(session)
    _live(status="active")
    await _apply(session, _sub_event("evt_1"))
    _live(status="canceled", account=None)
    outcome = await _apply(session, _sub_event("evt_2", kind="customer.subscription.deleted"))
    assert outcome == "applied_account_mismatch"
    m = await _membership(session)
    assert m is not None and m.status == "canceled"
    assert not await _pro(session, NOW + timedelta(days=400))


async def test_h2_metadata_naming_an_unknown_account_cannot_freeze_it_either(session: Any) -> None:
    """Case H2. Turns red if: an unknown account in metadata stops the update."""
    await _user(session)
    _live(status="active")
    await _apply(session, _sub_event("evt_1"))
    _live(status="canceled", account="usr_" + "f" * 32)
    outcome = await _apply(session, _sub_event("evt_2", kind="customer.subscription.deleted"))
    assert outcome == "applied_account_mismatch"
    m = await _membership(session)
    assert m is not None and m.status == "canceled"
    assert not await _pro(session)


async def test_g_a_subscription_stays_bound_to_the_account_it_was_bought_for(session: Any) -> None:
    """Case G: metadata moved to another existing account. The binding does not
    change (a move is a manual support step), the mismatch is recorded, and the
    status still follows Stripe. Turns red if: the row silently changes account,
    or the event is dropped."""
    await _user(session)
    await _user(session, OTHER)
    _live(status="active")
    await _apply(session, _sub_event("evt_1"))
    _live(status="active", account=OTHER)
    assert await _apply(session, _sub_event("evt_2")) == "applied_account_mismatch"
    m = await _membership(session)
    assert m is not None and m.account_id == ACCOUNT
    event = await session.get(StripeEvent, "evt_2")
    assert event is not None and event.account_id == ACCOUNT  # the audit log agrees
    _live(status="canceled", account=OTHER)
    await _apply(session, _sub_event("evt_3", kind="customer.subscription.deleted"))
    assert not await _pro(session)


async def test_an_unbound_subscription_never_creates_a_row(session: Any) -> None:
    """Partner for H: a subscription we never bound (no row) and whose metadata
    names no account of ours grants nothing. Turns red if: a row is created."""
    _live(status="active", account=None)
    assert await _apply(session, _sub_event("evt_1")) == "unmatched"
    assert (await session.execute(select(Membership))).scalars().all() == []


# ---------------------------------------------------------------------------
# K: which subscription the view and the portal describe
# ---------------------------------------------------------------------------


def test_k_the_primary_row_is_a_paid_one_that_is_not_ending() -> None:
    """With two paid subscriptions, one pending cancellation, the user must see the
    one that keeps billing them. Turns red if: the latest row wins regardless."""
    from app.billing.memberships import primary

    ending = _m(status="active", cancel_at_period_end=True, stripe_subscription_id="sub_1")
    continuing = _m(status="active", cancel_at_period_end=False, stripe_subscription_id="sub_2")
    lapsed = _m(status="canceled", stripe_subscription_id="sub_0")
    assert primary([lapsed, ending, continuing], NOW) is continuing
    assert primary([lapsed, ending], NOW) is ending
    assert primary([lapsed], NOW) is lapsed
    assert primary([], NOW) is None


def test_k_an_account_in_grace_is_described_by_its_grace_row() -> None:
    """A payment failed on one subscription (still Pro, in grace) and another was
    cancelled more recently. The user must see the grace row, so they see the
    7-day warning. Turns red if: primary picks paid rows by status alone."""
    from app.billing.memberships import primary

    in_grace = _m(status="past_due", grace_until=NOW + timedelta(days=3))
    lapsed = _m(status="canceled")
    assert primary([lapsed, in_grace], NOW) is in_grace


async def test_the_mismatch_is_logged_for_support(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Support moves a subscription by hand, so they need the log line. Turns red
    if: the mismatch warning is not emitted."""
    await _user(session)
    await _user(session, OTHER)
    _live(status="active")
    await _apply(session, _sub_event("evt_1"))
    _live(status="active", account=OTHER)
    with caplog.at_level("WARNING", logger="citevyn.billing"):
        await _apply(session, _sub_event("evt_2"))
    [record] = [
        r for r in caplog.records if r.getMessage() == "billing_subscription_account_mismatch"
    ]
    assert record.__dict__["subscription"] == "sub_1"


async def test_the_mismatch_is_judged_on_the_read_inside_the_lock(session: Any) -> None:
    """The metadata is cleared between the first read and the one inside the lock.
    Turns red if: the mismatch check uses the first, unlocked read."""
    await _user(session)
    _live(status="active")
    await _apply(session, _sub_event("evt_1"))
    reads: list[int] = []

    async def cleared_on_second_read(sub_id: str) -> dict[str, Any]:
        reads.append(1)
        return _live(status="active", account=ACCOUNT if len(reads) == 1 else None)

    outcome = await apply_event(
        session,
        _sub_event("evt_2"),
        fetch_subscription=cleared_on_second_read,
        grace_days=7,
        now=NOW,
    )
    assert len(reads) == 2
    assert outcome == "applied_account_mismatch"


async def test_a_concurrent_handlers_committed_write_is_not_kept_by_a_stale_row(
    tmp_path: Any,
) -> None:
    """Lost update on an EXISTING row (found in review). Handler A loads the row
    before the lock; handler B then commits past_due with a grace; A's read inside
    the lock says active (the payment recovered). A must store active. Turns red
    if: the in-lock read returns the object loaded before the lock without
    refreshing it (no populate_existing): only columns differing from that stale
    copy are written, so B's past_due survives and the payer drops to Free after
    7 days. Uses a file database so two sessions see each other's commits."""
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    make = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with make() as s:
            await _user(s)
            _live(status="active")
            await _apply(s, _sub_event("evt_1"))
            await s.commit()
        reads: list[int] = []

        async def b_commits_during_a(sub_id: str) -> dict[str, Any]:
            reads.append(1)
            if len(reads) == 2:  # B held the lock, stored past_due, released
                async with make() as b:
                    await b.execute(
                        update(Membership).values(
                            status="past_due", grace_until=NOW + timedelta(days=7)
                        )
                    )
                    await b.commit()
            return _live(status="active")

        async with make() as a:
            await apply_event(
                a,
                _invoice_event("evt_A", "invoice.paid"),
                fetch_subscription=b_commits_during_a,
                grace_days=7,
                now=NOW,
            )
            await a.commit()
        async with make() as r:
            m = await _membership(r)
        assert len(reads) == 2
        assert m is not None and m.status == "active" and m.grace_until is None
    finally:
        await engine.dispose()
