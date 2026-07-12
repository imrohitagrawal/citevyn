"""Retrieval layer tests.

Exercises the three orthogonal retrievers (exact, keyword, vector)
against the seeded catalog, then verifies the hybrid orchestrator
fuses the scores and short-circuits the exact-lookup intent.
"""

from __future__ import annotations

import contextlib

import pytest

from app.embeddings import EmbedderIdentity
from app.guardrails.domain import Domain
from app.retrieval.exact import ExactRetriever
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.keyword import KeywordRetriever
from app.retrieval.rerank import Reranker
from app.retrieval.types import VectorDegrade
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

    # _safe_vector_retrieve returns (chunks, degrade); a transient outage reports
    # the Tier-1 ``unavailable`` reason.
    assert result == ([], VectorDegrade.unavailable)
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
    result = await h.retrieve(
        "CLAUDE_API_RATE_LIMIT",
        product_area=Domain.claude_api.value,
        intent=Intent.exact_lookup,
    )
    hits = result.hits
    assert len(hits) >= 1
    assert hits[0].retrieval_type.value == "exact"
    # The exact-lookup short-circuit never consults the vector arm, so it is not
    # degraded — the orchestrator caches this embedder-independent answer (#72).
    assert result.vector_degrade is VectorDegrade.none


async def test_hybrid_exact_lookup_falls_back_when_no_exact_hit(seeded_session) -> None:
    # PRD §3.2 answer-flow step 3: an exact_lookup question whose exact-term
    # index misses must fall back to keyword/vector, not return [] (which the
    # orchestrator turns into no_answer). ``ExactRetriever`` matches only when
    # the whole normalized question equals a term_text, so a natural-language
    # question never hits exact — but "rate" is a keyword in the seeded chunk.
    # Regression guard: reverting the fall-through (returning only exact hits)
    # would make this return [] and fail.
    h = HybridRetriever(seeded_session, active_index_version="v1")
    hits = (
        await h.retrieve(
            "explain the rate limit behaviour please",
            product_area=Domain.claude_api.value,
            intent=Intent.exact_lookup,
        )
    ).hits
    assert len(hits) >= 1
    # It fell through to keyword/vector — no exact-typed hit.
    assert all(hit.retrieval_type.value != "exact" for hit in hits)


async def test_hybrid_merges_keyword_and_exact(seeded_session) -> None:
    h = HybridRetriever(seeded_session, active_index_version="v1")
    # Two questions; we run them separately and confirm the hybrid
    # orchestrator returns the same chunk via either path, and that
    # a query with both an exact term and a keyword wins on score.
    exact_hits = (
        await h.retrieve(
            "CLAUDE_API_RATE_LIMIT",
            product_area=Domain.claude_api.value,
            intent=Intent.faq,
        )
    ).hits
    keyword_hits = (
        await h.retrieve(
            "rate",
            product_area=Domain.claude_api.value,
            intent=Intent.faq,
        )
    ).hits
    assert exact_hits and keyword_hits
    # chunk found by both retrievers should outscore a single-retriever hit
    keyword_only_score = keyword_hits[0].score
    assert exact_hits[0].score > keyword_only_score


async def test_hybrid_respects_domain_filter(seeded_session) -> None:
    h = HybridRetriever(seeded_session, active_index_version="v1")
    hits = (
        await h.retrieve(
            "rate",
            product_area=Domain.codex.value,
            intent=Intent.faq,
        )
    ).hits
    assert all(h_.product_area == "codex" for h_ in hits)
    # The codex doc has no "rate" term, so this should be empty.
    assert hits == []


# ---------------------------------------------------------------------------
# Tier 3 enforcement (#57): degrade the vector arm when the active index was
# built by a different embedder than the one configured to embed queries.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _capture_retrieval_logs():
    """Attach a handler directly to the ``citevyn.retrieval`` logger.

    Mirrors ``test_hybrid_degrades_when_embedder_unavailable``: caplog depends on
    propagation/root state that a dependency's ``dictConfig`` can flip mid-suite,
    so we capture on the concrete logger and force it enabled for determinism.
    """
    import logging

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("citevyn.retrieval")
    handler = _Capture()
    logger.addHandler(handler)
    prev_level, logger.level = logger.level, logging.WARNING
    prev_disabled, logger.disabled = logger.disabled, False
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.level = prev_level
        logger.disabled = prev_disabled


async def _stamp_active_index(session, *, provider, model, dim) -> None:
    """Set the embedding provenance on the seeded active ``IndexVersion`` (``v1``)."""
    from app.models import IndexVersion

    row = await session.get(IndexVersion, "v1")
    assert row is not None
    row.embedding_provider = provider
    row.embedding_model = model
    row.embedding_dim = dim
    await session.flush()


