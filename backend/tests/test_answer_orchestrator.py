"""Orchestrator tests.

Covers the six required paths (per Slice 6 spec):

* Grounded answer — guardrail pass → retrieval → generation →
  citation validation → cache write.
* Cache hit — warm cache → no retrieval / no LLM call.
* No-answer — empty evidence → no_answer response, nothing cached.
* Unsupported — guardrail rejects → unsupported response, nothing
  cached.
* Citation validation failure — bad ``[n]`` marker → no_answer
  response with the citation_validation_failed reason in the audit
  event, nothing cached.
* Session / message / retrieved_evidence / audit rows are persisted
  with the right shapes.

The orchestrator is wired with the in-memory SQLite engine and a
deterministic ``StubLLMClient`` so the suite is hermetic. We inject a
fake retriever that returns a pre-built :class:`EvidenceHit` list so
the tests do not depend on the hybrid retriever's keyword matching.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.answer.orchestrator import (
    Orchestrator,
    OrchestratorError,
    RetrievalStrategy,
)
from app.core.config import Settings
from app.llm.errors import LLMUnavailable
from app.llm.stub import StubLLMClient
from app.models import (
    AnswerCache,
    AuditAction,
    AuditEvent,
    Confidence,
    ExactTerm,
    Message,
    MessageRole,
    RetrievalType,
    RetrievedEvidence,
)
from app.models.enums import (
    RetrievalType as ModelRetrievalType,
)
from app.retrieval.types import EvidenceHit, RetrievalResult, VectorDegrade
from app.routing.intent import Intent
from tests.conftest import seed_catalog

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    """Hermetic settings: stub LLM, cache enabled, deterministic TTLs."""
    base: dict[str, Any] = dict(
        llm_provider="stub",
        llm_model="claude-opus-4-8",
        cache_enabled=True,
        cache_ttl_seconds=3600,
        unsupported_refusal=(
            "I can answer questions about Claude, Claude Code, Codex, and "
            "Gemini using their official documentation. I do not have "
            "credible source material in this assistant to answer that."
        ),
        no_answer_fallback=(
            "I do not have credible source material in this assistant to answer that."
        ),
    )
    base.update(overrides)
    return Settings(**base)


def _evidence(*, count: int, score: float = 1.0) -> list[EvidenceHit]:
    """Build a deterministic list of evidence bullets.

    The chunks reference random UUIDs; the orchestrator only uses
    them to look up the chunk_id and to populate
    :class:`RetrievedEvidence`. Real Chunk rows are not required
    because the tests do not need the FK to resolve.
    """
    out: list[EvidenceHit] = []
    for i in range(count):
        out.append(
            EvidenceHit(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                product_area="claude_code",
                source_name="docs.test",
                document_title=f"Doc {i + 1}",
                section_path="/x",
                heading="H",
                parent_heading=None,
                chunk_text=f"snippet {i + 1}",
                context_summary="summary",
                source_url=f"https://docs.test/{i + 1}",
                score=score,
                retrieval_type=RetrievalType.hybrid,
                rank=i + 1,
            )
        )
    return out


class _FakeRetriever:
    """In-memory retriever that returns a pre-built evidence list.

    ``vector_degrade`` lets a test simulate what the real hybrid retriever reports
    when the vector arm degraded at runtime (a Tier-3 mismatch or a transient
    Tier-1 outage) so the orchestrator's cache-write gate can be exercised without
    the full hybrid wiring (#70/#72).
    """

    def __init__(
        self,
        evidence: list[EvidenceHit],
        *,
        vector_degrade: VectorDegrade = VectorDegrade.none,
    ) -> None:
        self._evidence = evidence
        self._vector_degrade = vector_degrade
        self.calls: list[dict[str, Any]] = []
        self.multi_calls: list[dict[str, Any]] = []

    async def retrieve(
        self,
        question: str,
        *,
        product_area: str | None,
        intent: Intent,
        limit: int,
        top_k: int,
    ) -> RetrievalResult:
        self.calls.append(
            {
                "question": question,
                "product_area": product_area,
                "intent": intent,
                "limit": limit,
                "top_k": top_k,
            }
        )
        return RetrievalResult(hits=list(self._evidence), vector_degrade=self._vector_degrade)

    async def retrieve_multi(
        self,
        question: str,
        *,
        product_areas: list[str],
        intent: Intent,
        limit: int,
        top_k: int,
    ) -> RetrievalResult:
        # Records the multi-hop routing; the merge/degrade logic is exercised at the
        # HybridRetriever level (test_retrieval_vector_gate / a dedicated retriever test).
        self.multi_calls.append(
            {"question": question, "product_areas": product_areas, "intent": intent}
        )
        return RetrievalResult(hits=list(self._evidence), vector_degrade=self._vector_degrade)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_index_version(session: Any) -> str:
    """Insert an active IndexVersion row and return its source version hash."""
    from datetime import UTC, datetime

    from app.models import IndexStatus, IndexVersion

    version = IndexVersion(
        index_version="index_v1",
        status=IndexStatus.active,
        source_version_hash="sha256:index-v1",
        created_at=datetime.now(UTC),
        promoted_at=datetime.now(UTC),
    )
    session.add(version)
    await session.flush()
    return version.source_version_hash


# ---------------------------------------------------------------------------
# 1. Grounded answer path
# ---------------------------------------------------------------------------


async def test_grounded_answer_persists_messages_and_evidence_and_caches(
    session: Any,
) -> None:
    """The full happy path: grounded answer, cache write, four
    persistence rows."""
    settings = _settings()
    source_version_hash = await _seed_index_version(session)
    retriever = _FakeRetriever(_evidence(count=2))
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    response = await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_grounded",
        session_id=uuid.uuid4(),
    )

    # Response shape.
    assert response["no_answer"] is False
    assert response["unsupported"] is False
    assert response["cache_hit"] is False
    assert response["retrieval_strategy"] == RetrievalStrategy.hybrid_reranked.value
    # The stub cites [1] only, so 1 of 2 evidence is cited → medium.
    assert response["confidence"] == Confidence.medium.value
    assert response["domain"] == "claude_code"
    assert response["intent"] == "how_to"
    assert response["source_version_hash"] == source_version_hash
    assert response["answer_policy_version"] == settings.answer_policy_version
    assert "[1]" in response["answer"]  # stub emits a citation marker
    # Citation list excludes the chunk we did not cite (the model only
    # cites [1]); the trace still records both.
    assert len(response["citations"]) == 1
    assert response["citations"][0]["source_name"] == "docs.test"

    # Persistence: 1 user message, 1 assistant message, 2 evidence rows,
    # 1 audit event.
    msg_list = list((await session.execute(select(Message))).scalars().all())
    assert len(msg_list) == 2
    user_msgs = [m for m in msg_list if m.role == MessageRole.user]
    assistant_msgs = [m for m in msg_list if m.role == MessageRole.assistant]
    assert len(user_msgs) == 1
    assert len(assistant_msgs) == 1
    user_msg = user_msgs[0]
    assistant_msg = assistant_msgs[0]
    assert user_msg.content == "How do I configure Claude Code permissions?"
    assert user_msg.domain == "claude_code"
    assert user_msg.intent == "how_to"
    assert assistant_msg.content == response["answer"]
    assert assistant_msg.normalized_query is not None

    evidence_rows = (await session.execute(select(RetrievedEvidence))).scalars().all()
    assert len(evidence_rows) == 2
    assert {e.rank for e in evidence_rows} == {1, 2}
    used = {e.chunk_id for e in evidence_rows if e.used_in_answer}
    unused = {e.chunk_id for e in evidence_rows if not e.used_in_answer}
    assert used == {uuid.UUID(response["citations"][0]["chunk_id"])}
    assert len(unused) == 1
    assert all(e.retrieval_type == ModelRetrievalType.hybrid for e in evidence_rows)

    audit_rows = (await session.execute(select(AuditEvent))).scalars().all()
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.action == AuditAction.ask_question
    assert audit.resource_id == str(assistant_msg.message_id)
    assert audit.metadata_["outcome"] == "answer"
    assert audit.metadata_["retrieval_strategy"] == "hybrid_reranked"
    assert audit.metadata_["source_version_hash"] == source_version_hash
    assert audit.metadata_["domain"] == "claude_code"
    assert audit.metadata_["intent"] == "how_to"

    # Cache row written through.
    cache_rows = (await session.execute(select(AnswerCache))).scalars().all()
    assert len(cache_rows) == 1
    assert cache_rows[0].answer == response["answer"]
    assert cache_rows[0].source_version_hash == source_version_hash
    assert cache_rows[0].answer_policy_version == settings.answer_policy_version
    assert cache_rows[0].normalized_question == "how do i configure claude code permissions?"
    assert cache_rows[0].product_area == "claude_code"

    # Retriever was called once with the expected args.
    assert len(retriever.calls) == 1
    assert retriever.calls[0]["intent"] is Intent.how_to


# ---------------------------------------------------------------------------
# 2. Cache hit path
# ---------------------------------------------------------------------------


async def test_cache_hit_skips_retrieval_and_llm(session: Any) -> None:
    """A warm cache must bypass retrieval entirely and the LLM call
    must not be made."""
    source_version_hash = await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=2))
    # Wrap the LLM in a spy so we can assert it was not called.
    llm = StubLLMClient()
    llm_spy = AsyncMock(wraps=llm)

    # Warm the cache: the first call writes through; the second
    # should hit it.
    orchestrator = Orchestrator(settings, session, llm=llm_spy, retriever=retriever)
    first = await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_first",
        session_id=uuid.uuid4(),
    )
    assert first["cache_hit"] is False

    # Second request: same question, same session_id family
    # (orchestrator doesn't require a real Session row).
    second = await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_second",
        session_id=uuid.uuid4(),
    )

    assert second["cache_hit"] is True
    assert second["retrieval_strategy"] == RetrievalStrategy.cache.value
    assert second["answer"] == first["answer"]
    assert second["source_version_hash"] == source_version_hash

    # The retriever was called only on the first (uncached) request.
    assert len(retriever.calls) == 1
    # LLM was called only once (during the warm-up).
    assert llm_spy.complete.await_count == 1

    # Persistence: each request wrote its own user + assistant
    # message and audit event. The cache row remains unique.
    assert len((await session.execute(select(Message))).scalars().all()) == 4
    assert len((await session.execute(select(AuditEvent))).scalars().all()) == 2
    assert len((await session.execute(select(AnswerCache))).scalars().all()) == 1


# ---------------------------------------------------------------------------
# 3. No-answer path (empty evidence)
# ---------------------------------------------------------------------------


async def test_empty_evidence_returns_no_answer_without_caching(
    session: Any,
) -> None:
    """Empty retrieval results in a no_answer response and no cache
    write."""
    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(evidence=[])
    llm_spy = AsyncMock(wraps=StubLLMClient())
    orchestrator = Orchestrator(settings, session, llm=llm_spy, retriever=retriever)

    response = await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_noanswer",
        session_id=uuid.uuid4(),
    )

    assert response["no_answer"] is True
    assert response["unsupported"] is False
    assert response["confidence"] == "none"
    assert response["retrieval_strategy"] == RetrievalStrategy.none.value
    assert response["citations"] == []
    assert response["answer"] == settings.no_answer_fallback
    assert response["domain"] == "claude_code"
    assert response["intent"] == "how_to"

    # No cache write.
    assert (await session.execute(select(AnswerCache))).scalars().all() == []
    # LLM was not called because retrieval was empty.
    assert llm_spy.complete.await_count == 0

    # Audit event records the no_answer outcome.
    audits = (await session.execute(select(AuditEvent))).scalars().all()
    assert len(audits) == 1
    assert audits[0].metadata_["outcome"] == "no_answer"
    assert audits[0].metadata_["reason"] == "weak_evidence"
    # The audit records the ACTUAL retrieval strategy (hybrid ran but
    # produced no evidence), not a hardcoded "none" — the observability
    # the no_answer-strategy change (Issue #81 / F3) exists to provide.
    assert audits[0].metadata_["retrieval_strategy"] == RetrievalStrategy.hybrid_reranked.value


async def test_embedder_outage_with_no_evidence_raises_transient_error_not_refusal(
    session: Any,
) -> None:
    """A transient embedding-provider outage must surface as a transient error.

    When the embedding provider is transiently unavailable (OpenRouter not
    responding, a timeout, a provider usage limit), the hybrid retriever degrades
    the vector arm to no hits and reports ``VectorDegrade.unavailable``. If the
    remaining arms also found nothing, "no source" is UNTRUSTWORTHY — the grounded
    answer may exist and we simply could not retrieve it. The orchestrator must
    raise :class:`OrchestratorError` (→ a 5xx with a generic, non-technical
    "temporarily unavailable" envelope, no provider detail leaked) rather than
    return a content ``no_answer`` refusal. The content-refusal 200 was the bug:
    it mislabels an infra outage as "the corpus has no answer" AND, because the
    client records a 200-refusal as a *successful* answer, silently blocks
    retry-on-re-ask.
    """
    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(evidence=[], vector_degrade=VectorDegrade.unavailable)
    llm_spy = AsyncMock(wraps=StubLLMClient())
    orchestrator = Orchestrator(settings, session, llm=llm_spy, retriever=retriever)

    with pytest.raises(OrchestratorError):
        await orchestrator.ask(
            question="How do I configure Claude Code permissions?",
            request_id="req_embedder_down",
            session_id=uuid.uuid4(),
        )

    # A transient failure must not burn an LLM call, must not persist a refusal to
    # the answer cache, and must not record a no_answer audit outcome — the request
    # never produced a real answer, so there is nothing legitimate to cache/replay.
    assert llm_spy.complete.await_count == 0
    assert (await session.execute(select(AnswerCache))).scalars().all() == []


async def test_empty_evidence_without_outage_still_refuses(session: Any) -> None:
    """The transient-error path is guarded on the outage flag, not empty evidence.

    A genuinely empty retrieval whose vector arm ran cleanly
    (``VectorDegrade.none``) is a real content no_answer — it must keep refusing,
    not be misread as an outage. This pins the guard so a future refactor cannot
    turn every empty result into a 5xx.
    """
    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(evidence=[], vector_degrade=VectorDegrade.none)
    orchestrator = Orchestrator(
        settings, session, llm=AsyncMock(wraps=StubLLMClient()), retriever=retriever
    )

    response = await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_clean_empty",
        session_id=uuid.uuid4(),
    )
    assert response["no_answer"] is True


async def test_llm_refusal_with_evidence_records_runtime_strategy(
    session: Any,
) -> None:
    """LLM emits the no-answer refusal despite non-empty evidence.

    The LLM-refusal branch passes the ACTUAL runtime strategy to the audit,
    not a hardcoded ``none`` — the other half of the Issue #81 / F3
    observability change (the weak-evidence branch is covered above). Without
    this test, reverting ``strategy=strategy`` to the default on that branch
    would break nothing.
    """
    from app.llm.prompts import NO_ANSWER_REFUSAL
    from app.llm.types import LLMResult

    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=2))
    refusing_llm = AsyncMock()
    refusing_llm.complete.return_value = LLMResult(
        text=NO_ANSWER_REFUSAL,
        input_tokens=1,
        output_tokens=1,
        model="stub-deterministic-v1",
        provider="stub",
    )
    orchestrator = Orchestrator(settings, session, llm=refusing_llm, retriever=retriever)

    response = await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_llm_refusal",
        session_id=uuid.uuid4(),
    )

    assert response["no_answer"] is True
    audits = (await session.execute(select(AuditEvent))).scalars().all()
    assert len(audits) == 1
    assert audits[0].metadata_["outcome"] == "no_answer"
    assert audits[0].metadata_["reason"] == "no_answer"
    # Non-empty evidence WAS retrieved and reranked, so the audit records
    # hybrid_reranked — not the default none.
    assert audits[0].metadata_["retrieval_strategy"] == RetrievalStrategy.hybrid_reranked.value


# ---------------------------------------------------------------------------
# 4. Unsupported path
# ---------------------------------------------------------------------------


async def test_unsupported_offcorpus_refuses_after_global_retrieval(
    session: Any,
) -> None:
    """ "Answer when grounded" (Phase 2): a genuinely off-corpus question now
    retrieves GLOBALLY (product_area=None), and — finding nothing confident (the
    confidence gate drops it, modeled here by an empty retriever) — falls back to
    the SAME helpful unsupported refusal, with no cache write and no LLM call."""
    settings = _settings()
    retriever = _FakeRetriever([])  # gate drops the off-corpus query → no evidence
    llm_spy = AsyncMock(wraps=StubLLMClient())
    orchestrator = Orchestrator(settings, session, llm=llm_spy, retriever=retriever)

    response = await orchestrator.ask(
        question="What is the recipe for chocolate cake?",
        request_id="req_unsupported",
        session_id=uuid.uuid4(),
    )

    assert response["unsupported"] is True
    assert response["no_answer"] is True
    assert response["domain"] == "unsupported"
    assert response["intent"] == "unsupported"
    assert response["answer"] == settings.unsupported_refusal

    # Retrieval WAS attempted globally (the new behavior), but no LLM, no cache.
    assert len(retriever.calls) == 1
    assert retriever.calls[0]["product_area"] is None  # global "answer when grounded"
    assert llm_spy.complete.await_count == 0
    assert (await session.execute(select(AnswerCache))).scalars().all() == []

    # Audit event still records the unsupported outcome.
    audits = (await session.execute(select(AuditEvent))).scalars().all()
    assert len(audits) == 1
    assert audits[0].metadata_["outcome"] == "unsupported"
    assert audits[0].metadata_["reason"] == "unsupported_domain"


class _RewritingLLM:
    """A non-stub LLM whose ``complete`` returns a fixed text. Used to prove the #112 rewrite
    is a PURE RECALL improver: even when it (adversarially) injects a product name into a
    pivot, routing must NOT flip to the scoped path — the query stays global (gated)."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def complete(self, *, system: str, user: str, max_tokens: int, temperature: float):
        from app.llm.types import LLMResult

        return LLMResult(
            text=self._reply, input_tokens=1, output_tokens=1, model="fake", provider="router"
        )

    async def aclose(self) -> None: ...


async def test_content_noun_pivot_rewrite_never_hijacks_routing_to_scoped(session: Any) -> None:
    """#112 anti-hijack: a pivot follow-up ("what's the weather?") after a product turn routes
    ``unsupported`` and the LLM rewrite fires — but because routing is fixed from the ORIGINAL
    query, even a rewrite that injects "Claude API" retrieves GLOBALLY (product_area=None,
    confidence-gated), never the scoped un-gated path. Empty evidence → still refuses."""
    from datetime import UTC, datetime

    # A real provider (not stub) so the entity-aware rewrite is enabled; the injected LLM
    # below is used directly, so no network call happens.
    settings = _settings(llm_provider="router")
    # A prior product turn so recent_user_questions is non-empty and the rewrite fires.
    session_id = uuid.uuid4()
    session.add(
        Message(
            session_id=session_id,
            role=MessageRole.user,
            content="What is the rate limit for the Claude API?",
            normalized_query="what is the rate limit for the claude api?",
            domain=None,
            intent=None,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    retriever = _FakeRetriever([])  # gate drops the off-corpus query → no evidence
    # The rewrite adversarially injects a product token — the worst case for hijacking.
    hijacking_llm = _RewritingLLM("What is the Claude API weather tomorrow?")
    orchestrator = Orchestrator(settings, session, llm=hijacking_llm, retriever=retriever)

    response = await orchestrator.ask(
        question="What's the weather tomorrow?",
        request_id="req_pivot",
        session_id=session_id,
    )

    # Refused (empty evidence on the global gated path), exactly as today.
    assert response["unsupported"] is True and response["no_answer"] is True
    # The rewrite changed the retrieval TEXT but NOT the routing: still global, never scoped.
    assert len(retriever.calls) == 1
    assert retriever.calls[0]["product_area"] is None  # NOT "claude_api"
    assert retriever.calls[0]["question"] == "What is the Claude API weather tomorrow?"


async def test_multihop_question_routes_to_retrieve_multi(session: Any) -> None:
    """A cross-product question retrieves EACH named product area (retrieve_multi),
    not the single first-match domain (Phase 3)."""
    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=2))
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    response = await orchestrator.ask(
        question="How do the rate limits compare between the Claude API and Gemini?",
        request_id="req_multihop",
        session_id=uuid.uuid4(),
    )

    assert response["no_answer"] is False
    assert response["unsupported"] is False
    # retrieve_multi was used with BOTH areas; the single-domain retrieve was not.
    assert len(retriever.multi_calls) == 1
    assert retriever.multi_calls[0]["product_areas"] == ["claude_api", "gemini_api"]
    assert retriever.calls == []


async def test_multihop_embedder_outage_with_no_evidence_also_raises_transient_error(
    session: Any,
) -> None:
    """The outage guard covers the multi-hop ``retrieve_multi`` path too.

    A cross-product question routes to ``retrieve_multi``, which combines each
    area's degrade reason. When that reports ``VectorDegrade.unavailable`` and no
    evidence survived, the transient-error guard must fire exactly as it does on the
    single-domain path — a transient outage is not a content refusal regardless of
    how many areas were queried.
    """
    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(evidence=[], vector_degrade=VectorDegrade.unavailable)
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    with pytest.raises(OrchestratorError):
        await orchestrator.ask(
            question="How do the rate limits compare between the Claude API and Gemini?",
            request_id="req_multihop_outage",
            session_id=uuid.uuid4(),
        )
    # It really went through the multi-hop arm, not the single-domain one.
    assert len(retriever.multi_calls) == 1
    assert retriever.calls == []


async def test_single_product_question_still_routes_to_retrieve(session: Any) -> None:
    """A single-product question uses the normal single-domain retrieve, not multi."""
    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=2))
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    await orchestrator.ask(
        question="What is the rate limit for the Claude API?",
        request_id="req_single",
        session_id=uuid.uuid4(),
    )
    assert len(retriever.calls) == 1
    assert retriever.calls[0]["product_area"] == "claude_api"
    assert retriever.multi_calls == []


