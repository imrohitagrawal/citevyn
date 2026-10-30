"""Grouped citation markers such as ``[1, 2, 3]`` (#554).

A real model answered a production question with ``[1, 2, 3, 4, 5, 6]``. The
answer contract (``docs/API_SPEC.md``, formatting contract #303) allows only the
single ``[n]`` form, so the validator found no markers in the group and the
client rendered it as literal text instead of six source links.

The agreed fix, which these tests pin:

* ``app.llm.validation.expand_grouped_markers(text) -> str`` rewrites a group of
  comma-separated integers into adjacent single markers: ``[1, 2, 3]`` becomes
  ``[1][2][3]``. Nothing else in the text changes.
* The grammar is deliberately tight: digits separated by commas, with optional
  whitespace AROUND THE COMMAS only. ``[ 1, 2 ]``, ``[1-3]``, ``[1; 2]``,
  ``[1 2]``, ``[1,]``, ``[a, b]``, ``[1, x]`` and ``[]`` are left exactly as
  written. A range such as ``[1-3]`` is NOT expanded: the model is never asked
  for ranges, and guessing what a dash means could attach a card the model did
  not cite. Left alone, it is simply not a marker, as it is today.
* Markdown code is left alone, using the same definition of "code" as the #237
  scanner (``_strip_code``): inline spans and ``` / ~~~ fences.
* The orchestrator expands BEFORE ``validate_citations`` on both generation
  paths (the main path and the #208 exact-lookup retry), and the EXPANDED text is
  what is stored (message row, answer cache) and returned to the client.
* The system prompt asks for one marker per bracket, forbids citing a sentence
  that says the evidence does not contain the answer, and forbids the phrase
  "based on the provided evidence". The prompt change bumps
  ``answer_policy_version`` (pinned in ``test_settings_slice3_slice4.py``).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.llm.prompts import SYSTEM_PROMPT
from app.llm.types import LLMResult
from app.llm.validation import validate_citations
from app.models import AnswerCache, AuditEvent, Message, MessageRole, RetrievalType
from app.retrieval.types import EvidenceHit
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
    _GroundedOnlyLLM,
    _hit,
    _IntentAwareRetriever,
)


def _expand(text: str) -> str:
    """Call the expander. Imported here so each test fails on its own, by name."""
    from app.llm.validation import expand_grouped_markers

    return expand_grouped_markers(text)


# ---------------------------------------------------------------------------
# The expander: happy path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expanded"),
    [
        ("Use the flag [1, 2, 3].", "Use the flag [1][2][3]."),
        ("Use the flag [1,2].", "Use the flag [1][2]."),
        # Mixed spacing around the commas.
        ("A [1,2, 3] and B [4 ,  5].", "A [1][2][3] and B [4][5]."),
        # A group next to ordinary markers, on both sides.
        ("X [1] Y [2, 3][4].", "X [1] Y [2][3][4]."),
        # Multi-digit indices.
        ("Many sources [10, 12].", "Many sources [10][12]."),
        # Two groups in one sentence.
        ("First [1, 2], then [3, 4].", "First [1][2], then [3][4]."),
    ],
)
def test_a_grouped_marker_becomes_adjacent_single_markers(raw: str, expanded: str) -> None:
    # Red when: expand_grouped_markers stops splitting a comma group into [n][n] markers.
    assert _expand(raw) == expanded


def test_the_answer_from_the_issue_is_expanded_and_its_code_span_kept() -> None:
    # Red when: the expander misses the long group from the live answer, or touches `--model`.
    raw = (
        "The specific models supported by Codex are not explicitly listed "
        "[1, 2, 3, 4, 5, 6]. However, the model used by Codex can be selected "
        "using the `--model` flag [2]."
    )
    assert _expand(raw) == (
        "The specific models supported by Codex are not explicitly listed "
        "[1][2][3][4][5][6]. However, the model used by Codex can be selected "
        "using the `--model` flag [2]."
    )


def test_line_endings_are_preserved_around_an_expanded_group() -> None:
    # Red when: the expander rebuilds the text and drops a \r from a CRLF answer.
    assert _expand("Line [1, 2]\r\nNext [3]\r\n") == "Line [1][2]\r\nNext [3]\r\n"


def test_expanding_twice_changes_nothing_more() -> None:
    # Red when: the expander is not idempotent (e.g. it re-wraps already single markers).
    once = _expand("A [1, 2] B `c[3, 4]` D [5].")
    assert _expand(once) == once


# ---------------------------------------------------------------------------
# The expander: text it must leave exactly as written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "No markers at all.",
        "A single marker [1].",
        "Already split [1][2][3].",
        "Spread out [1] and [3].",
    ],
)
def test_text_without_a_group_is_returned_unchanged(text: str) -> None:
    # Red when: the expander rewrites text that holds no comma group.
    assert _expand(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Letters [a, b] are not markers.",
        "A word in the group [1, x] is not a marker group.",
        "Empty brackets [] stay.",
        "A trailing comma [1,] stays.",
        "A leading comma [,1] stays.",
        "A dangling comma [1, 2,] stays.",
        # A range is NOT expanded: the model is never asked for ranges, and
        # reading a dash as "every index between" could attach an uncited card.
        "A range [1-3] stays.",
        "An en-dash range [1–3] stays.",
        "Semicolons [1; 2] stay.",
        "Spaces only [1 2] stay.",
        # Whitespace is allowed around commas only, never inside the brackets'
        # ends: ``[ 1 ]`` is not a marker today, so ``[ 1, 2 ]`` is not a group.
        "Padded [ 1, 2 ] stays.",
        "Decimals [1.5, 2] stay.",
        "Negative [-1, 2] stays.",
    ],
)
def test_brackets_that_are_not_a_marker_group_are_left_alone(text: str) -> None:
    # Red when: the expander's grammar is loosened beyond digits separated by commas.
    assert _expand(text) == text


# ---------------------------------------------------------------------------
# The expander: markdown code is not touched (#237's definition of code)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Index it with `arr[1, 2]` in Python.",
        "A double-tick span ``a`[1, 2]`` is code.",
        "```python\nm = grid[1, 2]\n```",
        "~~~\nm = grid[1,2]\n~~~",
        # Unterminated fence: _strip_code treats the tail as code (truncated output).
        "Intro.\n```\nm = grid[1, 2]",
    ],
)
def test_a_group_inside_markdown_code_is_left_alone(text: str) -> None:
    # Red when: the expander rewrites brackets inside an inline code span or a fence.
    assert _expand(text) == text


def test_prose_group_is_expanded_while_the_code_span_on_the_same_line_is_not() -> None:
    # Red when: the expander skips the whole line because it holds code, or expands the code.
    assert _expand("Use `a[1, 2]` then cite [1, 2].") == "Use `a[1, 2]` then cite [1][2]."


def test_prose_group_after_a_closed_fence_is_expanded_and_the_fence_is_not() -> None:
    # Red when: the expander treats the text after a closed fence as still inside it.
    raw = "```\nm = grid[1, 2]\n```\nSee [1, 2]."
    assert _expand(raw) == "```\nm = grid[1, 2]\n```\nSee [1][2]."


def test_prose_group_after_a_closed_double_tick_span_is_expanded() -> None:
    # Red when: a backtick inside a double-tick span ends the span early or swallows the line.
    raw = "``a`[1, 2]`` and [1, 2]"
    assert _expand(raw) == "``a`[1, 2]`` and [1][2]"


# ---------------------------------------------------------------------------
# Expansion feeds validate_citations exactly as split markers would
# ---------------------------------------------------------------------------


def test_an_expanded_group_validates_the_same_as_split_markers() -> None:
    # Red when: an expanded group yields different cited indices than [1][2][3].
    evidence = _evidence_hits(count=3)
    grouped = validate_citations(answer_text=_expand("A [1, 2, 3]."), evidence=evidence)
    split = validate_citations(answer_text="A [1][2][3].", evidence=evidence)
    assert grouped == split
    assert grouped.valid is True
    assert grouped.cited_indices == [1, 2, 3]


def test_an_out_of_range_index_inside_a_group_fails_exactly_as_split_markers_do() -> None:
    # Red when: expansion drops or clamps an out-of-range index inside a group.
    evidence = _evidence_hits(count=2)
    grouped = validate_citations(answer_text=_expand("A [1, 9]."), evidence=evidence)
    split = validate_citations(answer_text="A [1][9].", evidence=evidence)
    assert grouped == split
    assert grouped.valid is False
    assert grouped.reason is not None and "[9]" in grouped.reason


def test_a_range_stays_out_of_the_cited_set() -> None:
    # Red when: a range such as [1-3] is read as citing 1, 2 and 3.
    evidence = _evidence_hits(count=3)
    result = validate_citations(answer_text=_expand("Steps [1-3]. Done [2]."), evidence=evidence)
    assert result.cited_indices == [2]


# ---------------------------------------------------------------------------
# Orchestrator: the expanded text is what is served, stored and cached
# ---------------------------------------------------------------------------


class _FixedTextLLM:
    """Returns one fixed answer text, whatever the prompt."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, **_kwargs: Any) -> LLMResult:
        return LLMResult(
            text=self._text,
            input_tokens=1,
            output_tokens=1,
            model="stub-deterministic-v1",
            provider="stub",
        )

    async def aclose(self) -> None:
        return None


