"""Per-call cost metering for the EMBEDDER (#153 Layer 1).

Mirrors ``tests/test_cost_metering.py``. The embedding seam has its own failure
modes, and these are the ones that decide whether the §9 budget can be trusted:

1. **The stub is never wrapped.** ``tests/eval/retrieval.py`` and
   ``tests/eval/distractors.py`` branch on ``isinstance(embedder, StubEmbedder)``
   to skip the vector arm on the free hermetic path. Behind a decorator that goes
   False and the eval silently reports hash-bucket "retrieval" numbers as if they
   were semantic.
2. **Both production construction sites are metered** — the API singleton AND the
   worker CLI, which builds its own embedder. Wrapping only the first would leave
   ingest, the burstiest embedding spend there is, invisible to the budget.
3. **Provider-reported tokens beat the estimate, and a partial report is flagged.**
   OpenAI-compatible ``/embeddings`` bodies carry ``usage.prompt_tokens``; the code
   used to discard it. An estimate that lands below the billed number under-counts,
   which is the failure that costs money.
4. **A budget stop is not swallowed by the vector arm's degrade path.** The
   retriever catches ``EmbedderUnavailable``; a ``CostLimitReached`` that fell into
   that net would turn a hard cost stop into a silent "no source".
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest

import app.cost.metered as metered_mod
from app.core.config import Settings
from app.cost.call_site import CallSite, call_site
from app.cost.metered import MeteredEmbedder, _resolve_embedding_tokens
from app.cost.pricing import canonical_provider, price_for
from app.cost.usage import (
    EmbeddingUsage,
    collect_embedding_usage,
    report_embedding_usage,
)
from app.embeddings.factory import build_embedder, metered_embedder
from app.embeddings.openrouter import OpenRouterEmbedder, _prompt_tokens
from app.embeddings.stub import StubEmbedder
from app.llm.errors import CostLimitReached

# Metering is exercised against a dummy sessionmaker, so the Layer-3 budget would
# correctly FAIL CLOSED against it. Disabled here so each test is about one thing;
# the budget-on-embeddings behaviour has its own section below with it ON.
_NO_BUDGET = Settings(llm_provider="stub", cost_budget_enabled=False)

_OR_SETTINGS = Settings(
    llm_provider="stub",
    embedding_provider="openrouter",
    embedding_model="openai/text-embedding-3-small",
    openrouter_api_key="or-test",
    cost_budget_enabled=False,
)


class _FakeEmbedder:
    """An embedder that optionally reports usage, like a real provider client."""

    def __init__(
        self, *, dim: int = 4, tokens_per_request: int | None = None, requests: int = 1
    ) -> None:
        self._dim = dim
        self._tokens = tokens_per_request
        self._requests = requests
        self.calls = 0
        self.closed = False

    @property
    def dim(self) -> int:
        return self._dim

    def _report(self) -> None:
        report_embedding_usage(input_tokens=self._tokens, requests=self._requests)

    async def embed(self, text: str) -> list[float]:
        del text
        self.calls += 1
        self._report()
        return [0.1] * self._dim

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self._report()
        return [[0.1] * self._dim for _ in texts]

    async def aclose(self) -> None:
        self.closed = True


def _metered(inner: object, **kw: object) -> MeteredEmbedder:
    params: dict[str, Any] = {
        "provider": "openrouter",
        "model": "openai/text-embedding-3-small",
        "sessionmaker": object(),
        "settings": _NO_BUDGET,
    }
    params.update(kw)
    return MeteredEmbedder(inner, **params)  # type: ignore[arg-type]


def _capture(fn) -> list[Any]:  # type: ignore[no-untyped-def]
    """Run ``fn`` with ``record_call`` swapped for a collector; return the rows."""
    rows: list[Any] = []

    async def _record(sm, call):  # type: ignore[no-untyped-def]
        rows.append(call)

    original = metered_mod.record_call
    metered_mod.record_call = _record  # type: ignore[assignment]
    try:
        fn()
    finally:
        metered_mod.record_call = original  # type: ignore[assignment]
    return rows


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_the_shipped_default_embedding_model_is_priced() -> None:
    """An unpriced default under-counts the budget from day one."""
    s = Settings(llm_provider="stub")
    assert price_for(provider="gemini", model=s.embedding_model) is not None, (
        f"default embedding_model {s.embedding_model!r} is not in the price book"
    )


def test_openai_embedding_price_is_two_cents_per_million_input_tokens() -> None:
    price = price_for(provider="openrouter", model="openai/text-embedding-3-small")
    assert price is not None
    assert price.cost_for(input_tokens=1_000_000, output_tokens=0) == Decimal("0.02")


def test_embeddings_bill_input_only() -> None:
    """Embeddings have no completion half; an output rate would invent spend."""
    for provider, model in (
        ("openrouter", "openai/text-embedding-3-small"),
        ("openrouter", "openai/text-embedding-3-large"),
        ("gemini", "gemini-embedding-001"),
    ):
        price = price_for(provider=provider, model=model)
        assert price is not None
        assert price.output_per_1m == Decimal(0)
        assert price.cost_for(input_tokens=0, output_tokens=10**9) == Decimal(0)


def test_the_large_embedding_tier_is_not_priced_as_the_small_one() -> None:
    """6.5x apart. Collapsing them is the sibling-tier bug that already shipped once
    on the LLM side."""
    small = price_for(provider="openrouter", model="openai/text-embedding-3-small")
    large = price_for(provider="openrouter", model="openai/text-embedding-3-large")
    assert small is not None and large is not None
    assert large.input_per_1m > small.input_per_1m * 5


def test_the_openrouter_provider_alias_is_canonicalised() -> None:
    """CITEVYN_EMBEDDING_PROVIDER says 'openrouter'; LLMResult.provider says
    'router'. Two names for one vendor splits every spend-by-provider report."""
    assert canonical_provider("openrouter") == "router"
    assert canonical_provider("gemini") == "gemini"
    assert canonical_provider("stub") == "stub"
    assert price_for(provider="openrouter", model="openai/gpt-4o-mini") == price_for(
        provider="router", model="openai/gpt-4o-mini"
    )


def test_an_unknown_embedding_model_is_still_unpriced_not_free() -> None:
    assert price_for(provider="openrouter", model="openai/text-embedding-9-huge") is None


# ---------------------------------------------------------------------------
# The factory seam — the safety-critical part
# ---------------------------------------------------------------------------


def test_the_stub_embedder_is_NOT_wrapped() -> None:
    """``tests/eval/{retrieval,distractors}.py`` gate the vector arm on
    ``isinstance(embedder, StubEmbedder)``. Behind a decorator that check goes
    False and the hermetic eval starts scoring a hash-bucket vector arm as if it
    were semantic retrieval."""
    settings = Settings(llm_provider="stub", embedding_provider="stub")
    wrapped = metered_embedder(build_embedder(settings), settings)
    assert isinstance(wrapped, StubEmbedder)
    assert not isinstance(wrapped, MeteredEmbedder)


def test_the_api_singleton_meters_a_real_embedder() -> None:
    from app.embeddings import factory as emb_factory

    emb_factory.reset_embedder()
    try:
        embedder = emb_factory.get_embedder(_OR_SETTINGS)
    finally:
        emb_factory.reset_embedder()
    assert isinstance(embedder, MeteredEmbedder)
    assert isinstance(embedder.inner, OpenRouterEmbedder)
    # build_embedder stays PURE — provider selection, not wiring.
    assert not isinstance(build_embedder(_OR_SETTINGS), MeteredEmbedder)


def test_the_singleton_still_returns_the_stub_itself_for_the_free_path() -> None:
    from app.embeddings import factory as emb_factory

    emb_factory.reset_embedder()
    try:
        embedder = emb_factory.get_embedder(Settings(llm_provider="stub"))
    finally:
        emb_factory.reset_embedder()
    assert isinstance(embedder, StubEmbedder)


def test_the_WORKER_meters_its_own_embedder() -> None:
    """The worker never touches the API singleton — it builds its own. Ingest is
    the burstiest embedding spend there is, so an unmetered worker is the biggest
    hole this layer could leave."""
    from app.worker import cli

    runner = cli._build_runner(_OR_SETTINGS, index_version="v-test")
    assert isinstance(runner._embedder, MeteredEmbedder)


def test_the_worker_does_NOT_wrap_the_stub_either() -> None:
    from app.worker import cli

    runner = cli._build_runner(Settings(llm_provider="stub"), index_version="v-test")
    assert isinstance(runner._embedder, StubEmbedder)


def test_the_wrapper_delegates_dim() -> None:
    """Retrieval and the ingest runner both read ``dim`` off the embedder."""
    assert _metered(_FakeEmbedder(dim=1536)).dim == 1536


# ---------------------------------------------------------------------------
# MeteredEmbedder behaviour
# ---------------------------------------------------------------------------


def test_the_query_path_records_an_embedding_row_and_returns_the_vector() -> None:
    inner = _FakeEmbedder(tokens_per_request=42)
    client = _metered(inner)
    result: list[list[float]] = []
    rows = _capture(lambda: result.append(asyncio.run(client.embed("how much does it cost?"))))

    assert result[0] == [0.1] * 4
    assert len(rows) == 1
    assert rows[0].kind == "embedding"
    assert rows[0].provider == "router"  # canonicalised from "openrouter"
    assert rows[0].model == "openai/text-embedding-3-small"
    assert rows[0].input_tokens == 42
    assert rows[0].output_tokens == 0
    assert rows[0].priced is True


def test_provider_reported_tokens_are_used_verbatim_and_not_flagged_estimated() -> None:
    """The whole point of plumbing usage through: a known number must not be
    recorded as a guess."""
    rows = _capture(
        lambda: asyncio.run(_metered(_FakeEmbedder(tokens_per_request=137)).embed("x" * 4000))
    )
    assert rows[0].input_tokens == 137, "the chars/4 estimate (1000) overwrote a billed count"
    assert rows[0].tokens_estimated is False


def test_a_provider_that_reports_no_usage_is_ESTIMATED_and_flagged() -> None:
    """Gemini's embed endpoints return no token counts at all. Recording 0 would
    look exactly like a free call and vanish from the budget."""
    rows = _capture(
        lambda: asyncio.run(_metered(_FakeEmbedder(tokens_per_request=None)).embed("x" * 400))
    )
    assert rows[0].tokens_estimated is True
    assert rows[0].input_tokens == 100  # 400 chars / 4
    assert rows[0].cost_usd > 0, "an estimated call must still contribute to the budget"


def test_a_zero_token_usage_block_is_treated_as_missing_not_as_free() -> None:
    """A provider reporting 0 tokens for text we definitely sent is a missing usage
    block wearing a number."""
    rows = _capture(
        lambda: asyncio.run(_metered(_FakeEmbedder(tokens_per_request=0)).embed("x" * 400))
    )
    assert rows[0].tokens_estimated is True
    assert rows[0].input_tokens == 100


def test_attempts_counts_the_provider_requests_including_retries() -> None:
    """A flaky provider costs up to 3x, and the budget under-counts exactly when it
    matters most."""
    rows = _capture(
        lambda: asyncio.run(
            _metered(_FakeEmbedder(tokens_per_request=10, requests=3)).embed("hello")
        )
    )
    assert rows[0].attempts == 3


def test_the_ingest_path_meters_the_whole_batch_as_one_row() -> None:
    client = _metered(_FakeEmbedder(tokens_per_request=900))
    rows = _capture(lambda: asyncio.run(client.embed_documents(["a" * 100, "b" * 100])))
    assert len(rows) == 1
    assert rows[0].call_site == "ingest"
    assert rows[0].input_tokens == 900


def test_the_call_site_is_derived_from_the_METHOD() -> None:
    """``embed`` is only ever the query path and ``embed_documents`` only ever
    ingest, so the wrapper knows the label better than the caller — and cannot
    forget to set it."""
    query = _capture(lambda: asyncio.run(_metered(_FakeEmbedder()).embed("q")))
    assert query[0].call_site == "answer"
    ingest = _capture(lambda: asyncio.run(_metered(_FakeEmbedder()).embed_documents(["d"])))
    assert ingest[0].call_site == "ingest"


def test_an_explicit_ambient_label_outranks_the_method_default() -> None:
    """An eval harness run must not be filed under 'answer' — the derived default
    is a floor, not an override."""

    async def _run() -> None:
        with call_site(CallSite.eval):
            await _metered(_FakeEmbedder()).embed("q")

    rows = _capture(lambda: asyncio.run(_run()))
    assert rows[0].call_site == "eval"


def test_metering_failure_does_not_break_the_embedding() -> None:
    """Losing the ROW is the acceptable loss; failing ingest over bookkeeping is not."""
    inner = _FakeEmbedder()

    async def _boom(sm, call):  # type: ignore[no-untyped-def]
        raise RuntimeError("database is on fire")

    original = metered_mod.record_call
    metered_mod.record_call = _boom  # type: ignore[assignment]
    try:
        vector = asyncio.run(_metered(inner).embed("still works"))
    finally:
        metered_mod.record_call = original  # type: ignore[assignment]
    assert vector == [0.1] * 4


def test_a_failed_embedding_is_not_metered() -> None:
    """A call that errored before the provider embedded anything was not billed."""

    class _Failing:
        dim = 4

        async def embed(self, text: str) -> list[float]:
            raise RuntimeError("provider 500")

    def _run() -> None:
        with pytest.raises(RuntimeError):
            asyncio.run(_metered(_Failing()).embed("boom"))

    assert _capture(_run) == []


def test_an_empty_batch_makes_no_call_and_records_nothing() -> None:
    """``embed_documents([])`` never reaches the provider; a $0 row for it would be
    indistinguishable from a real free call."""
    inner = _FakeEmbedder()
    rows = _capture(lambda: asyncio.run(_metered(inner).embed_documents([])))
    assert rows == []
    assert inner.calls == 1  # still delegated — the caller's contract is unchanged


def test_aclose_is_delegated() -> None:
    inner = _FakeEmbedder()
    asyncio.run(_metered(inner).aclose())
    assert inner.closed is True


def test_aclose_on_a_resourceless_embedder_does_not_raise() -> None:
    """``aclose`` is not part of the Embedder protocol; the stub has none."""

    class _NoClose:
        dim = 4

    asyncio.run(_metered(_NoClose()).aclose())


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def test_a_partially_reported_call_is_flagged_AND_takes_the_larger_number() -> None:
    """One sub-batch answered with usage, another did not. Recording only the
    reported fraction would under-count tokens we KNOW were billed."""
    usage = EmbeddingUsage(input_tokens=50, requests=2, reported_requests=1)
    tokens, estimated = _resolve_embedding_tokens(usage, chars=8000)
    assert estimated is True
    assert tokens == 2000  # the chars/4 estimate, which exceeds the partial report


def test_a_partial_report_never_lands_below_the_tokens_already_known_billed() -> None:
    usage = EmbeddingUsage(input_tokens=9000, requests=2, reported_requests=1)
    tokens, estimated = _resolve_embedding_tokens(usage, chars=400)
    assert estimated is True
    assert tokens == 9000


def test_a_fully_reported_call_is_a_fact_not_a_guess() -> None:
    usage = EmbeddingUsage(input_tokens=7, requests=2, reported_requests=2)
    assert _resolve_embedding_tokens(usage, chars=8000) == (7, False)


def test_usage_is_never_credited_across_concurrent_calls() -> None:
    """A module global would let one request's tokens land on another's row."""

    async def _one(tokens: int, delay: float) -> int:
        with collect_embedding_usage() as usage:
            await asyncio.sleep(delay)
            report_embedding_usage(input_tokens=tokens)
            await asyncio.sleep(delay)
            return usage.input_tokens

    async def _run() -> list[int]:
        return list(await asyncio.gather(_one(10, 0.02), _one(20, 0.01), _one(30, 0.0)))

    assert asyncio.run(_run()) == [10, 20, 30]


def test_reporting_without_a_collector_is_a_no_op() -> None:
    """Direct construction of a provider client (unit tests, the un-metered stub
    path) must not raise."""
    report_embedding_usage(input_tokens=5)


# ---------------------------------------------------------------------------
# The OpenAI-compatible usage block, end to end through the REAL client
# ---------------------------------------------------------------------------


def _body(vectors: list[list[float]], usage: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _openrouter(handler) -> OpenRouterEmbedder:  # type: ignore[no-untyped-def]
    return OpenRouterEmbedder(
        model="openai/text-embedding-3-small",
        api_key="or-test",
        api_base="https://openrouter.ai/api/v1",
        dim=4,
        timeout_seconds=5.0,
        max_retries=2,
        retry_backoff_seconds=0.0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_prompt_tokens_reads_the_usage_block() -> None:
    assert _prompt_tokens({"usage": {"prompt_tokens": 11, "total_tokens": 11}}) == 11
    # Aggregate-only providers still report something usable.
    assert _prompt_tokens({"usage": {"total_tokens": 9}}) == 9


def test_prompt_tokens_is_None_when_the_provider_omits_usage() -> None:
    assert _prompt_tokens({}) is None
    assert _prompt_tokens({"usage": None}) is None
    assert _prompt_tokens({"usage": {"prompt_tokens": 0}}) is None
    # A bookkeeping field must never be able to fail an embedding that succeeded.
    assert _prompt_tokens({"usage": {"prompt_tokens": "lots"}}) is None
    assert _prompt_tokens({"usage": {"prompt_tokens": True}}) is None


def test_the_REAL_openrouter_clients_usage_reaches_the_recorded_row() -> None:
    """The response field this change stopped discarding, proven end to end."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body([[0.5] * 4], {"prompt_tokens": 1234}))

    client = _metered(_openrouter(_handler))
    rows = _capture(lambda: asyncio.run(client.embed("q" * 4000)))
    assert rows[0].input_tokens == 1234
    assert rows[0].tokens_estimated is False
    assert rows[0].attempts == 1


