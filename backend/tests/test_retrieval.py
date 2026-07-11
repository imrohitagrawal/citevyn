"""Retrieval layer tests.

Exercises the three orthogonal retrievers (exact, keyword, vector)
against the seeded catalog, then verifies the hybrid orchestrator
fuses the scores and short-circuits the exact-lookup intent.
"""

from __future__ import annotations

import pytest

from app.guardrails.domain import Domain
from app.retrieval.exact import ExactRetriever
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.keyword import KeywordRetriever
from app.retrieval.rerank import Reranker
from app.retrieval.vector import StubEmbedder
from app.routing.intent import Intent

pytestmark = pytest.mark.asyncio


async def test_exact_retriever_finds_env_var(seeded_session) -> None:
    r = ExactRetriever(seeded_session, active_index_version="v1")
    hits = await r.retrieve("CLAUDE_API_RATE_LIMIT", product_area=Domain.claude_api.value)
    assert len(hits) == 1
    assert hits[0].product_area == "claude_api"
    assert "rate limit" in hits[0].chunk_text.lower()
    assert hits[0].score == 1.0


async def test_exact_retriever_filters_deprecated_docs(session) -> None:
    """Inactive documents must not surface in retrieval."""
    from tests.conftest import seed_catalog

    catalog = await seed_catalog(session)
    claude_api_doc = next(d for d in catalog["docs"] if d.product_area == "claude_api")
    claude_api_doc.status = "deprecated"  # type: ignore[assignment]
    await session.commit()

    r = ExactRetriever(session, active_index_version="v1")
    hits = await r.retrieve("CLAUDE_API_RATE_LIMIT", product_area=Domain.claude_api.value)
    assert hits == []


async def test_exact_retriever_filters_inactive_index_version(seeded_session) -> None:
    r = ExactRetriever(seeded_session, active_index_version="v999")
    hits = await r.retrieve("CLAUDE_API_RATE_LIMIT", product_area=Domain.claude_api.value)
    assert hits == []


async def test_keyword_retriever_filters_by_domain(seeded_session) -> None:
    r = KeywordRetriever(seeded_session, active_index_version="v1")
    hits = await r.retrieve("model", product_area=Domain.codex.value)
    assert len(hits) >= 1
    assert all(h.product_area == "codex" for h in hits)


async def test_keyword_retriever_empty_for_stopwords_only(seeded_session) -> None:
    r = KeywordRetriever(seeded_session, active_index_version="v1")
    assert await r.retrieve("how is the", product_area=Domain.claude_api.value) == []


async def test_stub_embedder_deterministic() -> None:
    e = StubEmbedder(dim=8)
    a = await e.embed("hello world")
    b = await e.embed("hello world")
    assert a == b
    assert len(a) == 8


async def test_stub_embedder_unit_norm() -> None:
    import math

    e = StubEmbedder(dim=16)
    v = await e.embed("anything goes here")
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-6


async def test_vector_retriever_returns_empty_on_sqlite(seeded_session) -> None:
    """Vector retrieval is pgvector-only; on SQLite the retriever must
    return ``[]`` cleanly so the rest of the pipeline still works."""
    from app.retrieval.vector import VectorRetriever

    r = VectorRetriever(
        seeded_session,
        active_index_version="v1",
        embedder=StubEmbedder(dim=8),
    )
    assert await r.retrieve("anything", product_area=Domain.claude_api.value) == []


async def test_vector_retriever_returns_empty_without_embedder(seeded_session) -> None:
    from app.retrieval.vector import VectorRetriever

    r = VectorRetriever(seeded_session, active_index_version="v1")
    assert await r.retrieve("anything", product_area=Domain.claude_api.value) == []


async def test_hybrid_degrades_when_embedder_unavailable(seeded_session) -> None:
    """A transient embedder outage degrades the vector arm to [] with a WARN,
    so exact+keyword still answer instead of the whole query 500-ing (#51 review).

    A handler is attached directly to the ``citevyn.retrieval`` logger (rather than
    caplog, which depends on propagation/root state that other tests mutate) so the
    "logged, not silent" assertion is deterministic under any test ordering."""
    import logging

    from app.embeddings import EmbedderUnavailable

    class _RaisingVector:
        async def retrieve(self, question, *, product_area, limit):
            raise EmbedderUnavailable("Gemini embeddings returned 503")

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("citevyn.retrieval")
    handler = _Capture()
    logger.addHandler(handler)
    # Neutralise any logging-config side effect from earlier tests (a dependency's
    # dictConfig can flip .disabled on pre-existing loggers). Production only calls
    # basicConfig, which never disables loggers, so this is test hygiene, not a
    # behavior change.
    prev_level, logger.level = logger.level, logging.WARNING
    prev_disabled, logger.disabled = logger.disabled, False
    try:
        h = HybridRetriever(seeded_session, active_index_version="v1")
        result = await h._safe_vector_retrieve(
            _RaisingVector(),  # type: ignore[arg-type]
            "anything",
            product_area=Domain.claude_api.value,
            limit=5,
        )
    finally:
        logger.removeHandler(handler)
        logger.level = prev_level
        logger.disabled = prev_disabled

    assert result == []
    assert any("vector_retrieval_degraded" in r.getMessage() for r in records)


