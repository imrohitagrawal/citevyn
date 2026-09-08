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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.middleware import get_current_request_id
from app.embeddings import (
    EmbedderIdentity,
    EmbedderUnavailable,
    IndexStampStatus,
    is_index_embedder_mismatch,
)
from app.models import IndexVersion
from app.models.enums import RetrievalType
from app.retrieval.exact import ExactRetriever
from app.retrieval.keyword import KeywordRetriever
from app.retrieval.rerank import Reranker
from app.retrieval.types import EvidenceHit, RetrievalResult, RetrievedChunk, VectorDegrade
from app.retrieval.vector import Embedder, VectorRetriever
from app.routing.intent import Intent
from app.services.index_resolution import ActiveIndexState, resolve_active_index

_logger = logging.getLogger("citevyn.retrieval")

# Cap on how many product areas a single multi-hop question fans out to. The
# per-area searches are sequential (shared session) and each embeds the query, so
# this bounds cost/latency; a realistic cross-product question names 2–3 products.
_MAX_MULTIHOP_DOMAINS = 3


# Precedence for :func:`_combine_degrades`, most severe first. It is a LIST and
# not a chain of ``if`` statements so that a new :class:`VectorDegrade` member
# cannot be added without either appearing here or being caught by
# ``test_retrieval_multihop.py``'s exhaustiveness guard: the old two-test ladder
# fell through to ``none``, i.e. FAILED OPEN — a multi-hop answer where one area
# reported a reason this function had never heard of was cached as if clean.
#
# ``ambiguous_index`` sorts above ``mismatch`` because the areas of a single
# ``retrieve_multi`` share one session and one database: if any area saw more than
# one active index, they all did, and ``index_versions`` is then the root cause an
# operator has to fix before an embedder verdict means anything.
_DEGRADE_PRECEDENCE = (
    VectorDegrade.ambiguous_index,
    VectorDegrade.mismatch,
    VectorDegrade.unavailable,
)


def _combine_degrades(degrades: list[VectorDegrade]) -> VectorDegrade:
    """Merge per-area vector-degrade signals for the cache-write gate (#70/#72).

    ``none`` only when EVERY area retrieved cleanly, so a multi-hop answer built
    while one area's vector arm was degraded — or while the active index was
    ambiguous (#352) — is never cached as if fully grounded.
    """
    for reason in _DEGRADE_PRECEDENCE:
        if any(d is reason for d in degrades):
            return reason
    return VectorDegrade.none