async def _ask_fixed(
    session: Any, *, text: str, count: int
) -> tuple[dict[str, Any], list[EvidenceHit]]:
    # The index version first: seeding evidence chunks reuses it.
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


async def _assistant_content(session: Any) -> str:
    rows = (await session.execute(select(Message))).scalars().all()
    (assistant,) = [m for m in rows if m.role == MessageRole.assistant]
    return assistant.content


@pytest.mark.asyncio
async def test_a_grouped_answer_is_served_stored_and_cached_in_expanded_form(
    session: Any,
) -> None:
    # Red when: the orchestrator validates or stores the raw model text instead of the expanded.
    response, evidence = await _ask_fixed(
        session, text="Use `cfg[1, 2]` here [1, 2]. Also see [3].", count=3
    )
    expanded = "Use `cfg[1, 2]` here [1][2]. Also see [3]."

    assert response["no_answer"] is False
    assert response["answer"] == expanded
    assert [c["marker"] for c in response["citations"]] == [1, 2, 3]
    assert [c["chunk_id"] for c in response["citations"]] == [str(h.chunk_id) for h in evidence]
    assert response["confidence"] == "high"
    assert await _assistant_content(session) == expanded
    cache_rows = (await session.execute(select(AnswerCache))).scalars().all()
    assert [row.answer for row in cache_rows] == [expanded]