async def test_answer_when_grounded_flag_off_restores_refuse_early(
    session: Any,
) -> None:
    """The documented kill-switch: with ``answer_when_grounded=False`` an
    unsupported-routed question refuses BEFORE any retrieval (the pre-Phase-2
    behavior) — no retrieval, no LLM, no cache. Guards the rollback path."""
    settings = _settings(answer_when_grounded=False)
    retriever = _FakeRetriever(_evidence(count=2))  # would answer if consulted
    llm_spy = AsyncMock(wraps=StubLLMClient())
    orchestrator = Orchestrator(settings, session, llm=llm_spy, retriever=retriever)

    response = await orchestrator.ask(
        question="What is the recipe for chocolate cake?",
        request_id="req_flag_off",
        session_id=uuid.uuid4(),
    )

    assert response["unsupported"] is True
    assert response["answer"] == settings.unsupported_refusal
    # Refused early: the retriever and LLM were never consulted, nothing cached.
    assert retriever.calls == []
    assert llm_spy.complete.await_count == 0
    assert (await session.execute(select(AnswerCache))).scalars().all() == []


async def test_unsupported_but_grounded_question_answers_globally(
    session: Any,
) -> None:
    """The new capability: an unsupported-routed question that DOES find confident
    global evidence is answered (not refused) — the whole point of "answer when
    grounded". The retriever is called with product_area=None."""
    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=2))  # confident evidence survives the gate
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    response = await orchestrator.ask(
        question="How do I restrict which tools the coding assistant may run?",
        request_id="req_grounded",
        session_id=uuid.uuid4(),
    )

    assert response["unsupported"] is False
    assert response["no_answer"] is False
    assert response["domain"] == "unsupported"  # routed unsupported, but answered
    assert len(retriever.calls) == 1
    assert retriever.calls[0]["product_area"] is None


