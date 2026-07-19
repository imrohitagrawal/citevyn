"""Orchestrator: composes the answer engine.

Wires together the Slice 4 seams (domain guardrail, intent router,
hybrid retrieval, LLM client, citation validator) and the Slice 5
answer cache with the Slice 2 persistence (Session, Message,
RetrievedEvidence). The HTTP route in Slice 7 calls
:meth:`Orchestrator.ask` and maps the returned dict to the
``/v1/sessions/{id}/messages`` response shape.

Pipeline (per ``docs/ARCHITECTURE.md`` §5.2):

1. Domain guardrail — refuse off-domain questions cheaply.
2. Intent router — short-circuit unsupported / clarify paths.
3. Cache lookup — bypass retrieval and generation on hit.
4. Hybrid retrieval — fetch evidence only when needed.
5. Answer generator — embed evidence, call the LLM.
6. Citation validator — gate grounded answers.
7. Cache write — only for grounded answers.
8. Persistence — user/assistant messages, evidence, audit event.
"""

from __future__ import annotations

import enum
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.answer.generate import AnswerGenerator
from app.answer.memory import (
    build_contextual_query,
    condense_question_llm,
    recent_user_questions,
)
from app.answer.no_answer import build_no_answer_response, build_suggestions
from app.cache.answer_cache import (
    AnswerCacheStore,
    CachedAnswer,
    build_cache_key,
)
from app.cache.factory import build_answer_cache_store
from app.core.config import Settings
from app.embeddings import (
    configured_embedder_identity,
    get_embedder,
)
from app.guardrails.domain import Domain, classify_domain, classify_domains, is_unsupported
from app.llm.errors import LLMUnavailable
from app.llm.factory import get_llm_client
from app.llm.protocol import LLMClient
from app.llm.validation import validate_citations
from app.models import (
    Confidence,
    Message,
    MessageRole,
    RetrievedEvidence,
    Session,
    User,
    UserRole,
)
from app.models.enums import RetrievalType
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.types import (
    EvidenceHit,
    RetrievalResult,
    VectorDegrade,
    chunk_to_citation,
)
from app.routing.intent import Intent, classify_intent, should_skip_retrieval

_logger = logging.getLogger("citevyn.answer")

# ---------------------------------------------------------------------------
# Greeting short-circuit
# ---------------------------------------------------------------------------