_GEMINI = EmbedderIdentity(provider="gemini", model="gemini-embedding-001", dim=1536)


async def test_vector_arm_enabled_when_stamp_matches(seeded_session) -> None:
    await _stamp_active_index(
        seeded_session, provider="gemini", model="gemini-embedding-001", dim=1536
    )
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_enabled() is True
    assert not any("mismatch" in r.getMessage() for r in records)


async def test_vector_arm_degrades_on_stamp_mismatch(seeded_session) -> None:
    # Same dim, different provider/model: the dimension guard does NOT fire, so
    # without this enforcement the corrupted rankings would be served silently.
    await _stamp_active_index(seeded_session, provider="stub", model="stub", dim=1536)
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_enabled() is False
    msgs = [r.getMessage() for r in records]
    assert any("vector_retrieval_index_embedder_mismatch" in m for m in msgs)


async def test_vector_arm_mismatch_warn_carries_identifiers(seeded_session) -> None:
    # The WARN's operational value is its structured ``extra`` payload — assert it
    # carries the stamped-vs-configured provider/model/dim (identifiers only).
    await _stamp_active_index(seeded_session, provider="stub", model="stub", dim=1536)
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_enabled() is False
    rec = next(r for r in records if "vector_retrieval_index_embedder_mismatch" in r.getMessage())
    assert rec.index_embedding_provider == "stub"
    assert rec.configured_embedding_provider == "gemini"
    assert rec.configured_embedding_model == "gemini-embedding-001"
    assert rec.configured_embedding_dim == 1536


async def test_vector_arm_degrades_on_dim_only_mismatch(seeded_session) -> None:
    # Same provider AND model, only the dim differs. The boot guard pins the
    # configured dim to the pgvector column width, but the index could have been
    # stamped at a different dim; ``EmbedderIdentity`` compares all three fields,
    # so this must still degrade — it is exactly the silent-corruption case.
    await _stamp_active_index(
        seeded_session, provider="gemini", model="gemini-embedding-001", dim=768
    )
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_enabled() is False
    assert any("vector_retrieval_index_embedder_mismatch" in r.getMessage() for r in records)


async def test_vector_arm_degrades_on_partial_stamp(seeded_session) -> None:
    # A half-written stamp (provider set, model/dim NULL) is not "unknown
    # provenance" (only a NULL *provider* is), so it does not match a fully
    # populated identity and must fail closed → degrade, not silently allow.
    await _stamp_active_index(seeded_session, provider="gemini", model=None, dim=None)
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_enabled() is False
    assert any("vector_retrieval_index_embedder_mismatch" in r.getMessage() for r in records)


async def test_vector_arm_allowed_on_null_stamp(seeded_session) -> None:
    # The seeded ``v1`` index carries no provenance stamp (legacy / stub-seeded
    # demo). Unknown provenance must be ALLOWED, or the demo and every pre-#51
    # index would break.
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_enabled() is True
    assert not any("mismatch" in r.getMessage() for r in records)


async def test_gate_delegates_to_shared_predicate(seeded_session, monkeypatch) -> None:
    """Single-source-of-truth guard (#71): the canonical read-time gate
    ``HybridRetriever._vector_arm_enabled`` (#57) must DELEGATE its allow/degrade
    comparison to the shared ``is_index_embedder_mismatch`` predicate (#65), not
    carry an inline copy. Post-#71, agreement with the orchestrator's predictor is
    guaranteed by construction; this asserts the delegation directly so a future
    edit that reintroduces an inline copy — and could drift from the predictor —
    is caught. We spy on the predicate: for every stamp the gate calls it exactly
    once with ``(configured_identity, resolved_stamp)`` and returns ``enabled ==
    (not mismatch)`` (with the enforcement-off short-circuit still gate-side)."""
    import app.retrieval.hybrid as hybrid_mod
    from app.embeddings import is_index_embedder_mismatch

    stamps = [
        dict(provider="gemini", model="gemini-embedding-001", dim=1536),  # exact match
        dict(provider="stub", model="stub", dim=1536),  # provider/model swap, same dim
        dict(provider="gemini", model="gemini-embedding-001", dim=768),  # dim-only
        dict(provider="gemini", model=None, dim=None),  # partial stamp (fail closed)
        dict(provider=None, model=None, dim=None),  # NULL provider (unknown ⇒ allow)
    ]
    for stamp_cfg in stamps:
        await _stamp_active_index(seeded_session, **stamp_cfg)
        h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
        resolved = await h._active_index_stamp()

        calls: list[tuple] = []

        def _spy(configured, index_stamp, *, _calls=calls):
            _calls.append((configured, index_stamp))
            return is_index_embedder_mismatch(configured, index_stamp)

        monkeypatch.setattr(hybrid_mod, "is_index_embedder_mismatch", _spy)
        with _capture_retrieval_logs():
            enabled = await h._vector_arm_enabled()

        # The gate routed its decision through the shared predicate, exactly once,
        # with the configured identity and the resolved active-index stamp.
        assert calls == [(_GEMINI, resolved)], f"gate did not delegate for {stamp_cfg}"
        assert enabled == (not is_index_embedder_mismatch(_GEMINI, resolved)), stamp_cfg