# ---------------------------------------------------------------------------
# 4b. Conversation memory (Phase 3b)
# ---------------------------------------------------------------------------


async def test_followup_resolves_against_prior_turn(session: Any) -> None:
    """An anaphoric follow-up ("How can I raise it?") after a Claude-API turn is
    contextualized: retrieval sees the resolved query and scopes to claude_api (a
    product), NOT the global unsupported arm it would hit single-turn."""
    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=2))
    sid = uuid.uuid4()
    orch = Orchestrator(settings, session, retriever=retriever)

    # Turn 1 — a self-contained product question (establishes the topic).
    await orch.ask(
        question="What is the rate limit for the Claude API?",
        request_id="req_t1",
        session_id=sid,
    )
    # Turn 2 — the anaphoric follow-up on the SAME session.
    response = await orch.ask(question="How can I raise it?", request_id="req_t2", session_id=sid)

    assert response["no_answer"] is False
    assert response["unsupported"] is False
    # The follow-up was routed to the product topic, not the global unsupported arm.
    followup_call = retriever.calls[-1]
    assert followup_call["product_area"] == "claude_api"
    assert "Claude API" in followup_call["question"]  # prior turn was prepended
    assert followup_call["question"].endswith("How can I raise it?")
    # The persisted user message keeps the ORIGINAL utterance, not the rewrite.
    user_msgs = (
        (await session.execute(select(Message).where(Message.role == MessageRole.user)))
        .scalars()
        .all()
    )
    assert any(m.content == "How can I raise it?" for m in user_msgs)


