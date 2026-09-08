"""Retrieval layer tests.

Exercises the three orthogonal retrievers (exact, keyword, vector)
against the seeded catalog, then verifies the hybrid orchestrator
fuses the scores and short-circuits the exact-lookup intent.
"""

from __future__ import annotations

import contextlib

import pytest

from app.embeddings import EmbedderIdentity, IndexStampStatus
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


async def test_keyword_retriever_strips_trailing_punctuation(seeded_session) -> None:
    """A *matching* token carrying trailing '?' still matches after stripping.

    Regression: without ``rstrip("?!.,;:")`` the query "gemini header?"
    tokenizes "header?" and builds ``ILIKE '%header?%'`` — which matches
    nothing, leaving only "gemini" (one distinct token, below the >=2
    relevance floor), so the seeded gemini chunk (whose text mentions the
    ``x-goog-api-key header``) would be dropped and ``retrieve()`` would
    return ``[]``. Stripping restores "header" so both tokens match and the
    chunk is returned. Asserting the chunk IS returned makes this test fail
    if the stripping is ever reverted (the prior assertion "no hit contains
    'key?'" was vacuously true either way).
    """
    r = KeywordRetriever(seeded_session, active_index_version="v1")
    hits = await r.retrieve("gemini header?", product_area=Domain.gemini_api.value)
    assert hits, "punctuation-stripped 'header?' should match the seeded gemini chunk"
    assert any("header" in h.chunk_text.lower() for h in hits)


async def test_keyword_retriever_requires_two_token_matches(session) -> None:
    """A single-token hit on a sparse corpus is too weak to keep.

    Regression: ``gemini`` alone used to return the "Streaming" chunk
    as flat 0.5 evidence for the question "How do I get the gemini
    api key?" — pure noise. The relevance floor (≥2 distinct query
    tokens matched) keeps such single-token false positives out of
    the evidence list.
    """
    import uuid
    from datetime import UTC, datetime

    from app.models import Chunk, Document, DocumentStatus, IndexStatus, IndexVersion

    now = datetime.now(UTC)
    session.add(
        IndexVersion(
            index_version="v-floor",
            status=IndexStatus.active,
            source_version_hash="sha256:floor",
            created_at=now,
            promoted_at=now,
        )
    )
    doc = Document(
        document_id=uuid.uuid4(),
        index_version="v-floor",
        source_name="docs.test",
        product_area="gemini_api",
        source_url="https://docs.test/gemini-streaming",
        title="Gemini API",
        identity_checksum="sha256:demo-streaming",
        status=DocumentStatus.active,
        last_fetched_at=now,
        last_indexed_at=now,
    )
    session.add(doc)
    session.add(
        Chunk(
            chunk_id=uuid.uuid4(),
            document_id=doc.document_id,
            product_area=doc.product_area,
            section_path="/section",
            heading="Streaming",
            parent_heading=None,
            # Only ``gemini`` overlaps with the query tokens
            # ["obtain", "gemini", "credentials"] — "credentials" doesn't
            # appear here. Pre-fix this returned the chunk as flat 0.5
            # evidence (any single-token match was sufficient); post-fix
            # the 2-token floor rejects it because only 1 token matched.
            chunk_text=(
                "The Gemini SDK provides Go, Rust, and Swift bindings "
                "alongside the primary Python and Node.js clients."
            ),
            context_summary="Gemini SDK languages",
            chunk_order=1,
            content_checksum="sha256:demo-chunk-sdk",
        )
    )
    await session.flush()

    r = KeywordRetriever(session, active_index_version="v-floor")
    # Query tokens (after stopword removal) = ["obtain", "gemini",
    # "credentials"]. Only "gemini" appears in the chunk above, so the
    # 2-token floor should reject the hit.
    hits = await r.retrieve("Obtain Gemini credentials", product_area="gemini_api")
    assert hits == []


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


async def _add_second_active_index(session) -> None:
    """Drive the DB into the dual-active state (#58): add a second ``active`` row."""
    from datetime import UTC, datetime

    from app.models import IndexStatus, IndexVersion

    now = datetime.now(UTC)
    # ``v1`` is already active (seeded); add a second active row.
    session.add(
        IndexVersion(
            index_version="v2",
            status=IndexStatus.active,
            source_version_hash="sha256:second-active",
            created_at=now,
            promoted_at=now,
        )
    )
    await session.flush()


async def _add_candidate_index(session, *, provider: str, model: str, dim: int) -> None:
    """Add a NON-active ``candidate`` index carrying its own provenance stamp."""
    from datetime import UTC, datetime

    from app.models import IndexStatus, IndexVersion

    session.add(
        IndexVersion(
            index_version="v-cand",
            status=IndexStatus.candidate,
            source_version_hash="sha256:candidate",
            created_at=datetime.now(UTC),
            embedding_provider=provider,
            embedding_model=model,
            embedding_dim=dim,
        )
    )
    await session.flush()


# -- #226: the gate must read the stamp of the index it is actually querying ---
#
# Before #226 ``_active_index_stamp`` resolved purely by ``status == active`` and
# never looked at ``active_index_version``, so document scoping and the Tier-3
# provenance gate could be reasoning about two different indexes.


