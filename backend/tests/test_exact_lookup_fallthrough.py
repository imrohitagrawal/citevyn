"""Issue #208: a prose question that merely MENTIONS a CLI flag must not be
hijacked into a refusal by the ``exact_lookup`` fast path.

``classify_intent`` is a token-shape regex cascade, so "Is there a config file
option for the Codex --model flag instead?" routes to ``exact_lookup``; the
hybrid retriever then short-circuits on the flag's own chunk and never runs
keyword+vector, so the LLM refuses on evidence that does not answer the
question — while the identical question WITHOUT the flag token answers with a
citation.

The fix lives in the orchestrator, not the router: an ``exact_lookup`` that
yields no GROUNDED answer gets one deterministic second pass through the full
hybrid retrieval path. These tests pin both halves — the fallthrough answers
the reported pair, and it never manufactures an answer for a question the
corpus does not support.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.answer.orchestrator import Orchestrator, RetrievalStrategy
from app.llm.prompts import NO_ANSWER_REFUSAL
from app.llm.types import LLMResult
from app.models import AuditEvent, RetrievalType
from app.retrieval.types import EvidenceHit, RetrievalResult, VectorDegrade
from app.routing.intent import Intent
from tests.test_answer_orchestrator import _seed_index_version, _settings

pytestmark = pytest.mark.asyncio

# The passage that actually answers the reported question, mirroring
# ``backend/app/worker/sources/codex.md`` line 64.
_CONFIG_PASSAGE = "Persistent settings live in a config file so you do not repeat flags every run."
_FLAG_PASSAGE = "The --model flag selects the model for a single run."

_FLAG_QUESTION = "Is there a config file option for the Codex --model flag instead?"
_PROSE_QUESTION = "Is there a config file option for the Codex model setting instead?"


def _hit(text: str, *, retrieval_type: RetrievalType) -> EvidenceHit:
    return EvidenceHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        product_area="codex",
        source_name="codex.docs",
        document_title="Codex CLI",
        section_path="/cli",
        heading="CLI configuration",
        parent_heading=None,
        chunk_text=text,
        context_summary="Codex CLI configuration.",
        source_url="https://docs.test/codex",
        score=1.0,
        retrieval_type=retrieval_type,
        rank=1,
    )


class _IntentAwareRetriever:
    """Mirrors :class:`app.retrieval.hybrid.HybridRetriever`'s branch.

    ``intent=exact_lookup`` short-circuits on the exact-term arm; any other
    intent runs the full hybrid path. That is exactly the seam the #208
    fallthrough exercises.
    """

    def __init__(self, *, exact: list[EvidenceHit], hybrid: list[EvidenceHit]) -> None:
        self._exact = exact
        self._hybrid = hybrid
        self.calls: list[Intent] = []
        self.multi_calls: list[Intent] = []

    async def retrieve(
        self,
        question: str,
        *,
        product_area: str | None,
        intent: Intent,
        limit: int,
        top_k: int,
    ) -> RetrievalResult:
        self.calls.append(intent)
        hits = self._exact if intent is Intent.exact_lookup else self._hybrid
        return RetrievalResult(hits=list(hits), vector_degrade=VectorDegrade.none)

    async def retrieve_multi(
        self,
        question: str,
        *,
        product_areas: list[str],
        intent: Intent,
        limit: int,
        top_k: int,
    ) -> RetrievalResult:
        self.multi_calls.append(intent)
        hits = self._exact if intent is Intent.exact_lookup else self._hybrid
        return RetrievalResult(hits=list(hits), vector_degrade=VectorDegrade.none)


class _GroundedOnlyLLM:
    """Answers only when the config-file passage is in the prompt.

    Stands in for the real model's grounding behaviour: the flag chunk alone
    does not support "is there a config file option?", so it refuses.
    """

    def __init__(self, *, answer_text: str = "Yes — settings live in a config file. [1]") -> None:
        self.prompts: list[str] = []
        self._answer_text = answer_text

    async def complete(self, *, system: str, user: str, **_kwargs: Any) -> LLMResult:
        self.prompts.append(user)
        text = self._answer_text if _CONFIG_PASSAGE in user else NO_ANSWER_REFUSAL
        return LLMResult(
            text=text,
            input_tokens=1,
            output_tokens=1,
            model="stub-deterministic-v1",
            provider="stub",
        )

    async def aclose(self) -> None: ...


class _AlwaysAnswersLLM:
    """Cites [1] whatever the evidence — used for the exact fast-path test."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, *, system: str, user: str, **_kwargs: Any) -> LLMResult:
        self.prompts.append(user)
        return LLMResult(
            text="The --model flag selects the model. [1]",
            input_tokens=1,
            output_tokens=1,
            model="stub-deterministic-v1",
            provider="stub",
        )

    async def aclose(self) -> None: ...