# A bare social greeting ("hi", "hello CiteVyn") is neither an off-domain
# refusal nor a no-answer — it is a non-informational query that should get a
# friendly static reply without burning a retrieval + LLM round-trip. The
# detector runs BEFORE the unsupported refusal because "hello" classifies as
# an unsupported domain (no product keyword), and before retrieval so no
# evidence is ever fetched for it.
#
# The pattern matches the WHOLE message, not just a prefix: a greeting opener,
# an optional short addressee ("CiteVyn", "there", "team"…), and only trailing
# punctuation to the end of the string. Anchoring the end is what keeps a real
# question that merely opens with a greeting ("hey do embeddings work", "yo
# bitcoin price today") from being swallowed — the substantive tail fails the
# ``$`` anchor, so those fall through to the normal pipeline (and an off-domain
# tail still reaches the unsupported refusal). A prefix match plus negative
# keyword guards leaked exactly those cases.
_GREETING_RE = re.compile(
    r"""
    ^\s*
    (?:                                   # opener
        hi|hello|hey|heya|hiya|howdy|yo|sup|greetings
        | (?:good\s+)?(?:morning|afternoon|evening)   # "good morning" or bare "morning"
    )
    (?:                                   # zero or more short addressees
        [\s,]+
        (?:there|citevyn|sitevyn|team|folks|everyone|all|bot|assistant|user)
    )*
    [\s,.!?]*$                            # only trailing space/punctuation (incl. "?")
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Friendly static reply for a bare greeting. No citations, not a refusal.
GREETING_RESPONSE = (
    "Hi! I'm CiteVyn. I answer questions about Claude API, Claude Code, "
    "Codex, and Gemini API using cited official documentation. What would "
    "you like to know?"
)


def is_greeting(question: str) -> bool:
    """Return ``True`` when ``question`` is a bare social greeting.

    A greeting is an opener (``hi``, ``hello``, ``howdy``, ``sup``, ``good
    morning``…) optionally trailed by a short addressee (``hello CiteVyn``,
    ``hi there``) and only trailing punctuation — a bare ``hello?`` still
    counts. The pattern is anchored at both ends, so any message carrying
    substantive content past the greeting — a real question ("hello, how do I
    get the Gemini API key?"), a bare ask riding a greeting token ("hey do
    embeddings work"), or off-domain text ("yo bitcoin price today") — does
    not match and flows to the normal pipeline instead.
    """
    return _GREETING_RE.match(question) is not None


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class Citation(dict[str, object]):
    """A citation row in the response.

    Shaped exactly like :func:`app.retrieval.types.chunk_to_citation`
    emits. ``Citation`` is a ``dict`` subclass so the type system can
    mark it as the response surface without forcing callers through
    pydantic.
    """


class RetrievalStrategy(enum.StrEnum):
    """Public-facing retrieval strategy label.

    Maps to the ``retrieval_strategy`` field in the response. The
    hybrid path emits ``hybrid_reranked``; the cache hit path emits
    ``cache``; unsupported / no-answer paths emit ``none``.
    """

    none = "none"
    cache = "cache"
    exact_lookup = "exact_lookup"
    hybrid_reranked = "hybrid_reranked"


# Type alias for the orchestrator response. The concrete value is a
# ``dict[str, object]`` matching ``docs/API_SPEC.md`` §5. The alias
# is purely documentary — it gives readers a single name to grep for.
AnswerResponse = dict[str, object]


class OrchestratorError(RuntimeError):
    """Raised when the orchestrator cannot answer the question at all.

    Distinct from a no-answer response: this is a transport-level
    failure (LLM provider unavailable, cost limit, etc.) that the
    Slice 7 route layer maps to a 5xx.
    """


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------


class _RetrieverLike(Protocol):
    """Minimum surface :class:`Orchestrator` requires from a retriever.

    Both :class:`app.retrieval.hybrid.HybridRetriever` and the
    dependency-injected test double satisfy this; keeping the
    protocol narrow lets tests skip the full hybrid wiring.
    """

    async def retrieve(
        self,
        question: str,
        *,
        product_area: str | None,
        intent: Intent,
        limit: int,
        top_k: int,
    ) -> RetrievalResult: ...

    async def retrieve_multi(
        self,
        question: str,
        *,
        product_areas: list[str],
        intent: Intent,
        limit: int,
        top_k: int,
    ) -> RetrievalResult: ...


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _retrieve_active_index(
    session: AsyncSession,
) -> tuple[str, str]:
    """Return ``(active_index_version, source_version_hash)``.

    Looks up the first ``IndexVersion`` row in ``active`` status and resolves what
    the ``ask`` pipeline needs from it in ONE query (#58/#65): the version and
    source hash feed the cache key and retriever scoping. If no active index
    exists, returns ``("", "")`` so the cache key uses an empty source version —
    every subsequent run invalidates the cache once the index is promoted, which
    is the desired behavior before Slice 2 ingestion is wired.

    The Tier-3 vector-arm degrade is no longer predicted here: the retriever
    reports the *actual* runtime degrade reason back from ``retrieve()`` and the
    write gate uses that (#70/#72), so this no longer resolves the embedder stamp.
    """
    from app.core.middleware import get_current_request_id
    from app.models import IndexStatus, IndexVersion

    # Enforce the single-active-row invariant (#58) loudly rather than silently
    # picking a deterministic-but-arbitrary winner. Two ``active`` rows means
    # the previous promote did not demote the prior active, or two operators
    # raced. Returning ``("", "")`` falls through to the status-only filter
    # (the caller's ``or None``) so retrieval still runs rather than 500-ing
    # the request; the WARNING is the operator-visible signal that this DB
    # is in an inconsistent state and needs ``promote_version`` to converge.
    count_stmt = select(func.count(IndexVersion.index_version)).where(
        IndexVersion.status == IndexStatus.active
    )
    active_count = (await session.execute(count_stmt)).scalar_one()
    if active_count > 1:
        _logger.warning(
            "orchestrator_multiple_active_indexes",
            extra={
                "request_id": get_current_request_id(),
                "active_count": int(active_count),
            },
        )
        return "", ""

    # The ``index_version`` secondary sort is a deterministic tiebreaker so this
    # resolution stays consistent with the retrieval-layer provenance gate
    # (``HybridRetriever._active_index_stamp``, which sorts identically) if two
    # rows are ever simultaneously ``active`` with equal ``promoted_at`` (a
    # single-active-row invariant tracked by #58, now enforced above).
    stmt = (
        select(IndexVersion.index_version, IndexVersion.source_version_hash)
        .where(IndexVersion.status == IndexStatus.active)
        .order_by(
            IndexVersion.promoted_at.desc().nulls_last(),
            IndexVersion.index_version.desc(),
        )
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return "", ""
    return row[0], row[1]


def _default_retriever(
    settings: Settings,
    session: AsyncSession,
    *,
    active_index_version: str | None = None,
) -> _RetrieverLike:
    """Build the default hybrid retriever.

    Injected as a free function so tests can pass their own
    :class:`_RetrieverLike` and the orchestrator never imports the
    pgvector or FTS machinery on its own.

    ``active_index_version`` scopes every retrieval arm to the documents of the
    currently-active index version (#58): once "re-ingest as a *new* index
    version" is used, old- and new-vector-space ``Document`` rows both sit at
    ``status=active``, so filtering on status alone would mix vector spaces into
    ``<=>`` ranking and break the ADR-0003 failover invariant. ``None`` (the
    no-active-index case) leaves the arms filtering on ``status`` only — exactly
    the pre-#58 behavior — so a fresh / un-promoted database still answers rather
    than filtering to nothing. This is orthogonal to the #57 provenance gate
    (``embedder_identity``, "which embedder"); this is "which documents".

    The embedder comes from the process-wide singleton
    (:func:`app.embeddings.get_embedder`) so the vector arm is live: the query is
    embedded with the same provider that built the index. On SQLite the vector
    retriever still short-circuits to ``[]`` (no pgvector), so wiring a stub
    embedder here is harmless for hermetic tests.

    ``embedder_identity`` carries that same provider/model/dim so the retriever
    can enforce it against the active index's provenance stamp and degrade the
    vector arm on a mismatch (Tier 3 enforcement, #57). It MUST describe the same
    embedder as ``embedder`` — both are derived from the one ``settings`` here,
    and in production ``settings`` is the ``get_settings()`` singleton the
    embedder singleton was also built from, so they cannot diverge. (Tests that
    mutate settings must ``reset_embedder`` to keep the pair consistent.)
    """
    return HybridRetriever(
        session,
        active_index_version=active_index_version,
        embedder=get_embedder(settings),
        embedder_identity=configured_embedder_identity(settings),
        global_confidence=(
            settings.retrieval_global_min_top_score,
            settings.retrieval_global_min_margin,
        ),
    )


class Orchestrator:
    """Composes the answer pipeline and persists the trace.

    The orchestrator is built once per request (it owns the session
    it persists to) and is invoked via :meth:`ask`. It is safe to
    use in tests without a network or live Postgres as long as the
    default ``StubLLMClient`` and ``in_memory_engine`` are used.
    """

    def __init__(
        self,
        settings: Settings,
        session: AsyncSession,
        *,
        llm: LLMClient | None = None,
        retriever: _RetrieverLike | None = None,
        cache: AnswerCacheStore | None = None,
    ) -> None:
        self._settings = settings
        self._session = session
        self._llm = llm or get_llm_client(settings)
        # An explicitly-injected retriever always wins (tests). The DEFAULT is
        # built lazily in ``ask`` — only after the active index version is
        # resolved — so it can scope retrieval to that version (#58). Building it
        # here in ``__init__`` (a sync method) could not see the async-resolved
        # active index, which is why the default was previously stuck at
        # ``active_index_version=None`` and mixed old-version documents in.
        self._injected_retriever = retriever
        self._cache = cache or build_answer_cache_store(settings, session)
        self._generator = AnswerGenerator(
            self._llm,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def ask(
        self,
        *,
        question: str,
        request_id: str,
        session_id: uuid.UUID,
    ) -> AnswerResponse:
        """Process a single user question end-to-end.

        The returned dict matches :class:`AnswerResponse` and the
        ``/v1/sessions/{id}/messages`` response in
        ``docs/API_SPEC.md`` §5. The caller is responsible for
        committing the session once it has serialized the response.
        """
        normalized = question.strip()

        # A bare social greeting short-circuits to a friendly static reply before the
        # unsupported refusal (a lone "hello" classifies as an unsupported domain) and
        # before retrieval, so we never burn an LLM call on "hi". It runs on the
        # ORIGINAL utterance, BEFORE conversation memory, so a bare "hi" mid-session
        # stays a greeting instead of borrowing the prior topic. A real question that
        # merely opens with a greeting falls through to the normal pipeline (see
        # :func:`is_greeting`).
        if is_greeting(question):
            return await self._respond_greeting(
                request_id=request_id,
                session_id=session_id,
                question=question,
                normalized=normalized,
                domain=classify_domain(question),
            )

        # Conversation memory (Phase 3b): an anaphoric follow-up ("How can I raise
        # it?") names no product on its own, so single-turn it would route to
        # ``unsupported`` and be refused. Resolve it against the session's recent turns
        # so retrieval AND the answer see the topic. ``build_contextual_query`` only
        # rewrites a genuine anaphoric/elliptical follow-up (a self-contained off-domain
        # sentence is left alone → still refused); with no prior turns it is a no-op, so
        # every single-turn path is byte-for-byte unchanged. Everything downstream
        # classifies + retrieves + answers from ``retrieval_query`` (the resolved
        # topic), while the ORIGINAL ``question`` stays the persisted user utterance.
        retrieval_query = question
        prior_questions: list[str] = []
        if self._settings.conversation_memory:
            prior_questions = await recent_user_questions(
                self._session, session_id, limit=self._settings.memory_recent_turns
            )
            retrieval_query = build_contextual_query(question, prior_questions)

        domain = classify_domain(retrieval_query)
        intent = classify_intent(retrieval_query, domain)
        # The router emits ``Intent.unsupported`` when the guardrail refused, but the
        # orchestrator re-derives it from the domain so a stale intent cannot leak
        # through.
        if is_unsupported(domain):
            intent = Intent.unsupported

        # Multi-hop (Phase 3): a cross-product question names >=2 products ("compare the
        # Claude API and Gemini rate limits"). ``classify_domain`` returns only the
        # first, so the single-domain path answers only half. When >=2 product areas
        # are named we retrieve each and merge below. (A multi-hop question always
        # resolves to a product domain — not ``unsupported`` — so it never collides with
        # the answer-when-grounded / refuse-early branches; CiteVyn-meta short-circuits
        # to ``[citevyn]``.) Classified from ``retrieval_query`` so that IF a memory
        # rewrite prepended a prior turn that itself named a second product, the fan-out
        # still fires. (A follow-up that names a product on its own is self-contained —
        # ``build_contextual_query`` leaves it unchanged — so it does NOT inherit the
        # prior topic; resolving cross-product follow-ups by pronoun is out of scope.)
        multi_domains = classify_domains(retrieval_query)
        multi_hop = len(multi_domains) >= 2

        # "Answer when grounded" (Phase 2): a question that names no product routes
        # to ``unsupported``, but the docs may still cover it ("restrict which tools
        # the coding assistant may run" is a Claude Code question). Instead of an
        # immediate refusal, retrieve GLOBALLY below (``product_area=None``) and let
        # the confidence gate + the LLM grounding-refusal decline genuinely
        # off-corpus questions. When the flag is off, keep the old refuse-early.
        answer_globally = self._settings.answer_when_grounded and intent is Intent.unsupported

        # Standalone-question condensation (#112 + #169). ``retrieval_query`` has now done
        # its ROUTING job (everything above this line is derived and FIXED), and from here
        # on it feeds only RETRIEVAL, GENERATION and the CACHE KEY. Those two roles want
        # different strings, which is the whole of this block:
        #
        # * ROUTING wants the deterministic CONCATENATION ("What is Codex CLI? who built
        #   it?") — prepending the antecedent is exactly what pulls a bare anaphor onto the
        #   ``codex`` domain instead of ``unsupported``.
        # * RETRIEVAL/GENERATION want a TRUE STANDALONE question ("Who built Codex CLI?").
        #   Handed the concatenation, the LLM sees a leading clause that is already a
        #   complete self-contained question, answers THAT, and ignores the trailing
        #   fragment — so the follow-up returns the PREVIOUS answer verbatim, which is then
        #   cached under its own key and replayed forever (#169).
        #
        # So we condense here, for BOTH follow-up shapes:
        #
        # * the CONTENT-NOUN follow-up ("is there a credentials file option?", #112), which
        #   names no product and carries no bare anaphora, so ``build_contextual_query``
        #   left it unchanged and it routed ``unsupported`` → answer-when-grounded; and
        # * the ANAPHORIC follow-up ("who built it?", #169), which the deterministic rewrite
        #   DID resolve — by concatenation. This case was previously locked out by a
        #   ``retrieval_query == question`` guard, i.e. excluded exactly where it was needed.
        #
        # This stays a PURE RECALL improver. ``domain``/``intent``/``multi_domains``/
        # ``answer_globally`` are already computed above from the un-condensed query, so a
        # rewrite can NEVER flip a topic pivot onto the scoped, un-gated path; the
        # confidence gate + the LLM grounding-refusal net remain the sole authority on
        # declining an off-corpus pivot.
        #
        # Skipped when no real LLM is configured (``llm_provider == "stub"``) — the stub's
        # canned text is not a rewrite, and a test spy that wraps the stub must behave the
        # same as the stub.
        #
        # Gated to the two shapes above and nothing else. A mid-session question that
        # ALREADY stands on its own ("how do I install the Codex CLI?") routes to a product
        # domain AND was left unchanged by the deterministic rewrite — it has nothing to
        # resolve, so we skip the call rather than pay an LLM round-trip per turn for a
        # rewrite the condenser is instructed to decline anyway.
        needs_condense = answer_globally or retrieval_query != question
        if needs_condense and prior_questions and self._settings.llm_provider != "stub":
            # The fallback is whatever routing already resolved: the concatenation when the
            # deterministic rewrite fired, else the raw question. NEVER the bare fragment —
            # dropping back to "who built it?" would strip the antecedent and refuse a
            # perfectly answerable question, i.e. worse than today. ``condense_question_llm``
            # returns ``question`` VERBATIM when it declines (no history, empty/overlong
            # output, or the question already stands alone), so that is treated as "no
            # rewrite" and lands on the same fallback.
            fallback = retrieval_query
            try:
                condensed = await condense_question_llm(question, prior_questions, self._llm)
            except Exception:  # noqa: BLE001 — any LLM failure degrades to today's behaviour
                condensed = question
            retrieval_query = fallback if condensed == question else condensed

        if intent is Intent.unsupported and not answer_globally:
            return await self._respond_unsupported(
                request_id=request_id,
                session_id=session_id,
                question=question,
                normalized=normalized,
                domain=domain,
                intent=Intent.unsupported,
            )

        # Look up the active index ONCE so the cache key carries the current
        # source version and the default retriever can scope to that index version
        # (#58). A missing index returns empty strings, which still produce a
        # stable (and unique) cache key. The Tier-3 vector-arm degrade is NOT
        # predicted here anymore — the retriever reports the actual runtime reason
        # back from ``retrieve()`` and the write gate uses that (#70/#72).
        active_index_version, source_version_hash = await _retrieve_active_index(self._session)

        # The configured query embedder's identity — encoded in the cache key (#65)
        # so a config-only embedder swap (which leaves ``source_version_hash``
        # unchanged) still invalidates affected entries, so a stale answer built in
        # a different vector space is not re-served after the operator fixes the
        # config. Only ``provider/model/dim`` — no secret — enters the pre-image.
        configured_identity = configured_embedder_identity(self._settings)

        # Normalize the question for the cache key. The slice 5 contract pins the
        # inputs verbatim; whitespace normalization is a soft enhancement that improves
        # hit rate on minor formatting differences. Built from ``retrieval_query`` (not
        # the raw ``question``) so two sessions asking the SAME anaphoric follow-up
        # ("how can I raise it?") under DIFFERENT prior topics get DISTINCT cache keys
        # (the resolved query + resolved ``product_area`` both differ) and never
        # cross-serve a wrong cached answer (adversarial review R3/#6).
        cache_normalized = retrieval_query.strip().lower()
        # A multi-hop answer is built from several product areas, so its cache key
        # encodes the sorted joined set (e.g. "claude_api+gemini_api") — self-
        # describing and impossible to collide with a single-domain row. The read
        # and write both use this same key (the question text determines the set).
        cache_product_area = (
            "+".join(sorted(d.value for d in multi_domains)) if multi_hop else domain.value
        )
        cache_key = build_cache_key(
            normalized_question=cache_normalized,
            product_area=cache_product_area,
            source_version_hash=source_version_hash,
            answer_policy_version=self._settings.answer_policy_version,
            embedder_identity=configured_identity.cache_key_component(),
        )
        cached = await self._cache.get(cache_key=cache_key)
        if cached is not None:
            return await self._respond_cache_hit(
                request_id=request_id,
                session_id=session_id,
                question=question,
                normalized=normalized,
                domain=domain,
                intent=intent,
                source_version_hash=source_version_hash,
                cached=cached,
            )

        # Empty / very short questions map to ``Intent.clarify`` in
        # the router. The orchestrator surfaces them as no-answer so
        # we never burn an LLM call on "hi". The global "answer when grounded"
        # path deliberately overrides this for ``Intent.unsupported`` (it retrieves
        # instead of refusing) — but ``clarify`` (empty/too-short) still short-circuits.
        if should_skip_retrieval(intent) and not answer_globally:
            return await self._respond_no_answer(
                request_id=request_id,
                session_id=session_id,
                question=question,
                normalized=normalized,
                domain=domain,
                intent=intent,
                source_version_hash=source_version_hash,
                reason="no_answer",
            )

        # Build the default retriever scoped to the active index version. The
        # ``or None`` converts the "" no-active-index sentinel to ``None`` so the
        # arms fall back to a status-only filter instead of filtering on
        # ``index_version == ""`` (which matches NOTHING and would turn every
        # answer into no_answer on a fresh / un-promoted database). An injected
        # retriever bypasses this and owns its own scoping.
        retriever = self._injected_retriever or _default_retriever(
            self._settings,
            self._session,
            active_index_version=active_index_version or None,
        )
        # Multi-hop → retrieve each named product area and merge (Phase 3). Otherwise
        # ``product_area=None`` triggers the global confidence-gated vector path for an
        # unsupported-routed question (answer when grounded); a real domain scopes the
        # arms as before.
        if multi_hop:
            result = await retriever.retrieve_multi(
                retrieval_query,
                product_areas=[d.value for d in multi_domains],
                intent=intent,
                limit=self._settings.retrieval_max_candidates,
                top_k=self._settings.retrieval_top_k,
            )
        else:
            result = await retriever.retrieve(
                retrieval_query,
                product_area=None if answer_globally else domain.value,
                intent=intent,
                limit=self._settings.retrieval_max_candidates,
                top_k=self._settings.retrieval_top_k,
            )
        evidence = result.hits
        # The ACTUAL runtime degrade of the vector arm (its reason: a transient
        # outage, a Tier-3 mismatch that was truly consulted, or none) — this, not
        # a config prediction, gates the cache write and labels its WARN (#70/#72).
        vector_degrade = result.vector_degrade

        if not evidence:
            # A transiently-unavailable embedding provider (OpenRouter not
            # responding, a timeout, a provider-side rate/quota limit) degrades the
            # vector arm to no hits (#70 ⇒ ``VectorDegrade.unavailable``). When that
            # outage leaves us with NO evidence from any arm, "no source" is
            # UNTRUSTWORTHY: the grounded answer may well exist — we simply could not
            # retrieve it this time. Emitting a content refusal here mislabels a
            # transient infrastructure outage as "the corpus has no answer" and, worse,
            # the client records that 200-refusal as a *successful* answer, so a re-ask
            # of the same question is deduped and never retried. Raise a transient error
            # instead: the API maps it (main.py) to a 5xx with a generic, non-technical
            # "temporarily unavailable, please retry" envelope — no provider detail
            # leaks — and the client's failed-question set lets the user retry, which
            # succeeds once the provider recovers (or correctly refuses if genuinely
            # off-corpus). This mirrors how an LLM-generation outage already surfaces.
            if vector_degrade is VectorDegrade.unavailable:
                raise OrchestratorError(
                    "retrieval degraded: embedding provider unavailable — no evidence retrieved"
                )
            # A question that named no product (answer-when-grounded, global
            # retrieval) and found no confident evidence is genuinely off-corpus —
            # give it the SAME helpful "I can answer about Claude/Codex/Gemini…"
            # refusal it got before, not the generic no-answer. Only unsupported
            # questions that DID ground now answer; the rest keep the crisp refusal.
            if answer_globally:
                return await self._respond_unsupported(
                    request_id=request_id,
                    session_id=session_id,
                    question=question,
                    normalized=normalized,
                    domain=domain,
                    intent=Intent.unsupported,
                )
            return await self._respond_no_answer(
                request_id=request_id,
                session_id=session_id,
                question=question,
                normalized=normalized,
                domain=domain,
                intent=intent,
                source_version_hash=source_version_hash,
                reason="weak_evidence",
                strategy=RetrievalStrategy.hybrid_reranked,
            )

        # Record retrieval strategy on the response. The hybrid
        # retriever tags each hit with its actual type; the
        # orchestrator picks the most specific label.
        strategy = self._strategy_for(intent, evidence)

        try:
            # Answer the RESOLVED query so the LLM can resolve the follow-up's pronoun
            # from the prepended topic ("What is the Claude API rate limit? How can I
            # raise it?") — handing it only the bare "How can I raise it?" makes the LLM
            # refuse a genuine anaphora it cannot disambiguate (measured). An off-corpus
            # PIVOT that opens with an anaphor ("and how do I do that on Kubernetes?") is
            # still declined: the routed chunk carries no support for the new topic, so
            # the LLM grounding-refusal net refuses (the multi-turn refusal golden cases
            # gate this). Single-turn ``retrieval_query == question`` so this is a no-op.
            llm_result = await self._generator.generate(retrieval_query, evidence)
        except LLMUnavailable as exc:
            # Slice 7 maps this to ``cost_limit_reached`` (503) when
            # the cause is 429, otherwise to ``internal_error`` (500).
            raise OrchestratorError(str(exc)) from exc

        # Validate citations; a hard-fail collapses to a no-answer
        # response carrying the citation_validation_failed reason
        # in the audit event.
        validation = validate_citations(answer_text=llm_result.text, evidence=evidence)
        if not validation.valid:
            return await self._respond_validation_failed(
                request_id=request_id,
                session_id=session_id,
                question=question,
                normalized=normalized,
                domain=domain,
                intent=intent,
                evidence=evidence,
                strategy=strategy,
                source_version_hash=source_version_hash,
                reason=validation.reason or "citation_validation_failed",
            )

        # The LLM may emit the no-answer refusal even when evidence
        # is non-empty. Honor the contract and treat it as a
        # weak-evidence fallback.
        if not validation.cited_indices and self._is_no_answer_refusal(llm_result.text):
            return await self._respond_no_answer(
                request_id=request_id,
                session_id=session_id,
                question=question,
                normalized=normalized,
                domain=domain,
                intent=intent,
                source_version_hash=source_version_hash,
                reason="no_answer",
                evidence=evidence,
                strategy=strategy,
            )

        citations: list[Citation] = [Citation(chunk_to_citation(hit)) for hit in evidence]
        # Cite-once: the response surface only shows the citations
        # the model actually referenced. The trace keeps every
        # retrieved chunk.
        cited_set = set(validation.cited_indices)
        used_indices = sorted(cited_set) if cited_set else list(range(1, len(evidence) + 1))
        used_chunk_ids = {evidence[i - 1].chunk_id for i in used_indices}
        visible_citations = [
            c for c, hit in zip(citations, evidence, strict=True) if hit.chunk_id in used_chunk_ids
        ]
        confidence = self._confidence_for(used_indices, len(evidence))

        response = await self._persist_and_respond(
            request_id=request_id,
            session_id=session_id,
            question=question,
            normalized=normalized,
            domain=domain,
            intent=intent,
            answer=llm_result.text,
            citations=visible_citations,
            evidence=evidence,
            strategy=strategy,
            source_version_hash=source_version_hash,
            confidence=confidence,
            cache_hit=False,
            cache_key=cache_key,
            cache_normalized=cache_normalized,
            vector_degrade=vector_degrade,
        )
        return response

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _respond_unsupported(
        self,
        *,
        request_id: str,
        session_id: uuid.UUID,
        question: str,
        normalized: str,
        domain: Domain,
        intent: Intent,
    ) -> AnswerResponse:
        """Persist an unsupported refusal and return the no-answer shape."""
        message_id = await self._persist_messages(
            session_id=session_id,
            question=question,
            normalized=normalized,
            domain=domain,
            intent=intent,
            answer=self._settings.unsupported_refusal,
            confidence=Confidence.none,
            evidence=[],
            citations=[],
        )
        await self._persist_audit(
            request_id=request_id,
            session_id=session_id,
            message_id=message_id,
            domain=domain,
            intent=intent,
            outcome="unsupported",
            metadata={
                "reason": "unsupported_domain",
                "retrieval_strategy": RetrievalStrategy.none.value,
            },
        )
        await self._session.flush()
        return build_no_answer_response(
            request_id=request_id,
            domain_value=domain.value,
            intent=Intent.unsupported,
            reason="unsupported",
            copy=self._settings.unsupported_refusal,
            message_id=str(message_id),
        )

    async def _respond_greeting(
        self,
        *,
        request_id: str,
        session_id: uuid.UUID,
        question: str,
        normalized: str,
        domain: Domain,
    ) -> AnswerResponse:
        """Persist and return a friendly greeting response.

        Not a refusal and not a no-answer: ``unsupported`` and ``no_answer``
        are both ``False`` and the intent is :attr:`Intent.greeting`. No
        retrieval ran, so there are no citations and the retrieval strategy
        is ``none``. A lone greeting ("hi", "good morning") classifies as
        ``unsupported`` domain; that would break the
        ``domain == unsupported`` ⟺ ``unsupported == true`` invariant on a
        non-refusal reply, so it is stamped with the neutral
        :attr:`Domain.general` instead (#89). A ``citevyn``-addressed
        greeting ("hi CiteVyn") already classifies as ``citevyn`` and keeps
        it — the greeting flags, not the domain, are the signal. The neutral
        domain is used for the persisted row, the audit trace, and the wire
        response alike, so the stored value replayed by ``GET /messages``
        agrees with the response.
        """
        response_domain = Domain.general if is_unsupported(domain) else domain
        message_id = await self._persist_messages(
            session_id=session_id,
            question=question,
            normalized=normalized,
            domain=response_domain,
            intent=Intent.greeting,
            answer=GREETING_RESPONSE,
            confidence=Confidence.none,
            evidence=[],
            citations=[],
        )
        await self._persist_audit(
            request_id=request_id,
            session_id=session_id,
            message_id=message_id,
            domain=response_domain,
            intent=Intent.greeting,
            outcome="greeting",
            metadata={
                "retrieval_strategy": RetrievalStrategy.none.value,
            },
        )
        await self._session.flush()
        return AnswerResponse(
            request_id=request_id,
            message_id=str(message_id),
            answer=GREETING_RESPONSE,
            citations=[],
            domain=response_domain.value,
            intent=Intent.greeting.value,
            confidence=Confidence.none.value,
            cache_hit=False,
            retrieval_strategy=RetrievalStrategy.none.value,
            unsupported=False,
            no_answer=False,
            source_version_hash="",
            answer_policy_version="",
        )

    async def _respond_cache_hit(
        self,
        *,
        request_id: str,
        session_id: uuid.UUID,
        question: str,
        normalized: str,
        domain: Domain,
        intent: Intent,
        source_version_hash: str,
        cached: CachedAnswer,
    ) -> AnswerResponse:
        """Persist a cache-hit response and return the cached payload."""
        message_id = await self._persist_messages(
            session_id=session_id,
            question=question,
            normalized=normalized,
            domain=domain,
            intent=intent,
            answer=cached.answer,
            confidence=cached.confidence,
            evidence=[],
            citations=[Citation(c) for c in cached.citations],
        )
        await self._persist_audit(
            request_id=request_id,
            session_id=session_id,
            message_id=message_id,
            domain=domain,
            intent=intent,
            outcome="cache_hit",
            metadata={
                "retrieval_strategy": RetrievalStrategy.cache.value,
                "source_version_hash": cached.source_version_hash,
            },
        )
        await self._session.flush()
        return AnswerResponse(
            request_id=request_id,
            message_id=str(message_id),
            answer=cached.answer,
            citations=[Citation(c) for c in cached.citations],
            domain=domain.value,
            intent=intent.value,
            confidence=cached.confidence.value,
            cache_hit=True,
            retrieval_strategy=RetrievalStrategy.cache.value,
            unsupported=False,
            no_answer=False,
            source_version_hash=cached.source_version_hash,
            answer_policy_version=cached.answer_policy_version,
        )

    async def _respond_no_answer(
        self,
        *,
        request_id: str,
        session_id: uuid.UUID,
        question: str,
        normalized: str,
        domain: Domain,
        intent: Intent,
        source_version_hash: str,
        reason: str,
        evidence: list[EvidenceHit] | None = None,
        strategy: RetrievalStrategy = RetrievalStrategy.none,
    ) -> AnswerResponse:
        """Persist and return a no-answer response (weak evidence, etc.)."""
        message_id = await self._persist_messages(
            session_id=session_id,
            question=question,
            normalized=normalized,
            domain=domain,
            intent=intent,
            answer=self._settings.no_answer_fallback,
            confidence=Confidence.none,
            evidence=evidence or [],
            citations=[],
        )
        await self._persist_audit(
            request_id=request_id,
            session_id=session_id,
            message_id=message_id,
            domain=domain,
            intent=intent,
            outcome="no_answer",
            metadata={
                "reason": reason,
                "retrieval_strategy": strategy.value,
                "source_version_hash": source_version_hash,
            },
        )
        await self._session.flush()
        return build_no_answer_response(
            request_id=request_id,
            domain_value=domain.value,
            intent=intent,
            reason=reason,
            copy=self._settings.no_answer_fallback,
            message_id=str(message_id),
            # Graceful fallback (Phase 4a): when evidence WAS retrieved for an IN-DOMAIN
            # question but the LLM declined to ground an answer, offer those nearest docs
            # instead of a bare refusal. Suppressed when the question routed to
            # ``unsupported`` (an off-corpus question that only surfaced a nearest chunk
            # via the global "answer when grounded" arm) — suggesting that cross-domain
            # doc would imply coverage we don't have and partly undo the refusal (review
            # finding 1). An empty-evidence no_answer yields [] regardless.
            suggestions=(
                build_suggestions(evidence or []) if intent is not Intent.unsupported else []
            ),
        )

    async def _respond_validation_failed(
        self,
        *,
        request_id: str,
        session_id: uuid.UUID,
        question: str,
        normalized: str,
        domain: Domain,
        intent: Intent,
        evidence: list[EvidenceHit],
        strategy: RetrievalStrategy,
        source_version_hash: str,
        reason: str,
    ) -> AnswerResponse:
        """Persist a citation-validation failure as a no-answer response.

        The reason flows into the audit event so a SRE can grep for
        the exact failure mode; the response body itself carries the
        no-answer fallback copy, not the bad LLM output, so we never
        ship a citation-incorrect answer to the user.
        """
        message_id = await self._persist_messages(
            session_id=session_id,
            question=question,
            normalized=normalized,
            domain=domain,
            intent=intent,
            answer=self._settings.no_answer_fallback,
            confidence=Confidence.none,
            evidence=evidence,
            citations=[],
        )
        await self._persist_audit(
            request_id=request_id,
            session_id=session_id,
            message_id=message_id,
            domain=domain,
            intent=intent,
            outcome="citation_validation_failed",
            metadata={
                "reason": reason,
                "retrieval_strategy": strategy.value,
                "source_version_hash": source_version_hash,
            },
        )
        await self._session.flush()
        return build_no_answer_response(
            request_id=request_id,
            domain_value=domain.value,
            intent=intent,
            reason="citation_validation_failed",
            copy=self._settings.no_answer_fallback,
            message_id=str(message_id),
            retrieval_strategy=strategy.value,
            # The retrieved docs are the nearest in-corpus content — offer them even
            # though the model's citation markers didn't validate (Phase 4a). Suppressed
            # for an off-corpus (``unsupported``) question so a cross-domain doc is not
            # offered as helpful (review finding 1).
            suggestions=(build_suggestions(evidence) if intent is not Intent.unsupported else []),
        )

    async def _persist_and_respond(
        self,
        *,
        request_id: str,
        session_id: uuid.UUID,
        question: str,
        normalized: str,
        domain: Domain,
        intent: Intent,
        answer: str,
        citations: list[Citation],
        evidence: list[EvidenceHit],
        strategy: RetrievalStrategy,
        source_version_hash: str,
        confidence: Confidence,
        cache_hit: bool,
        cache_key: str,
        cache_normalized: str,
        vector_degrade: VectorDegrade,
    ) -> AnswerResponse:
        """Persist a grounded answer, write the cache, and return the response.

        The cache write is *intentionally skipped* when ``vector_degrade`` is not
        :attr:`VectorDegrade.none` — the RUNTIME reason from the retriever that the
        vector arm actually degraded to no hits (a transient Tier-1 outage, #70, or
        a Tier-3 embedder mismatch that was truly consulted, #57). Caching an answer
        built without the vector arm would freeze that weaker answer to TTL and
        silence the degrade WARN on subsequent hits (#65). The skip is logged with a
        reason-specific event so it is observable and never indistinguishable from a
        silent drop; the answer itself is still served and persisted to the trace
        exactly as normal.
        """
        message_id = await self._persist_messages(
            session_id=session_id,
            question=question,
            normalized=normalized,
            domain=domain,
            intent=intent,
            answer=answer,
            confidence=confidence,
            evidence=evidence,
            citations=citations,
        )
        cache_written = vector_degrade is VectorDegrade.none
        if cache_written:
            await self._cache.put(
                cache_key=cache_key,
                value=CachedAnswer(
                    answer=answer,
                    citations=[dict(c) for c in citations],
                    confidence=confidence,
                    source_version_hash=source_version_hash,
                    answer_policy_version=self._settings.answer_policy_version,
                    created_at=_utcnow(),
                    ttl_expires_at=_utcnow_from_seconds(self._settings.cache_ttl_seconds),
                ),
            )
        else:
            skip_event = (
                "answer_cache_write_skipped_embedder_mismatch"
                if vector_degrade is VectorDegrade.mismatch
                else "answer_cache_write_skipped_vector_unavailable"
            )
            _logger.warning(skip_event, extra={"request_id": request_id})
        await self._persist_audit(
            request_id=request_id,
            session_id=session_id,
            message_id=message_id,
            domain=domain,
            intent=intent,
            outcome="answer",
            metadata={
                "retrieval_strategy": strategy.value,
                "source_version_hash": source_version_hash,
                "cache_hit": False,
                "cache_written": cache_written,
            },
        )
        # Backfill the normalized_question / product_area fields the cache
        # factory leaves blank so the row is queryable — only when a row was
        # actually written (a degraded answer is not cached, #65).
        if cache_written:
            await self._backfill_cache_metadata(
                cache_key=cache_key,
                normalized_question=cache_normalized,
                product_area=domain.value,
            )
        await self._session.flush()
        return AnswerResponse(
            request_id=request_id,
            message_id=str(message_id),
            answer=answer,
            citations=list(citations),
            domain=domain.value,
            intent=intent.value,
            confidence=confidence.value,
            cache_hit=cache_hit,
            retrieval_strategy=strategy.value,
            unsupported=False,
            no_answer=False,
            source_version_hash=source_version_hash,
            answer_policy_version=self._settings.answer_policy_version,
        )

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _ensure_user(self, session_id: uuid.UUID) -> str:
        """Make sure a User + Session row pair exists for ``session_id``.

        The orchestrator writes messages against a session the Slice 7
        route already created. To keep the orchestrator unit-testable
        without that scaffolding, this helper upserts the minimum
        rows needed to satisfy foreign keys when the caller passes a
        bare ``session_id``. Returns the ``user_id`` to attach to
        audit events.

        Raises ``RuntimeError`` if a real session row exists but
        references a user that the orchestrator cannot resolve —
        that means the orchestrator was handed a stale session id
        and the route layer should retry.
        """
        session = await self._session.get(Session, session_id)
        if session is not None:
            user_id = session.user_id
        else:
            # Caller passed a bare UUID with no Session row yet.
            # Create both rows so the message inserts do not trip
            # the FK on ``sessions.session_id``.
            user_id = "demo_user"
            self._session.add(
                Session(
                    session_id=session_id,
                    user_id=user_id,
                    channel="chat",
                    created_at=_utcnow(),
                    expires_at=_utcnow_from_seconds(self._settings.index_session_ttl_seconds),
                )
            )
            await self._session.flush()

        existing_user = await self._session.get(User, user_id)
        if existing_user is None:
            self._session.add(
                User(
                    user_id=user_id,
                    role=UserRole.demo_user,
                    created_at=_utcnow(),
                )
            )
            await self._session.flush()
        return user_id

    async def _persist_messages(
        self,
        *,
        session_id: uuid.UUID,
        question: str,
        normalized: str,
        domain: Domain,
        intent: Intent,
        answer: str,
        confidence: Confidence,
        evidence: list[EvidenceHit],
        citations: list[Citation],
    ) -> uuid.UUID:
        """Persist the user + assistant messages and retrieved evidence.

        Returns the assistant message id so the audit event can
        reference it. Both messages are flushed before the function
        returns so the row ids are available.
        """
        await self._ensure_user(session_id)
        now = _utcnow()
        user_msg = Message(
            session_id=session_id,
            role=MessageRole.user,
            content=question,
            normalized_query=normalized,
            domain=domain.value,
            intent=intent.value,
            created_at=now,
        )
        self._session.add(user_msg)
        await self._session.flush()

        # used_in_answer is per-evidence; we mark only the chunks
        # that appear in the citation list (or all of them when the
        # answer is a no-answer / unsupported refusal because no
        # specific chunk is cited).
        cited_chunk_ids: set[uuid.UUID] = {
            uuid.UUID(str(c["chunk_id"]))  # type: ignore[arg-type]
            for c in citations
        }
        for hit in evidence:
            self._session.add(
                RetrievedEvidence(
                    message_id=user_msg.message_id,
                    chunk_id=hit.chunk_id,
                    rank=hit.rank,
                    score=float(hit.score),
                    retrieval_type=RetrievalType(hit.retrieval_type.value),
                    used_in_answer=hit.chunk_id in cited_chunk_ids,
                )
            )
        await self._session.flush()

        assistant_msg = Message(
            session_id=session_id,
            role=MessageRole.assistant,
            content=answer,
            normalized_query=normalized,
            domain=domain.value,
            intent=intent.value,
            created_at=now,
        )
        self._session.add(assistant_msg)
        await self._session.flush()
        return assistant_msg.message_id

    async def _persist_audit(
        self,
        *,
        request_id: str,
        session_id: uuid.UUID,
        message_id: uuid.UUID,
        domain: Domain,
        intent: Intent,
        outcome: str,
        metadata: dict[str, Any],
    ) -> None:
        """Persist an ``ask_question`` audit event.

        The :mod:`app.services.audit` helper owns the row shape so the
        SRE dashboard's parser doesn't have to special-case the
        orchestrator's output.
        """
        user_id = await self._ensure_user(session_id)
        from app.services.audit import record_ask_question

        await record_ask_question(
            self._session,
            user_id=user_id,
            role=UserRole.demo_user,
            request_id=request_id,
            session_id=session_id,
            message_id=message_id,
            domain=domain.value,
            intent=intent.value,
            outcome=outcome,
            extra=metadata,
        )

    async def _backfill_cache_metadata(
        self,
        *,
        cache_key: str,
        normalized_question: str,
        product_area: str,
    ) -> None:
        """Populate the cache row's query / area columns post-write.

        :class:`PostgresAnswerCacheStore.put` writes the answer +
        citations but leaves ``normalized_question`` and
        ``product_area`` blank for performance. The orchestrator
        owns the missing metadata so the cache is queryable from
        the admin surface.
        """
        from app.models import AnswerCache

        row = await self._session.get(AnswerCache, cache_key)
        if row is None:
            return
        row.normalized_question = normalized_question
        row.product_area = product_area
        await self._session.flush()

    # ------------------------------------------------------------------
    # Pure helpers (no DB, no network)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_no_answer_refusal(answer_text: str) -> bool:
        """Match the canonical no-answer paragraph the model emits."""
        from app.llm.prompts import NO_ANSWER_REFUSAL

        stripped = answer_text.strip()
        if stripped == NO_ANSWER_REFUSAL:
            return True
        return "do not have credible source material" in stripped.lower()

    @staticmethod
    def _strategy_for(intent: Intent, evidence: list[EvidenceHit]) -> RetrievalStrategy:
        if not evidence:
            return RetrievalStrategy.none
        # Label from the evidence actually retrieved, not from intent alone. An
        # exact_lookup question whose exact index misses falls through to the
        # full hybrid path (see app/retrieval/hybrid.py), so its evidence is
        # keyword/vector — reporting "exact_lookup" then would misdescribe the
        # retrieval in both the response and the audit trail.
        if intent is Intent.exact_lookup and all(
            hit.retrieval_type == RetrievalType.exact for hit in evidence
        ):
            return RetrievalStrategy.exact_lookup
        return RetrievalStrategy.hybrid_reranked

    @staticmethod
    def _confidence_for(cited_indices: list[int], evidence_count: int) -> Confidence:
        if not cited_indices or evidence_count == 0:
            return Confidence.low
        ratio = len(cited_indices) / evidence_count
        if ratio >= 0.66:
            return Confidence.high
        if ratio >= 0.33:
            return Confidence.medium
        return Confidence.low


def _utcnow_from_seconds(seconds: int) -> datetime:
    """Return ``now() + seconds`` as a naive-or-aware UTC datetime.

    Mirrors the :func:`app.cache.answer_cache._utcnow` convention so
    the cache and the message timestamps stay comparable.
    """
    from datetime import timedelta

    return datetime.now(UTC) + timedelta(seconds=seconds)


__all__ = [
    "AnswerResponse",
    "Citation",
    "GREETING_RESPONSE",
    "Orchestrator",
    "OrchestratorError",
    "RetrievalStrategy",
    "is_greeting",
]
