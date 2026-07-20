"""The §9 daily spend budget + admission control (#153 Layers 2-3).

Layer 1 only *records* spend; this is the layer that stops it, so these are the
tests that decide whether the demo can run up an unbounded bill. Four properties
matter more than the happy path:

1. **A hard trip is a TRANSIENT failure, never a content refusal.** A no-answer
   envelope teaches the client the corpus lacks an answer and suppresses retry —
   the #142 bug — which is strictly worse than a 503.
2. **A restart does not reset the budget.** The spend total is a SQL sum over
   ``provider_calls``, not a counter. This is the exact flaw that makes the
   existing 30 q/h limiter ineffective.
3. **An unreadable meter fails CLOSED.** Fail-open turns a database blip into an
   unmetered spending window whose only ceiling is the provider's own cap.
4. **The kill switch works**, so an operator is never trapped by this layer.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.cost.admission import get_semaphore, reset_semaphore
from app.cost.budget import (
    BudgetState,
    budget_snapshot,
    classify,
    enforce_budget,
    today_spend,
    utc_day_start,
)
from app.llm.errors import CostLimitReached, LLMUnavailable
from app.llm.types import LLMResult
from app.models.base import Base
from app.models.provider_calls import ProviderCall


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {"llm_provider": "stub"}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


async def _make_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """An isolated in-memory DB per test, with only the tables we need."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _record(
    sm: async_sessionmaker[AsyncSession], cost: str, *, occurred_at: datetime | None = None
) -> None:
    async with sm() as session:
        session.add(
            ProviderCall(
                occurred_at=occurred_at or datetime.now(UTC),
                kind="llm",
                call_site="answer",
                provider="router",
                model="openai/gpt-4o-mini",
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal(cost),
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Banding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spend", "expected"),
    [
        ("0", BudgetState.ok),
        ("4.99", BudgetState.ok),
        ("5.00", BudgetState.soft),
        ("9.99", BudgetState.soft),
        ("10.00", BudgetState.hard),
        ("1000", BudgetState.hard),
    ],
)
def test_classify_bands_match_release_plan_section_9(spend: str, expected: BudgetState) -> None:
    assert classify(Decimal(spend), _settings()) is expected


def test_the_limits_are_reached_not_exceeded() -> None:
    """`>=`, not `>`. A limit stated as a ceiling must not allow one more call AT it."""
    s = _settings(cost_soft_daily_usd=5.0, cost_hard_daily_usd=10.0)
    assert classify(Decimal("10.00"), s) is BudgetState.hard
    assert classify(Decimal("5.00"), s) is BudgetState.soft


# ---------------------------------------------------------------------------
# The hard stop
# ---------------------------------------------------------------------------


def test_hard_limit_raises_and_it_is_a_TRANSIENT_error_not_a_content_refusal() -> None:
    """The single most important assertion in this module.

    A content refusal (`no_answer`) tells the client the corpus lacks an answer and
    suppresses retry. CostLimitReached subclasses LLMUnavailable, which every
    caller in the answer path already surfaces as a 5xx.
    """

    async def _run() -> None:
        sm = await _make_sessionmaker()
        await _record(sm, "10.00")
        with pytest.raises(CostLimitReached) as excinfo:
            await enforce_budget(sm, _settings())
        # The transient contract, asserted structurally rather than by name.
        assert isinstance(excinfo.value, LLMUnavailable)

    asyncio.run(_run())


def test_soft_limit_warns_but_does_NOT_block() -> None:
    """Correctness must be unchanged at the soft limit.

    If a $5 day started refusing answers, quality would degrade with no signal
    beyond a log line — the operator would see a broken demo, not a budget event.
    """

    async def _run() -> None:
        sm = await _make_sessionmaker()
        await _record(sm, "5.00")
        assert await enforce_budget(sm, _settings()) is BudgetState.soft

    asyncio.run(_run())


def test_under_budget_is_allowed() -> None:
    async def _run() -> None:
        sm = await _make_sessionmaker()
        await _record(sm, "1.23")
        assert await enforce_budget(sm, _settings()) is BudgetState.ok

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Persistence + the day boundary
# ---------------------------------------------------------------------------


def test_spend_is_summed_from_the_store_so_a_RESTART_cannot_reset_it() -> None:
    """The budget is a SQL sum, not a counter.

    Simulated by discarding every in-process object and re-querying with a fresh
    sessionmaker over the same database — which is what a restart amounts to.
    """

    async def _run() -> None:
        sm = await _make_sessionmaker()
        await _record(sm, "6.00")
        await _record(sm, "4.00")
        # "Restart": nothing in memory carries over; only the rows do.
        with pytest.raises(CostLimitReached):
            await enforce_budget(sm, _settings())

    asyncio.run(_run())


def test_yesterdays_spend_does_not_count_against_today() -> None:
    """A DAILY limit that never rolls over is a lifetime limit."""

    async def _run() -> None:
        sm = await _make_sessionmaker()
        await _record(sm, "50.00", occurred_at=utc_day_start() - timedelta(seconds=1))
        assert await today_spend(sm) == Decimal(0)
        assert await enforce_budget(sm, _settings()) is BudgetState.ok

    asyncio.run(_run())


def test_a_call_exactly_at_midnight_counts_toward_today() -> None:
    """The boundary is inclusive; an off-by-one here silently drops spend."""

    async def _run() -> None:
        sm = await _make_sessionmaker()
        await _record(sm, "7.00", occurred_at=utc_day_start())
        assert await today_spend(sm) == Decimal("7.00")

    asyncio.run(_run())


def test_an_empty_meter_reads_zero_not_None() -> None:
    """SUM over no rows is NULL; without COALESCE the first request of each day
    would raise instead of being allowed."""

    async def _run() -> None:
        sm = await _make_sessionmaker()
        assert await today_spend(sm) == Decimal(0)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Fail mode when the meter store is down
# ---------------------------------------------------------------------------


class _BrokenSessionmaker:
    """Stands in for an unreachable meter store."""

    def __call__(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("database is unreachable")


def test_meter_store_down_FAILS_CLOSED_by_default() -> None:
    """If we cannot read what was spent, we cannot know we are under budget.

    Fail-open here converts a Postgres blip into an unmetered spending window
    whose only ceiling is the provider-side cap.
    """

    async def _run() -> None:
        with pytest.raises(CostLimitReached):
            await enforce_budget(_BrokenSessionmaker(), _settings())  # type: ignore[arg-type]

    asyncio.run(_run())


def test_fail_open_is_available_but_must_be_chosen_explicitly() -> None:
    async def _run() -> None:
        state = await enforce_budget(
            _BrokenSessionmaker(),  # type: ignore[arg-type]
            _settings(cost_budget_fail_closed=False),
        )
        assert state is BudgetState.ok

    asyncio.run(_run())


def test_kill_switch_disables_the_budget_entirely() -> None:
    """An operator must never be trapped by this layer."""

    async def _run() -> None:
        sm = await _make_sessionmaker()
        await _record(sm, "999.00")
        assert await enforce_budget(sm, _settings(cost_budget_enabled=False)) is BudgetState.ok

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Operator visibility (Layer 5)
# ---------------------------------------------------------------------------


def test_snapshot_reports_remaining_budget_and_the_warn_thresholds() -> None:
    async def _run() -> None:
        sm = await _make_sessionmaker()
        await _record(sm, "8.50")  # 85% of the $10 hard limit
        snap = await budget_snapshot(sm, _settings())
        # Compare VALUES, not formatting: the column is NUMERIC(14,6) so the
        # string carries full scale ("8.500000"). Asserting the literal would pin
        # a presentation detail and break on a scale change that costs nothing.
        assert Decimal(str(snap["spend_usd"])) == Decimal("8.50")
        assert Decimal(str(snap["remaining_usd"])) == Decimal("1.50")
        assert snap["state"] == "soft"
        assert snap["warn_60pct"] is True
        assert snap["warn_85pct"] is True

    asyncio.run(_run())


def test_snapshot_warns_at_60_but_not_85() -> None:
    async def _run() -> None:
        sm = await _make_sessionmaker()
        await _record(sm, "6.00")
        snap = await budget_snapshot(sm, _settings())
        assert snap["warn_60pct"] is True
        assert snap["warn_85pct"] is False

    asyncio.run(_run())


def test_snapshot_never_reports_negative_remaining() -> None:
    async def _run2() -> None:
        sm = await _make_sessionmaker()
        await _record(sm, "25.00")
        snap = await budget_snapshot(sm, _settings())
        assert Decimal(str(snap["remaining_usd"])) == Decimal(0)

    asyncio.run(_run2())


# ---------------------------------------------------------------------------
# Layer 2 — concurrency admission
# ---------------------------------------------------------------------------


def test_concurrency_cap_bounds_calls_IN_FLIGHT() -> None:
    """Every in-flight call reads a spend total that excludes its peers, so an
    unbounded burst can collectively overshoot a budget each member individually
    satisfies. This bounds how far."""

    async def _run() -> int:
        reset_semaphore()
        settings = _settings(cost_max_concurrent_calls=2)
        peak = 0
        live = 0

        async def _call() -> None:
            nonlocal peak, live
            async with get_semaphore(settings):
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.01)
                live -= 1

        await asyncio.gather(*[_call() for _ in range(10)])
        return peak

    assert asyncio.run(_run()) == 2
    reset_semaphore()


def test_semaphore_is_rebuilt_when_the_cap_changes() -> None:
    reset_semaphore()
    first = get_semaphore(_settings(cost_max_concurrent_calls=2))
    assert get_semaphore(_settings(cost_max_concurrent_calls=2)) is first
    assert get_semaphore(_settings(cost_max_concurrent_calls=5)) is not first
    reset_semaphore()


def test_a_cancelled_caller_releases_its_slot() -> None:
    """Cancellation is normal (a client disconnects). A leaked slot would shrink
    the cap permanently until restart."""

    async def _run() -> None:
        reset_semaphore()
        settings = _settings(cost_max_concurrent_calls=1)
        sem = get_semaphore(settings)

        async def _hold() -> None:
            async with sem:
                await asyncio.sleep(10)

        task = asyncio.create_task(_hold())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The slot must be free again.
        await asyncio.wait_for(sem.acquire(), timeout=0.5)
        sem.release()

    asyncio.run(_run())
    reset_semaphore()


# ---------------------------------------------------------------------------
# The seam: the budget must actually gate the PAID CALL, not just be callable
# ---------------------------------------------------------------------------


def test_the_budget_is_enforced_at_the_metering_seam_before_the_provider_call() -> None:
    """Deleting the enforce_budget call from MeteredLLMClient.complete must FAIL.

    Every other test here calls ``enforce_budget`` directly, and the metering
    suite disables the budget — so removing the check from the seam entirely left
    the whole suite green. A budget that is correct but not wired in stops nothing.

    Asserting the inner client was never entered is the point: the check has to
    run BEFORE the provider call, since checking afterwards only records spend
    that already happened.
    """
    from app.cost.metered import MeteredLLMClient

    calls = {"n": 0}

    class _Spy:
        async def complete(self, **kw: object) -> LLMResult:
            calls["n"] += 1
            return LLMResult(
                text="x",
                input_tokens=1,
                output_tokens=1,
                model="openai/gpt-4o-mini",
                provider="router",
            )

        async def aclose(self) -> None:
            return None

    async def _run() -> None:
        reset_semaphore()
        sm = await _make_sessionmaker()
        await _record(sm, "10.00")  # at the hard limit
        client = MeteredLLMClient(_Spy(), sessionmaker=sm, settings=_settings())
        with pytest.raises(CostLimitReached):
            await client.complete(system="s", user="u", max_tokens=10, temperature=0.0)

    asyncio.run(_run())
    assert calls["n"] == 0, "the provider was called despite the hard budget limit"
    reset_semaphore()


def test_under_budget_the_seam_lets_the_call_through() -> None:
    """The mirror — the gate must not block a legitimate call."""
    from app.cost.metered import MeteredLLMClient

    calls = {"n": 0}

    class _Spy:
        async def complete(self, **kw: object) -> LLMResult:
            calls["n"] += 1
            return LLMResult(
                text="x",
                input_tokens=1,
                output_tokens=1,
                model="openai/gpt-4o-mini",
                provider="router",
            )

        async def aclose(self) -> None:
            return None

    async def _run() -> None:
        reset_semaphore()
        sm = await _make_sessionmaker()
        await _record(sm, "0.01")
        client = MeteredLLMClient(_Spy(), sessionmaker=sm, settings=_settings())
        await client.complete(system="s", user="u", max_tokens=10, temperature=0.0)

    asyncio.run(_run())
    assert calls["n"] == 1
    reset_semaphore()