@pytest.mark.asyncio
async def test_the_live_answer_shape_is_served_with_six_real_markers(session: Any) -> None:
    # Red when: a six-index group still reaches the client as literal "[1, 2, 3, 4, 5, 6]".
    response, _ = await _ask_fixed(
        session,
        text=(
            "Codex can select its model with the `--model` flag [1, 2, 3, 4, 5, 6]. "
            "The default is set in the config file [2]."
        ),
        count=6,
    )

    assert response["no_answer"] is False
    assert "[1, 2" not in response["answer"]
    assert "[1][2][3][4][5][6]" in response["answer"]
    assert "`--model`" in response["answer"]
    assert [c["marker"] for c in response["citations"]] == [1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_an_out_of_range_group_fails_citation_validation_not_as_uncited(
    session: Any,
) -> None:
    # Red when: an out-of-range index inside a group is not seen by the range check.
    response, _ = await _ask_fixed(session, text="Settings live in a file [1, 9].", count=2)

    assert response["no_answer"] is True
    assert response["citations"] == []
    assert (await session.execute(select(AnswerCache))).scalars().all() == []
    (audit,) = (await session.execute(select(AuditEvent))).scalars().all()
    assert audit.metadata_["outcome"] == "citation_validation_failed"
    assert "[9]" in audit.metadata_["reason"]


@pytest.mark.asyncio
async def test_the_exact_lookup_retry_also_expands_a_grouped_answer(session: Any) -> None:
    # Red when: only the main path expands, so the #208 retry still drops a grouped answer.
    retriever = _IntentAwareRetriever(
        exact=[_hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.exact)],
        hybrid=[
            _hit(_CONFIG_PASSAGE, retrieval_type=RetrievalType.hybrid),
            _hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.hybrid),
        ],
    )
    llm = _GroundedOnlyLLM(answer_text="Yes, settings live in a config file [1, 2].")

    response = await _ask(session, question=_FLAG_QUESTION, retriever=retriever, llm=llm)

    assert response["no_answer"] is False
    assert response["answer"] == "Yes, settings live in a config file [1][2]."
    assert [c["marker"] for c in response["citations"]] == [1, 2]
    assert await _assistant_content(session) == "Yes, settings live in a config file [1][2]."


# ---------------------------------------------------------------------------
# The system prompt asks for the right form in the first place
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n")


def _prompt_sentences() -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(SYSTEM_PROMPT) if s.strip()]


def test_the_prompt_shows_one_marker_per_bracket_and_names_the_grouped_form() -> None:
    # Red when: the prompt drops the "[1][2], never [1, 2]" instruction.
    assert "[1][2]" in SYSTEM_PROMPT
    assert "[1, 2]" in SYSTEM_PROMPT
    # Both in one sentence, so the grouped form reads as the thing NOT to write.
    assert any("[1][2]" in s and "[1, 2]" in s for s in _prompt_sentences())


def test_the_prompt_says_not_to_cite_a_sentence_saying_the_answer_is_absent() -> None:
    # Red when: the prompt drops the rule that a "not in the evidence" sentence carries no marker.
    absent = ("not contain", "not in the evidence", "not listed", "not covered", "not mention")
    cite = ("cite", "citation", "marker")
    matches = [
        s
        for s in _prompt_sentences()
        if any(a in s.lower() for a in absent) and any(c in s.lower() for c in cite)
    ]
    assert matches, SYSTEM_PROMPT


def test_the_prompt_forbids_the_based_on_the_provided_evidence_phrasing() -> None:
    # Red when: the prompt stops telling the model not to say "based on the provided evidence".
    assert "based on the provided evidence" in SYSTEM_PROMPT.lower()


def test_the_new_prompt_rules_come_before_the_refusal_clause() -> None:
    # Red when: a new rule is appended after "respond with exactly ... and nothing else".
    refusal_at = SYSTEM_PROMPT.index("respond with exactly")
    assert SYSTEM_PROMPT.index("[1][2]") < refusal_at
    assert SYSTEM_PROMPT.lower().index("based on the provided evidence") < refusal_at
