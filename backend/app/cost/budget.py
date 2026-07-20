"""The §9 daily spend budget — soft warn, hard stop (#153 Layer 3).

`RELEASE_PLAN.md` §9 specifies a soft $5 / hard $10 daily limit. Layer 1 records
what each call cost; this layer is what actually *stops* one. Metering alone
changes nothing about how much can be spent.

Three decisions here are load-bearing and none of them is the obvious default.

**Persisted, not in-process.** The budget is computed by summing `provider_calls`
since midnight UTC. An in-memory counter resets on every API restart, which is
precisely how the existing 30 q/h rate limiter came to be ineffective — a process
restart hands out a fresh allowance. A daily cap that a restart clears is not a
cap.

**A hard trip is a TRANSIENT failure, never a content refusal.** See
:class:`~app.llm.errors.CostLimitReached`. Returning a no-answer envelope would
teach the client that the corpus lacks an answer and suppress retry (#142).

**Fail CLOSED when the meter store is unreachable.** If we cannot read what has
been spent, we cannot know whether we are over budget, and fail-open converts a
Postgres blip into an unmetered spending window with no ceiling below the
provider's own cap. The demo going 503 for the length of a database outage is
strictly cheaper than an unbounded one. This is a documented setting
(`cost_budget_fail_closed`), not an emergent property of the error handling — an
operator who genuinely wants availability over cost can flip it, deliberately and
visibly.

The soft limit deliberately does NOT change correctness. It warns and biases
toward cache; it must never start refusing answers, or a $5 day would silently
degrade quality with no operator signal beyond a log line.
"""

from __future__ import annotations

import enum
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.llm.errors import CostLimitReached
from app.models.provider_calls import ProviderCall

_logger = logging.getLogger(__name__)


class BudgetState(enum.StrEnum):
    """Where today's spend sits relative to the §9 limits."""

    ok = "ok"
    soft = "soft"
    hard = "hard"


def utc_day_start(now: datetime | None = None) -> datetime:
    """Midnight UTC for the day containing ``now``.

    UTC rather than local time on purpose: the bucket boundary must not move when
    a server's timezone or DST changes, which would either grant a second daily
    allowance or truncate one.
    """
    moment = now or datetime.now(UTC)
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def spend_since(session: AsyncSession, since: datetime) -> Decimal:
    """Sum priced spend recorded at or after ``since``.

    ``COALESCE`` so an empty table returns 0 rather than ``None`` — the caller
    compares against a threshold and ``None`` would raise, turning "no spend yet"
    into an outage on the very first request of the day.
    """
    stmt = select(func.coalesce(func.sum(ProviderCall.cost_usd), 0)).where(
        ProviderCall.occurred_at >= since
    )
    total = (await session.execute(stmt)).scalar_one()
    return Decimal(str(total))


async def today_spend(sessionmaker: async_sessionmaker[AsyncSession]) -> Decimal:
    """Total USD recorded since midnight UTC."""
    async with sessionmaker() as session:
        return await spend_since(session, utc_day_start())


def classify(spend: Decimal, settings: Settings) -> BudgetState:
    """Map a spend total onto the §9 bands.

    Both comparisons are ``>=``: at exactly $10.00 the hard limit is *reached*,
    which §9 says stops generation. Using ``>`` would allow one more call past a
    limit stated as a ceiling.
    """
    if spend >= Decimal(str(settings.cost_hard_daily_usd)):
        return BudgetState.hard
    if spend >= Decimal(str(settings.cost_soft_daily_usd)):
        return BudgetState.soft
    return BudgetState.ok


async def enforce_budget(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> BudgetState:
    """Gate a paid call on today's spend. Raises :class:`CostLimitReached` on hard.

    Returns the state so a caller can react to ``soft`` (prefer cache, skip
    optional work) without duplicating the query.

    A meter-store failure is resolved by ``settings.cost_budget_fail_closed``:
    closed (default) raises, open logs loudly and allows. Either way the decision
    is explicit and logged — the one outcome not permitted is silently allowing an
    unmetered call because a query happened to raise.
    """
    if not settings.cost_budget_enabled:
        return BudgetState.ok
    try:
        spend = await today_spend(sessionmaker)
    except Exception as exc:  # noqa: BLE001 - the fail mode is the whole point
        if settings.cost_budget_fail_closed:
            _logger.error("budget_store_unavailable_failing_closed", exc_info=True)
            raise CostLimitReached(
                "Cost budget cannot be verified (meter store unavailable); refusing paid calls.",
                cause=exc,
            ) from exc
        _logger.error("budget_store_unavailable_failing_OPEN_spend_is_unbounded", exc_info=True)
        return BudgetState.ok

    state = classify(spend, settings)
    if state is BudgetState.hard:
        _logger.error(
            "cost_hard_limit_reached",
            extra={"spend_usd": str(spend), "hard_limit_usd": settings.cost_hard_daily_usd},
        )
        raise CostLimitReached(
            f"Daily cost limit reached (${spend} of ${settings.cost_hard_daily_usd}); "
            "paid model calls are stopped until the UTC day rolls over."
        )
    if state is BudgetState.soft:
        _logger.warning(
            "cost_soft_limit_reached",
            extra={"spend_usd": str(spend), "soft_limit_usd": settings.cost_soft_daily_usd},
        )
    return state


async def budget_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> dict[str, object]:
    """Operator-facing view of today's spend (#153 Layer 5).

    ``warn_at`` mirrors the 60% / 85% thresholds so a dashboard does not have to
    re-derive them and drift from the enforcement path.
    """
    spend = await today_spend(sessionmaker)
    hard = Decimal(str(settings.cost_hard_daily_usd))
    fraction = float(spend / hard) if hard > 0 else 0.0
    return {
        "day_start_utc": utc_day_start().isoformat(),
        "spend_usd": str(spend),
        "soft_limit_usd": settings.cost_soft_daily_usd,
        "hard_limit_usd": settings.cost_hard_daily_usd,
        "remaining_usd": str(max(Decimal(0), hard - spend)),
        "state": str(classify(spend, settings)),
        "fraction_of_hard": round(fraction, 4),
        "warn_60pct": fraction >= 0.60,
        "warn_85pct": fraction >= 0.85,
    }


__all__ = [
    "BudgetState",
    "budget_snapshot",
    "classify",
    "enforce_budget",
    "spend_since",
    "today_spend",
    "utc_day_start",
]