async def test_offtopic_followup_is_not_hijacked_into_prior_product(session: Any) -> None:
    """Adversarial R1: a full off-topic sentence mid-session must NOT borrow the prior
    product topic — it carries no anaphora, so it stays unsupported (global arm) and,
    finding nothing, refuses. It must never be scoped to claude_api."""
    await _seed_index_version(session)
    settings = _settings()
    # Empty retriever → the global arm finds nothing confident → refusal.
    retriever = _FakeRetriever([])
    llm_spy = AsyncMock(wraps=StubLLMClient())
    sid = uuid.uuid4()
    orch = Orchestrator(settings, session, llm=llm_spy, retriever=retriever)

    # Turn 1 — a product question (would be the hijack antecedent).
    scoped_retriever = _FakeRetriever(_evidence(count=2))
    orch_t1 = Orchestrator(settings, session, retriever=scoped_retriever)
    await orch_t1.ask(
        question="What is the rate limit for the Claude API?",
        request_id="req_t1",
        session_id=sid,
    )
    # Turn 2 — a self-contained off-topic sentence.
    response = await orch.ask(
        question="What's the weather in Paris tomorrow?",
        request_id="req_t2",
        session_id=sid,
    )

    assert response["unsupported"] is True
    assert response["domain"] == "unsupported"
    # Retrieval, if attempted, went to the GLOBAL arm — never scoped to claude_api.
    assert all(c["product_area"] is None for c in retriever.calls)
    assert llm_spy.complete.await_count == 0


async def test_followup_cache_key_differs_by_prior_topic(session: Any) -> None:
    """Adversarial R3/#6: the SAME anaphoric follow-up text under DIFFERENT prior
    topics must get DISTINCT cache keys — session B must not be served session A's
    cached answer."""
    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=2))
    sid_a, sid_b = uuid.uuid4(), uuid.uuid4()
    orch = Orchestrator(settings, session, retriever=retriever)

    # Session A: Claude prior, then the follow-up → caches under the resolved key.
    await orch.ask(
        question="What is the rate limit for the Claude API?",
        request_id="a1",
        session_id=sid_a,
    )
    await orch.ask(question="How can I raise it?", request_id="a2", session_id=sid_a)

    # Session B: Gemini prior, then the SAME follow-up text.
    await orch.ask(
        question="Which header carries the Gemini API key?",
        request_id="b1",
        session_id=sid_b,
    )
    resp_b = await orch.ask(question="How can I raise it?", request_id="b2", session_id=sid_b)

    # B's follow-up is NOT a cache hit — its resolved query (Gemini) keys differently.
    assert resp_b["cache_hit"] is False
    cache_rows = (await session.execute(select(AnswerCache))).scalars().all()
    normalized_qs = {r.normalized_question for r in cache_rows}
    # Two DISTINCT resolved follow-up queries were cached, not one shared key.
    assert any("claude api" in q and "raise it" in q for q in normalized_qs)
    assert any("gemini" in q and "raise it" in q for q in normalized_qs)


async def test_anaphoric_pivot_followup_still_declines_when_llm_refuses(session: Any) -> None:
    """Adversarial R1 / refusal-safety: a follow-up that opens with an anaphor/ellipsis
    but PIVOTS to an off-corpus topic ("and how do I do that on Kubernetes?") IS
    contextualized (routed to the prior product, retrieves that chunk) — memory must not
    bypass the refusal. The LLM grounding-refusal net is the authoritative gate: when it
    declines (the routed chunk has no support for the new topic), the orchestrator
    returns no_answer, exactly as it would single-turn. Proven against the real LLM in
    the judged eval (the k8s pivot refuses); here we lock that memory routing HONORS the
    LLM's refusal rather than forcing a grounded answer out of the prepended antecedent.
    """
    from app.llm.prompts import NO_ANSWER_REFUSAL
    from app.llm.types import LLMResult

    await _seed_index_version(session)
    settings = _settings()
    # The rewrite routes the pivot to claude_api and retrieves the prior chunk...
    retriever = _FakeRetriever(_evidence(count=2))
    # ...but the grounding-refusal net declines it (no support for the new topic).
    refusing_llm = AsyncMock()
    refusing_llm.complete.return_value = LLMResult(
        text=NO_ANSWER_REFUSAL, input_tokens=1, output_tokens=1, model="stub", provider="stub"
    )
    sid = uuid.uuid4()
    orch = Orchestrator(settings, session, llm=refusing_llm, retriever=retriever)

    await orch.ask(
        question="What is the rate limit for the Claude API?", request_id="t1", session_id=sid
    )
    response = await orch.ask(
        question="and how do I do that on Kubernetes?", request_id="t2", session_id=sid
    )

    # Memory routed it (retrieval ran, scoped to the product) but the LLM refusal wins.
    assert response["no_answer"] is True
    assert retriever.calls[-1]["product_area"] == "claude_api"  # was contextualized
    # The pivot turn was NOT short-circuited to a refusal — it retrieved + consulted the
    # LLM (turn 1 + the pivot = 2 calls), and the LLM's decline is what produced no_answer.
    assert refusing_llm.complete.await_count == 2


async def test_no_answer_with_evidence_surfaces_nearest_doc_suggestions(session: Any) -> None:
    """Graceful fallback (Phase 4a): when evidence was retrieved but the LLM declined,
    the no_answer response offers the nearest docs as suggestions instead of a bare
    refusal — deduped by source, projecting title/url/product_area."""
    from app.llm.prompts import NO_ANSWER_REFUSAL
    from app.llm.types import LLMResult

    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=2))
    refusing_llm = AsyncMock()
    refusing_llm.complete.return_value = LLMResult(
        text=NO_ANSWER_REFUSAL, input_tokens=1, output_tokens=1, model="stub", provider="stub"
    )
    orch = Orchestrator(settings, session, llm=refusing_llm, retriever=retriever)

    response = await orch.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_sugg",
        session_id=uuid.uuid4(),
    )

    assert response["no_answer"] is True
    suggestions = response["suggestions"]
    assert len(suggestions) >= 1
    assert set(suggestions[0]) == {"title", "url", "product_area"}
    # The two _evidence hits share source "docs.test" → deduped to one suggestion.
    assert len(suggestions) == 1
    assert suggestions[0]["title"] == "Doc 1"


async def test_unsupported_refusal_has_no_suggestions(session: Any) -> None:
    """A truly off-corpus refusal (no evidence retrieved) stays a CLEAN refusal — no
    suggestions to avoid implying coverage we don't have."""
    settings = _settings()
    retriever = _FakeRetriever([])  # gate drops the off-corpus query → no evidence
    orch = Orchestrator(settings, session, retriever=retriever)

    response = await orch.ask(
        question="What is the recipe for chocolate cake?",
        request_id="req_unsupp_sugg",
        session_id=uuid.uuid4(),
    )

    assert response["unsupported"] is True
    assert response["suggestions"] == []


async def test_offcorpus_declined_with_evidence_has_no_suggestions(session: Any) -> None:
    """Review finding 1: an OFF-CORPUS question routes to ``unsupported`` but the global
    "answer when grounded" arm may surface a nearest (cross-domain) chunk that the LLM
    then declines. Suggesting THAT doc would imply coverage we don't have — so a
    ``unsupported``-intent no_answer must have NO suggestions even when evidence exists."""
    from app.llm.prompts import NO_ANSWER_REFUSAL
    from app.llm.types import LLMResult

    await _seed_index_version(session)
    settings = _settings()
    # The global arm surfaced a nearest chunk (evidence non-empty)...
    retriever = _FakeRetriever(_evidence(count=2))
    # ...but the grounding-refusal net declines the off-corpus question.
    refusing_llm = AsyncMock()
    refusing_llm.complete.return_value = LLMResult(
        text=NO_ANSWER_REFUSAL, input_tokens=1, output_tokens=1, model="stub", provider="stub"
    )
    orch = Orchestrator(settings, session, llm=refusing_llm, retriever=retriever)

    response = await orch.ask(
        question="How do I call the OpenAI GPT-4 API?",
        request_id="req_offcorpus_sugg",
        session_id=uuid.uuid4(),
    )

    assert response["no_answer"] is True
    assert response["domain"] == "unsupported"
    assert response["suggestions"] == []  # cross-domain doc NOT offered as helpful


