"""Hybrid retriever.

Orchestrates the three orthogonal retrievers and produces the final
ordered hit list. The reranker is then called on that list.

Scoring contract:

* exact hits start at ``1.0``.
* keyword hits start at ``0.5``.
* vector hits start at ``1 - distance`` (already done by
  :class:`VectorRetriever`).

When two retrievers find the same chunk we add the per-retriever
scores. The result is sorted descending and capped at ``limit``. The
exact lookup path short-circuits the keyword and vector retrievers
when intent is ``exact_lookup``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.middleware import get_current_request_id
from app.embeddings import EmbedderIdentity, EmbedderUnavailable, is_index_embedder_mismatch
from app.models import IndexStatus, IndexVersion
from app.models.enums import RetrievalType
from app.retrieval.exact import ExactRetriever
from app.retrieval.keyword import KeywordRetriever
from app.retrieval.rerank import Reranker
from app.retrieval.types import EvidenceHit, RetrievalResult, RetrievedChunk, VectorDegrade
from app.retrieval.vector import Embedder, VectorRetriever
from app.routing.intent import Intent

_logger = logging.getLogger("citevyn.retrieval")


class HybridRetriever:
    def __init__(
        self,
        session: AsyncSession,
        *,
        active_index_version: str | None = None,
        embedder: Embedder | None = None,
        embedder_identity: EmbedderIdentity | None = None,
        reranker: Reranker | None = None,
        global_confidence: tuple[float, float] | None = None,
    ) -> None:
        self._session = session
        self._active_index_version = active_index_version
        self._embedder = embedder
        # The provenance triple of the query embedder (provider/model/dim). When
        # supplied, the vector arm is gated on it matching the active index's
        # stamp (Tier 3 enforcement, #57); when ``None``, enforcement is off and
        # the vector arm runs unconditionally (legacy callers / unit tests).
        self._embedder_identity = embedder_identity
        self._reranker = reranker or Reranker()
        # ``(min_top_score, min_margin)`` for the global "answer when grounded"
        # confidence gate (Phase 2). Used only on a ``product_area=None`` retrieval;
        # ``None`` disables the gate (the vector arm returns whatever it finds).
        self._global_confidence = global_confidence

    async def retrieve(
        self,
        question: str,
        *,
        product_area: str | None,
        intent: Intent,
        limit: int = 20,
        top_k: int = 6,
    ) -> RetrievalResult:
        # "Answer when grounded" (Phase 2): a ``None`` product area means the
        # question named no product (routed ``unsupported``) but we still try to
        # answer it from the whole corpus. Exact + keyword have nothing to scope to
        # and a global keyword ILIKE would surface spurious generic-token matches,
        # so this path uses the confidence-gated global VECTOR arm alone — it
        # answers real paraphrases and returns nothing for off-corpus questions
        # (which then decline via weak_evidence / the LLM grounding-refusal).
        if product_area is None:
            return await self._retrieve_global(question, limit=limit, top_k=top_k)

        exact = ExactRetriever(self._session, active_index_version=self._active_index_version)
        keyword = KeywordRetriever(self._session, active_index_version=self._active_index_version)
        vector = VectorRetriever(
            self._session,
            active_index_version=self._active_index_version,
            embedder=self._embedder,
        )

        if intent is Intent.exact_lookup:
            exact_hits = await exact.retrieve(question, product_area=product_area, limit=top_k)
            if exact_hits:
                evidence = [
                    _to_evidence(h, RetrievalType.exact, idx + 1)
                    for idx, h in enumerate(exact_hits)
                ]
                # The vector arm was never consulted on this short-circuit, so the
                # answer is embedder-independent and NOT degraded even under a
                # Tier-3 mismatch — report ``VectorDegrade.none`` so the
                # orchestrator caches it (#72).
                return RetrievalResult(hits=evidence[:top_k], vector_degrade=VectorDegrade.none)
            # PRD §3.2 answer flow step 3: "Fall back to keyword search if
            # needed." A natural-language exact-lookup question ("What does the
            # --model flag do?") often doesn't resolve to an exact-term chunk
            # even when the docs cover it, so an empty exact result must not
            # short-circuit to no_answer. Fall through to the hybrid path.
            # ``exact`` already returned nothing here, and an empty match is
            # limit-independent (0 rows at top_k ⇒ 0 at ``limit``), so re-running
            # it in the gather below would be guaranteed-empty dead work — query
            # only keyword+vector and merge against the known-empty exact list.
            vector_enabled = await self._vector_arm_enabled()
            keyword_hits, (vector_hits, vector_degrade) = await asyncio.gather(
                keyword.retrieve(question, product_area=product_area, limit=limit),
                self._safe_vector_retrieve(
                    vector, question, product_area=product_area, limit=limit, enabled=vector_enabled
                ),
            )
            exact_hits = []
        else:
            vector_enabled = await self._vector_arm_enabled()
            exact_hits, keyword_hits, (vector_hits, vector_degrade) = await asyncio.gather(
                exact.retrieve(question, product_area=product_area, limit=limit),
                keyword.retrieve(question, product_area=product_area, limit=limit),
                self._safe_vector_retrieve(
                    vector, question, product_area=product_area, limit=limit, enabled=vector_enabled
                ),
            )

        merged = _merge(question, exact_hits, keyword_hits, vector_hits)
        merged.sort(key=lambda h: h.score, reverse=True)
        merged = merged[:limit]

        reranked = await self._reranker.rerank(question, merged, top_k=top_k)
        for idx, hit in enumerate(reranked, start=1):
            hit.rank = idx
        return RetrievalResult(hits=reranked, vector_degrade=vector_degrade)

    async def _retrieve_global(self, question: str, *, limit: int, top_k: int) -> RetrievalResult:
        """Global "answer when grounded" retrieval: the confidence-gated vector arm.

        Runs the vector arm unscoped (``product_area=None``) with the global
        confidence gate applied inside :class:`VectorRetriever`. An off-corpus
        question yields no hits (gated), so the orchestrator declines it via the
        empty-evidence path; a real paraphrase yields its chunk(s). Exact + keyword
        are intentionally not consulted (nothing to scope to; a global ILIKE leaks).
        """
        vector = VectorRetriever(
            self._session,
            active_index_version=self._active_index_version,
            embedder=self._embedder,
            global_confidence=self._global_confidence,
        )
        enabled = await self._vector_arm_enabled()
        hits, degrade = await self._safe_vector_retrieve(
            vector, question, product_area=None, limit=limit, enabled=enabled
        )
        evidence = [_to_evidence(h, RetrievalType.vector, i + 1) for i, h in enumerate(hits)]
        reranked = await self._reranker.rerank(question, evidence, top_k=top_k)
        for idx, hit in enumerate(reranked, start=1):
            hit.rank = idx
        return RetrievalResult(hits=reranked, vector_degrade=degrade)

    async def _safe_vector_retrieve(
        self,
        vector: VectorRetriever,
        question: str,
        *,
        product_area: str | None,
        limit: int,
        enabled: bool = True,
    ) -> tuple[list[RetrievedChunk], VectorDegrade]:
        """Run the vector arm, degrading to ``[]`` when it cannot be served safely.

        Returns ``(chunks, degrade)`` where ``degrade`` names *why* the arm fell
        back to no hits, or :attr:`VectorDegrade.none` when it actually ran (even
        to a legitimately empty result). The orchestrator gates the answer-cache
        write on that runtime reason and labels its skip-WARN from it (#70/#72), so
        the two "degraded to []" cases must be distinguishable from a genuine empty
        vector result — and from each other.

        The vector arm is the only retriever that depends on an external embedding
        provider. Two conditions degrade it to no hits (the request stays
        answerable from the exact-term and keyword arms rather than 500-ing or
        serving corrupt rankings), each logged as a WARNING so the degradation is
        observable and never silent:

        * ``enabled is False`` — the active index was built by a *different*
          embedder than the one configured to embed queries (Tier 3 mismatch,
          #57 ⇒ :attr:`VectorDegrade.mismatch`). The caller
          (:meth:`_vector_arm_enabled`) has already logged the mismatch; here we
          simply skip the arm without embedding or querying.
        * :class:`EmbedderUnavailable` — the embedding provider is transiently
          down (Tier 1 ⇒ :attr:`VectorDegrade.unavailable`).

        Genuine database errors from the pgvector query are NOT caught here — they
        propagate as real failures.
        """
        if not enabled:
            return [], VectorDegrade.mismatch
        try:
            hits = await vector.retrieve(question, product_area=product_area, limit=limit)
            return hits, VectorDegrade.none
        except EmbedderUnavailable:
            _logger.warning(
                "vector_retrieval_degraded_embedder_unavailable",
                extra={"request_id": get_current_request_id()},
            )
            return [], VectorDegrade.unavailable

    async def _vector_arm_enabled(self) -> bool:
        """Whether the vector arm may run against the active index (Tier 3, #57).

        The stored document vectors and the query vector must come from the same
        embedding model, or cosine distance is meaningless and the LLM cites
        wrongly-ranked sources — a *silent* correctness failure. The dimension
        guard alone does not catch a same-dim provider/model swap without
        re-ingest (e.g. ``stub`` → ``gemini``, both 1536).

        Returns ``True`` (vector arm runs) when:

        * enforcement is off — no ``embedder_identity`` was wired; or
        * the active index carries no provenance stamp (legacy / stub-seeded
          indexes: ``embedding_provider is None`` ⇒ "unknown provenance, allow",
          which protects the seeded demo and pre-#51 indexes); or
        * the stamp matches the configured embedder's ``(provider, model, dim)``.

        Returns ``False`` (and logs a loud WARNING) when the stamp is present and
        does *not* match — degrade rather than serve corrupted rankings. This is a
        read-time correctness net, not failover (that is the deferred #59): the
        request still answers from exact + keyword.

        The allow/degrade comparison is delegated to the pure
        :func:`app.embeddings.is_index_embedder_mismatch` predicate so there is a
        single source of truth (#71): the orchestrator uses the same predicate to
        predict this degrade before retrieval runs and skip caching a degraded
        answer (#65). The ``embedder_identity is None`` enforcement-off
        short-circuit stays here — the predicate takes a non-optional configured
        identity and reasons only about the stamp.

        Deliberately awaited by :meth:`retrieve` *before* the ``asyncio.gather``
        rather than from inside the vector arm: keeping this one small indexed
        lookup (the single active-index row) off the shared ``AsyncSession`` while
        the three retrieval arms run concurrently avoids a concurrent-use hazard,
        at a cost that is negligible next to the embedding + LLM calls that follow.
        """
        if self._embedder_identity is None:
            return True
        stamp = await self._active_index_stamp()
        if not is_index_embedder_mismatch(self._embedder_identity, stamp):
            return True
        # A True mismatch guarantees a provider-bearing stamp (the predicate
        # returns False for a ``None`` / provider-less stamp), so the identifiers
        # logged below are always populated.
        assert stamp is not None
        _logger.warning(
            "vector_retrieval_index_embedder_mismatch",
            extra={
                "request_id": get_current_request_id(),
                "index_embedding_provider": stamp.provider,
                "index_embedding_model": stamp.model,
                "index_embedding_dim": stamp.dim,
                "configured_embedding_provider": self._embedder_identity.provider,
                "configured_embedding_model": self._embedder_identity.model,
                "configured_embedding_dim": self._embedder_identity.dim,
            },
        )
        return False

    async def _active_index_stamp(self) -> EmbedderIdentity | None:
        """The embedding provenance stamped on the active ``IndexVersion``.

        Resolves the same active index the orchestrator uses for the cache key
        (``status == active``, most-recently promoted). Returns its
        ``(provider, model, dim)`` triple, or ``None`` when no index is active.
        Scoping the read path to a single active version is the separate concern
        of #58; this only reads the winning row's stamp.

        The ``index_version`` secondary sort is a deterministic tiebreaker so
        that, in the (today-impossible, #58-tracked) event of two simultaneously
        active rows with equal ``promoted_at``, this query and the orchestrator's
        cache-key resolution (``_retrieve_active_index``, which sorts
        identically) always pick the *same* winning row — the gate must reason
        about the index the rest of the pipeline uses.
        """
        # Enforce single-active-row invariant (#58) — see
        # ``orchestrator._retrieve_active_index`` for the rationale. When
        # the guard fires there it returns ``("", "")``; here we mirror
        # the WARNING + ``None`` so the vector arm gates itself on
        # provenance=None / unknown, not on the wrong embedder stamp.
        count_stmt = select(func.count(IndexVersion.index_version)).where(
            IndexVersion.status == IndexStatus.active
        )
        active_count = (await self._session.execute(count_stmt)).scalar_one()
        if active_count > 1:
            _logger.warning(
                "retrieval_multiple_active_indexes",
                extra={
                    "request_id": get_current_request_id(),
                    "active_count": int(active_count),
                },
            )
            return None
        stmt = (
            select(
                IndexVersion.embedding_provider,
                IndexVersion.embedding_model,
                IndexVersion.embedding_dim,
            )
            .where(IndexVersion.status == IndexStatus.active)
            .order_by(
                IndexVersion.promoted_at.desc().nulls_last(),
                IndexVersion.index_version.desc(),
            )
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return EmbedderIdentity(provider=row[0], model=row[1], dim=row[2])


def _merge(
    question: str,
    exact: list[RetrievedChunk],
    keyword: list[RetrievedChunk],
    vector: list[RetrievedChunk],
) -> list[EvidenceHit]:
    by_chunk: dict[uuid.UUID, EvidenceHit] = {}
    for chunk in exact:
        by_chunk[chunk.chunk_id] = _to_evidence(chunk, RetrievalType.exact, 0, base_score=1.0)
    for chunk in keyword:
        if chunk.chunk_id in by_chunk:
            by_chunk[chunk.chunk_id].score += 0.5
            continue
        by_chunk[chunk.chunk_id] = _to_evidence(chunk, RetrievalType.keyword, 0, base_score=0.5)
    for chunk in vector:
        if chunk.chunk_id in by_chunk:
            by_chunk[chunk.chunk_id].score += chunk.score
            continue
        by_chunk[chunk.chunk_id] = _to_evidence(
            chunk, RetrievalType.vector, 0, base_score=chunk.score
        )
    return list(by_chunk.values())


def _to_evidence(
    chunk: RetrievedChunk,
    retrieval_type: RetrievalType,
    rank: int,
    *,
    base_score: float | None = None,
) -> EvidenceHit:
    return EvidenceHit(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        product_area=chunk.product_area,
        source_name=chunk.source_name,
        document_title=chunk.document_title,
        section_path=chunk.section_path,
        heading=chunk.heading,
        parent_heading=chunk.parent_heading,
        chunk_text=chunk.chunk_text,
        context_summary=chunk.context_summary,
        source_url=chunk.source_url,
        score=base_score if base_score is not None else chunk.score,
        retrieval_type=retrieval_type,
        rank=rank,
    )