async def test_vector_arm_degrades_when_scoped_candidate_stamp_mismatches(
    seeded_session,
) -> None:
    """The issue's own measured repro (#226): a mismatched CANDIDATE must degrade.

    A candidate stamped ``openrouter/text-embedding-3-small`` is being retrieved
    while a different, ``gemini``-stamped index is live and the configured query
    embedder is gemini. The arm would be scoring openrouter vectors with gemini
    embeddings — meaningless cosine, exactly the silent failure #57 exists to
    prevent. This is the promotion evaluator's shape (#216): it measures a
    candidate while another index still serves.

    Turns RED if ``_active_index_stamp`` goes back to resolving by
    ``status == active`` — it then reads the LIVE index's matching gemini stamp,
    sees no mismatch, and returns True (the behaviour on main before this fix).
    """
    await _stamp_active_index(
        seeded_session, provider="gemini", model="gemini-embedding-001", dim=1536
    )
    await _add_candidate_index(
        seeded_session, provider="openrouter", model="text-embedding-3-small", dim=1536
    )
    h = HybridRetriever(seeded_session, active_index_version="v-cand", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._active_index_stamp() == EmbedderIdentity(
            provider="openrouter", model="text-embedding-3-small", dim=1536
        )
        assert await h._vector_arm_degrade() is VectorDegrade.mismatch
    assert any("vector_retrieval_index_embedder_mismatch" in r.getMessage() for r in records)


async def test_vector_arm_runs_when_scoped_candidate_stamp_matches(seeded_session) -> None:
    """The other direction: version-scoping must not blanket-disable non-active indexes.

    Mirror image of the test above — the CANDIDATE matches the configured
    embedder and the LIVE index is the mismatched one. The arm must RUN. Without
    this partner, "always return False for a non-active index" would satisfy the
    repro test and destroy the promotion evaluator's vector arm entirely.

    Turns RED if the scoped lookup is made to fail closed on any non-active
    status, or if it still resolves by ``status == active`` (it would then read
    the live openrouter stamp and degrade).
    """
    await _stamp_active_index(
        seeded_session, provider="openrouter", model="text-embedding-3-small", dim=1536
    )
    await _add_candidate_index(
        seeded_session, provider="gemini", model="gemini-embedding-001", dim=1536
    )
    h = HybridRetriever(seeded_session, active_index_version="v-cand", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_degrade() is VectorDegrade.none
    assert not any("mismatch" in r.getMessage() for r in records)


async def test_active_index_stamp_scoped_by_version_ignores_a_second_active_row(
    seeded_session,
) -> None:
    """A retriever pinned to its own index does not care how many others are active.

    Replaces the first half of the pre-#226
    ``test_active_index_stamp_none_on_dual_active``. That test asserted ``None``
    here; it was pinning the fail-open. With ``active_index_version="v1"`` the
    arms filter documents to ``v1`` alone, so the gate reads ``v1``'s own stamp
    and the dual-active count is irrelevant — no ambiguity, no WARN.

    Turns RED if the scoped branch is removed (resolution falls through to the
    status-based lookup, which sees two active rows and returns ``ambiguous``).
    """
    await _stamp_active_index(
        seeded_session, provider="gemini", model="gemini-embedding-001", dim=1536
    )
    await _add_second_active_index(seeded_session)
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._active_index_stamp() == _GEMINI
        assert await h._vector_arm_degrade() is VectorDegrade.none
    assert not any("retrieval_multiple_active_indexes" in r.getMessage() for r in records)


async def test_active_index_stamp_ambiguous_on_dual_active_when_unscoped(
    seeded_session,
) -> None:
    """Dual-active + no pinned version -> the AMBIGUOUS sentinel, not ``None``.

    Replaces the second half of the pre-#226
    ``test_active_index_stamp_none_on_dual_active``, which pinned ``None`` and so
    pinned the fail-open: the shared predicate reads ``None`` as "unknown
    provenance ⇒ allow", and the vector arm ran against a coin flip. This is the
    shape a production dual-active request really arrives in — the orchestrator's
    own guard returns ``("", "")``, which reaches the retriever as
    ``active_index_version=None``.

    Turns RED if the ``active_count > 1`` branch returns ``None`` again (or is
    deleted).
    """
    await _add_second_active_index(seeded_session)
    h = HybridRetriever(seeded_session, active_index_version=None, embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._active_index_stamp() is IndexStampStatus.ambiguous
    assert any("retrieval_multiple_active_indexes" in r.getMessage() for r in records)


async def test_vector_arm_fails_closed_on_dual_active_when_unscoped(seeded_session) -> None:
    """The consequence of the sentinel, at the method retrieval actually calls.

    ``_vector_arm_degrade`` is the gate; ``_active_index_stamp`` is only its
    input. This also pins that the ambiguous resolution does not crash the
    mismatch WARN's identifier payload (the sentinel has no ``.provider``), and
    that the fail-closed decision is separately observable.

    It pins the REASON and not merely "not none" (#352). Reporting ambiguity as
    ``VectorDegrade.mismatch`` — which is what the old ``bool`` return collapsed
    to — makes the orchestrator log
    ``answer_cache_write_skipped_embedder_mismatch``, sending an operator to the
    embedder config when the database needs ``promote_version``.

    Turns RED if ``is_index_embedder_mismatch`` stops returning True for the
    sentinel (the arm goes back to running with unknown provenance), or if the
    ``isinstance(stamp, IndexStampStatus)`` branch returns
    ``VectorDegrade.mismatch`` again.
    """
    await _add_second_active_index(seeded_session)
    h = HybridRetriever(seeded_session, active_index_version=None, embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_degrade() is VectorDegrade.ambiguous_index
    msgs = [r.getMessage() for r in records]
    assert any("vector_retrieval_index_provenance_ambiguous" in m for m in msgs)
    rec = next(
        r for r in records if "vector_retrieval_index_provenance_ambiguous" in r.getMessage()
    )
    assert rec.configured_embedding_provider == "gemini"


async def test_dual_active_still_allowed_when_enforcement_is_off(seeded_session) -> None:
    """Fail-closed is the GATE's verdict, not the resolver's.

    With no ``embedder_identity`` wired, Tier-3 enforcement is off and the arm
    runs even on a dual-active database — the pre-#226 behaviour for legacy
    callers, unchanged. Proves the new deny is scoped to enforcement being on.

    NB this is a BOUNDARY guard, not a #226-defect probe: it passes against
    pre-fix source too (the enforcement-off short-circuit never changed). Its job
    is to stop the new deny from creeping above that short-circuit.

    Turns RED if the ambiguity check is hoisted above the
    ``embedder_identity is None`` short-circuit in ``_vector_arm_degrade``.
    """
    await _add_second_active_index(seeded_session)
    h = HybridRetriever(seeded_session, active_index_version=None)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_degrade() is VectorDegrade.none
    assert not any("ambiguous" in r.getMessage() for r in records)


async def test_active_index_stamp_none_when_named_version_does_not_exist(
    seeded_session,
) -> None:
    """A pinned version with no row resolves to ``None`` (allow), not ambiguous.

    The three arms filter documents on that same missing version, so the vector
    arm has nothing to score and the gate's verdict is unobservable. "No row ⇒ no
    stamp ⇒ unknown provenance ⇒ allow" keeps this consistent with the
    no-active-index case instead of inventing a third rule.

    Turns RED if the scoped branch falls back to the status-based lookup when the
    named row is missing — it would then read ``v1``'s stamp, which is a
    different index entirely.
    """
    await _stamp_active_index(seeded_session, provider="stub", model="stub", dim=1536)
    h = HybridRetriever(seeded_session, active_index_version="v-nope", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._active_index_stamp() is None
        assert await h._vector_arm_degrade() is VectorDegrade.none
    assert not any("mismatch" in r.getMessage() for r in records)


async def test_vector_arm_allowed_when_unscoped_with_zero_active_indexes(session) -> None:
    """Pins today's ALLOW on the zero-active-rows path — the sibling left OPEN (#265).

    Unscoped (``active_index_version=None``) with NO active index row and
    enforcement ON. The resolver finds no row, returns ``None``, and the shared
    predicate allows. That is deliberate: a fresh / un-promoted database must
    still answer (see ``_default_retriever``), so this is NOT the dual-active
    case and must not fail closed with it.

    It is also not safe, and this test says so rather than hiding it: with zero
    active rows the three arms drop their ``index_version`` filter and scan by
    document STATUS alone, so an ingested-but-unpromoted index built by another
    embedder is in scope with nothing to check it against. Fixing that needs the
    arms and the gate to agree on a document set, which is #265, not #226.

    Turns RED the moment #265 changes this verdict — which is the point: that
    change must be a deliberate, visible edit, not a silent side effect.
    """
    h = HybridRetriever(session, active_index_version=None, embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._active_index_stamp() is None
        assert await h._vector_arm_degrade() is VectorDegrade.none
    assert not any("retrieval_multiple_active_indexes" in r.getMessage() for r in records)


async def test_gate_delegates_to_shared_predicate_for_the_ambiguous_sentinel(
    seeded_session, monkeypatch
) -> None:
    """#226 extension of the #71 single-source-of-truth guard.

    The existing delegation test loops over ``EmbedderIdentity | None`` stamps
    only. The ambiguous sentinel is a THIRD shape, and it must also be decided by
    the shared predicate — not by an inline ``if stamp is ambiguous: return
    False`` in the gate, which could drift from what
    ``app.services.index_health`` reports on ``GET /health/index``.

    Turns RED if the gate short-circuits the sentinel before calling
    ``is_index_embedder_mismatch`` (the spy's call list stays empty).
    """
    import app.retrieval.hybrid as hybrid_mod
    from app.embeddings import is_index_embedder_mismatch

    await _add_second_active_index(seeded_session)
    h = HybridRetriever(seeded_session, active_index_version=None, embedder_identity=_GEMINI)
    resolved = await h._active_index_stamp()
    assert resolved is IndexStampStatus.ambiguous

    calls: list[tuple] = []

    def _spy(configured, index_stamp, *, _calls=calls):
        _calls.append((configured, index_stamp))
        return is_index_embedder_mismatch(configured, index_stamp)

    monkeypatch.setattr(hybrid_mod, "is_index_embedder_mismatch", _spy)
    with _capture_retrieval_logs():
        degrade = await h._vector_arm_degrade()

    assert calls == [(_GEMINI, IndexStampStatus.ambiguous)]
    assert degrade is VectorDegrade.ambiguous_index


async def test_vector_arm_runs_when_stamp_matches(seeded_session) -> None:
    await _stamp_active_index(
        seeded_session, provider="gemini", model="gemini-embedding-001", dim=1536
    )
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_degrade() is VectorDegrade.none
    assert not any("mismatch" in r.getMessage() for r in records)


async def test_vector_arm_degrades_on_stamp_mismatch(seeded_session) -> None:
    # Same dim, different provider/model: the dimension guard does NOT fire, so
    # without this enforcement the corrupted rankings would be served silently.
    await _stamp_active_index(seeded_session, provider="stub", model="stub", dim=1536)
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_degrade() is VectorDegrade.mismatch
    msgs = [r.getMessage() for r in records]
    assert any("vector_retrieval_index_embedder_mismatch" in m for m in msgs)


async def test_vector_arm_mismatch_warn_carries_identifiers(seeded_session) -> None:
    # The WARN's operational value is its structured ``extra`` payload — assert it
    # carries the stamped-vs-configured provider/model/dim (identifiers only).
    await _stamp_active_index(seeded_session, provider="stub", model="stub", dim=1536)
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_degrade() is VectorDegrade.mismatch
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
        assert await h._vector_arm_degrade() is VectorDegrade.mismatch
    assert any("vector_retrieval_index_embedder_mismatch" in r.getMessage() for r in records)


async def test_vector_arm_degrades_on_partial_stamp(seeded_session) -> None:
    # A half-written stamp (provider set, model/dim NULL) is not "unknown
    # provenance" (only a NULL *provider* is), so it does not match a fully
    # populated identity and must fail closed → degrade, not silently allow.
    await _stamp_active_index(seeded_session, provider="gemini", model=None, dim=None)
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_degrade() is VectorDegrade.mismatch
    assert any("vector_retrieval_index_embedder_mismatch" in r.getMessage() for r in records)


async def test_vector_arm_allowed_on_null_stamp(seeded_session) -> None:
    # The seeded ``v1`` index carries no provenance stamp (legacy / stub-seeded
    # demo). Unknown provenance must be ALLOWED, or the demo and every pre-#51
    # index would break.
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        assert await h._vector_arm_degrade() is VectorDegrade.none
    assert not any("mismatch" in r.getMessage() for r in records)


async def test_gate_delegates_to_shared_predicate(seeded_session, monkeypatch) -> None:
    """Single-source-of-truth guard (#71): the canonical read-time gate
    ``HybridRetriever._vector_arm_degrade`` (#57) must DELEGATE its allow/degrade
    comparison to the shared ``is_index_embedder_mismatch`` predicate (#65), not
    carry an inline copy. Post-#71, agreement with the orchestrator's predictor is
    guaranteed by construction; this asserts the delegation directly so a future
    edit that reintroduces an inline copy — and could drift from the predictor —
    is caught. We spy on the predicate: for every stamp the gate calls it exactly
    once with ``(configured_identity, resolved_stamp)`` and returns
    ``VectorDegrade.none`` exactly when the predicate says there is no mismatch
    (with the enforcement-off short-circuit still gate-side)."""
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
            degrade = await h._vector_arm_degrade()

        # The gate routed its decision through the shared predicate, exactly once,
        # with the configured identity and the resolved active-index stamp.
        assert calls == [(_GEMINI, resolved)], f"gate did not delegate for {stamp_cfg}"
        assert (degrade is VectorDegrade.none) == (
            not is_index_embedder_mismatch(_GEMINI, resolved)
        ), stamp_cfg


async def test_vector_arm_enforcement_off_without_identity(seeded_session) -> None:
    # No identity wired ⇒ enforcement is off, even against a mismatching stamp.
    await _stamp_active_index(seeded_session, provider="stub", model="stub", dim=1536)
    h = HybridRetriever(seeded_session, active_index_version="v1")
    assert await h._vector_arm_degrade() is VectorDegrade.none


async def test_vector_arm_allowed_when_no_active_index(session) -> None:
    # Empty catalog (no active IndexVersion) ⇒ nothing to enforce against ⇒ allow.
    h = HybridRetriever(session, embedder_identity=_GEMINI)
    assert await h._vector_arm_degrade() is VectorDegrade.none


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
        arm_degrade=VectorDegrade.mismatch,
    )
    # A disabled arm degrades to no hits and hands back the REASON it was given —
    # it no longer re-derives ``mismatch`` from a bare bool (#352).
    assert result == ([], VectorDegrade.mismatch)


async def test_safe_vector_retrieve_passes_the_ambiguity_reason_through(seeded_session) -> None:
    """PARTNER for the test above, and the #352 bite: a skipped arm must report
    the reason it was ACTUALLY given, not a hard-coded ``mismatch``.

    Same call, one different input, one different output — which is what proves
    the return value is threaded rather than constant.

    RED if ``_safe_vector_retrieve``'s skip branch returns
    ``VectorDegrade.mismatch`` instead of ``arm_degrade``.
    """

    class _ExplodingVector:
        async def retrieve(self, question, *, product_area, limit):
            raise AssertionError("vector arm must not run when disabled")

    h = HybridRetriever(seeded_session, active_index_version="v1")
    result = await h._safe_vector_retrieve(
        _ExplodingVector(),  # type: ignore[arg-type]
        "anything",
        product_area=Domain.claude_api.value,
        limit=5,
        arm_degrade=VectorDegrade.ambiguous_index,
    )
    assert result == ([], VectorDegrade.ambiguous_index)


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
        arm_degrade=VectorDegrade.none,
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
            identity_checksum=f"sha256:{version}",
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


# ---------------------------------------------------------------------------
# #87 regression: a question that NAMES a source and carries a question word
# ("install" / "how do I" / "need") must return non-empty evidence on the
# hermetic path (exact + keyword; the vector arm is off on SQLite). The bug was
# that the thin conftest codex/gemini fixtures lacked the content the real
# shipped corpus has, so scoped keyword retrieval found nothing and the
# orchestrator refused a legitimate, indexed question. The enriched fixtures now
# mirror the real sources (codex.md install/auth; gemini_api.md streaming).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "product_area", "expected_substr"),
    [
        # ``expected_substr`` must appear in some retrieved chunk — a stronger
        # assertion than "non-empty", because the pre-fix thin fixture could still
        # return the WRONG chunk via a noise token (e.g. ILIKE '%i%' matching
        # "generation"). Requiring the ACTUAL answer content makes this fail on the
        # un-enriched fixture, so it is a genuine #87 regression tripwire.
        ("How do I install the Codex CLI?", Domain.codex.value, "npm install"),
        ("Does Codex need an API key?", Domain.codex.value, "OPENAI_API_KEY"),
        # Assert a unique identifier ("streamGenerateContent") rather than the bare
        # word "streaming" so a future unrelated gemini chunk mentioning "streaming"
        # can't satisfy this without the actual #87 streaming-response content.
        ("google gemini streaming responses", Domain.gemini_api.value, "streamGenerateContent"),
        # #170, same class: claude_code.md had NO installation content at all, so this
        # refused identically single-turn and as a follow-up. Assert the package name
        # rather than the bare word "install" — the Permissions text already contains
        # "install"-adjacent noise, and only the real content carries this token.
        (
            "How do I install Claude Code?",
            Domain.claude_code.value,
            "@anthropic-ai/claude-code",
        ),
    ],
)
async def test_source_named_question_word_returns_evidence_hermetically(
    seeded_session, question: str, product_area: str, expected_substr: str
) -> None:
    r = HybridRetriever(seeded_session, active_index_version="v1")
    result = await r.retrieve(question, product_area=product_area, intent=Intent.how_to)
    assert result.hits, f"#87: no evidence for source-named question {question!r}"
    # Evidence stays scoped to the named source (no cross-product contamination)...
    assert all(h.product_area == product_area for h in result.hits)
    # ...and the RIGHT content was retrieved, not just any chunk in the domain.
    assert any(expected_substr in h.chunk_text for h in result.hits), (
        f"#87: retrieved evidence for {question!r} lacks {expected_substr!r}"
    )


# ---------------------------------------------------------------------------
# #352: an answer built on a dual-active database is not cacheable, and an answer
# that ACTUALLY drew on more than one active index says so.
#
# Two separate signals on two separate facts. The cache gate keys on the DATABASE
# being ambiguous; the WARN keys on the EVIDENCE having spanned indexes. The pair
# of tests below is what proves they are not the same check wearing two names.
# ---------------------------------------------------------------------------


async def _mirror_exact_term_into_second_index(session) -> None:
    """Give ``v2`` its own active document + chunk carrying the SAME exact term.

    This is what makes a dual-active database actually UNION: with the term present
    under both ``v1`` and ``v2`` and ``active_index_version=None``, ``ExactRetriever``
    drops its ``Document.index_version ==`` predicate and returns rows from both.
    """
    import uuid as _uuid
    from datetime import UTC, datetime

    from app.models import Chunk, Document, DocumentStatus, ExactTerm, TermType

    now = datetime.now(UTC)
    doc = Document(
        document_id=_uuid.uuid4(),
        index_version="v2",
        source_name="claude_api",
        product_area="claude_api",
        source_url="https://example.invalid/v2",
        title="Claude API (v2 index)",
        identity_checksum="sha256:claude_api-v2",
        last_fetched_at=now,
        last_indexed_at=now,
        status=DocumentStatus.active,
    )
    session.add(doc)
    await session.flush()
    chunk = Chunk(
        chunk_id=_uuid.uuid4(),
        document_id=doc.document_id,
        product_area="claude_api",
        section_path="/rate-limits",
        heading="Rate limits",
        parent_heading=None,
        chunk_text="CLAUDE_API_RATE_LIMIT caps requests per minute on the v2 index.",
        context_summary="v2 copy",
        exact_terms=[],
        chunk_order=0,
        content_checksum="sha256:claude_api-v2-chunk-0",
    )
    session.add(chunk)
    await session.flush()
    session.add(
        ExactTerm(
            term_id=_uuid.uuid4(),
            term_text="CLAUDE_API_RATE_LIMIT",
            term_type=TermType.environment_variable,
            product_area="claude_api",
            document_id=doc.document_id,
            chunk_id=chunk.chunk_id,
        )
    )
    await session.flush()


async def test_exact_lookup_evidence_spanning_two_indexes_is_warned_and_not_cacheable(
    seeded_session,
) -> None:
    """#352, the union made visible.

    The exact arm short-circuits before the provenance gate ever runs, so this
    result used to come back ``VectorDegrade.none`` -- cacheable -- while its
    evidence was drawn from two different indexes at once.

    RED without the fix on BOTH halves: ``retrieve`` returns
    ``VectorDegrade.none``, and no ``retrieval_evidence_spans_multiple_indexes``
    record is emitted (the event does not exist).
    """
    await _add_second_active_index(seeded_session)
    await _mirror_exact_term_into_second_index(seeded_session)

    h = HybridRetriever(seeded_session, active_index_version=None, embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        result = await h.retrieve(
            "CLAUDE_API_RATE_LIMIT",
            product_area=Domain.claude_api.value,
            intent=Intent.exact_lookup,
            limit=20,
            top_k=6,
        )

    # PARTNER: the answer is still SERVED. "Not cacheable" must not become
    # "not answered" -- that would be the fail-closed option the owner rejected.
    assert len(result.hits) == 2, [hit.index_version for hit in result.hits]
    assert {hit.index_version for hit in result.hits} == {"v1", "v2"}
    assert result.vector_degrade is VectorDegrade.ambiguous_index

    rec = next(
        (r for r in records if "retrieval_evidence_spans_multiple_indexes" in r.getMessage()),
        None,
    )
    assert rec is not None, [r.getMessage() for r in records]
    assert rec.index_versions == "v1,v2"
    assert rec.index_version_count == 2


async def test_a_single_index_answer_on_an_ambiguous_database_is_uncacheable_but_unwarned(
    seeded_session,
) -> None:
    """THE PARTNER THAT SEPARATES THE TWO SIGNALS.

    Same dual-active database, but nothing was mirrored into ``v2``, so the
    evidence came from ``v1`` alone. The cache gate still fires (the database is
    ambiguous and ``promote_version`` may yet demote the row this answer came
    from), and the span WARN does NOT -- it would be noise, and it is the rarer,
    more actionable line precisely because it does not fire here.

    Without this test, ``_warn_if_evidence_spans_indexes`` could log on EVERY
    ambiguous request and every other assertion in this file would still pass.

    RED if ``_finalize`` gates the cache on the SPAN instead of the database
    state, or if ``_warn_if_evidence_spans_indexes`` drops its ``> 1`` guard.
    """
    await _add_second_active_index(seeded_session)

    h = HybridRetriever(seeded_session, active_index_version=None, embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        result = await h.retrieve(
            "CLAUDE_API_RATE_LIMIT",
            product_area=Domain.claude_api.value,
            intent=Intent.exact_lookup,
            limit=20,
            top_k=6,
        )

    assert {hit.index_version for hit in result.hits} == {"v1"}, "precondition: one index only"
    assert result.vector_degrade is VectorDegrade.ambiguous_index, (
        "the DATABASE was ambiguous, so the answer is not cacheable even from one index"
    )
    assert not any(
        "retrieval_evidence_spans_multiple_indexes" in r.getMessage() for r in records
    ), "the span WARN must key on the evidence, not on the database state"


async def test_a_healthy_scoped_retrieval_is_cacheable_and_costs_no_extra_query(
    seeded_session,
) -> None:
    """NON-VACUITY PARTNER for both tests above, plus the performance claim.

    If ``_finalize`` returned ``ambiguous_index`` unconditionally every assertion
    about "not cached" would pass while the answer cache was dead. It must stay
    ``none`` on the ordinary single-active path.

    It also pins the cost argument in ``_active_index_ambiguous``'s docstring: a
    retriever SCOPED to a named index issues NO ``index_versions`` query at all,
    because the arms already filtered to that one index.

    RED if ``_active_index_ambiguous`` drops its ``active_index_version is not
    None`` short-circuit (an ``index_versions`` statement appears), or if
    ``_finalize`` stops returning the incoming degrade unchanged.
    """
    from sqlalchemy import event

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    engine = seeded_session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
        result = await h.retrieve(
            "CLAUDE_API_RATE_LIMIT",
            product_area=Domain.claude_api.value,
            intent=Intent.exact_lookup,
            limit=20,
            top_k=6,
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert result.hits, "precondition: the exact arm hit"
    assert result.vector_degrade is VectorDegrade.none, "a healthy answer must stay cacheable"
    # PARTNER for the count: the exact-term SELECT really did run, so "no
    # index_versions query" is not "no queries at all".
    assert any("exact_terms" in s for s in statements), statements
    assert not any("index_versions" in s for s in statements), (
        f"a scoped retriever must not resolve the active index: {statements}"
    )


# ---------------------------------------------------------------------------
# Review round 2 (#352). Every test below was added because a MUTATION of the
# line it covers survived the whole 2047-test suite. Each names that mutation.
# ---------------------------------------------------------------------------


async def test_a_real_degrade_reason_is_never_overwritten_by_the_ambiguity_overlay(
    seeded_session,
) -> None:
    """THE INVARIANT `_finalize`'s docstring asserts, which nothing pinned.

    Dropping the ``degrade is VectorDegrade.none`` guard survived the full suite,
    and losing it is not cosmetic: ``Orchestrator.ask`` raises the transient 5xx
    of #142 only when the reason is exactly ``unavailable``. Relabel an embedder
    outage as ``ambiguous_index`` and the user gets "the corpus has no answer" --
    a content refusal the client records as a SUCCESS and never retries -- for
    what is actually a retryable provider outage.

    RED if ``_finalize`` escalates a reason that is already named, i.e. if the
    ``degrade is VectorDegrade.none and`` guard is removed.
    """
    await _add_second_active_index(seeded_session)
    h = HybridRetriever(seeded_session, active_index_version=None, embedder_identity=_GEMINI)

    # Precondition: this database really is ambiguous, so the overlay WOULD fire
    # on a `none`. Without this the test could pass because nothing was ambiguous.
    assert await h._active_index_ambiguous() is True

    for named in (VectorDegrade.unavailable, VectorDegrade.mismatch):
        result = await h._finalize([], named)
        assert result.vector_degrade is named, (
            f"the ambiguity overlay overwrote {named!r}, losing the reason the "
            "orchestrator branches on"
        )


async def test_the_global_answer_when_grounded_path_is_finalized_too(seeded_session) -> None:
    """``_retrieve_global`` is live production (`product_area is None`) and its
    ``_finalize`` call was LINE-COVERED but behaviourally untested -- replacing it
    with a bare ``RetrievalResult(...)`` left the whole suite green. That is this
    repo's "coverage is not assertion" scar, inside this very change.

    With Tier-3 enforcement OFF -- which #226 pins as a legitimate state and which
    is the stub-seeded demo shape -- ``_vector_arm_degrade`` returns ``none``, so
    ``_finalize`` is the ONLY thing that can withhold the cache write here.

    RED if ``_retrieve_global`` returns ``RetrievalResult(...)`` directly instead
    of awaiting ``_finalize``.
    """
    await _add_second_active_index(seeded_session)
    # No ``embedder_identity`` => enforcement off => the arm is allowed to run.
    h = HybridRetriever(seeded_session, active_index_version=None)

    result = await h.retrieve(
        "something entirely unscoped", product_area=None, intent=Intent.how_to, limit=20, top_k=6
    )

    assert result.vector_degrade is VectorDegrade.ambiguous_index, (
        "a global answer built while the active index is ambiguous must not be cached"
    )


async def test_the_keyword_arm_carries_the_index_version_it_retrieved_from(
    seeded_session,
) -> None:
    """Deleting ``index_version=doc.index_version`` from the KEYWORD arm left the
    whole suite green -- only the exact arm's projection was pinned. The
    consequence is not cosmetic: on every non-``exact_lookup`` question (the
    common case) every hit would carry ``None``, the span set would be empty, and
    ``retrieval_evidence_spans_multiple_indexes`` could never fire again.

    RED if ``KeywordRetriever`` stops setting ``index_version``.
    """
    r = KeywordRetriever(seeded_session, active_index_version="v1")
    hits = await r.retrieve("model", product_area=Domain.codex.value)

    assert hits, "precondition: the keyword arm hit"
    assert {h.index_version for h in hits} == {"v1"}


async def test_a_keyword_answer_spanning_two_indexes_is_warned_and_not_cacheable(
    seeded_session,
) -> None:
    """The keyword arm's projection, exercised END TO END through the hybrid layer
    rather than only at the arm -- the operator-facing half of #352 on the path
    that produces most evidence.

    RED if the keyword arm stops carrying ``index_version`` (the span set empties
    and neither the WARN nor the gate fires).
    """
    await _add_second_active_index(seeded_session)
    await _mirror_exact_term_into_second_index(seeded_session)

    h = HybridRetriever(seeded_session, active_index_version=None, embedder_identity=_GEMINI)
    with _capture_retrieval_logs() as records:
        # ``how_to`` never touches the exact short-circuit, so this evidence comes
        # from the keyword arm.
        result = await h.retrieve(
            "how do I raise the claude api rate limit per minute",
            product_area=Domain.claude_api.value,
            intent=Intent.how_to,
            limit=20,
            top_k=6,
        )

    assert {h_.index_version for h_ in result.hits} == {"v1", "v2"}, [
        (h_.retrieval_type, h_.index_version) for h_ in result.hits
    ]
    assert result.vector_degrade is VectorDegrade.ambiguous_index
    assert any("retrieval_evidence_spans_multiple_indexes" in r.getMessage() for r in records)


async def test_a_cross_area_span_is_caught_on_the_merged_multi_hop_list(
    seeded_session,
) -> None:
    """Two reviewers reproduced this independently: area A returns only ``v1``,
    area B only ``v2``, so NEITHER per-area check fires while the merged answer
    draws on both indexes. The first draft skipped finalizing the merged list,
    arguing it was "a subset of lists that were each already checked" -- true, and
    not sufficient, because spanning is not preserved under subsetting.

    RED if ``retrieve_multi`` returns ``RetrievalResult(...)`` directly instead of
    awaiting ``_finalize`` on the merged list.
    """
    from app.retrieval.types import RetrievalResult

    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)

    a_only_v1 = _hit_on_index("claude_api", "v1")
    b_only_v2 = _hit_on_index("gemini_api", "v2")

    async def _fake_retrieve(question, *, product_area, intent, limit, top_k):
        hit = a_only_v1 if product_area == "claude_api" else b_only_v2
        # Each area is internally single-index, so each per-area ``_finalize``
        # sees nothing to warn about -- that is the whole point.
        return RetrievalResult(hits=[hit], vector_degrade=VectorDegrade.none)

    h.retrieve = _fake_retrieve  # type: ignore[method-assign]

    with _capture_retrieval_logs() as records:
        result = await h.retrieve_multi(
            "compare them",
            product_areas=["claude_api", "gemini_api"],
            intent=Intent.how_to,
            limit=20,
            top_k=6,
        )

    assert {h_.index_version for h_ in result.hits} == {"v1", "v2"}
    rec = next(
        (r for r in records if "retrieval_evidence_spans_multiple_indexes" in r.getMessage()),
        None,
    )
    assert rec is not None, "the merged list spanned two indexes and nothing said so"
    assert rec.index_versions == "v1,v2"
    assert result.vector_degrade is VectorDegrade.ambiguous_index


def _hit_on_index(area: str, index_version: str):
    """One EvidenceHit pinned to a named index, for the merge tests."""
    import uuid as _uuid

    from app.models.enums import RetrievalType
    from app.retrieval.types import EvidenceHit

    return EvidenceHit(
        chunk_id=_uuid.uuid4(),
        document_id=_uuid.uuid4(),
        product_area=area,
        source_name=area,
        document_title=f"{area} doc",
        section_path="/s",
        heading="H",
        parent_heading=None,
        chunk_text=f"{area} chunk",
        context_summary="s",
        source_url=f"https://x/{area}",
        score=1.0,
        index_version=index_version,
        retrieval_type=RetrievalType.hybrid,
        rank=1,
    )


async def test_the_span_warn_is_not_fired_by_hits_that_simply_do_not_know_their_index(
    seeded_session,
) -> None:
    """PARTNER for the ``is not None`` filter, which also survived mutation.

    Counting ``None`` as its own version would make a single-index answer look
    like a union the moment ONE hand-built hit reached ``_finalize`` -- and the
    overlay now gates the cache on the span, so that would also stop caching a
    perfectly healthy answer.

    RED if ``_warn_if_evidence_spans_indexes`` drops its ``is not None`` filter.
    """
    h = HybridRetriever(seeded_session, active_index_version="v1", embedder_identity=_GEMINI)
    hits = [_hit_on_index("claude_api", "v1"), _hit_on_index("claude_api", "v1")]
    hits[1].index_version = None

    with _capture_retrieval_logs() as records:
        result = await h._finalize(hits, VectorDegrade.none)

    assert result.vector_degrade is VectorDegrade.none, (
        "an unknown index is not a second index; this answer is still cacheable"
    )
    assert not any(
        "retrieval_evidence_spans_multiple_indexes" in r.getMessage() for r in records
    ), [r.getMessage() for r in records]


async def test_a_spanning_answer_on_a_ZERO_active_database_is_not_cached_either(
    seeded_session,
) -> None:
    """THE ONLY PRODUCTION-REACHABLE CELL THE SPAN GATE CHANGES, driven through the
    REAL arms.

    A skeptic enumerated all 12 cells of {0,1,2 active} x {scoped, unscoped} x
    {evidence spans 1, 2} and found the gate changes exactly two, of which only
    this one is reachable through ``Orchestrator.ask`` -- the retriever is unscoped
    precisely when the database is zero-active or ambiguous. It then found that the
    span half of the gate was held by a single test that monkeypatches ``retrieve``
    on a SCOPED retriever, a shape the real arms cannot produce.

    Zero active rows is #265's state and its POLICY is untouched here: the arms
    still answer, and a zero-active database whose evidence comes from ONE index is
    still cacheable (pinned by the partner below). What changes is only the case
    where the evidence itself proves a union.

    RED if ``_finalize`` stops escalating on ``spans_indexes`` (the resolver says
    ``none``, not ``ambiguous``, so the resolver half cannot cover this).
    """
    await _add_second_active_index(seeded_session)
    await _mirror_exact_term_into_second_index(seeded_session)
    # Demote BOTH index rows to ``candidate``: that is #265's state -- ingested but
    # never promoted. The documents stay ``status=active`` regardless, because
    # ``_upsert_document`` marks them so at INGEST, before promotion, which is
    # exactly why the arms still return them.
    await _demote_every_index(seeded_session)

    h = HybridRetriever(seeded_session, active_index_version=None, embedder_identity=_GEMINI)
    assert await h._active_index_ambiguous() is False, (
        "precondition: zero active rows is NOT 'ambiguous' -- the resolver half is silent here"
    )

    result = await h.retrieve(
        "CLAUDE_API_RATE_LIMIT",
        product_area=Domain.claude_api.value,
        intent=Intent.exact_lookup,
        limit=20,
        top_k=6,
    )

    assert {hit.index_version for hit in result.hits} == {"v1", "v2"}
    assert result.vector_degrade is VectorDegrade.ambiguous_index, (
        "the evidence PROVES a union, so it must not be frozen even though the "
        "resolver reports 'none' rather than 'ambiguous'"
    )


async def test_a_zero_active_database_with_single_index_evidence_still_caches(
    seeded_session,
) -> None:
    """PARTNER, and the #265 boundary. Without it the test above would be satisfied
    by "a zero-active database never caches", which IS #265's owner-gated policy
    change and is deliberately NOT made here -- it would disable the answer cache on
    every pre-first-promote and seeded-demo deploy.

    RED if ``_active_index_ambiguous`` is widened to ``state is not one``.
    """
    await _demote_every_index(seeded_session)

    h = HybridRetriever(seeded_session, active_index_version=None, embedder_identity=_GEMINI)
    result = await h.retrieve(
        "CLAUDE_API_RATE_LIMIT",
        product_area=Domain.claude_api.value,
        intent=Intent.exact_lookup,
        limit=20,
        top_k=6,
    )

    assert {hit.index_version for hit in result.hits} == {"v1"}, "precondition: one index"
    assert result.vector_degrade is VectorDegrade.none, (
        "an un-promoted database still caches; #265's policy is not changed here"
    )


async def _demote_every_index(session) -> None:
    """Leave ZERO rows in ``status=active`` without deleting anything.

    Deleting ``index_versions`` trips the ``documents.index_version`` foreign key;
    demoting is also what the real un-promoted state looks like.
    """
    from sqlalchemy import select as _select

    from app.models import IndexStatus, IndexVersion

    for row in (await session.execute(_select(IndexVersion))).scalars().all():
        row.status = IndexStatus.candidate
    await session.flush()