async def test_conversation_memory_flag_off_disables_rewrite(session: Any) -> None:
    """The kill-switch: with ``conversation_memory=False`` a follow-up is NOT rewritten
    — it stays unsupported (global arm), the pre-Phase-3b behavior."""
    await _seed_index_version(session)
    settings = _settings(conversation_memory=False)
    retriever = _FakeRetriever(_evidence(count=2))
    sid = uuid.uuid4()
    orch = Orchestrator(settings, session, retriever=retriever)

    await orch.ask(
        question="What is the rate limit for the Claude API?",
        request_id="t1",
        session_id=sid,
    )
    await orch.ask(question="How can I raise it?", request_id="t2", session_id=sid)

    # Flag off → the follow-up was NOT scoped to claude_api; it hit the global arm.
    assert retriever.calls[-1]["product_area"] is None
    assert retriever.calls[-1]["question"] == "How can I raise it?"


# ---------------------------------------------------------------------------
# 4c. CiteVyn alias canonicalization reaches retrieval (#84 item 1)
# ---------------------------------------------------------------------------
#
# Routing the alias is only half the fix. "what is sitewin?" routes to ``citevyn``
# from the guardrail alone, but its ONLY content word is the mangled token, which
# appears nowhere in the corpus — so both retrieval arms come back empty and the
# user still gets the refusal. ``canonicalize_product_name`` in ``ask`` is what
# closes that, and these tests cover the WIRING: the unit tests in
# test_guardrails_domain.py exercise the function in isolation and stay green even
# if the orchestrator stops calling it.


async def test_alias_is_canonicalized_before_retrieval(session: Any) -> None:
    """The query handed to the retriever must carry the canonical name, not the
    mangled one. Deleting the call site in ``ask`` must fail HERE."""
    await _seed_index_version(session)
    retriever = _FakeRetriever(_evidence(count=2))
    orch = Orchestrator(_settings(), session, retriever=retriever)

    await orch.ask(
        question="Is sitewin free to use right now?",
        request_id="alias_1",
        session_id=uuid.uuid4(),
    )

    assert retriever.calls[-1]["question"] == "Is CiteVyn free to use right now?"
    assert retriever.calls[-1]["product_area"] == "citevyn"


async def test_alias_canonicalization_reaches_the_generator(session: Any) -> None:
    """Generation sees the canonical name too — otherwise the LLM is asked about a
    product whose name appears in none of the evidence it was handed."""
    await _seed_index_version(session)
    retriever = _FakeRetriever(_evidence(count=2))
    llm_spy = AsyncMock(wraps=StubLLMClient())
    orch = Orchestrator(_settings(), session, llm=llm_spy, retriever=retriever)

    await orch.ask(question="what is sitewin?", request_id="alias_2", session_id=uuid.uuid4())

    prompt = llm_spy.complete.await_args.kwargs["user"]
    assert "CiteVyn" in prompt
    assert "sitewin" not in prompt


async def test_alias_canonicalization_does_not_rewrite_the_persisted_message(
    session: Any,
) -> None:
    """The transcript must show what the user actually typed. Canonicalization rebinds
    ``retrieval_query``; rebinding ``question`` instead would garble the persisted
    message, the audit trail, and the prior turns conversation memory reads back."""
    await _seed_index_version(session)
    retriever = _FakeRetriever(_evidence(count=2))
    orch = Orchestrator(_settings(), session, retriever=retriever)

    await orch.ask(question="what is sitewin?", request_id="alias_3", session_id=uuid.uuid4())

    user_msgs = (
        (await session.execute(select(Message).where(Message.role == MessageRole.user)))
        .scalars()
        .all()
    )
    assert [m.content for m in user_msgs] == ["what is sitewin?"]


async def test_non_alias_question_is_not_rewritten(session: Any) -> None:
    """Regression guard: canonicalization is a no-op for every question that contains
    no alias, so no existing single-turn path changes."""
    await _seed_index_version(session)
    retriever = _FakeRetriever(_evidence(count=2))
    orch = Orchestrator(_settings(), session, retriever=retriever)

    await orch.ask(
        question="How do I configure Claude Code permissions?",
        request_id="alias_4",
        session_id=uuid.uuid4(),
    )

    assert retriever.calls[-1]["question"] == "How do I configure Claude Code permissions?"


async def test_ambiguous_alias_in_ordinary_english_is_not_rewritten(session: Any) -> None:
    """The costly failure: ordinary English containing an alias-like phrase must not be
    silently rewritten and answered from the CiteVyn docs. "may the best site win!" is a
    set phrase that an earlier version of the matcher turned into "may the best CiteVyn!"."""
    await _seed_index_version(session)
    retriever = _FakeRetriever([])
    orch = Orchestrator(_settings(), session, retriever=retriever)

    response = await orch.ask(
        question="may the best site win!",
        request_id="alias_5",
        session_id=uuid.uuid4(),
    )

    assert response["domain"] == "unsupported"
    assert all(c["question"] == "may the best site win!" for c in retriever.calls)
    assert all(c["product_area"] is None for c in retriever.calls)


# ---------------------------------------------------------------------------
# 5. Citation validation failure
# ---------------------------------------------------------------------------


async def test_citation_validation_failure_returns_no_answer_with_audit(
    session: Any,
) -> None:
    """A bad citation marker collapses to a no_answer response with
    the citation_validation_failed reason in the audit event and
    nothing written to the cache."""
    await _seed_index_version(session)
    settings = _settings()

    class _BadLLM:
        async def complete(self, **_kwargs: Any) -> Any:
            from app.llm.types import LLMResult

            return LLMResult(
                text="This answer cites a non-existent bullet [99].",
                input_tokens=10,
                output_tokens=8,
                model="bad-stub",
                provider="stub",
            )

        async def aclose(self) -> None:
            return None

    retriever = _FakeRetriever(_evidence(count=2))
    orchestrator = Orchestrator(settings, session, llm=_BadLLM(), retriever=retriever)

    response = await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_citation_fail",
        session_id=uuid.uuid4(),
    )

    assert response["no_answer"] is True
    assert response["unsupported"] is False
    assert response["confidence"] == "none"
    assert response["citations"] == []
    assert response["answer"] == settings.no_answer_fallback
    assert response["retrieval_strategy"] == RetrievalStrategy.hybrid_reranked.value

    # No cache write.
    assert (await session.execute(select(AnswerCache))).scalars().all() == []

    # Audit event records the citation_validation_failed reason.
    audits = (await session.execute(select(AuditEvent))).scalars().all()
    assert len(audits) == 1
    audit = audits[0]
    assert audit.metadata_["outcome"] == "citation_validation_failed"
    assert "out of range" in audit.metadata_["reason"].lower()

    # Evidence rows ARE still persisted (the orchestrator ran
    # retrieval, it just didn't trust the answer). The user message
    # is also persisted.
    assert len((await session.execute(select(Message))).scalars().all()) == 2
    assert len((await session.execute(select(RetrievedEvidence))).scalars().all()) == 2


# ---------------------------------------------------------------------------
# 6. LLM unavailable → orchestrator error
# ---------------------------------------------------------------------------


async def test_llm_unavailable_raises_orchestrator_error(session: Any) -> None:
    """An LLM transport failure surfaces as :class:`OrchestratorError`
    so the slice 7 route can map it to 503 / cost_limit_reached."""
    await _seed_index_version(session)
    settings = _settings()

    class _DownLLM:
        async def complete(self, **_kwargs: Any) -> Any:
            raise LLMUnavailable("provider is down")

        async def aclose(self) -> None:
            return None

    retriever = _FakeRetriever(_evidence(count=1))
    orchestrator = Orchestrator(settings, session, llm=_DownLLM(), retriever=retriever)

    with pytest.raises(OrchestratorError):
        await orchestrator.ask(
            question="How do I configure Claude Code permissions?",
            request_id="req_llm_down",
            session_id=uuid.uuid4(),
        )


# ---------------------------------------------------------------------------
# 7. Session / user upsert
# ---------------------------------------------------------------------------


async def test_orchestrator_creates_session_and_user_when_missing(
    session: Any,
) -> None:
    """The orchestrator should be able to operate with a bare
    ``session_id`` (no Session row pre-seeded)."""
    from app.models import Session, User

    await _seed_index_version(session)
    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=1))
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    session_id = uuid.uuid4()
    response = await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_upsert",
        session_id=session_id,
    )

    # Session row + User row created.
    session_row = await session.get(Session, session_id)
    assert session_row is not None
    assert session_row.user_id == "demo_user"
    user_row = await session.get(User, "demo_user")
    assert user_row is not None
    assert response["message_id"] is not None


def _hit_with_type(rtype: RetrievalType) -> EvidenceHit:
    """One evidence hit tagged with a specific retrieval_type."""
    return _evidence(count=1)[0].model_copy(update={"retrieval_type": rtype})