def test_a_retried_openrouter_request_records_every_attempt() -> None:
    seen: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) < 3:
            return httpx.Response(503, json={"error": "upstream"})
        return httpx.Response(200, json=_body([[0.5] * 4], {"prompt_tokens": 7}))

    rows = _capture(lambda: asyncio.run(_metered(_openrouter(_handler)).embed("q")))
    assert rows[0].attempts == 3
    assert rows[0].input_tokens == 7


def test_a_multi_subbatch_ingest_sums_the_reported_tokens() -> None:
    """``embed_documents`` splits into sub-batches; each POST reports separately and
    the row must carry the TOTAL, not the last one."""

    def _handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        count = len(_json.loads(request.content)["input"])
        return httpx.Response(200, json=_body([[0.5] * 4] * count, {"prompt_tokens": 100}))

    texts = [f"chunk {i}" for i in range(200)]  # > _EMBED_BATCH_SIZE (96) → 3 POSTs
    rows = _capture(lambda: asyncio.run(_metered(_openrouter(_handler)).embed_documents(texts)))
    assert rows[0].attempts == 3
    assert rows[0].input_tokens == 300
    assert rows[0].tokens_estimated is False


def test_an_openrouter_body_without_usage_falls_back_to_the_flagged_estimate() -> None:
    """OpenRouter really does return 200 with no usage on some routed upstreams."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body([[0.5] * 4], None))

    rows = _capture(lambda: asyncio.run(_metered(_openrouter(_handler)).embed("q" * 400)))
    assert rows[0].tokens_estimated is True
    assert rows[0].input_tokens == 100


# ---------------------------------------------------------------------------
# Layer 3 — the budget gates embeddings too
# ---------------------------------------------------------------------------


def test_the_budget_gate_runs_BEFORE_the_provider_call() -> None:
    """Checking after the call would merely record the spend, not prevent it."""
    inner = _FakeEmbedder()

    async def _tripped(sm, settings):  # type: ignore[no-untyped-def]
        raise CostLimitReached("daily cap")

    original = metered_mod.enforce_budget
    metered_mod.enforce_budget = _tripped  # type: ignore[assignment]
    try:
        with pytest.raises(CostLimitReached):
            asyncio.run(
                MeteredEmbedder(
                    inner,
                    provider="openrouter",
                    model="openai/text-embedding-3-small",
                    sessionmaker=object(),  # type: ignore[arg-type]
                    settings=Settings(llm_provider="stub"),
                ).embed("q")
            )
    finally:
        metered_mod.enforce_budget = original  # type: ignore[assignment]
    assert inner.calls == 0, "the provider was called after the budget said stop"


def test_the_ingest_path_is_gated_too() -> None:
    """A corpus-wide re-ingest is the fastest way to blow a daily cap."""
    inner = _FakeEmbedder()

    async def _tripped(sm, settings):  # type: ignore[no-untyped-def]
        raise CostLimitReached("daily cap")

    original = metered_mod.enforce_budget
    metered_mod.enforce_budget = _tripped  # type: ignore[assignment]
    try:
        with pytest.raises(CostLimitReached):
            asyncio.run(
                MeteredEmbedder(
                    inner,
                    provider="openrouter",
                    model="openai/text-embedding-3-small",
                    sessionmaker=object(),  # type: ignore[arg-type]
                    settings=Settings(llm_provider="stub"),
                ).embed_documents(["a", "b"])
            )
    finally:
        metered_mod.enforce_budget = original  # type: ignore[assignment]
    assert inner.calls == 0


def test_a_cost_stop_is_NOT_swallowed_by_the_vector_arms_degrade_path() -> None:
    """``HybridRetriever._safe_vector_retrieve`` catches ``EmbedderUnavailable`` and
    degrades to zero hits. If a hard cost stop fell into that net it would surface
    as "no source" — a content refusal for an infrastructure stop, the #142 bug —
    instead of the transient 5xx the budget is specified to raise."""
    from app.embeddings.errors import EmbedderUnavailable
    from app.retrieval.hybrid import HybridRetriever

    assert not issubclass(CostLimitReached, EmbedderUnavailable)

    class _Vector:
        async def retrieve(self, question, *, product_area, limit):  # type: ignore[no-untyped-def]
            raise CostLimitReached("daily cap")

    retriever = HybridRetriever(session=object(), embedder=None)  # type: ignore[arg-type]
    with pytest.raises(CostLimitReached):
        asyncio.run(
            retriever._safe_vector_retrieve(
                _Vector(),  # type: ignore[arg-type]
                "q",
                product_area=None,
                limit=5,
                enabled=True,
            )
        )