async def test_vector_arm_enforcement_off_without_identity(seeded_session) -> None:
    # No identity wired ⇒ enforcement is off, even against a mismatching stamp.
    await _stamp_active_index(seeded_session, provider="stub", model="stub", dim=1536)
    h = HybridRetriever(seeded_session, active_index_version="v1")
    assert await h._vector_arm_enabled() is True


async def test_vector_arm_allowed_when_no_active_index(session) -> None:
    # Empty catalog (no active IndexVersion) ⇒ nothing to enforce against ⇒ allow.
    h = HybridRetriever(session, embedder_identity=_GEMINI)
    assert await h._vector_arm_enabled() is True


async def test_safe_vector_retrieve_skips_when_disabled(seeded_session) -> None:
    # A disabled vector arm must not embed or query — it returns [] directly.
    class _ExplodingVector:
        async def retrieve(self, question, *, product_area, limit):
            raise AssertionError("vector arm must not run when disabled")

    h = HybridRetriever(seeded_session, active_index_version="v1")
    result = await h._safe_vector_retrieve(
        _ExplodingVector(),  # type: ignore[arg-type]
        "anything",
        product_area=Domain.claude_api.value,
        limit=5,
        enabled=False,
    )
    # A disabled arm degrades to no hits with the Tier-3 ``mismatch`` reason.
    assert result == ([], VectorDegrade.mismatch)


async def test_safe_vector_retrieve_reports_not_degraded_on_success(seeded_session) -> None:
    # A live vector arm that actually runs reports degraded=False even when it
    # legitimately finds no chunks — a genuine empty result must be distinguishable
    # from a degrade so the orchestrator caches a normally-retrieved answer.
    from app.retrieval.types import RetrievedChunk

    class _EmptyVector:
        async def retrieve(self, question, *, product_area, limit) -> list[RetrievedChunk]:
            return []

    h = HybridRetriever(seeded_session, active_index_version="v1")
    result = await h._safe_vector_retrieve(
        _EmptyVector(),  # type: ignore[arg-type]
        "anything",
        product_area=Domain.claude_api.value,
        limit=5,
        enabled=True,
    )
    assert result == ([], VectorDegrade.none)


async def test_hybrid_retrieve_answers_from_keyword_on_mismatch(seeded_session) -> None:
    # End-to-end: a provenance mismatch degrades the vector arm but the request
    # still answers from exact/keyword — it does not raise or return [].
    await _stamp_active_index(seeded_session, provider="stub", model="stub", dim=1536)
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        result = await h.retrieve("rate", product_area=Domain.claude_api.value, intent=Intent.faq)
    hits = result.hits
    assert hits  # keyword still answers
    assert all(hit.retrieval_type.value != "vector" for hit in hits)
    assert any("vector_retrieval_index_embedder_mismatch" in r.getMessage() for r in records)
    # The vector arm was consulted and degraded on the mismatch — the runtime
    # reason is ``mismatch`` so the orchestrator skips caching this weaker answer
    # (#65) and labels the skip as an embedder mismatch.
    assert result.vector_degrade is VectorDegrade.mismatch


async def test_exact_lookup_fallthrough_still_degrades_on_mismatch(seeded_session) -> None:
    # Boundary of the #72 carve-out: the exact_lookup SHORT-CIRCUIT (exact hit
    # present) is embedder-independent and reports ``none``, but an exact_lookup
    # question whose exact arm MISSES falls THROUGH to the hybrid path and DOES
    # consult the vector arm — so under a mismatch it must still degrade
    # (``mismatch``), exactly like a non-exact query. Guards against the carve-out
    # accidentally widening to all exact_lookup intents (which would freeze a
    # degraded answer). "explain the rate limit" is exact_lookup-classified by the
    # caller yet matches no exact term, and "rate" keyword-hits the seeded chunk.
    await _stamp_active_index(seeded_session, provider="stub", model="stub", dim=1536)
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        result = await h.retrieve(
            "explain the rate limit behaviour please",
            product_area=Domain.claude_api.value,
            intent=Intent.exact_lookup,
        )
    assert result.hits  # keyword still answers on the fall-through
    assert all(hit.retrieval_type.value == "keyword" for hit in result.hits)
    assert any("vector_retrieval_index_embedder_mismatch" in r.getMessage() for r in records)
    # The fall-through consulted the vector arm and it degraded — NOT the #72
    # short-circuit, so it must report a real degrade, not ``none``.
    assert result.vector_degrade is VectorDegrade.mismatch