async def test_strategy_for_labels_from_evidence_not_intent() -> None:
    """``_strategy_for`` reports the strategy actually used, not the intent.

    Regression guard for the fix: an exact_lookup question whose exact index
    missed falls through to hybrid retrieval, and the label must reflect that
    (hybrid_reranked), not the intent (exact_lookup). Reverting the fix to
    ``if intent is Intent.exact_lookup: return exact_lookup`` fails the
    fell-through and mixed cases below.
    """
    from app.answer.orchestrator import Orchestrator, RetrievalStrategy

    all_exact = [_hit_with_type(RetrievalType.exact)]
    fell_through = [_hit_with_type(RetrievalType.keyword)]
    mixed = [_hit_with_type(RetrievalType.exact), _hit_with_type(RetrievalType.vector)]

    # exact_lookup intent + all-exact evidence → exact_lookup
    assert Orchestrator._strategy_for(Intent.exact_lookup, all_exact) is (
        RetrievalStrategy.exact_lookup
    )
    # exact_lookup intent that fell through (keyword/vector evidence) → hybrid
    assert Orchestrator._strategy_for(Intent.exact_lookup, fell_through) is (
        RetrievalStrategy.hybrid_reranked
    )
    # exact_lookup intent + mixed (not all exact) → hybrid
    assert Orchestrator._strategy_for(Intent.exact_lookup, mixed) is (
        RetrievalStrategy.hybrid_reranked
    )
    # no evidence → none
    assert Orchestrator._strategy_for(Intent.exact_lookup, []) is RetrievalStrategy.none
    # non-exact intent with evidence → hybrid
    assert Orchestrator._strategy_for(Intent.faq, all_exact) is RetrievalStrategy.hybrid_reranked


# ---------------------------------------------------------------------------
# #58: the orchestrator resolves the active index version ONCE and threads it
# into the DEFAULT retriever (no injection), so the persisted evidence trace
# contains only active-version documents. The ""→None trap: on a database with
# no active index, ask() must pass None (status-only filter), not "" (which
# would filter on ``index_version == ""``, match nothing, and blank every
# answer to no_answer).
# ---------------------------------------------------------------------------


async def _seed_codex_doc(
    session: Any,
    *,
    index_version: str,
    doc_status: str = "active",
) -> uuid.UUID:
    """Seed one codex ``Document`` + ``Chunk`` at ``index_version`` sharing the
    marker keyword ``zorptastic``. Returns the document id. No ``IndexVersion``
    row is created here — callers control which version (if any) is active."""
    from datetime import UTC, datetime

    from app.models import Chunk, Document

    now = datetime.now(UTC)
    doc = Document(
        document_id=uuid.uuid4(),
        index_version=index_version,
        source_name="codex",
        product_area="codex",
        source_url=f"https://docs.example.com/{index_version}",
        title=f"Codex {index_version}",
        content_checksum=f"sha256:{index_version}",
        last_fetched_at=now,
        last_indexed_at=now,
        status=doc_status,  # type: ignore[arg-type]
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
            chunk_text=f"The codex zorptastic behaviour in {index_version}.",
            context_summary="zorptastic",
            exact_terms=[],
            chunk_order=0,
            content_checksum=f"sha256:{index_version}-chunk",
        )
    )
    await session.flush()
    return doc.document_id


async def _persisted_evidence_versions(session: Any) -> set[str]:
    """Return the set of ``Document.index_version`` values referenced by every
    persisted ``RetrievedEvidence`` row (via its chunk → document)."""
    from app.models import Chunk, Document

    rows = (await session.execute(select(RetrievedEvidence))).scalars().all()
    versions: set[str] = set()
    for ev in rows:
        chunk = await session.get(Chunk, ev.chunk_id)
        assert chunk is not None
        doc = await session.get(Document, chunk.document_id)
        assert doc is not None
        versions.add(doc.index_version)
    return versions


async def test_ask_default_retriever_scopes_to_active_index_version(session: Any) -> None:
    """Wiring proof: the default retriever the orchestrator builds is scoped to
    the active index version, so a prior version's (still status=active) docs
    never enter the evidence trace. Fails before the fix (default retriever was
    built with active_index_version=None → both versions retrieved)."""
    from datetime import UTC, datetime, timedelta

    from app.models import IndexStatus, IndexVersion

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
    await _seed_codex_doc(session, index_version="v-old")
    await _seed_codex_doc(session, index_version="v-active")
    await session.commit()

    # No injected retriever → the orchestrator builds the DEFAULT one in ask().
    orch = Orchestrator(_settings(), session)
    await orch.ask(
        question="codex zorptastic behaviour",
        request_id="req-58-scope",
        session_id=uuid.uuid4(),
    )

    versions = await _persisted_evidence_versions(session)
    assert versions == {"v-active"}, f"prior-version docs leaked into the trace: {versions}"


