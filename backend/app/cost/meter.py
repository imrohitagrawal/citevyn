"""Persist what each paid provider call cost (#153 Layer 1).

The meter is the *write* half of cost control. It runs on the critical path of
every paid call, which drives two non-obvious rules:

**A metering failure must not fail the user's request.** If the DB write raises,
the answer the user already paid for is still returned; the failure is logged
loudly and the run is left under-counted. This is fail-OPEN *on recording*, and it
is deliberately not the same decision as the budget check (Layer 3), which fails
CLOSED when it cannot read the meter. Recording and enforcing have opposite
asymmetries: a lost row costs accuracy, whereas an unenforced budget costs money.

**The meter writes on its own session, not the request's.** Sharing the request
session would tie the spend row to that transaction — so a request that rolls back
(a validation error, a failed citation check, a 500) would roll back the record of
money that was *already spent*. Spend is a fact about the outside world; it must
not be transactional with our own success.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cost.call_site import CallSite, get_call_site
from app.cost.pricing import price_for
from app.models.provider_calls import ProviderCall

_logger = logging.getLogger(__name__)


def build_call(
    *,
    kind: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    attempts: int = 1,
    tokens_estimated: bool = False,
    request_id: str | None = None,
    call_site: CallSite | None = None,
    occurred_at: datetime | None = None,
) -> ProviderCall:
    """Price a call and return the (unsaved) :class:`ProviderCall` row.

    Split out from :func:`record_call` so the pricing decision is testable without
    a database — it is the part that can be quietly wrong.

    An unknown (provider, model) yields ``priced=False`` and ``cost_usd=0``: the
    call is recorded and visibly under-counted rather than silently guessed at or
    dropped entirely. See :mod:`app.cost.pricing`.
    """
    price = price_for(provider=provider, model=model)
    if price is None:
        _logger.warning(
            "provider_call_unpriced",
            extra={"provider": provider, "model": model, "kind": kind},
        )
    return ProviderCall(
        occurred_at=occurred_at or datetime.now(UTC),
        kind=kind,
        call_site=str(call_site if call_site is not None else get_call_site()),
        provider=provider,
        model=model,
        input_tokens=max(0, input_tokens),
        output_tokens=max(0, output_tokens),
        attempts=max(1, attempts),
        cost_usd=(
            price.cost_for(input_tokens=input_tokens, output_tokens=output_tokens)
            if price is not None
            else Decimal(0)
        ),
        input_price_per_1m=price.input_per_1m if price is not None else None,
        output_price_per_1m=price.output_per_1m if price is not None else None,
        priced=price is not None,
        tokens_estimated=tokens_estimated,
        request_id=request_id,
    )


async def record_call(sessionmaker: async_sessionmaker[AsyncSession], call: ProviderCall) -> None:
    """Commit one spend row on a fresh session.

    Never raises: see the module docstring. A failure is logged with
    ``exc_info`` so it is visible in the structured log rather than swallowed.
    """
    try:
        async with sessionmaker() as session:
            session.add(call)
            await session.commit()
    except Exception:  # noqa: BLE001 - metering must never break the request path
        _logger.exception(
            "provider_call_record_failed",
            extra={
                "provider": call.provider,
                "model": call.model,
                "call_site": call.call_site,
            },
        )


__all__ = ["build_call", "record_call"]
