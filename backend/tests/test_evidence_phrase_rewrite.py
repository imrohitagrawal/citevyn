"""Answers must not call the retrieval context "the provided evidence" (#554, item 3).

The system prompt already bans the phrasing, and production still served an
answer opening "The provided evidence mentions ...". So the served text is now
rewritten in code before it reaches the user, on the same line that splits
grouped markers.

Interface pinned here (``app.llm.validation``):

* ``neutralise_evidence_references(text: str) -> str``. Pure, no I/O.
* A match is, case-insensitively, word-bounded and on one line: ``the`` + an
  optional pre-modifier (``provided``, ``supplied``, ``given``, ``available``,
  ``retrieved``, ``above``, ``listed``) + ``evidence`` + an optional plural head
  (``bullets``, ``items``, ``snippets``, ``excerpts``).
* The replacement is "the sources I found", first letter in the case it had.
  Because it is plural, a match is rewritten ONLY when what follows is on an
  ALLOWLIST:

  (a) end of text, punctuation (``, . ; : ) ! ?``), or one or more ``[n]``
      markers followed by punctuation or the end; or
  (b) at most ONE adverb (``clearly``, ``only``, ``also``, ``explicitly``,
      ``specifically``, ``directly``, ``not``) and then a verb from a closed
      singular-to-plural map, which is rewritten too (``mentions`` -> ``mention``,
      ``does`` -> ``do``, ``doesn't`` -> ``don't``, ``is`` -> ``are`` ...), keeping
      the verb's case. A plural head already takes a plural verb: only the noun
      is rewritten.

  Anything else ("for X is", "you provided", "provided to me", "-based",
  "that ...", an unknown verb) is left exactly as written and counted as
  skipped. The judged eval case catches what is left.
* Markdown code (as ``_strip_code`` defines it) is never touched, grouped
  markers are not this function's job, and every byte outside a rewrite is
  returned as it was, line endings included.
* Only nouns headed by "evidence": Claude's own docs say "the provided
  documents" and "the provided context" in their own meaning.

The orchestrator applies it to the served, stored and cached text, and logs
``answer_evidence_phrase_rewritten`` with int fields ``rewrites`` and
``skipped`` when either is non-zero.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.answer.comparison import missing_note
from app.llm.prompts import NO_ANSWER_REFUSAL
from app.llm.types import LLMResult
from app.models import AnswerCache, Message, MessageRole, RetrievalType
from app.retrieval.types import EvidenceHit
from tests.orchestrator_double import Orchestrator
from tests.test_answer_orchestrator import (
    _evidence,
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

_FOUND = "the sources I found"

# The answer from the issue body, word for word (Fly v24).
_LIVE_ANSWER = (
    "Based on the provided evidence, the specific models supported by Codex are not "
    "explicitly listed [1, 2, 3, 4, 5, 6]. However, the model used by Codex for code "
    "generation can be selected using the `--model` flag [2]."
)
_LIVE_ANSWER_REWRITTEN = (
    "Based on the sources I found, the specific models supported by Codex are not "
    "explicitly listed [1, 2, 3, 4, 5, 6]. However, the model used by Codex for code "
    "generation can be selected using the `--model` flag [2]."
)

_VERB_MAP = {
    "mentions": "mention",
    "says": "say",
    "states": "state",
    "shows": "show",
    "lists": "list",
    "contains": "contain",
    "describes": "describe",
    "indicates": "indicate",
    "suggests": "suggest",
    "notes": "note",
    "includes": "include",
    "covers": "cover",
    "explains": "explain",
    "specifies": "specify",
    "confirms": "confirm",
    "provides": "provide",
    "does": "do",
    "doesn't": "don't",
    "is": "are",
    "isn't": "aren't",
    "has": "have",
    "hasn't": "haven't",
    "was": "were",
    "wasn't": "weren't",
}
_ADVERBS = ["clearly", "only", "also", "explicitly", "specifically", "directly", "not"]


def _rewrite(text: str) -> str:
    """Call the rewriter. Imported here so each test fails on its own, by name."""
    from app.llm.validation import neutralise_evidence_references

    return neutralise_evidence_references(text)


# ---------------------------------------------------------------------------
# The rewriter: what it rewrites
# ---------------------------------------------------------------------------


def test_the_live_answer_from_the_issue_is_rewritten_and_its_groups_left_alone() -> None:
    # Red when: the rewriter is missing, or it touches anything beyond the phrase (the grouped
    # marker, the code span, the rest of the sentence).
    assert _rewrite(_LIVE_ANSWER) == _LIVE_ANSWER_REWRITTEN


def test_the_answer_seen_after_the_v29_deploy_is_rewritten_with_plural_verbs() -> None:
    # Red when: only the first occurrence is rewritten, a sentence-initial "The" loses its
    # capital, or a verb is left singular after the plural replacement.
    raw = (
        "The provided evidence mentions the `--model` flag [2]. However, the evidence does "
        "not list the specific models that Codex supports."
    )
    assert _rewrite(raw) == (
        "The sources I found mention the `--model` flag [2]. However, the sources I found do "
        "not list the specific models that Codex supports."
    )


@pytest.mark.parametrize(
    "modifier", ["provided", "supplied", "given", "available", "retrieved", "above", "listed"]
)
def test_each_pre_modifier_is_rewritten(modifier: str) -> None:
    # Red when: a pre-modifier from the plan's list is not part of the match.
    assert _rewrite(f"According to the {modifier} evidence, X is true [1].") == (
        f"According to {_FOUND}, X is true [1]."
    )


@pytest.mark.parametrize(("singular", "plural"), sorted(_VERB_MAP.items()))
def test_each_mapped_verb_is_made_plural(singular: str, plural: str) -> None:
    # Red when: a verb in the closed map is missing, or mapped to the wrong plural form.
    assert _rewrite(f"The evidence {singular} it [1].") == f"The sources I found {plural} it [1]."


@pytest.mark.parametrize("adverb", _ADVERBS)
def test_one_adverb_before_a_mapped_verb_is_kept(adverb: str) -> None:
    # Red when: an allowlisted adverb blocks the rewrite, or the adverb is dropped or moved.
    assert _rewrite(f"The evidence {adverb} states X [1].") == (
        f"The sources I found {adverb} state X [1]."
    )


def test_the_evidence_clearly_states_from_the_owner_examples() -> None:
    # Red when: the adverb-then-verb path is missing.
    assert (
        _rewrite("The evidence clearly states X [1].") == "The sources I found clearly state X [1]."
    )


@pytest.mark.parametrize(
    ("raw", "rewritten"),
    [
        ("THE PROVIDED EVIDENCE IS THIN [1].", "The sources I found ARE THIN [1]."),
        ("The Provided Evidence Shows it [1].", "The sources I found Show it [1]."),
        ("so the Evidence shows it [1].", "so the sources I found show it [1]."),
        ("The provided evidence mentions [1].", "The sources I found mention [1]."),
    ],
)
def test_the_first_letter_and_the_verb_keep_their_case(raw: str, rewritten: str) -> None:
    # Red when: the match or the verb lookup is case-sensitive, the replacement copies the
    # original casing ("THE SOURCES I FOUND"), or the verb loses its case.
    assert _rewrite(raw) == rewritten


@pytest.mark.parametrize(
    ("raw", "rewritten"),
    [
        ("Nothing is in the evidence.", f"Nothing is in {_FOUND}."),
        ("the evidence, then [1]", f"{_FOUND}, then [1]"),
        ("(the evidence) [1]", f"({_FOUND}) [1]"),
        ("Per the evidence; see [1].", f"Per {_FOUND}; see [1]."),
        ("Per the evidence: see [1].", f"Per {_FOUND}: see [1]."),
        ("Is it in the evidence?", f"Is it in {_FOUND}?"),
        ("It is in the evidence!", f"It is in {_FOUND}!"),
        ("It is in the evidence", f"It is in {_FOUND}"),
        ("It is in the evidence [1].", f"It is in {_FOUND} [1]."),
        ("It is in the evidence [1][2]", f"It is in {_FOUND} [1][2]"),
    ],
)
def test_a_match_followed_by_punctuation_markers_or_the_end_is_rewritten(
    raw: str, rewritten: str
) -> None:
    # Red when: punctuation, trailing markers or the end of text are not on the allowlist.
    assert _rewrite(raw) == rewritten


@pytest.mark.parametrize("head", ["bullets", "items", "snippets", "excerpts"])
def test_a_plural_head_is_rewritten_and_its_verb_left_alone(head: str) -> None:
    # Red when: a plural head is left behind ("the sources I found bullets"), or its already
    # plural verb is changed.
    assert _rewrite(f"The evidence {head} describe it [1].") == (
        "The sources I found describe it [1]."
    )


def test_a_pre_modifier_and_a_plural_head_before_punctuation() -> None:
    # Red when: a pre-modifier and a plural head are not matched as one phrase.
    assert _rewrite("Per the provided evidence snippets, use a flag [1].") == (
        f"Per {_FOUND}, use a flag [1]."
    )


def test_line_endings_are_preserved_around_a_rewrite() -> None:
    # Red when: the rewriter normalises CRLF or LF, or rewrites across a line end.
    raw = "Line one uses the provided evidence [1].\r\nLine two.\r\n\nThe evidence says [2].\n"
    assert _rewrite(raw) == (
        "Line one uses the sources I found [1].\r\nLine two.\r\n\nThe sources I found say [2].\n"
    )


def test_prose_is_rewritten_while_a_code_span_on_the_same_line_is_not() -> None:
    # Red when: the rewriter ignores code masking, or masks the whole line.
    assert _rewrite("Use `the evidence` but the evidence says [1].") == (
        f"Use `the evidence` but {_FOUND} say [1]."
    )


# ---------------------------------------------------------------------------
# The rewriter: what it leaves alone (and counts as skipped)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The evidence for this is thin.",
        "The evidence provided to me does not list models [1].",
        "Based on the evidence you provided, the flag selects a model [1].",
        "The evidence supplied covers flags [1].",
        "The evidence given covers flags [1].",
        "The evidence listed covers flags [1].",
        "The evidence shown covers flags [1].",
        "The evidence I was given covers flags [1].",
        "The evidence we have covers flags [1].",
        "the evidence that X works is thin [1].",
        "the evidence which follows is thin [1].",
        "The evidence-based answer is below [1].",
        "the evidenced claim holds [1].",
        # A verb outside the closed map, two adverbs, or an adverb before an unknown verb.
        "The evidence lacks detail [1].",
        "The evidence clearly also states X [1].",
        "The evidence clearly lacks detail [1].",
        # Markers before a verb are not on the allowlist (only markers then punctuation/end).
        "The evidence [1] says X.",
        # A plural head followed by an unknown word.
        "the evidence bullets for X agree [1].",
    ],
)
def test_a_match_with_a_follower_outside_the_allowlist_is_left_as_written(text: str) -> None:
    # Red when: the rewriter goes beyond the allowlist, so "the evidence provided to me" or
    # "the evidence for this is thin" becomes ungrammatical plural text.
    assert _rewrite(text) == text


@pytest.mark.parametrize(
    "text",
    [
        # The phrase split by markdown bold, or across a line break (advisory, recorded).
        "Per the **provided evidence**, use a flag [1].",
        "Per the provided\nevidence, use a flag [1].",
        "Per the\r\nevidence, use a flag [1].",
        # Markdown code is not prose.
        "Run `grep 'the provided evidence' notes.txt` [1].",
        "```\nprint('the provided evidence.')\n```\nSee [1].",
        "~~~text\nThe evidence says so.\n~~~\nSee [1].",
        "``the evidence`` [1]",
    ],
)
def test_bold_line_broken_and_code_phrases_are_left_alone(text: str) -> None:
    # Red when: the pattern lets whitespace include a newline, looks through markdown
    # emphasis, or skips the code masking.
    assert _rewrite(text) == text


@pytest.mark.parametrize(
    "text",
    [
        # Claude's own docs use these in their own meaning; only "evidence" heads a match.
        "Answer only from the provided documents [1].",
        "Answer only from the provided context [1].",
        "the retrieved information and the given sources [1]",
        "the provided excerpts [1]",
        # Bare "evidence", or another determiner.
        "There is no evidence that it supports X [1].",
        "Evidence suggests a flag [1].",
        "This evidence and that evidence agree [1].",
        # "the" must be its own word.
        "Bathe evidence aside; a lathe provided evidence [1].",
        # Nothing to rewrite at all.
        "Use the --model flag [1].",
        "",
    ],
)
def test_text_without_a_match_is_returned_unchanged(text: str) -> None:
    # Red when: the rewriter widens to other nouns or determiners, drops a word boundary, or
    # alters text it did not match.
    assert _rewrite(text) == text


def test_the_refusal_and_the_comparison_note_are_untouched() -> None:
    # Red when: the rewriter changes the refusal constant or the shipped "does not cover" note.
    assert _rewrite(NO_ANSWER_REFUSAL) == NO_ANSWER_REFUSAL
    assert _rewrite(_settings().no_answer_fallback) == _settings().no_answer_fallback
    note = missing_note(["codex"])
    assert note, "partner: the note must be non-empty"
    assert _rewrite(note) == note


@pytest.mark.parametrize(
    "text",
    [
        _LIVE_ANSWER,
        "The provided evidence mentions [1]. The evidence provided to me does not [2].",
        "The evidence bullets describe it [1].",
        "THE PROVIDED EVIDENCE IS THIN [1].",
        "The evidence doesn't say. Use `the evidence` but the evidence says [1].\r\n",
    ],
)
def test_rewriting_twice_changes_nothing_more(text: str) -> None:
    # Red when: the replacement itself matches the pattern, or a second pass rewrites more.
    once = _rewrite(text)
    assert once != text, "partner: the sample must contain a rewrite"
    assert _rewrite(once) == once


@pytest.mark.parametrize(
    "text",
    [
        _LIVE_ANSWER,
        "The provided evidence mentions [1]. The evidence provided to me does not [2].",
        "As the evidence shows [1][2], see `arr[3]` and [4, 5].",
    ],
)
def test_the_rewrite_never_changes_a_marker(text: str) -> None:
    # Red when: the rewrite adds, drops or moves a "[n]" or a grouped marker, which would
    # change what validation, citations and confidence read.
    marker = re.compile(r"\[[0-9, ]+\]")
    assert _rewrite(text) != text, "partner: the sample must contain a rewrite"
    assert marker.findall(_rewrite(text)) == marker.findall(text)


# ---------------------------------------------------------------------------
# Orchestrator: served, stored, cached, logged; citations and confidence unchanged
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


async def _cached_answers(session: Any) -> list[str]:
    return [row.answer for row in (await session.execute(select(AnswerCache))).scalars().all()]


def _phrase_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "answer_evidence_phrase_rewritten"]


_WITH_PHRASE = "Based on the provided evidence, settings live in a file [1]. See also [3]."
_WITHOUT_PHRASE = "Based on the sources I found, settings live in a file [1]. See also [3]."


@pytest.mark.asyncio
async def test_a_phrased_answer_is_served_stored_and_cached_rewritten(session: Any) -> None:
    # Red when: the orchestrator serves, stores or caches the model's text without the rewrite.
    response, _ = await _ask_fixed(session, text=_WITH_PHRASE, count=3)

    assert response["no_answer"] is False
    assert response["answer"] == _WITHOUT_PHRASE
    assert "evidence" not in response["answer"]
    assert await _assistant_content(session) == _WITHOUT_PHRASE
    assert await _cached_answers(session) == [_WITHOUT_PHRASE]


@pytest.mark.parametrize("text", [_WITH_PHRASE, _WITHOUT_PHRASE])
@pytest.mark.asyncio
async def test_citations_and_confidence_do_not_depend_on_the_phrase(
    session: Any, text: str
) -> None:
    # Red when: the rewrite runs before validation or citation mapping and changes what they
    # read, so the phrased answer gets different cards or a different confidence.
    response, evidence = await _ask_fixed(session, text=text, count=3)

    assert response["no_answer"] is False
    assert response["answer"] == _WITHOUT_PHRASE
    assert [c["marker"] for c in response["citations"]] == [1, 3]
    assert [c["chunk_id"] for c in response["citations"]] == [
        str(evidence[0].chunk_id),
        str(evidence[2].chunk_id),
    ]
    assert response["confidence"] == "high"


@pytest.mark.asyncio
async def test_the_live_answer_is_served_rewritten_with_groups_split_and_low_confidence(
    session: Any,
) -> None:
    # Red when: the rewrite is not chained with the group split, or it changes the #554
    # "split, don't count" result (six cards, confidence from the single [2] only).
    response, _ = await _ask_fixed(session, text=_LIVE_ANSWER, count=6)

    assert response["no_answer"] is False
    assert response["answer"].startswith("Based on the sources I found, the specific models")
    assert "listed [1][2][3][4][5][6]." in response["answer"]
    assert "`--model` flag [2]." in response["answer"]
    assert "evidence" not in response["answer"]
    assert [c["marker"] for c in response["citations"]] == [1, 2, 3, 4, 5, 6]
    assert response["confidence"] == "low"


@pytest.mark.asyncio
async def test_a_rewrite_and_a_skip_are_logged_as_int_counts(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # Red when: the orchestrator does not log the event, logs it below INFO, or logs the
    # counts as anything but ints (the log redactor prints only ints without an allowlist).
    text = "The provided evidence mentions a flag [1]. The evidence provided to me does not [2]."
    with caplog.at_level(logging.DEBUG, logger="citevyn.answer"):
        response, _ = await _ask_fixed(session, text=text, count=2)

    assert response["answer"] == (
        "The sources I found mention a flag [1]. The evidence provided to me does not [2]."
    )
    (event,) = _phrase_events(caplog)
    assert event.levelno >= logging.INFO
    rewrites: Any = getattr(event, "rewrites", None)
    skipped: Any = getattr(event, "skipped", None)
    assert type(rewrites) is int and rewrites == 1
    assert type(skipped) is int and skipped == 1


@pytest.mark.parametrize(
    "text",
    [
        "The evidence provided to me does not list models [1].",
        "The evidence for this is thin [1].",
    ],
)
@pytest.mark.asyncio
async def test_a_skip_alone_is_logged_and_the_text_left_as_written(
    session: Any, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    # Red when: the event is emitted only when the text changed, so a skipped phrase that
    # still reaches the user leaves no trace in production.
    with caplog.at_level(logging.DEBUG, logger="citevyn.answer"):
        response, _ = await _ask_fixed(session, text=text, count=1)

    assert response["answer"] == text
    (event,) = _phrase_events(caplog)
    assert getattr(event, "rewrites", None) == 0
    assert getattr(event, "skipped", None) == 1


@pytest.mark.asyncio
async def test_an_answer_without_the_phrase_logs_no_event(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # Red when: the event fires on every answer (it would no longer measure the leak).
    # Partner: the two tests above prove the event exists when there is something to count.
    with caplog.at_level(logging.DEBUG, logger="citevyn.answer"):
        response, _ = await _ask_fixed(session, text="Settings live in a file [1].", count=1)

    assert response["no_answer"] is False
    assert _phrase_events(caplog) == []


@pytest.mark.asyncio
async def test_the_exact_lookup_retry_answer_is_rewritten_too(session: Any) -> None:
    # Red when: only the first-pass answer is rewritten, so a #208 retry answer still says
    # "the provided evidence".
    retriever = _IntentAwareRetriever(
        exact=[_hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.exact)],
        hybrid=[
            _hit(_CONFIG_PASSAGE, retrieval_type=RetrievalType.hybrid),
            _hit(_FLAG_PASSAGE, retrieval_type=RetrievalType.hybrid),
        ],
    )
    llm = _GroundedOnlyLLM(answer_text="The provided evidence says settings live in a file [1].")

    response = await _ask(session, question=_FLAG_QUESTION, retriever=retriever, llm=llm)

    rewritten = "The sources I found say settings live in a file [1]."
    assert response["no_answer"] is False
    assert response["answer"] == rewritten
    assert await _assistant_content(session) == rewritten
