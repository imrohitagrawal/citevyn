"""The answer allowance: how many answered questions a caller may still ask.

ADR-0005 §1 and §2 ("Quota"), Phase 4B-2. The KIND of allowance comes from the
policy table (``app.access.policy``: Free has ``once``, Pro has ``monthly``); the
NUMBERS are settings. Counted from ``answer_usage``, one row per answered
question, written by the answer route in the same transaction as the answer.

* **Free:** ``access_free_trial_answers`` answered questions, ever, and only for a
  VERIFIED account (``users.email_verified_at``). An unverified account has none.
* **Pro:** ``access_pro_monthly_answers`` Pro answers per calendar month in UTC
  (rows record their tier, so trial answers never count against it). Not the
  Stripe billing period: yearly plans have no monthly period, and the count must
  not depend on how fresh the last webhook was. It matches the daily spend cap's
  UTC buckets. The hourly limit still applies on top.
* Only ``paid_by='platform'`` rows count; BYOK answers (Phase 8) never do.

Two questions asked at the same moment at 24 of 25 can both be answered: the
check and the write are not one atomic step, because locking the account for the
length of a model call would block its Stripe webhooks. The overshoot is bounded
by how many requests one account has in flight, and the hourly limit caps that.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.policy import POLICY, Allowance, Capability, Tier
from app.core.config import Settings
from app.core.errors import APIErrorCode, error_response
from app.models import AnswerUsage, User


def is_counted_answer(response: Mapping[str, object]) -> bool:
    """True only for an answered question: a cited answer, fresh or cached.

    A refusal, a no-answer, an off-topic reply, a greeting (no citations) or an
    uncited reply never counts. Only the booleans ``False`` mean "answered", so
    a malformed response fails in the user's favour.
    """
    citations = response.get("citations")
    return (
        response.get("no_answer") is False
        and response.get("unsupported") is False
        and isinstance(citations, list)
        and bool(cast("list[object]", citations))
    )


def month_start(now: datetime) -> datetime:
    """Midnight UTC on the first of ``now``'s month."""
    now = now.astimezone(UTC)
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def _next_month(start: datetime) -> datetime:
    if start.month == 12:
        return datetime(start.year + 1, 1, 1, tzinfo=UTC)
    return datetime(start.year, start.month + 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Usage:
    """Where one account stands against its allowance."""

    kind: str  # "trial" (Free, once), "monthly" (Pro) or "none"
    used: int
    limit: int
    resets_at: datetime | None
    verified: bool

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
            "verified": self.verified,
        }


async def usage_for(
    db: AsyncSession, user_id: str, tier: Tier, settings: Settings, now: datetime
) -> Usage:
    """Count this account's platform-paid answers against its tier's allowance."""
    user = await db.get(User, user_id)
    verified = user is not None and user.email_verified_at is not None
    allowance = POLICY[tier].allowance
    since: datetime | None = None
    resets_at: datetime | None = None
    if allowance is Allowance.monthly:
        kind, limit = "monthly", settings.access_pro_monthly_answers
        since = month_start(now)
        resets_at = _next_month(since)
    elif allowance is Allowance.once:
        kind = "trial"
        limit = settings.access_free_trial_answers if verified else 0
    else:
        return Usage(kind="none", used=0, limit=0, resets_at=None, verified=verified)
    query = select(func.count()).where(
        AnswerUsage.user_id == user_id, AnswerUsage.paid_by == "platform"
    )
    if since is not None:
        # Pro: this month's PRO answers only, so trial answers from earlier in
        # the month do not shrink the first Pro month.
        query = query.where(AnswerUsage.occurred_at >= since, AnswerUsage.tier == Tier.pro.value)
    used = int((await db.execute(query)).scalar_one())
    return Usage(kind=kind, used=used, limit=limit, resets_at=resets_at, verified=verified)


def refuse_if_used_up(usage: Usage, tier: Tier, request_id: str) -> None:
    """Raise the error that tells the caller what helps; return if they may ask.

    * Free, not verified: 403 ``verification_required`` (verifying helps).
    * Free, trial used: 403 ``plan_required`` (upgrading helps).
    * Pro, month used: 429 ``quota_exceeded`` with ``resets_at`` (waiting helps).
    """
    if usage.remaining > 0:
        return
    if usage.kind == "trial" and not usage.verified:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.verification_required,
            message="Verify your email address to start your free questions.",
        )
    if tier is Tier.pro:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.quota_exceeded,
            message="You have used this month's questions.",
            details={
                "used": usage.used,
                "limit": usage.limit,
                "resets_at": usage.resets_at.isoformat() if usage.resets_at else None,
            },
        )
    raise error_response(
        request_id=request_id,
        code=APIErrorCode.plan_required,
        message="You have used your free questions. Upgrade to Pro to keep asking.",
        details={
            "capability": Capability.chat.value,
            "required_tier": Tier.pro.value,
            "reason": "trial_used",
            "used": usage.used,
            "limit": usage.limit,
        },
    )


def usage_row(
    user_id: str, tier: Tier, response: Mapping[str, object], request_id: str, now: datetime
) -> AnswerUsage:
    """The row recording one answered question (platform-paid, from chat)."""
    raw = response.get("message_id")
    try:
        message_id = uuid.UUID(str(raw)) if raw is not None else None
    except ValueError:
        message_id = None
    return AnswerUsage(
        usage_id=uuid.uuid4(),
        user_id=user_id,
        occurred_at=now,
        paid_by="platform",
        channel="chat",
        tier=tier.value,
        message_id=message_id,
        request_id=request_id,
    )


__all__ = [
    "Usage",
    "is_counted_answer",
    "month_start",
    "refuse_if_used_up",
    "usage_for",
    "usage_row",
]