def _round_robin_merge(per_area_hits: list[list[EvidenceHit]]) -> list[EvidenceHit]:
    """Interleave per-area hit lists by rank, deduping by chunk, so each product
    area is represented rather than one high-scoring area crowding out the others."""
    seen: set[uuid.UUID] = set()
    merged: list[EvidenceHit] = []
    for rank in range(max((len(h) for h in per_area_hits), default=0)):
        for hits in per_area_hits:
            if rank < len(hits) and hits[rank].chunk_id not in seen:
                seen.add(hits[rank].chunk_id)
                merged.append(hits[rank])
    return merged


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
                # orchestrator caches it (#72). ``_finalize`` still applies the
                # index-ambiguity overlay (#352), which is NOT an embedder
                # question: under ambiguity this arm dropped its
                # ``Document.index_version ==`` predicate and the evidence above
                # may union documents from several active indexes.
                return await self._finalize(evidence[:top_k], VectorDegrade.none)
            # PRD §3.2 answer flow step 3: "Fall back to keyword search if
            # needed." A natural-language exact-lookup question ("What does the
            # --model flag do?") often doesn't resolve to an exact-term chunk
            # even when the docs cover it, so an empty exact result must not
            # short-circuit to no_answer. Fall through to the hybrid path.
            # ``exact`` already returned nothing here, and an empty match is
            # limit-independent (0 rows at top_k ⇒ 0 at ``limit``), so re-running
            # it in the gather below would be guaranteed-empty dead work — query
            # only keyword+vector and merge against the known-empty exact list.
            arm_degrade = await self._vector_arm_degrade()
            keyword_hits, (vector_hits, vector_degrade) = await asyncio.gather(
                keyword.retrieve(question, product_area=product_area, limit=limit),
                self._safe_vector_retrieve(
                    vector,
                    question,
                    product_area=product_area,
                    limit=limit,
                    arm_degrade=arm_degrade,
                ),
            )
            exact_hits = []
        else:
            arm_degrade = await self._vector_arm_degrade()
            exact_hits, keyword_hits, (vector_hits, vector_degrade) = await asyncio.gather(
                exact.retrieve(question, product_area=product_area, limit=limit),
                keyword.retrieve(question, product_area=product_area, limit=limit),
                self._safe_vector_retrieve(
                    vector,
                    question,
                    product_area=product_area,
                    limit=limit,
                    arm_degrade=arm_degrade,
                ),
            )

        merged = _merge(question, exact_hits, keyword_hits, vector_hits)
        merged.sort(key=lambda h: h.score, reverse=True)
        merged = merged[:limit]

        reranked = await self._reranker.rerank(question, merged, top_k=top_k)
        for idx, hit in enumerate(reranked, start=1):
            hit.rank = idx
        return await self._finalize(reranked, vector_degrade)

    async def _finalize(self, hits: list[EvidenceHit], degrade: VectorDegrade) -> RetrievalResult:
        """Attach the index-ambiguity overlay to a finished hit list (#352).

        Two separate things happen here, and they deliberately key on DIFFERENT
        facts:

        * **The cache gate** keys on the DATABASE state — more than one row claims
          ``status=active``. An answer produced in that state is not frozen, even
          when its evidence happens to have come from a single index, because
          under ambiguity ``orchestrator._retrieve_active_index`` returns
          ``("", "")`` and the surviving index is a coin flip that
          ``promote_version`` is about to settle differently.
        * **The WARN** keys on what the ANSWER actually did — the evidence really
          spanning more than one ``index_version``. That the database is
          dual-active is already logged once per request
          (``orchestrator_multiple_active_indexes``) and, on the paths that reach
          the provenance gate, a second time (``retrieval_multiple_active_indexes``).
          "This answer was built from a union" is strictly rarer and strictly more
          actionable, and it is computed from the rows already in hand.

        **``hits`` are returned untouched.** Ambiguity never changes what a user is
        served — serving something rather than 500-ing is the standing decision at
        ``orchestrator._retrieve_active_index`` — only whether it is cached and
        what the operator is told.

        The overlay only ever ESCALATES ``none``: a real ``mismatch`` or
        ``unavailable`` already blocks the cache write and names a reason of its
        own, so overwriting it would lose information for no gain.
        """
        self._warn_if_evidence_spans_indexes(hits)
        if degrade is VectorDegrade.none and await self._active_index_ambiguous():
            return RetrievalResult(hits=hits, vector_degrade=VectorDegrade.ambiguous_index)
        return RetrievalResult(hits=hits, vector_degrade=degrade)

    def _warn_if_evidence_spans_indexes(self, hits: list[EvidenceHit]) -> None:
        """WARN when the evidence actually drew on more than one index (#352).

        Pure and synchronous — every ``index_version`` is already on the hits,
        because all three arms select the ``documents`` row anyway. ``None`` is
        skipped rather than counted as its own index: a hand-built
        :class:`EvidenceHit` (the eval harness, an injected test double) leaves the
        field unset, and treating "unknown" as a distinct version would fire this
        WARN on a perfectly healthy single-index database.

        The payload is shaped for ``app.core.logging``'s ``extra=`` allowlist:
        ``index_version_count`` is an ``int`` (printed verbatim under any key) and
        ``index_versions`` is a joined ``str`` under a key that is on both
        ``EMITTED_TEXT_KEYS`` and ``OPAQUE_ID_KEYS`` — without the latter the
        entropy sweep would blank a sha-shaped version to ``[REDACTED]``.
        """
        versions = sorted({h.index_version for h in hits if h.index_version is not None})
        if len(versions) <= 1:
            return
        _logger.warning(
            "retrieval_evidence_spans_multiple_indexes",
            extra={
                "request_id": get_current_request_id(),
                "index_versions": ",".join(versions),
                "index_version_count": len(versions),
            },
        )

    async def _active_index_ambiguous(self) -> bool:
        """Whether MORE THAN ONE ``IndexVersion`` row claims ``status=active`` (#352).

        Deliberately independent of ``embedder_identity``. The union this guards
        against is a *document scoping* fact — the arms dropped their
        ``Document.index_version ==`` predicate — and happens identically whether
        or not Tier-3 enforcement is wired, so reusing
        :meth:`_vector_arm_degrade` here would miss it exactly when enforcement is
        off (which ``test_dual_active_still_allowed_when_enforcement_is_off``
        pins as a legitimate state, #226).

        **Costs nothing on a healthy database.** When ``active_index_version`` was
        supplied, every arm filtered to that ONE named index, so the evidence
        cannot span indexes and there is nothing to resolve — this returns
        ``False`` without a query. Unscoped is the only shape that can union, and
        in production it is reached only through
        ``orchestrator._retrieve_active_index``'s ``("", "")`` sentinel, i.e. a
        database that is ALREADY ``none`` or ``ambiguous``. So the one small
        indexed read this adds lands only on a database that is already degenerate.

        It does not log: the condition is already on the record once per request
        from ``orchestrator_multiple_active_indexes``, and duplicating it here
        would add a line per retrieval without adding a fact.
        """
        if self._active_index_version is not None:
            return False
        resolution = await resolve_active_index(self._session)
        return resolution.state is ActiveIndexState.ambiguous

    async def retrieve_multi(
        self,
        question: str,
        *,
        product_areas: list[str],
        intent: Intent,
        limit: int = 20,
        top_k: int = 6,
    ) -> RetrievalResult:
        """Retrieve for a cross-product (multi-hop) question — Phase 3.

        Runs the scoped hybrid retrieval once PER product area and merges, so a
        question spanning several products ("compare the Claude API and Gemini rate
        limits") can be answered from all of them rather than the single first-match
        domain. Evidence is merged **round-robin by rank** so every product is
        represented (a higher-scoring area cannot crowd the other out), deduped by
        chunk, then reranked to ``top_k``.

        Cost/latency note (PR-review): the per-area searches run **sequentially**
        (they share one ``AsyncSession``) and each embeds the query, so cost scales
        with the number of areas — hence the ``_MAX_MULTIHOP_DOMAINS`` cap. Multi-hop
        is a minority path (most questions name one product). The combined
        ``vector_degrade`` is ``none`` only when EVERY area's retrieval was clean, so
        the orchestrator's cache-write gate never freezes a partially-degraded answer.

        Each per-area :meth:`retrieve` has already run :meth:`_finalize`, so the
        index-ambiguity overlay and its WARN are applied per area and are NOT
        re-applied to the merged list here — the merged evidence is a subset of the
        union of lists that were each already checked. On a dual-active database
        that means up to :data:`_MAX_MULTIHOP_DOMAINS` copies of the span WARN for
        one question; on such a database the per-area breakdown is worth more than
        the deduplication.
        """
        areas = product_areas[:_MAX_MULTIHOP_DOMAINS]
        per_area = [
            await self.retrieve(question, product_area=a, intent=intent, limit=limit, top_k=top_k)
            for a in areas
        ]
        merged = _round_robin_merge([r.hits for r in per_area])
        reranked = await self._reranker.rerank(question, merged, top_k=top_k)
        for idx, hit in enumerate(reranked, start=1):
            hit.rank = idx
        return RetrievalResult(
            hits=reranked,
            vector_degrade=_combine_degrades([r.vector_degrade for r in per_area]),
        )

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
        arm_degrade = await self._vector_arm_degrade()
        hits, degrade = await self._safe_vector_retrieve(
            vector, question, product_area=None, limit=limit, arm_degrade=arm_degrade
        )
        evidence = [_to_evidence(h, RetrievalType.vector, i + 1) for i, h in enumerate(hits)]
        reranked = await self._reranker.rerank(question, evidence, top_k=top_k)
        for idx, hit in enumerate(reranked, start=1):
            hit.rank = idx
        return await self._finalize(reranked, degrade)

    async def _safe_vector_retrieve(
        self,
        vector: VectorRetriever,
        question: str,
        *,
        product_area: str | None,
        limit: int,
        arm_degrade: VectorDegrade = VectorDegrade.none,
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

        * ``arm_degrade`` is not :attr:`VectorDegrade.none` — the gate
          (:meth:`_vector_arm_degrade`) already decided the arm may not run and
          has already logged why, so here we simply skip it without embedding or
          querying and hand its reason straight back. That reason is
          :attr:`VectorDegrade.mismatch` for a Tier-3 embedder mismatch (#57) or
          :attr:`VectorDegrade.ambiguous_index` when the index could not be
          resolved to one row (#226/#352). It is passed in rather than
          re-derived: a bare ``enabled: bool`` could not tell the two apart, so
          every dual-active request used to be reported to operators as an
          embedder mismatch, which points at the wrong fix.
        * :class:`EmbedderUnavailable` — the embedding provider is transiently
          down (Tier 1 ⇒ :attr:`VectorDegrade.unavailable`).

        Genuine database errors from the pgvector query are NOT caught here — they
        propagate as real failures.
        """
        if arm_degrade is not VectorDegrade.none:
            return [], arm_degrade
        try:
            hits = await vector.retrieve(question, product_area=product_area, limit=limit)
            return hits, VectorDegrade.none
        except EmbedderUnavailable:
            _logger.warning(
                "vector_retrieval_degraded_embedder_unavailable",
                extra={"request_id": get_current_request_id()},
            )
            return [], VectorDegrade.unavailable

    async def _vector_arm_degrade(self) -> VectorDegrade:
        """Whether the vector arm may run against the index being queried, AND WHY NOT
        (Tier 3, #57).

        The stored document vectors and the query vector must come from the same
        embedding model, or cosine distance is meaningless and the LLM cites
        wrongly-ranked sources — a *silent* correctness failure. The dimension
        guard alone does not catch a same-dim provider/model swap without
        re-ingest (e.g. ``stub`` → ``gemini``, both 1536).

        Returns :attr:`VectorDegrade.none` (vector arm runs) when:

        * enforcement is off — no ``embedder_identity`` was wired; or
        * the index carries no provenance stamp (legacy / stub-seeded indexes:
          ``embedding_provider is None`` ⇒ "unknown provenance, allow", which
          protects the seeded demo and pre-#51 indexes); or
        * the stamp matches the configured embedder's ``(provider, model, dim)``.

        Returns a REASON (and logs a loud WARNING) when either:

        * the stamp is present and does *not* match ⇒
          :attr:`VectorDegrade.mismatch`; or
        * the stamp could not be resolved to ONE index
          (:attr:`~app.embeddings.IndexStampStatus.ambiguous`, #226) — several
          indexes are simultaneously active and nothing can say which vector
          space the arm would be scoring, so this fails closed rather than
          gambling on the winner being compatible ⇒
          :attr:`VectorDegrade.ambiguous_index`.

        **Why a reason and not a bool (#352).** These two states have always taken
        different branches here and emitted different WARNs, and then collapsed
        into one ``False`` that ``_safe_vector_retrieve`` unconditionally turned
        into :attr:`VectorDegrade.mismatch`. So on a dual-active database the
        operator-facing cache-skip line read
        ``answer_cache_write_skipped_embedder_mismatch`` — sending them to the
        embedder config (ADR-0003) when the fix is ``promote_version``. Returning
        the reason costs nothing and removes a bool that carried two meanings, the
        same shape #226 removed from ``_active_index_stamp``'s ``None``.

        Either way this degrades rather than serving corrupted rankings. It is a
        read-time correctness net, not failover (that is the deferred #59): the
        request still answers from exact + keyword.

        The allow/degrade comparison is delegated to the pure
        :func:`app.embeddings.is_index_embedder_mismatch` predicate so there is a
        single source of truth (#71) shared with
        :func:`app.services.index_health.active_index_vector_health`, which reports
        the same verdict on ``GET /health/index``. Only the *logging* branches on
        the resolution shape here; the decision itself never does. The
        ``embedder_identity is None`` enforcement-off short-circuit also stays here
        — the predicate takes a non-optional configured identity and reasons only
        about the stamp.

        Deliberately awaited by :meth:`retrieve` *before* the ``asyncio.gather``
        rather than from inside the vector arm: keeping this one small indexed
        lookup (a single ``index_versions`` row) off the shared ``AsyncSession``
        while the three retrieval arms run concurrently avoids a concurrent-use
        hazard, at a cost that is negligible next to the embedding + LLM calls
        that follow.
        """
        if self._embedder_identity is None:
            return VectorDegrade.none
        stamp = await self._active_index_stamp()
        if not is_index_embedder_mismatch(self._embedder_identity, stamp):
            return VectorDegrade.none
        if isinstance(stamp, IndexStampStatus):
            # Ambiguous resolution (#226). There is no stamp to name, so the
            # mismatch WARN's identifier payload cannot be filled — emit a
            # distinct event instead. ``_active_index_stamp`` already logged
            # ``retrieval_multiple_active_indexes`` for the *condition*; this
            # records the *consequence*, which that WARN cannot imply on its own
            # (with enforcement off the very same condition leaves the arm ON).
            _logger.warning(
                "vector_retrieval_index_provenance_ambiguous",
                extra={
                    "request_id": get_current_request_id(),
                    "index_stamp_status": str(stamp),
                    "configured_embedding_provider": self._embedder_identity.provider,
                    "configured_embedding_model": self._embedder_identity.model,
                    "configured_embedding_dim": self._embedder_identity.dim,
                },
            )
            return VectorDegrade.ambiguous_index
        # A True mismatch that is not ambiguous guarantees a provider-bearing
        # stamp (the predicate returns False for a ``None`` / provider-less
        # stamp), so the identifiers logged below are always populated.
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
        return VectorDegrade.mismatch

    async def _active_index_stamp(self) -> EmbedderIdentity | IndexStampStatus | None:
        """The embedding provenance of the index this retriever is actually reading.

        **The stamp must describe the same index the three arms filter documents
        on, or the Tier-3 gate is enforcing against the wrong row (#226).** So
        when ``active_index_version`` was supplied — the value
        :class:`~app.retrieval.exact.ExactRetriever`,
        :class:`~app.retrieval.keyword.KeywordRetriever` and
        :class:`~app.retrieval.vector.VectorRetriever` each put in their
        ``Document.index_version ==`` predicate — this reads *that* row by primary
        key, whatever its status. Resolving by ``status == active`` instead (the
        pre-#226 behaviour) silently checked a candidate's vectors against the
        LIVE index's stamp, so the promotion evaluator (#216), which measures a
        candidate while a different index is still serving, ran its vector arm
        against a possibly foreign vector space and scored it as if valid.

        Three outcomes, deliberately distinct:

        * an :class:`~app.embeddings.EmbedderIdentity` — the row's
          ``(provider, model, dim)``, possibly all-``None`` for a legacy /
          stub-seeded index (which the predicate then allows);
        * ``None`` — there is no row to read. Either no index is active, or the
          named ``active_index_version`` does not exist. The latter is
          unobservable in practice: the arms filter documents on that same
          missing version, so the vector arm has nothing to score either way, and
          "no row ⇒ no stamp ⇒ unknown provenance ⇒ allow" keeps this consistent
          with the no-active-index case rather than inventing a third rule;
        * :attr:`~app.embeddings.IndexStampStatus.ambiguous` — the unscoped
          fallback found MORE THAN ONE active index (#58). Which one owns the
          documents about to be scored is genuinely unknowable, so the caller
          fails closed (#226). Before #226 this returned ``None``, which the
          predicate reads as "unknown provenance ⇒ allow" — so the arm ran on a
          coin flip. ``None`` cannot carry both meanings; hence the sentinel.

        The ``promoted_at`` / ``index_version`` sort on the fallback is a
        deterministic tiebreaker kept in step with the orchestrator's cache-key
        resolution (``_retrieve_active_index``, which sorts identically) so both
        name the same winning row.
        """
        if self._active_index_version is not None:
            # Scoped read: the gate must reason about the SAME index the arms
            # filter on, not whichever index happens to be live (#226).
            scoped_stmt = select(
                IndexVersion.embedding_provider,
                IndexVersion.embedding_model,
                IndexVersion.embedding_dim,
            ).where(IndexVersion.index_version == self._active_index_version)
            scoped_row = (await self._session.execute(scoped_stmt)).first()
            if scoped_row is None:
                return None
            return EmbedderIdentity(provider=scoped_row[0], model=scoped_row[1], dim=scoped_row[2])

        # Unscoped fallback: no version was pinned, so the arms filter on document
        # STATUS alone and the governing index is whichever one is active. Enforce
        # the single-active-row invariant (#58) — see
        # ``orchestrator._retrieve_active_index`` for the rationale. When the guard
        # fires there it returns ``("", "")``, which reaches us as
        # ``active_index_version=None`` — i.e. this branch is exactly how a
        # production dual-active request arrives here.
        #
        # The count guard, the status filter and the ordering are the SHARED ones
        # in ``app.services.index_resolution`` (#264) — the health route resolved
        # the active index differently and so reported a clean verdict at the very
        # moment this method fails closed. The WARN stays here rather than moving
        # into the resolver: ``_capture_retrieval_logs`` attaches a handler to
        # the concrete ``citevyn.retrieval`` logger, so this call site pins the
        # event name AND the logger it lands on.
        resolution = await resolve_active_index(self._session)
        if resolution.state is ActiveIndexState.ambiguous:
            _logger.warning(
                "retrieval_multiple_active_indexes",
                extra={
                    "request_id": get_current_request_id(),
                    "active_count": int(resolution.active_count),
                },
            )
            return IndexStampStatus.ambiguous
        if resolution.row is None:
            return None
        return EmbedderIdentity(
            provider=resolution.row.embedding_provider,
            model=resolution.row.embedding_model,
            dim=resolution.row.embedding_dim,
        )


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
        # Carried through, not dropped: :meth:`HybridRetriever._finalize` reads it
        # off the finished evidence to say whether the answer spanned indexes
        # (#352). This projection names every field explicitly, so a new field
        # that is not listed here silently becomes ``None`` downstream.
        index_version=chunk.index_version,
        retrieval_type=retrieval_type,
        rank=rank,
    )