class _AlwaysRefusesLLM:
    """Refuses whatever the evidence — a genuinely ungrounded question."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, *, system: str, user: str, **_kwargs: Any) -> LLMResult:
        self.prompts.append(user)
        return LLMResult(
            text=NO_ANSWER_REFUSAL,
            input_tokens=1,
            output_tokens=1,
            model="stub-deterministic-v1",
            provider="stub",
        )

    async def aclose(self) -> None: ...


async def _ask(session: Any, *, question: str, retriever: Any, llm: Any) -> Any:
    await _seed_index_version(session)
    orchestrator = Orchestrator(_settings(), session, llm=llm, retriever=retriever)
    return await orchestrator.ask(
        question=question,
        request_id=f"req_{uuid.uuid4().hex[:8]}",
        session_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# The reported pair — both phrasings must answer with a citation
# ---------------------------------------------------------------------------


async def test_flag_mentioning_prose_question_falls_through_and_answers(session: Any) -> None:
    """The reported bug: the flag token routed to exact_lookup and refused."""
    retriever = _IntentAwareRetriever(
        exact=[_hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.exact)],
        hybrid=[_hit(_CONFIG_PASSAGE, retrieval_type=RetrievalType.hybrid)],
    )
    llm = _GroundedOnlyLLM()

    response = await _ask(session, question=_FLAG_QUESTION, retriever=retriever, llm=llm)

    assert response["no_answer"] is False
    assert len(response["citations"]) == 1
    # Routing is untouched; only retrieval got a second chance.
    assert response["intent"] == Intent.exact_lookup.value
    assert response["retrieval_strategy"] == RetrievalStrategy.hybrid_reranked.value
    # Exact first, then the hybrid retry.
    assert retriever.calls == [Intent.exact_lookup, Intent.faq]
    assert _CONFIG_PASSAGE in response["answer"] or "[1]" in response["answer"]


async def test_same_question_without_the_flag_token_also_answers(session: Any) -> None:
    """Control half of the pair: the phrasing that always worked still works."""
    retriever = _IntentAwareRetriever(
        exact=[_hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.exact)],
        hybrid=[_hit(_CONFIG_PASSAGE, retrieval_type=RetrievalType.hybrid)],
    )
    llm = _GroundedOnlyLLM()

    response = await _ask(session, question=_PROSE_QUESTION, retriever=retriever, llm=llm)

    assert response["no_answer"] is False
    assert len(response["citations"]) == 1
    assert response["intent"] != Intent.exact_lookup.value
    assert retriever.calls == [Intent.faq]


# ---------------------------------------------------------------------------
# The fast path stays intact
# ---------------------------------------------------------------------------


async def test_bare_flag_lookup_still_uses_the_exact_fast_path(session: Any) -> None:
    """A genuine flag lookup grounds on the exact arm — no retry, no relabel."""
    retriever = _IntentAwareRetriever(
        exact=[_hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.exact)],
        hybrid=[_hit(_CONFIG_PASSAGE, retrieval_type=RetrievalType.hybrid)],
    )
    llm = _AlwaysAnswersLLM()

    response = await _ask(
        session,
        question="What does --model do in Codex?",
        retriever=retriever,
        llm=llm,
    )

    assert response["no_answer"] is False
    assert response["intent"] == Intent.exact_lookup.value
    assert response["retrieval_strategy"] == RetrievalStrategy.exact_lookup.value
    assert retriever.calls == [Intent.exact_lookup]  # never retried
    assert len(llm.prompts) == 1


# ---------------------------------------------------------------------------
# The refusal paths must NOT be weakened
# ---------------------------------------------------------------------------


async def test_ungrounded_in_domain_question_still_refuses(session: Any) -> None:
    """The retry ran and also failed to ground → the refusal stands."""
    retriever = _IntentAwareRetriever(
        exact=[_hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.exact)],
        hybrid=[_hit("Unrelated Codex prose.", retrieval_type=RetrievalType.hybrid)],
    )
    llm = _AlwaysRefusesLLM()

    response = await _ask(
        session,
        question="Does the Codex --model flag support fine-tuned checkpoints?",
        retriever=retriever,
        llm=llm,
    )

    assert response["no_answer"] is True
    assert retriever.calls == [Intent.exact_lookup, Intent.faq]  # retry attempted
    audits = (await session.execute(select(AuditEvent))).scalars().all()
    assert len(audits) == 1
    assert audits[0].metadata_["outcome"] == "no_answer"
    assert audits[0].metadata_["reason"] == "no_answer"


async def test_out_of_corpus_question_still_refuses(session: Any) -> None:
    """Off-corpus keeps the crisp unsupported refusal; no LLM, no retry."""
    retriever = _IntentAwareRetriever(exact=[], hybrid=[])
    llm = _AlwaysAnswersLLM()

    response = await _ask(
        session,
        question="What's the best laptop for AI coding?",
        retriever=retriever,
        llm=llm,
    )

    assert response["no_answer"] is True
    assert response["unsupported"] is True
    assert llm.prompts == []


async def test_retry_that_retrieves_nothing_keeps_the_original_refusal(session: Any) -> None:
    """Edge: hybrid arms come back empty → no answer is invented."""
    retriever = _IntentAwareRetriever(
        exact=[_hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.exact)],
        hybrid=[],
    )
    llm = _GroundedOnlyLLM()

    response = await _ask(session, question=_FLAG_QUESTION, retriever=retriever, llm=llm)

    assert response["no_answer"] is True
    assert retriever.calls == [Intent.exact_lookup, Intent.faq]
    assert len(llm.prompts) == 1  # empty retry never reached the generator


async def test_uncited_retry_answer_is_rejected(session: Any) -> None:
    """Edge: the retry produced prose with no ``[n]`` marker — not grounded."""
    retriever = _IntentAwareRetriever(
        exact=[_hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.exact)],
        hybrid=[_hit(_CONFIG_PASSAGE, retrieval_type=RetrievalType.hybrid)],
    )
    llm = _GroundedOnlyLLM(answer_text="Yes, use a config file.")  # no citation marker

    response = await _ask(session, question=_FLAG_QUESTION, retriever=retriever, llm=llm)

    assert response["no_answer"] is True
    assert response["citations"] == []
    audits = (await session.execute(select(AuditEvent))).scalars().all()
    # The ORIGINAL refusal is what is recorded, not the retry's uncited prose.
    assert audits[0].metadata_["reason"] == "no_answer"