async def test_hybrid_does_not_swallow_generic_errors(seeded_session) -> None:
    """A non-EmbedderUnavailable error is a real failure and must propagate."""

    class _BrokenVector:
        async def retrieve(self, question, *, product_area, limit):
            raise RuntimeError("database exploded")

    h = HybridRetriever(seeded_session, active_index_version="v1")
    with pytest.raises(RuntimeError, match="database exploded"):
        await h._safe_vector_retrieve(
            _BrokenVector(),  # type: ignore[arg-type]
            "anything",
            product_area=Domain.claude_api.value,
            limit=5,
        )


async def test_hybrid_short_circuits_on_exact_lookup(seeded_session) -> None:
    h = HybridRetriever(seeded_session, active_index_version="v1")
    hits = await h.retrieve(
        "CLAUDE_API_RATE_LIMIT",
        product_area=Domain.claude_api.value,
        intent=Intent.exact_lookup,
    )
    assert len(hits) >= 1
    assert hits[0].retrieval_type.value == "exact"


async def test_hybrid_exact_lookup_falls_back_when_no_exact_hit(seeded_session) -> None:
    # PRD §3.2 answer-flow step 3: an exact_lookup question whose exact-term
    # index misses must fall back to keyword/vector, not return [] (which the
    # orchestrator turns into no_answer). ``ExactRetriever`` matches only when
    # the whole normalized question equals a term_text, so a natural-language
    # question never hits exact — but "rate" is a keyword in the seeded chunk.
    # Regression guard: reverting the fall-through (returning only exact hits)
    # would make this return [] and fail.
    h = HybridRetriever(seeded_session, active_index_version="v1")
    hits = await h.retrieve(
        "explain the rate limit behaviour please",
        product_area=Domain.claude_api.value,
        intent=Intent.exact_lookup,
    )
    assert len(hits) >= 1
    # It fell through to keyword/vector — no exact-typed hit.
    assert all(hit.retrieval_type.value != "exact" for hit in hits)


async def test_hybrid_merges_keyword_and_exact(seeded_session) -> None:
    h = HybridRetriever(seeded_session, active_index_version="v1")
    # Two questions; we run them separately and confirm the hybrid
    # orchestrator returns the same chunk via either path, and that
    # a query with both an exact term and a keyword wins on score.
    exact_hits = await h.retrieve(
        "CLAUDE_API_RATE_LIMIT",
        product_area=Domain.claude_api.value,
        intent=Intent.faq,
    )
    keyword_hits = await h.retrieve(
        "rate",
        product_area=Domain.claude_api.value,
        intent=Intent.faq,
    )
    assert exact_hits and keyword_hits
    # chunk found by both retrievers should outscore a single-retriever hit
    keyword_only_score = keyword_hits[0].score
    assert exact_hits[0].score > keyword_only_score


async def test_hybrid_respects_domain_filter(seeded_session) -> None:
    h = HybridRetriever(seeded_session, active_index_version="v1")
    hits = await h.retrieve(
        "rate",
        product_area=Domain.codex.value,
        intent=Intent.faq,
    )
    assert all(h_.product_area == "codex" for h_ in hits)
    # The codex doc has no "rate" term, so this should be empty.
    assert hits == []


async def test_reranker_passthrough() -> None:
    from uuid import uuid4

    from app.retrieval.types import EvidenceHit

    hits = [
        EvidenceHit(
            chunk_id=uuid4(),
            document_id=uuid4(),
            product_area="claude_api",
            source_name="docs.test",
            document_title="t",
            section_path="/",
            heading="h",
            chunk_text="x",
            context_summary="x",
            source_url="https://x",
            score=1.0,
        )
        for _ in range(5)
    ]
    r = Reranker()
    out = await r.rerank("q", hits, top_k=3)
    assert len(out) == 3
