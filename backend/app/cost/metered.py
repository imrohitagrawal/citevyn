"""The metering seam: decorators that record every paid provider call (#153 Layer 1).

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

The embedder is wrapped the same way by :class:`MeteredEmbedder`, applied at the
production construction sites in :mod:`app.embeddings.factory` (the API singleton)
and :mod:`app.worker.cli` (the ingest runner). Embeddings are ~1/10th the per-token
price of generation on the query path, but ingest embeds an entire corpus in a
burst — the one operation that can move the daily number quickly — so leaving it
unmetered left the §9 budget blind to it.

What is deliberately NOT here
-----------------------------

Neither stub is wrapped. See :func:`app.llm.factory._metered` and
:func:`app.embeddings.factory.metered_embedder`: safety mechanisms test the client's
identity with ``isinstance``, and a decorator would defeat them.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.cost.admission import get_semaphore
from app.cost.budget import enforce_budget
from app.cost.call_site import CallSite, get_call_site
from app.cost.meter import build_call, record_call
from app.cost.pricing import canonical_provider
from app.cost.usage import EmbeddingUsage, collect_embedding_usage
from app.embeddings.protocol import Embedder
from app.llm.protocol import LLMClient
from app.llm.types import LLMResult

_logger = logging.getLogger(__name__)


def _resolve_settings(override: Settings | None) -> Settings:
    """The settings a wrapper meters against, resolved PER CALL when not pinned.

    ``get_settings`` is already an lru_cache, so resolving here costs nothing —
    while pinning an instance at construction would make a config reload invisible
    to the budget it gates.
    """
    if override is not None:
        return override
    from app.core.config import get_settings

    return get_settings()


def _resolve_sessionmaker(
    override: async_sessionmaker[AsyncSession] | None,
) -> async_sessionmaker[AsyncSession]:
    """The sessionmaker the meter writes on, resolved PER CALL when not pinned.

    Deliberately NOT cached on the wrapper: ``get_sessionmaker()`` is already a
    process-wide lazy cache that ``reset_engine()`` clears. Holding a reference here
    would pin a DISPOSED engine after an engine swap, so every write would raise,
    get swallowed by ``_meter``, and under-count spend with only a log line to show.
    """
    if override is not None:
        return override
    from app.core.db import get_sessionmaker

    return get_sessionmaker()


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
        settings: Settings | None = None,
    ) -> None:
        self._inner = inner
        # Both are explicit overrides (tests) or ``None`` to resolve per call — see
        # ``_resolve_settings`` / ``_resolve_sessionmaker`` for why neither is cached.
        self._settings = settings
        self._sessionmaker = sessionmaker

    @property
    def inner(self) -> LLMClient:
        """The wrapped provider client.

        Exposed so callers that legitimately need the concrete provider — tests
        asserting which provider a config selects, and any future health check —
        can see through the decorator instead of being defeated by it.
        """
        return self._inner

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        """Admit, call, meter — in that order.

        Admission runs BEFORE the provider call, which is the only ordering that
        can actually prevent spend; checking afterwards would merely record it.
        The budget check raises :class:`~app.llm.errors.CostLimitReached`, a
        transient failure, never a content refusal (#142).

        The concurrency slot is held across the call so the cap bounds calls
        IN FLIGHT, not calls started. Releasing before the await would make it a
        rate of admission rather than a ceiling on concurrency.
        """
        settings = _resolve_settings(self._settings)
        await enforce_budget(_resolve_sessionmaker(self._sessionmaker), settings)
        async with get_semaphore(settings):
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
            await record_call(_resolve_sessionmaker(self._sessionmaker), call)
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


class MeteredEmbedder:
    """Wraps an :class:`Embedder` and records the cost of each embedding call.

    Mirrors :class:`MeteredLLMClient` — admit, call, meter, never break the caller —
    with three differences that come from the seam itself:

    * **The provider and model are not in the result.** ``embed`` returns a bare
      ``list[float]``, so the identity is taken from the ``Settings`` the embedder
      was BUILT from. That is exact by construction: the same three values are what
      :func:`app.embeddings.factory.configured_embedder_identity` calls the vector
      space's identity, and the singleton is built from them.
    * **Token counts arrive out-of-band.** See :mod:`app.cost.usage`.
    * **The call site is derived from the METHOD**, not from the caller. ``embed``
      is only ever the query path and ``embed_documents`` is only ever ingest, so
      the wrapper knows the label better than the call site does — and cannot forget
      to set it. An ambient label still wins (``eval`` around a harness run), so the
      derived default is a floor, not an override.

    Embeddings are billed on INPUT ONLY; ``output_tokens`` is recorded as 0 rather
    than as an estimate of something that does not exist.
    """

    def __init__(
        self,
        inner: Embedder,
        *,
        provider: str,
        model: str,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._inner = inner
        # Canonicalised here, once, so the recorded provider column and the price
        # book key can never disagree about how a vendor is spelled.
        self._provider = canonical_provider(provider)
        self._model = model
        # Resolved per call when ``None``, exactly as in :class:`MeteredLLMClient`.
        self._settings = settings
        self._sessionmaker = sessionmaker

    @property
    def inner(self) -> Embedder:
        """The wrapped provider client, for callers that need the concrete type."""
        return self._inner

    @property
    def dim(self) -> int:
        """Delegate the vector dimension.

        Not optional: retrieval and the ingest runner both read ``dim`` off the
        embedder, and a decorator that swallowed it would break the vector arm.
        """
        return self._inner.dim

    async def embed(self, text: str) -> list[float]:
        """Embed one query. Metered as ``CallSite.answer``."""
        return await self._run(
            lambda: self._inner.embed(text), chars=len(text), default_site=CallSite.answer
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of chunks. Metered as ``CallSite.ingest``."""
        return await self._run(
            lambda: self._inner.embed_documents(texts),
            chars=sum(len(t) for t in texts),
            default_site=CallSite.ingest,
        )

    async def _run[T](
        self, call: Callable[[], Awaitable[T]], *, chars: int, default_site: CallSite
    ) -> T:
        """Admit, call, meter — in that order (see :meth:`MeteredLLMClient.complete`)."""
        settings = _resolve_settings(self._settings)
        if chars == 0:
            # ``embed_documents([])`` returns without touching the provider, and
            # ``embed("")`` is rejected locally by every real client. Neither spends
            # anything, so neither should consume budget or produce a spend row —
            # a phantom $0 row would be indistinguishable from a real free call.
            return await call()
        await enforce_budget(_resolve_sessionmaker(self._sessionmaker), settings)
        with collect_embedding_usage() as usage:
            async with get_semaphore(settings):
                result = await call()
        await self._meter(usage, chars=chars, default_site=default_site)
        return result

    async def _meter(self, usage: EmbeddingUsage, *, chars: int, default_site: CallSite) -> None:
        """Record one embedding call. Never raises — metering must not break ingest."""
        try:
            input_tokens, estimated = _resolve_embedding_tokens(usage, chars=chars)
            ambient = get_call_site()
            row = build_call(
                kind="embedding",
                provider=self._provider,
                model=self._model,
                input_tokens=input_tokens,
                output_tokens=0,
                attempts=max(1, usage.requests),
                tokens_estimated=estimated,
                request_id=_current_request_id(),
                # An explicitly-labelled block (the eval harness) outranks the
                # method-derived default; ``unknown`` means nobody labelled it.
                call_site=default_site if ambient is CallSite.unknown else ambient,
            )
            await record_call(_resolve_sessionmaker(self._sessionmaker), row)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - see MeteredLLMClient._meter
            _logger.exception("embedding_metering_failed")

    async def aclose(self) -> None:
        """Delegate resource cleanup to the wrapped client, if it has any.

        ``aclose`` is not part of the :class:`Embedder` protocol (the stub owns no
        resources), so it is delegated only when the inner client actually has one —
        otherwise wrapping a resource-less embedder would turn shutdown into an
        AttributeError.
        """
        aclose = getattr(self._inner, "aclose", None)
        if not callable(aclose):
            return
        result = aclose()
        # Mirrors ``app.embeddings.factory.shutdown_embedder``: an implementation is
        # free to define ``aclose`` synchronously, and awaiting a non-awaitable would
        # turn a clean shutdown into a TypeError.
        if inspect.isawaitable(result):
            await result


def _resolve_embedding_tokens(usage: EmbeddingUsage, *, chars: int) -> tuple[int, bool]:
    """Return ``(input_tokens, estimated)`` for one embedding call.

    Provider-reported counts win whenever EVERY request came back with them:
    OpenAI-compatible ``/embeddings`` responses carry ``usage.prompt_tokens``, and
    metering a known number as a guess would flag half the table ``estimated`` for
    nothing.

    A partially-reported call (one sub-batch answered without a usage block, or a
    provider like Gemini that never sends one) is flagged as an estimate AND takes
    ``max(reported, estimate)``. Both halves matter: the flag keeps a guess
    distinguishable from a fact, and the ``max`` keeps the guess from landing BELOW
    the tokens we already know were billed — under-counting is the failure mode that
    costs money, over-counting only trips the limit early.
    """
    estimate = max(1, chars // _CHARS_PER_TOKEN)
    if usage.fully_reported:
        return usage.input_tokens, False
    return max(estimate, usage.input_tokens), True


__all__ = ["MeteredEmbedder", "MeteredLLMClient"]