# ---------------------------------------------------------------------------
# #58: scope the read path to the active index version. With two index versions
# present (one active, one prior ``previous_good``) whose ``Document`` rows are
# BOTH ``status=active``, retrieval must return ONLY active-version documents —
# old-vector-space chunks from a prior version must never mix into ranking, or
# the ADR-0003 failover invariant breaks. Exercised via the KEYWORD arm because
# the vector arm is inert on SQLite.
# ---------------------------------------------------------------------------


async def _seed_two_versions(session) -> tuple:
    """Seed an ACTIVE (``v-active``) and a PRIOR (``v-old``, ``previous_good``)
    index version, each with one codex doc whose chunk shares the marker keyword
    ``zorptastic``. BOTH docs are ``status=active`` — the exact coexistence the
    #58 read-scoping fix must disambiguate. Returns ``(active_doc_id, old_doc_id)``.
    """
    import uuid
    from datetime import UTC, datetime, timedelta

    from app.models import Chunk, Document, DocumentStatus, IndexStatus, IndexVersion

    now = datetime.now(UTC)
    session.add_all(
        [
            IndexVersion(
                index_version="v-old",
                status=IndexStatus.previous_good,
                source_version_hash="sha256:v-old",
                created_at=now - timedelta(hours=1),
                promoted_at=now - timedelta(hours=1),
            ),
            IndexVersion(
                index_version="v-active",
                status=IndexStatus.active,
                source_version_hash="sha256:v-active",
                created_at=now,
                promoted_at=now,
            ),
        ]
    )
    await session.flush()

    doc_ids: dict[str, uuid.UUID] = {}
    for version in ("v-old", "v-active"):
        doc = Document(
            document_id=uuid.uuid4(),
            index_version=version,
            source_name="codex",
            product_area="codex",
            source_url=f"https://docs.example.com/{version}",
            title=f"Codex {version}",
            content_checksum=f"sha256:{version}",
            last_fetched_at=now,
            last_indexed_at=now,
            status=DocumentStatus.active,  # BOTH active — the coexistence bug
        )
        session.add(doc)
        await session.flush()
        session.add(
            Chunk(
                chunk_id=uuid.uuid4(),
                document_id=doc.document_id,
                product_area="codex",
                section_path="/x",
                heading="H",
                parent_heading=None,
                chunk_text=f"The codex zorptastic behaviour in {version}.",
                context_summary="zorptastic",
                exact_terms=[],
                chunk_order=0,
                content_checksum=f"sha256:{version}-chunk",
            )
        )
        await session.flush()
        doc_ids[version] = doc.document_id
    await session.commit()
    return doc_ids["v-active"], doc_ids["v-old"]


async def test_hybrid_scopes_to_active_index_version(session) -> None:
    # Regression: fails before the fix (both docs returned), passes after.
    active_doc, old_doc = await _seed_two_versions(session)
    h = HybridRetriever(session, active_index_version="v-active")
    hits = (await h.retrieve("zorptastic", product_area=Domain.codex.value, intent=Intent.faq)).hits
    assert hits, "the active-version chunk must be found"
    doc_ids = {h_.document_id for h_ in hits}
    assert active_doc in doc_ids
    assert old_doc not in doc_ids, "prior-version doc must never mix into ranking"


async def test_hybrid_no_active_index_returns_status_active_docs(session) -> None:
    # The no-active-index case (``active_index_version=None``) must NOT filter to
    # nothing — it falls back to a status-only filter (pre-#58 behavior), so BOTH
    # status=active docs come back rather than an empty result.
    active_doc, old_doc = await _seed_two_versions(session)
    h = HybridRetriever(session, active_index_version=None)
    hits = (await h.retrieve("zorptastic", product_area=Domain.codex.value, intent=Intent.faq)).hits
    doc_ids = {h_.document_id for h_ in hits}
    assert active_doc in doc_ids and old_doc in doc_ids


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
