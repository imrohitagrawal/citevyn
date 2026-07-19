"""One row per PAID provider call (#153 Layer 1 — metering).

This is the data the §9 daily budget is computed from. Nothing else in the system
records what a call cost: ``LLMResult`` carries token counts, but they were
discarded the moment the answer was returned, so "how much have we spent today"
had no answer at all before this table.

Design notes
------------

**Cost is stored, not derived.** A price is a snapshot (see
:mod:`app.cost.pricing`); recomputing historical cost from today's price book would
silently rewrite yesterday's spend when a provider changes its rates. The rate
actually applied is stored alongside, so a row is auditable on its own.

**Attempts, not logical calls.** ``attempts`` counts the HTTP requests the provider
client actually made. The embedders retry a transient failure up to twice, so a
flaky provider costs up to 3x what a naive per-call count would report — and a
budget that under-counts by 3x during an outage is exactly when it matters.

**Unpriced calls are recorded, not dropped.** ``priced=False`` with ``cost_usd=0``
means "this call happened and we do not know its price". Dropping the row would
make it invisible; guessing would corrupt the budget. See :mod:`app.cost.pricing`.

**No prompt or response text.** Only counts and identifiers. The table is queried by
an admin surface and could end up in a log or backup, so it must not become a
second copy of user questions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base


class ProviderCall(Base):
    """A single metered call to a paid model provider."""

    __tablename__ = "provider_calls"

    call_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    # When the call COMPLETED. The daily budget buckets on this column, so it is
    # written by the meter (not a DB default) to keep the value in the same clock
    # the budget logic reads.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # "llm" | "embedding". A plain string rather than a native enum: this is
    # observability data, and a new provider kind should not need a migration.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # app.cost.call_site.CallSite value — answer / condense / alias_intent / ...
    call_site: Mapped[str] = mapped_column(String(32), nullable=False)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Provider HTTP requests actually issued, including retries (>= 1).
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Numeric, never float: these are summed across thousands of rows and compared
    # against a dollar threshold. 6 decimal places resolves a single cheap call
    # (a 100-token gpt-4o-mini request is ~$0.000015).
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, default=Decimal(0))
    # The rates actually applied, so a row can be audited after the price book
    # changes. NULL exactly when ``priced`` is False.
    input_price_per_1m: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    output_price_per_1m: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    # False => the price book had no entry for (provider, model); cost_usd is 0 and
    # the daily budget is UNDER-counting this call. Surfaced as ``unpriced_calls``.
    priced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # True => token counts are a local estimate (the provider returned no usage
    # block), so cost_usd is approximate. Keeps a guess distinguishable from a fact.
    tokens_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Correlates a spend row with the request that caused it, via the same request
    # id the access log and error envelope carry. Nullable: ingest and eval calls
    # happen outside an HTTP request.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # The budget's hot query is "sum cost_usd since midnight UTC", so the index
        # leads on occurred_at. Without it that scan grows without bound as the
        # table does, on the critical path of every paid call.
        Index("ix_provider_calls_occurred_at", "occurred_at"),
    )
