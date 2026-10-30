"""Split, don't count: grouped markers are SHOWN but never COUNTED (#554).

Review reproduced that splitting a group makes the issue's own answer worse. The
live answer "...are not explicitly listed [1, 2, 3, 4, 5, 6]. However ... the
`--model` flag [2]." cited only ``[2]`` before #554 (confidence low); once the
group was split it cited all six and confidence became high. And an answer that
only says "The models are not explicitly listed [1, 2, 3]." used to be refused as
uncited (#174) and became a high-confidence answer. A sentence saying the docs do
NOT contain the answer is exactly where the model groups everything.

The owner's rule, which these tests pin:

* A grouped marker is still shown as adjacent real markers ``[1][2][3]`` (served,
  stored, cached), and those indices still get their source cards.
* An index that came ONLY from a group never counts toward confidence and never
  satisfies the uncited-answer gate (#174) or the #208 retry checks. Those use the
  markers the model wrote singly, exactly as before #554. An index written both
  singly and in a group counts.
* The out-of-range check still covers grouped indices.

Interface pinned here (``app.llm.validation``):

* ``validate_citations(answer_text=<the model's RAW text, groups intact>, ...)``.
* ``CitationValidationResult.cited_indices``: every index the answer SHOWS, which
  is ``_cited_markers(expand_grouped_markers(raw))``; these get cards.
* ``CitationValidationResult.singly_cited_indices`` (new, default ``[]``):
  ``_cited_markers(raw)``, i.e. what ``cited_indices`` was before #554; this drives
  confidence, the uncited gate and the retry checks.
* Every index in either list is range-checked.

It also pins three reproduced findings against ``expand_grouped_markers``: a
fence run directly followed by a group, a group split across lines, and
non-ASCII digits (the client's marker regex links ASCII digits only).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.llm.prompts import NO_ANSWER_REFUSAL
from app.llm.types import LLMResult
from app.llm.validation import _cited_markers, expand_grouped_markers, validate_citations
from app.models import AnswerCache, AuditEvent, RetrievalType
from app.retrieval.types import EvidenceHit
from app.routing.intent import Intent
from tests.orchestrator_double import Orchestrator
from tests.test_answer_orchestrator import (
    _evidence,
    _evidence_hits,
    _FakeRetriever,
    _seed_index_version,
    _settings,
)
from tests.test_exact_lookup_fallthrough import (
    _CONFIG_PASSAGE,
    _FLAG_PASSAGE,
    _FLAG_QUESTION,
    _ask,
    _hit,
    _IntentAwareRetriever,
)

# The answer from the issue, word for word.
_LIVE_ANSWER = (
    "Based on the provided evidence, the specific models supported by Codex are not "
    "explicitly listed [1, 2, 3, 4, 5, 6]. However, the model used by Codex for code "
    "generation can be selected using the `--model` flag [2]."
)


def _singly(result: Any) -> list[int]:
    """Read the new field; a missing field fails the test by name, not at import."""
    return result.singly_cited_indices


# ---------------------------------------------------------------------------
# validate_citations on the RAW text: what is shown vs what counts
# ---------------------------------------------------------------------------


def test_a_group_is_shown_but_only_the_single_marker_counts() -> None:
    # Red when: validate_citations ignores groups in raw text, or counts them as single.
    result = validate_citations(answer_text="X [1]. Y [2, 3].", evidence=_evidence_hits(count=3))
    assert result.valid is True
    assert result.cited_indices == [1, 2, 3]
    assert _singly(result) == [1]


def test_a_group_only_answer_shows_cards_and_counts_nothing() -> None:
    # Red when: an index that came only from a group appears in singly_cited_indices.
    result = validate_citations(
        answer_text="The models are not explicitly listed [1, 2, 3].",
        evidence=_evidence_hits(count=3),
    )
    assert result.valid is True
    assert result.cited_indices == [1, 2, 3]
    assert _singly(result) == []


def test_an_index_written_singly_and_in_a_group_counts_once() -> None:
    # Red when: an index is dropped from the counted set because it ALSO sits in a group.
    result = validate_citations(answer_text="A [2]. B [2, 3].", evidence=_evidence_hits(count=3))
    assert result.cited_indices == [2, 3]
    assert _singly(result) == [2]


def test_plain_single_markers_count_exactly_as_before() -> None:
    # Red when: singly_cited_indices differs from cited_indices on an answer with no group.
    result = validate_citations(answer_text="A [1]. B [3].", evidence=_evidence_hits(count=3))
    assert result.cited_indices == [1, 3]
    assert _singly(result) == [1, 3]


def test_an_out_of_range_index_in_a_raw_group_still_fails_validation() -> None:
    # Red when: the range check covers only the single markers, not the grouped indices.
    result = validate_citations(answer_text="A [1, 9].", evidence=_evidence_hits(count=2))
    assert result.valid is False
    assert result.reason is not None and "[9]" in result.reason


@pytest.mark.parametrize(
    "raw",
    [
        _LIVE_ANSWER,
        "X [1]. Y [2, 3].",
        "A [2]. B [2, 3].",
        "Use `a[1, 2]` here [1].",
        "```\nm = grid[1, 2]\n```\nSee [2, 3] and [1].",
        "Answer [1].\n```[1, 2]\nSee [3].",
        "No markers.",
    ],
)
def test_the_two_lists_are_defined_by_the_scanner_on_the_shown_and_raw_text(raw: str) -> None:
    # Red when: cited_indices stops meaning "markers in the shown text" or singly_cited_indices
    # stops meaning "markers the model wrote singly" (the pre-#554 scan of the raw text).
    result = validate_citations(answer_text=raw, evidence=_evidence_hits(count=6))
    shown = sorted({int(m) for m in _cited_markers(expand_grouped_markers(raw))})
    written_singly = sorted({int(m) for m in _cited_markers(raw)})
    assert result.cited_indices == shown
    assert _singly(result) == written_singly


def test_the_refusal_still_validates_with_nothing_cited() -> None:
    # Red when: the refusal path stops returning empty lists.
    result = validate_citations(answer_text=NO_ANSWER_REFUSAL, evidence=_evidence_hits(count=3))
    assert result.valid is True
    assert result.cited_indices == []
    assert _singly(result) == []


# ---------------------------------------------------------------------------
# Orchestrator: shown, carded, but not counted
# ---------------------------------------------------------------------------


class _FixedTextLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, **_kwargs: Any) -> LLMResult:
        return LLMResult(
            text=self._text, input_tokens=1, output_tokens=1, model="stub", provider="stub"
        )

    async def aclose(self) -> None:
        return None


async def _ask_fixed(
    session: Any, *, text: str, count: int
) -> tuple[dict[str, Any], list[EvidenceHit]]:
    await _seed_index_version(session)
    evidence = await _evidence(session, count=count)
    orchestrator = Orchestrator(
        _settings(), session, llm=_FixedTextLLM(text), retriever=_FakeRetriever(evidence)
    )
    response = await orchestrator.ask(
        question="How do I configure Claude Code permissions?",
        request_id=f"req_{uuid.uuid4().hex[:8]}",
        session_id=uuid.uuid4(),
    )
    return response, evidence


@pytest.mark.asyncio
async def test_the_live_answer_shows_six_markers_but_keeps_its_pre_554_confidence(
    session: Any,
) -> None:
    # Red when: the six grouped indices are counted, raising confidence from low (1 of 6) to high.
    response, _ = await _ask_fixed(session, text=_LIVE_ANSWER, count=6)

    assert response["no_answer"] is False
    assert "listed [1][2][3][4][5][6]." in response["answer"]
    assert "`--model` flag [2]." in response["answer"]
    assert [c["marker"] for c in response["citations"]] == [1, 2, 3, 4, 5, 6]
    assert response["confidence"] == "low"


@pytest.mark.asyncio
async def test_an_answer_cited_only_by_a_group_is_refused_as_uncited(session: Any) -> None:
    # Red when: grouped indices satisfy the #174 uncited-answer gate.
    response, _ = await _ask_fixed(
        session, text="The models are not explicitly listed [1, 2, 3].", count=3
    )

    assert response["no_answer"] is True
    assert response["citations"] == []
    assert response["answer"] == _settings().no_answer_fallback
    assert "not explicitly listed" not in response["answer"]
    assert (await session.execute(select(AnswerCache))).scalars().all() == []
    (audit,) = (await session.execute(select(AuditEvent))).scalars().all()
    assert audit.metadata_["outcome"] == "no_answer"
    assert audit.metadata_["reason"] == "uncited_answer"


@pytest.mark.asyncio
async def test_confidence_comes_from_the_single_marker_only(session: Any) -> None:
    # Red when: confidence uses every shown marker (3 of 3 -> high) instead of [1] (1 of 3).
    # Also the served partner of the refusal above: a group beside a single marker is served.
    response, _ = await _ask_fixed(session, text="X [1]. Y [2, 3].", count=3)

    assert response["no_answer"] is False
    assert response["answer"] == "X [1]. Y [2][3]."
    assert [c["marker"] for c in response["citations"]] == [1, 2, 3]
    assert response["confidence"] == "medium"


@pytest.mark.asyncio
async def test_an_index_written_singly_and_in_a_group_counts(session: Any) -> None:
    # Red when: [3] (group only) is counted (2 of 3 -> high), or [2] is not counted because it
    # also sits in a group (nothing counted -> refused as uncited).
    response, _ = await _ask_fixed(session, text="A [2]. B [2, 3].", count=3)

    assert response["no_answer"] is False
    assert response["answer"] == "A [2]. B [2][3]."
    assert [c["marker"] for c in response["citations"]] == [2, 3]
    assert response["confidence"] == "medium"


class _ByEvidenceLLM:
    """Answers with ``with_config`` when the config passage is in the prompt, else ``without``."""

    def __init__(self, *, with_config: str, without: str) -> None:
        self._with_config = with_config
        self._without = without

    async def complete(self, *, system: str, user: str, **_kwargs: Any) -> LLMResult:
        text = self._with_config if _CONFIG_PASSAGE in user else self._without
        return LLMResult(text=text, input_tokens=1, output_tokens=1, model="stub", provider="stub")

    async def aclose(self) -> None:
        return None


def _flag_then_config_retriever() -> _IntentAwareRetriever:
    return _IntentAwareRetriever(
        exact=[
            _hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.exact),
            _hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.exact),
        ],
        hybrid=[
            _hit(_CONFIG_PASSAGE, retrieval_type=RetrievalType.hybrid),
            _hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.hybrid),
        ],
    )


@pytest.mark.asyncio
async def test_a_group_only_exact_lookup_answer_still_triggers_the_retry(session: Any) -> None:
    # Red when: a group-only answer counts as grounded, so the #208 hybrid retry never runs.
    retriever = _flag_then_config_retriever()
    llm = _ByEvidenceLLM(
        with_config="Settings live in a config file [1].",
        without="A config file is not mentioned [1, 2].",
    )

    response = await _ask(session, question=_FLAG_QUESTION, retriever=retriever, llm=llm)

    assert retriever.calls == [Intent.exact_lookup, Intent.faq]
    assert response["no_answer"] is False
    assert response["answer"] == "Settings live in a config file [1]."


@pytest.mark.asyncio
async def test_a_group_only_retry_answer_is_not_kept(session: Any) -> None:
    # Red when: the #208 retry keeps an answer whose only citations are grouped.
    retriever = _flag_then_config_retriever()
    llm = _ByEvidenceLLM(
        with_config="Settings live in a config file [1, 2].",
        without=NO_ANSWER_REFUSAL,
    )

    response = await _ask(session, question=_FLAG_QUESTION, retriever=retriever, llm=llm)

    assert retriever.calls == [Intent.exact_lookup, Intent.faq]
    assert response["no_answer"] is True
    assert response["citations"] == []
    # The ORIGINAL refusal is the one recorded, as before #554: a kept retry would
    # record the grouped answer as "uncited_answer" on the hybrid evidence instead.
    (audit,) = (await session.execute(select(AuditEvent))).scalars().all()
    assert audit.metadata_["reason"] == "no_answer"
    assert audit.metadata_["retrieval_strategy"] == "exact_lookup"


# ---------------------------------------------------------------------------
# Reproduced findings against expand_grouped_markers
# ---------------------------------------------------------------------------

# "```[1, 2]" is prose (its info string has a space and no closer follows), but
# "```[1][2]" is a one-token info string, which OPENS a fence and swallows the rest.
_FENCE_RUN_THEN_GROUP = "Answer [1].\n```[1, 2]\nSee [3]."


def test_expansion_does_not_turn_a_prose_line_into_a_fence_that_hides_a_marker() -> None:
    # Red when: expanding "```[1, 2]" to "```[1][2]" opens a fence and hides "See [3]".
    assert _cited_markers(_FENCE_RUN_THEN_GROUP) == ["1", "3"]
    assert "3" in _cited_markers(expand_grouped_markers(_FENCE_RUN_THEN_GROUP))


@pytest.mark.parametrize(
    "text",
    [
        _FENCE_RUN_THEN_GROUP,
        "~~~[1, 2]\nSee [3].",
        "Prose [1, 2] and [3].\n```\ncode\n```\nMore [4].",
        _LIVE_ANSWER,
    ],
)
def test_expansion_never_loses_a_marker_the_scanner_already_saw(text: str) -> None:
    # Red when: any single marker counted in the raw text is no longer counted after expansion.
    before = set(_cited_markers(text))
    assert before, "partner: the sample must carry a counted marker"
    assert before <= set(_cited_markers(expand_grouped_markers(text)))


def test_expansion_is_idempotent_when_a_fence_run_precedes_a_group() -> None:
    # Red when: the first pass changes which lines are code, so a second pass expands more.
    text = "```[1, 2]\nx\n``` trailing\n[5, 6]"
    once = expand_grouped_markers(text)
    assert expand_grouped_markers(once) == once


@pytest.mark.parametrize("text", ["[1,\n2] x", "[1,\r\n2] x", "[1\n, 2] x"])
def test_a_group_never_spans_a_line(text: str) -> None:
    # Red when: the group grammar allows a newline around the comma.
    assert expand_grouped_markers(text) == text


def test_a_one_line_group_is_still_expanded() -> None:
    # Red when: the expander stops splitting a group (partner of the two pins above and below).
    assert expand_grouped_markers("[1, 2] x") == "[1][2] x"


@pytest.mark.parametrize(
    "text",
    [
        "Fullwidth [１, ２] stays.",
        "Arabic-Indic [١, ٢] stays.",
        "Mixed [1, ２] stays.",
        "Devanagari [१,२] stays.",
    ],
)
def test_non_ascii_digits_are_not_expanded(text: str) -> None:
    # Red when: the group grammar uses Unicode \d, so a group of fullwidth digits becomes
    # markers the client (frontend/src/lib/answerFormat.ts, /^\[(\d+)\]/, ASCII) cannot link.
    assert expand_grouped_markers(text) == text
