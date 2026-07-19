"""The metering seam: a decorator that records every paid LLM call (#153 Layer 1).

Why a decorator and not per-call-site instrumentation
-----------------------------------------------------

There are three LLM call sites today (answer generation, follow-up condensation,
the CiteVyn alias-intent check) and there will be more. Instrumenting each one
means every future call site must remember to meter, and the failure mode of
forgetting is **silent under-counting** — the budget reads low, nothing errors, and
the first symptom is an unexpected provider bill.

Wrapping the client inverts that: metering is applied once, at the single place the
shared client is constructed (``app.llm.factory.build_llm_client``), so a new call
site is metered by construction. The most it can get wrong is the *label*, which
degrades to ``unknown`` rather than to nothing (see :mod:`app.cost.call_site`).

The wrapper satisfies :class:`~app.llm.protocol.LLMClient` structurally, so nothing
downstream knows it is there.

What is deliberately NOT here
-----------------------------

The embedder is not yet wrapped. The seam is identical and the follow-up is
mechanical, but embeddings are ~1/10th the per-token price of generation and the
query path sends a single short string, so the LLM is where the money is. Tracked
separately so this change stays reviewable.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cost.meter import build_call, record_call
from app.llm.protocol import LLMClient
from app.llm.types import LLMResult

_logger = logging.getLogger(__name__)


class MeteredLLMClient:
    """Wraps an :class:`LLMClient` and records the cost of each completion.

    Metering happens **after** a successful call, for a specific reason: a failed
    call has no usage block, and providers do not bill for a request that errored
    before generation. Recording an estimate for it would inflate the budget with
    money that was never spent. A failure that *did* consume tokens (a timeout
    mid-stream) is a known blind spot — the provider-side cap (``COST_CONTROLS.md``
    §0) is what backstops it.
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._inner = inner
        # An explicit override (tests) or ``None`` to resolve per call. NOT cached:
        # ``get_sessionmaker()`` is already a process-wide lazy cache, and
        # ``reset_engine()`` clears it. Caching it here would pin a DISPOSED engine
        # after an engine swap, so every write would raise, get swallowed by
        # ``_meter``, and under-count spend with nothing but a log line to show.
        self._sessionmaker = sessionmaker

    @property
    def inner(self) -> LLMClient:
        """The wrapped provider client.

        Exposed so callers that legitimately need the concrete provider — tests
        asserting which provider a config selects, and any future health check —
        can see through the decorator instead of being defeated by it.
        """
        return self._inner

    def _resolve_sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is not None:
            return self._sessionmaker
        from app.core.db import get_sessionmaker

        return get_sessionmaker()

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        result = await self._inner.complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )
        await self._meter(result, prompt_chars=len(system) + len(user), max_tokens=max_tokens)
        return result

    async def _meter(self, result: LLMResult, *, prompt_chars: int, max_tokens: int) -> None:
        """Record one completion. Never raises — metering must not break answers."""
        try:
            input_tokens, output_tokens, estimated = _resolve_tokens(
                result, prompt_chars=prompt_chars, max_tokens=max_tokens
            )
            call = build_call(
                kind="llm",
                provider=result.provider,
                model=result.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tokens_estimated=estimated,
                request_id=_current_request_id(),
            )
            await record_call(self._resolve_sessionmaker(), call)
        except asyncio.CancelledError:
            # Cancellation is control flow, not an error: swallowing it here would
            # break task cancellation for the whole request.
            raise
        except Exception:  # noqa: BLE001 - see the class docstring
            _logger.exception("llm_metering_failed")

    async def aclose(self) -> None:
        """Delegate resource cleanup to the wrapped client."""
        await self._inner.aclose()


# Same convention as ``app.llm.stub._CHARS_PER_TOKEN`` and the eval measurement
# harness: a rough but consistent 4 chars/token.
_CHARS_PER_TOKEN = 4


def _resolve_tokens(
    result: LLMResult, *, prompt_chars: int, max_tokens: int
) -> tuple[int, int, bool]:
    """Return ``(input_tokens, output_tokens, estimated)`` for a completion.

    Every provider client defaults its usage fields to 0 when the response omits a
    usage block — ``usage.get("prompt_tokens", 0)`` on OpenRouter, a missing
    ``usageMetadata`` on Gemini, and the same on Anthropic. OpenRouter really does
    return ``200`` with a null/absent ``usage`` on some routed upstreams.

    Taken at face value, such a call is recorded as ``0 tokens, $0, priced=True`` —
    indistinguishable from a genuinely free call, absent from ``unpriced_calls``,
    and silently invisible to the §9 budget. A provider that omits usage would
    therefore let real spend accumulate while the meter reads zero.

    So a completion reporting no tokens at all is ESTIMATED instead, and flagged
    ``tokens_estimated=True`` so a guess stays distinguishable from a fact. The
    estimate is deliberately conservative-high on output (``max_tokens``, the
    ceiling we authorised): under-counting the budget is the failure that costs
    money, over-counting only trips the limit early.
    """
    if result.input_tokens > 0 or result.output_tokens > 0:
        return result.input_tokens, result.output_tokens, False
    return (
        max(1, prompt_chars // _CHARS_PER_TOKEN),
        max(1, len(result.text) // _CHARS_PER_TOKEN) if result.text else max_tokens,
        True,
    )


def _current_request_id() -> str | None:
    """Best-effort correlation id; ``None`` outside an HTTP request (ingest, eval).

    ``get_current_request_id`` returns ``""`` (not ``None``) when there is no
    ambient request, so the empty string is normalised here. Otherwise every
    ingest/eval row stores ``''`` and ``WHERE request_id IS NULL`` — the natural
    "spend not caused by a user request" query — silently returns nothing.
    """
    try:
        from app.core.middleware import get_current_request_id

        return get_current_request_id() or None
    except Exception:  # noqa: BLE001 - correlation is nice-to-have, never required
        return None


__all__ = ["MeteredLLMClient"]