async def test_ask_no_active_index_still_answers_status_only(session: Any) -> None:
    """The ""→None trap: with NO active IndexVersion, ask() must scope the
    default retriever with None (status-only), NOT "" — so a status=active doc is
    still retrieved instead of every answer collapsing to no_answer on a fresh /
    un-promoted database."""
    from datetime import UTC, datetime

    from app.models import IndexStatus, IndexVersion

    # A candidate index exists but nothing is promoted to active.
    session.add(
        IndexVersion(
            index_version="v1",
            status=IndexStatus.candidate,
            source_version_hash="sha256:v1",
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    await _seed_codex_doc(session, index_version="v1")
    await session.commit()

    orch = Orchestrator(_settings(), session)
    await orch.ask(
        question="codex zorptastic behaviour",
        request_id="req-58-noactive",
        session_id=uuid.uuid4(),
    )

    # The doc was retrieved despite no active index — evidence trace is non-empty.
    versions = await _persisted_evidence_versions(session)
    assert versions == {"v1"}, "no-active-index must fall back to status-only, not blank out"


# ---------------------------------------------------------------------------
# #65: embedder identity in the cache key + skip-cache-on-degrade
# ---------------------------------------------------------------------------


async def _seed_stamped_index(
    session: Any,
    *,
    provider: str | None,
    model: str | None,
    dim: int | None,
    source_version_hash: str = "sha256:stamped",
) -> None:
    """Insert an active IndexVersion carrying an embedding provenance stamp."""
    from datetime import UTC, datetime

    from app.models import IndexStatus, IndexVersion

    now = datetime.now(UTC)
    session.add(
        IndexVersion(
            index_version="index_v1",
            status=IndexStatus.active,
            source_version_hash=source_version_hash,
            embedding_provider=provider,
            embedding_model=model,
            embedding_dim=dim,
            created_at=now,
            promoted_at=now,
        )
    )
    await session.flush()


async def test_ask_skips_cache_write_on_embedder_mismatch(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """#65 gap (2)/(1): config on ``stub`` but the active index was stamped by
    ``gemini`` (a config-only swap that left source_version_hash unchanged). The
    vector arm degrades (Tier 3, #57), so the resulting weaker exact+keyword-only
    answer MUST NOT be cached — otherwise it freezes to TTL and silences the
    mismatch WARN on subsequent hits. Fails before the fix (row was written)."""
    settings = _settings(embedding_provider="stub")
    await _seed_stamped_index(session, provider="gemini", model="gemini-embedding-001", dim=1536)
    # The real hybrid retriever degrades the vector arm on this mismatch and reports
    # it back; the injected double mirrors that so the runtime-gated write is skipped.
    retriever = _FakeRetriever(_evidence(count=2), vector_degrade=VectorDegrade.mismatch)
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    with caplog.at_level(logging.WARNING, logger="citevyn.answer"):
        response = await orchestrator.ask(
            question="How do I configure Claude Code permissions?",
            request_id="req_mismatch",
            session_id=uuid.uuid4(),
        )

    # The answer is still served and persisted...
    assert response["no_answer"] is False
    assert response["cache_hit"] is False
    # ...but nothing is cached (the degrade is not frozen to TTL).
    cache_rows = (await session.execute(select(AnswerCache))).scalars().all()
    assert cache_rows == [], "a degraded (embedder-mismatch) answer must not be cached"

    # The skip is observable — both in the audit trail and as a loud WARN — so it
    # can never be confused with a silent drop.
    audit = (await session.execute(select(AuditEvent))).scalars().all()[0]
    assert audit.metadata_["cache_written"] is False
    assert "answer_cache_write_skipped_embedder_mismatch" in caplog.text


async def test_ask_caches_when_embedder_matches(session: Any) -> None:
    """Control for the mismatch test: when the configured embedder matches the
    active index stamp, the vector arm is live and the answer caches normally."""
    settings = _settings(embedding_provider="gemini")
    await _seed_stamped_index(session, provider="gemini", model="gemini-embedding-001", dim=1536)
    retriever = _FakeRetriever(_evidence(count=2))
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_match",
        session_id=uuid.uuid4(),
    )

    cache_rows = (await session.execute(select(AnswerCache))).scalars().all()
    assert len(cache_rows) == 1
    audit = (await session.execute(select(AuditEvent))).scalars().all()[0]
    assert audit.metadata_["cache_written"] is True


async def test_ask_caches_when_index_stamp_is_null(session: Any) -> None:
    """The NULL-stamp trap: a legacy / stub-seeded index carries no provenance
    (embedding_provider is None ⇒ "unknown, allow"). The vector arm is NOT
    degraded, so the answer must cache normally — the write-gate must not blank
    the cache or crash on a NULL stamp."""
    settings = _settings(embedding_provider="stub")
    await _seed_stamped_index(session, provider=None, model=None, dim=None)
    retriever = _FakeRetriever(_evidence(count=2))
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id="req_nullstamp",
        session_id=uuid.uuid4(),
    )

    cache_rows = (await session.execute(select(AnswerCache))).scalars().all()
    assert len(cache_rows) == 1, "NULL-stamp (unknown provenance) must still cache"
    # Assert the gate actively ALLOWED the write (not merely that a row exists,
    # which also held before the fix) so this cannot pass with no gate at all.
    audit = (await session.execute(select(AuditEvent))).scalars().all()[0]
    assert audit.metadata_["cache_written"] is True


async def test_cache_key_partitions_by_configured_embedder(session: Any) -> None:
    """#65 gap (2), option (a): the same question under two matching-but-distinct
    embedder configs lands on two DIFFERENT cache keys, so a config swap does not
    serve an answer built in the other vector space. Both writes succeed (each
    config matches its own index stamp), yielding two distinct rows."""
    question = "How do I configure Claude Code permissions?"

    # First: configured stub, index stamped stub → match → caches under stub key.
    await _seed_stamped_index(session, provider="stub", model="gemini-embedding-001", dim=1536)
    await Orchestrator(
        _settings(embedding_provider="stub"),
        session,
        retriever=_FakeRetriever(_evidence(count=2)),
    ).ask(question=question, request_id="req_stub", session_id=uuid.uuid4())

    # Flip the index stamp to gemini and the config to gemini → match again, but
    # a DIFFERENT configured identity → different cache key → second distinct row.
    stamp = await session.get(_index_version_model(), "index_v1")
    stamp.embedding_provider = "gemini"
    await session.flush()
    await Orchestrator(
        _settings(embedding_provider="gemini"),
        session,
        retriever=_FakeRetriever(_evidence(count=2)),
    ).ask(question=question, request_id="req_gemini", session_id=uuid.uuid4())

    keys = {row.cache_key for row in (await session.execute(select(AnswerCache))).scalars()}
    assert len(keys) == 2, "distinct embedder configs must occupy distinct cache keys"


def _index_version_model() -> Any:
    from app.models import IndexVersion

    return IndexVersion


# ---------------------------------------------------------------------------
# #70 + #72: gate the cache write on the vector arm's ACTUAL runtime degrade
# (reported back from ``retrieve()``), not a config-only prediction.
# ---------------------------------------------------------------------------


async def _restamp_active_index(
    session: Any, *, provider: str | None, model: str | None, dim: int | None
) -> None:
    """Mutate the catalog's active ``v1`` IndexVersion to carry a given stamp."""
    row = await session.get(_index_version_model(), "v1")
    row.embedding_provider = provider
    row.embedding_model = model
    row.embedding_dim = dim
    await session.flush()


async def test_ask_skips_cache_write_on_transient_embedder_unavailable(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """#70: the vector arm degraded on a *transient* ``EmbedderUnavailable`` (a
    Tier-1 outage the retriever reports as ``VectorDegrade.unavailable``). The
    weaker exact+keyword answer MUST NOT be cached, or it freezes to TTL until the
    provider recovers, and the skip is labeled ``vector_unavailable`` (NOT an
    embedder mismatch). Fails before the fix (config-only gate saw no mismatch →
    wrote the row)."""
    settings = _settings(embedding_provider="stub")
    await _seed_stamped_index(session, provider="stub", model="stub", dim=1536)
    retriever = _FakeRetriever(_evidence(count=2), vector_degrade=VectorDegrade.unavailable)
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    question = "How do I configure Claude Code permissions?"
    with caplog.at_level(logging.WARNING, logger="citevyn.answer"):
        first = await orchestrator.ask(
            question=question, request_id="req_transient_1", session_id=uuid.uuid4()
        )
    # Answer still served, but nothing cached.
    assert first["no_answer"] is False
    cache_rows = (await session.execute(select(AnswerCache))).scalars().all()
    assert cache_rows == [], "a transiently-degraded answer must not be cached (#70)"
    audit = (await session.execute(select(AuditEvent))).scalars().all()[0]
    assert audit.metadata_["cache_written"] is False
    # Labeled as a transient outage, NOT an embedder mismatch (no Tier-3 predicted).
    assert "answer_cache_write_skipped_vector_unavailable" in caplog.text
    assert "answer_cache_write_skipped_embedder_mismatch" not in caplog.text

    # A second identical ask re-runs retrieval (cache miss), so once the provider
    # recovers the fresh answer is served — the weak answer was never frozen.
    second = await orchestrator.ask(
        question=question, request_id="req_transient_2", session_id=uuid.uuid4()
    )
    assert second["cache_hit"] is False
    assert len(retriever.calls) == 2, "retrieval must re-run; nothing was cached"


async def test_exact_lookup_short_circuit_caches_under_mismatch(session: Any) -> None:
    """#72: an ``exact_lookup`` question whose exact arm hits short-circuits the
    REAL hybrid retriever BEFORE the vector arm is consulted, so the answer is
    embedder-independent and NOT degraded — it must be cached even though the
    active index carries a mismatched (Tier-3) stamp. Fails before the fix
    (config-only gate predicted a degrade and skipped the write)."""
    from app.models import TermType

    seeded = await seed_catalog(session)
    # Add an exact term that also carries a supported-domain keyword so the whole
    # normalized question ("claude api --model") both classifies as claude_api +
    # exact_lookup AND matches the term verbatim (ExactRetriever compares the full
    # normalized question to term_text).
    claude_chunk = next(c for c in seeded["chunks"] if c.product_area == "claude_api")  # type: ignore[attr-defined]
    session.add(
        ExactTerm(
            term_id=uuid.uuid4(),
            term_text="claude api --model",
            term_type=TermType.flag,
            product_area="claude_api",
            document_id=claude_chunk.document_id,
            chunk_id=claude_chunk.chunk_id,
        )
    )
    # Config on stub, index stamped gemini → a genuine Tier-3 mismatch is present.
    await _restamp_active_index(session, provider="gemini", model="gemini-embedding-001", dim=1536)
    await session.commit()

    orchestrator = Orchestrator(_settings(embedding_provider="stub"), session)
    question = "claude api --model"

    first = await orchestrator.ask(
        question=question, request_id="req_exact_1", session_id=uuid.uuid4()
    )
    assert first["cache_hit"] is False
    assert first["intent"] == Intent.exact_lookup.value
    assert first["retrieval_strategy"] == RetrievalStrategy.exact_lookup.value
    # The embedder-independent exact-lookup answer IS cached despite the mismatch.
    cache_rows = (await session.execute(select(AnswerCache))).scalars().all()
    assert len(cache_rows) == 1, "an exact-lookup short-circuit answer must be cached (#72)"

    second = await orchestrator.ask(
        question=question, request_id="req_exact_2", session_id=uuid.uuid4()
    )
    assert second["cache_hit"] is True, "the second identical ask must hit the cache"


async def test_ask_skips_cache_write_on_real_mismatch_non_exact(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """#65 guard via the REAL hybrid (not a hand-set flag): a genuine Tier-3
    mismatch on a NON-short-circuit (faq) question degrades the vector arm at
    runtime; the retriever reports ``VectorDegrade.mismatch``, so the answer (served
    from keyword) MUST NOT be cached and the skip is labeled as an embedder
    mismatch. Proves the runtime reason is computed for real, not just honored."""
    await seed_catalog(session)
    await _restamp_active_index(session, provider="gemini", model="gemini-embedding-001", dim=1536)
    await session.commit()

    orchestrator = Orchestrator(_settings(embedding_provider="stub"), session)
    question = "the rate limit for the claude api"
    with caplog.at_level(logging.WARNING, logger="citevyn.answer"):
        response = await orchestrator.ask(
            question=question, request_id="req_real_mismatch", session_id=uuid.uuid4()
        )

    assert response["no_answer"] is False, "keyword arm still answers under the mismatch"
    assert response["intent"] == Intent.faq.value
    cache_rows = (await session.execute(select(AnswerCache))).scalars().all()
    assert cache_rows == [], "a genuinely degraded (mismatch) answer must not be cached (#65)"
    audit = (await session.execute(select(AuditEvent))).scalars().all()[0]
    assert audit.metadata_["cache_written"] is False
    # Labeled a mismatch, NOT a transient outage (symmetric with the #70 tests).
    assert "answer_cache_write_skipped_embedder_mismatch" in caplog.text
    assert "answer_cache_write_skipped_vector_unavailable" not in caplog.text


async def test_ask_skips_cache_write_on_real_transient_outage(
    session: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#70 via the REAL hybrid end-to-end: with a matching (NULL) stamp the vector
    arm is ENABLED, so it is actually consulted — and a transient
    ``EmbedderUnavailable`` makes it degrade at runtime. The retriever reports
    ``VectorDegrade.unavailable``; the answer (served from keyword) MUST NOT be
    cached and the skip is labeled ``vector_unavailable`` (NOT a mismatch). Proves
    the transient reason is computed + labeled for real, not just honored."""
    from app.embeddings import EmbedderUnavailable
    from app.retrieval.vector import VectorRetriever

    async def _raise(self: Any, question: str, *, product_area: str | None = None, limit: int = 10):
        raise EmbedderUnavailable("Gemini embeddings returned 503")

    # Catalog seeds a NULL-stamp active index ⇒ the arm is enabled (unknown
    # provenance ⇒ allow), so it is consulted and the transient outage bites.
    await seed_catalog(session)
    await session.commit()
    monkeypatch.setattr(VectorRetriever, "retrieve", _raise)

    orchestrator = Orchestrator(_settings(embedding_provider="stub"), session)
    question = "the rate limit for the claude api"
    with caplog.at_level(logging.WARNING, logger="citevyn.answer"):
        response = await orchestrator.ask(
            question=question, request_id="req_real_transient", session_id=uuid.uuid4()
        )

    assert response["no_answer"] is False, "keyword arm still answers under the outage"
    cache_rows = (await session.execute(select(AnswerCache))).scalars().all()
    assert cache_rows == [], "a transiently-degraded answer must not be cached (#70)"
    audit = (await session.execute(select(AuditEvent))).scalars().all()[0]
    assert audit.metadata_["cache_written"] is False
    assert "answer_cache_write_skipped_vector_unavailable" in caplog.text
    assert "answer_cache_write_skipped_embedder_mismatch" not in caplog.text


# ---------------------------------------------------------------------------
# 12. Greeting short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "hi",
        "hey",
        "hello CiteVyn",
        "hi sitevyn",
        "good morning",
        "Hello, CiteVyn",
        "hello?",  # a greeting terminated with a question mark still counts
        "hi?",
        "howdy",  # common openers beyond the core set
        "sup",
        "hiya",
        "morning",  # bare time-of-day greeting (no "good")
        "good evening!",
        "hey there",
    ],
)
async def test_is_greeting_true_for_bare_greetings(text: str) -> None:
    """A bare social greeting (optionally addressed) is a greeting."""
    from app.answer.orchestrator import is_greeting

    assert is_greeting(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "hello, how do I get the Gemini API key?",  # real question wins
        "what is Claude Code?",  # not a greeting at all
        "hey, why does the codex CLI fail to install",  # substantive tail
        "hello there my friend how are you doing today",  # tail past the addressee
        "history of the claude api",  # 'hi' is only a substring, not the opener
        "hey do embeddings work",  # yes/no ask riding a greeting token
        "yo bitcoin price today",  # off-domain tail must not be swallowed
        "hi list all the claude code flags",  # imperative ask riding a greeting
        "",  # empty
    ],
)
async def test_is_greeting_false_for_real_queries(text: str) -> None:
    """A real question that opens with (or merely contains) a greeting is not
    short-circuited."""
    from app.answer.orchestrator import is_greeting

    assert is_greeting(text) is False


async def test_greeting_returns_friendly_reply_without_retrieval_or_llm(
    session: Any,
) -> None:
    """A bare "hello" short-circuits to the greeting reply: not a refusal, not
    a no-answer, no retrieval, no LLM, nothing cached."""
    from app.answer.orchestrator import GREETING_RESPONSE

    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=2))
    llm_spy = AsyncMock(wraps=StubLLMClient())
    orchestrator = Orchestrator(settings, session, llm=llm_spy, retriever=retriever)

    response = await orchestrator.ask(
        question="hello",
        request_id="req_greeting",
        session_id=uuid.uuid4(),
    )

    assert response["answer"] == GREETING_RESPONSE
    assert response["intent"] == "greeting"
    assert response["unsupported"] is False
    assert response["no_answer"] is False
    assert response["cache_hit"] is False
    assert response["retrieval_strategy"] == RetrievalStrategy.none.value
    assert response["citations"] == []
    # A bare greeting classifies as the unsupported domain, but a greeting is
    # not a refusal — echoing "unsupported" here would break the
    # domain == "unsupported" ⟺ unsupported == true invariant (#89). The
    # response carries the neutral "general" domain instead, and the persisted
    # message row (replayed verbatim by GET /messages) agrees.
    assert response["domain"] == "general"
    assert response["domain"] != "unsupported"
    stored = list((await session.execute(select(Message))).scalars().all())
    assert {m.domain for m in stored} == {"general"}

    # No retrieval, no LLM, no cache.
    assert retriever.calls == []
    assert llm_spy.complete.await_count == 0
    assert (await session.execute(select(AnswerCache))).scalars().all() == []

    # Audit event records the greeting outcome.
    audits = (await session.execute(select(AuditEvent))).scalars().all()
    assert len(audits) == 1
    assert audits[0].metadata_["outcome"] == "greeting"


async def test_greeting_addressed_to_citevyn_preserves_domain(session: Any) -> None:
    """ "hello CiteVyn" is still a greeting; the classified citevyn domain rides
    the trace but the greeting flags (not the domain) are the signal."""
    settings = _settings()
    retriever = _FakeRetriever(_evidence(count=2))
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    response = await orchestrator.ask(
        question="hello CiteVyn",
        request_id="req_greeting_citevyn",
        session_id=uuid.uuid4(),
    )

    assert response["intent"] == "greeting"
    assert response["no_answer"] is False
    assert response["unsupported"] is False
    assert response["domain"] == "citevyn"
    assert retriever.calls == []


async def test_greeting_prefixed_real_question_still_answers(session: Any) -> None:
    """ "hello, how do I ...?" is NOT a greeting — the real question flows through
    the normal retrieval + generation pipeline."""
    settings = _settings()
    await _seed_index_version(session)
    retriever = _FakeRetriever(_evidence(count=2))
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    response = await orchestrator.ask(
        question="hello, how do I configure Claude Code permissions?",
        request_id="req_greeting_prefix",
        session_id=uuid.uuid4(),
    )

    assert response["intent"] != "greeting"
    assert response["intent"] == "how_to"
    assert len(retriever.calls) == 1


async def test_offdomain_query_opening_with_greeting_still_refused(session: Any) -> None:
    """An off-domain query that merely opens with a greeting token
    ("yo bitcoin price today") must NOT be welcomed as a greeting — the
    substantive tail keeps it out of the short-circuit so it still reaches
    the unsupported refusal (greeting runs before the unsupported branch, so
    a leaky detector would otherwise swallow the refusal). Under "answer when
    grounded" the off-corpus tail retrieves globally and finds nothing confident
    (empty retriever models the gate), so it still lands on the refusal."""
    settings = _settings()
    retriever = _FakeRetriever([])  # off-corpus → confidence gate drops it → no evidence
    orchestrator = Orchestrator(settings, session, retriever=retriever)

    response = await orchestrator.ask(
        question="yo bitcoin price today",
        request_id="req_offdomain_greeting",
        session_id=uuid.uuid4(),
    )

    assert response["intent"] == "unsupported"
    assert response["unsupported"] is True
    assert response["no_answer"] is True
    assert response["answer"] == settings.unsupported_refusal
    assert retriever.calls[0]["product_area"] is None  # retrieved globally, found nothing
