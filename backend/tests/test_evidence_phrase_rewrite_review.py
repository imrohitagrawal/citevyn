"""Review findings on the evidence-phrase rewrite (#554 item 3).

* After a preposition ("Nothing in the provided evidence mentions ..."), the
  phrase is not the subject of the next verb, so only the noun phrase is
  rewritten and the verb is left as written.
* A line that starts with a code fence is never rewritten, and the phrase on it
  is counted as skipped.
* A curly apostrophe in the verb survives the plural rewrite.
* The counts returned by ``rewrite_evidence_references`` and the request id on
  the orchestrator's log event are pinned directly.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.llm.validation import neutralise_evidence_references, rewrite_evidence_references
from tests.test_evidence_phrase_rewrite import _ask_fixed, _phrase_events

_PREPOSITIONS = [
    "in",
    "of",
    "from",
    "within",
    "across",
    "on",
    "at",
    "by",
    "for",
    "with",
    "throughout",
    "among",
    "about",
    "under",
    "inside",
    "through",
    "into",
    "over",
]


@pytest.mark.parametrize(
    ("raw", "rewritten"),
    [
        (
            "Nothing in the provided evidence mentions a model list [1].",
            "Nothing in the sources I found mentions a model list [1].",
        ),
        (
            "Each section of the evidence lists a flag [1].",
            "Each section of the sources I found lists a flag [1].",
        ),
        (
            "The text in the evidence is short [1].",
            "The text in the sources I found is short [1].",
        ),
        (
            "Nothing IN the evidence says so [1].",
            "Nothing IN the sources I found says so [1].",
        ),
    ],
)
def test_after_a_preposition_only_the_noun_is_rewritten(raw: str, rewritten: str) -> None:
    # Red when: the verb after "<preposition> the evidence" is made plural, as if the
    # phrase were its subject ("Nothing in the sources I found mention ...").
    assert neutralise_evidence_references(raw) == rewritten


@pytest.mark.parametrize("preposition", _PREPOSITIONS)
def test_each_listed_preposition_keeps_the_verb(preposition: str) -> None:
    # Red when: any preposition is missing from the fixed list, so its verb is pluralised.
    raw = f"A note {preposition} the evidence mentions a flag [1]."
    result = rewrite_evidence_references(raw)
    assert result.text == f"A note {preposition} the sources I found mentions a flag [1]."
    assert (result.rewrites, result.skipped) == (1, 0)


@pytest.mark.parametrize(
    ("raw", "rewritten"),
    [
        # "within" ends in "in" but is its own word; "begin" ends in "in" and is no preposition.
        (
            "Stuff to begin the evidence mentions [1].",
            "Stuff to begin the sources I found mention [1].",
        ),
        # At the start of the text there is no word before "the": the verb is pluralised.
        ("The evidence mentions a flag [1].", "The sources I found mention a flag [1]."),
        # At the start of a later line, likewise.
        (
            "Intro.\nThe evidence mentions a flag [1].",
            "Intro.\nThe sources I found mention a flag [1].",
        ),
    ],
)
def test_the_preposition_must_be_a_whole_word_before_the(raw: str, rewritten: str) -> None:
    # Red when: the preposition check is not word-bounded (a word ending in "in" counts),
    # or it looks across a line break or misfires at the start of the text.
    assert neutralise_evidence_references(raw) == rewritten


@pytest.mark.parametrize(
    "text",
    [
        "```the provided evidence says X [1]",
        "``` the provided evidence.",
    ],
)
def test_a_fence_line_is_left_as_written_and_counted_skipped(text: str) -> None:
    # Red when: the fence-line check is removed, so a phrase on a line opening a code
    # fence is rewritten (or not counted as skipped).
    result = rewrite_evidence_references(text)
    assert result.text == text
    assert (result.rewrites, result.skipped) == (0, 1)


def test_a_curly_apostrophe_survives_the_plural_verb() -> None:
    # Red when: the plural verb is spelt with a straight apostrophe, or a curly one is not
    # recognised as a mapped verb.
    assert (
        neutralise_evidence_references("The evidence doesn’t say [1].")
        == "The sources I found don’t say [1]."
    )


@pytest.mark.parametrize(
    ("text", "rewrites", "skipped"),
    [
        ("Settings live in a file [1].", 0, 0),
        ("The provided evidence mentions a flag [1].", 1, 0),
        ("The evidence for this is thin [1].", 0, 1),
        (
            "The evidence says X [1]. The evidence provided to me does not [2]. Per the evidence.",
            2,
            1,
        ),
    ],
)
def test_the_counts_are_returned(text: str, rewrites: int, skipped: int) -> None:
    # Red when: either count is off by one, swapped, or not incremented for a phrase.
    result = rewrite_evidence_references(text)
    assert (result.rewrites, result.skipped) == (rewrites, skipped)


@pytest.mark.asyncio
async def test_the_log_event_carries_the_request_id(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # Red when: the orchestrator drops ``request_id`` from the event's extra fields, so a
    # leak in production cannot be traced to its request.
    with caplog.at_level(logging.DEBUG, logger="citevyn.answer"):
        await _ask_fixed(session, text="The provided evidence mentions a flag [1].", count=1)

    (event,) = _phrase_events(caplog)
    request_id: Any = getattr(event, "request_id", None)
    assert isinstance(request_id, str)
    assert request_id.startswith("req_") and len(request_id) == len("req_") + 8
