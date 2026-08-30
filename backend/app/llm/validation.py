"""Mechanical citation validator.

Lives under :mod:`app.llm` (rather than :mod:`app.validation`) because
the validation logic is tightly coupled to the citation contract
documented in :mod:`app.llm.prompts` and the evidence-block convention
in :mod:`app.llm.stub`. Slice 6 consumes this; tests consume it
directly with arbitrary evidence.

Contract (matches ``docs/API_SPEC.md`` §5 + the LLM client prompt):

* ``[n]`` markers in ``answer_text`` must be 1-indexed and in range:
  ``1 <= n <= len(evidence)``. ``[0]`` and ``[N+1]`` fail.
* Every ``[n]`` must reference an evidence bullet that exists.
* A ``[n]`` inside markdown code is NOT a marker (#237). ``arr[2]`` in a
  code span and ``delays[3]`` in a fence used to be counted, which
  attached a phantom source card, inflated ``confidence``, and — when the
  index exceeded the evidence count — discarded a correct answer. See
  the stripping note below for exactly which code forms are covered and
  which are deliberately not.
* Uncited evidence bullets — including bullets SKIPPED between two cited
  ones — are reported as a warning, not a failure (the model may
  legitimately not cite every retrieved chunk). A gap is therefore
  valid: citing ``[1]`` and ``[3]`` is as acceptable as citing ``[1]``
  alone. Requiring contiguity discarded correct answers in production
  (#215) and was never asked of the model by
  :mod:`app.llm.prompts`.
* When ``answer_text`` is the no-answer refusal (exact match against
  :data:`app.llm.prompts.NO_ANSWER_REFUSAL`, or contains the canonical
  no-answer substring), the result is ``valid=True`` with empty
  ``cited_indices`` and ``uncited_indices``.
* Hard-fail cases are surfaced by the orchestrator (Slice 6) as a
  no-answer response carrying ``APIErrorCode.citation_validation_failed``
  in the error envelope — that code is mapped to HTTP 200 in
  :mod:`app.core.errors` because it is not a transport failure.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.llm.prompts import NO_ANSWER_REFUSAL
from app.retrieval.types import EvidenceHit

# Matches a citation marker like ``[1]``, ``[12]``. Captures the digits.
_CITATION_RE = re.compile(r"\[(\d+)\]")

# --- Markdown code stripping (#237) ----------------------------------------
#
# ``[n]`` inside code is not a citation. Scanning the raw answer counted
# ``arr[2]`` in a span and ``delays[3]`` in a fence as markers, which attached
# a real-but-uncited source card labelled with that number, inflated
# ``confidence`` (``len(cited)/len(evidence)``) by a whole band, and — when the
# bracketed index exceeded the evidence count — discarded a correct, grounded
# answer through the out-of-range branch below.
#
# What this does NOT fix, deliberately: an answer that cites nothing in prose
# but contains one bracketed number inside a fence is still served as grounded.
# Stripping empties the set, the ``or`` fallback restores the raw scan, and the
# #174 gate at ``orchestrator.py:830`` sees a non-empty set exactly as before.
# That is the price of the fallback, not an oversight -- closing it would need
# a distinct "code-only citation" exit reason registered in
# ``_NO_ANSWER_REASONS``, which is a larger change than this one.
#
# The design rule here is asymmetric, because the two error directions are not
# equally bad. LEAKING a marker out of code leaves a visible phantom card that
# the range check often catches anyway. LOSING a real marker silently deletes a
# source card and depresses ``confidence``, with no refusal and no audit
# reason, and the ``or`` fallback cannot rescue it because the set is still
# non-empty. So every ambiguous construct is resolved towards leaking.
#
# That rule is what makes this scanner LINE-ORIENTED. An earlier draft used
# multi-line regexes and lost real citations ten different ways: an unmatched
# backtick paired with a span two paragraphs later, a CRLF answer defeated the
# fence closer so ``\Z`` ate the rest of the text, and a 4-space indent rule
# swallowed indented blockquotes, table rows, headings and nested bullets.
# Confining every inline match to its own line makes a stray backtick cost at
# most the line it sits on.
#
# COVERED: ``` fences, ~~~ fences (both including unterminated ones), and
# inline spans. NOT COVERED, deliberately: 4-space/tab indented code blocks.
# A line scanner cannot tell an indented code block from indented prose —
# a blockquote, table row, heading or nested list item — without a real block
# parser, and every misjudgement there DELETES a citation. The shipped corpus
# (``app/worker/sources/*.md``) contains no indented code at all, the system
# prompt never asks for code, and the frontend renders the answer as plain
# text, so the construct is both rare and low-impact. A bracketed index in
# plain prose (``delays[3]`` with no backticks) is likewise still counted;
# nothing short of a parser can distinguish it from a marker.

_FENCE_OPEN_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# An inline code span, confined to ONE line: a backtick run closed by a run of
# the same length. The lookarounds stop a longer run being closed by a shorter
# one. An unmatched lone backtick matches nothing, so it cannot swallow the
# rest of the line, let alone the rest of the answer.
_INLINE_CODE_RE = re.compile(r"(?<!`)(?P<ticks>`+)(?!`)[^\n]*?(?<!`)(?P=ticks)(?!`)")

_WHITESPACE_RE = re.compile(r"\s")


def _fence_run(line: str) -> tuple[str, int] | None:
    """Return ``(fence_char, run_length)`` if ``line`` starts a run of 3+ ticks/tildes.

    CommonMark requires a closing fence to be BARE and AT LEAST AS LONG as its
    opener. Exactly one of those two rules is relaxed here, and the asymmetry
    is deliberate.

    RELAXED -- trailing text. A closer written "``` (that is all)" is not a
    closer under CommonMark, so the fence stays open and swallows every marker
    to the end of the answer. Measured: two real source cards vanished, leaving
    the prose citing ``[2]`` and ``[3]`` with no such cards rendered -- the #215
    defect in the opposite direction, and silent (no refusal, no audit reason).
    Nothing depends on a closer being bare, so allowing trailing text is a pure
    win.

    KEPT -- the length rule, because it is what makes nesting possible. A
    four-backtick fence wrapping a three-backtick one is the standard way to
    show a code block inside a code block, and a shorter run must not close it.
    An earlier version of this fix dropped the length rule too and broke that
    idiom, leaking the inner block's brackets. That also settles a shape this
    function deliberately does NOT rescue: a four-backtick opener "closed" by
    three backticks is structurally identical to nesting, so it stays open and
    its later markers are lost. Unfixable without breaking real nesting.
    """
    body = line.strip()
    if len(body) < 3 or body[0] not in "`~" or body[:3] != body[0] * 3:
        return None
    char = body[0]
    run = len(body) - len(body.lstrip(char))
    return char, run


def _strip_code(text: str) -> str:
    """Blank out markdown code so ``[n]`` inside it is not read as a marker.

    Line-oriented by design (see the note above): an unbalanced backtick can
    never reach past the line it appears on.

    CRLF needs no separate normalisation pass: :func:`_closer` compares the
    STRIPPED line, so the ``\r`` a Windows-style answer leaves before the line
    end does not stop a closing fence from matching. That matters more than it
    sounds — a closer that fails to match leaves the fence open, and an open
    fence runs to the end of the answer, discarding every citation after it.

    A closing fence must use the SAME character and be at least as long as the
    opener, per CommonMark; matching on exact length would leave a ``` fence
    open when the model closes it with ````, eating the rest of the answer.
    """
    lines = text.split("\n")
    count = len(lines)

    # ``reachable[c][i]`` = the longest fence run of ``c`` at any line >= i.
    # One reverse pass makes "is this opener ever closed?" an O(1) lookup
    # instead of a rescan per opener, which would be quadratic on hostile
    # model output (45KB of fence openers: 5.8ms this way).
    reachable = {"`": [0] * (count + 1), "~": [0] * (count + 1)}
    for i in range(count - 1, -1, -1):
        for char in ("`", "~"):
            reachable[char][i] = reachable[char][i + 1]
        found = _fence_run(lines[i])
        if found:
            char, run = found
            reachable[char][i] = max(reachable[char][i], run)

    out: list[str] = []
    open_fence: tuple[str, int] | None = None
    for i, line in enumerate(lines):
        if open_fence is not None:
            found = _fence_run(line)
            if found and found[0] == open_fence[0] and found[1] >= open_fence[1]:
                open_fence = None
            out.append("")
            continue
        match = _FENCE_OPEN_RE.match(line)
        if match:
            fence = match.group("fence")
            info = match.group("info").strip()
            # A bare or one-token info string ("```", "```python") opens a
            # fence even with no closer in sight, on the assumption that this
            # is output truncated by ``llm_max_tokens`` and the tail really is
            # code. That assumption is ASSUMED, not proven, and it is the one
            # place this function knowingly resolves AGAINST the leak-rather-
            # than-lose rule above: a model that emits a stray unbalanced
            # ``` mid-answer loses every marker after it. Measured cost --
            # "Answer [1].\n```\nMore prose citing [2]." yields [1], dropping a
            # real card. Accepted because the alternative (never opening an
            # unterminated fence) leaks the entire tail of every truncated
            # code answer, which is the commoner shape. A multi-word info
            # string is prose that merely starts with backticks, so it only
            # opens a fence when a closer genuinely follows.
            simple = not _WHITESPACE_RE.search(info) and not (fence[0] == "`" and "`" in info)
            if simple or reachable[fence[0]][i + 1] >= len(fence):
                open_fence = (fence[0], len(fence))
                out.append("")
                continue
        # Inline spans collapse to a SPACE, never to the empty string.
        # Deleting outright welds neighbours into a marker never written:
        # ``[`x`99]`` would become ``[99]``, fabricating a citation from
        # untrusted output that the range check then refuses the answer over.
        out.append(_INLINE_CODE_RE.sub(" ", line))
    return "\n".join(out)


def _cited_markers(answer_text: str) -> list[str]:
    """Citation markers in ``answer_text``, ignoring markdown code.

    The ``or`` is load-bearing and must not be refactored away. When an
    answer's ONLY marker sits inside a fence, the stripped text yields
    nothing — and an empty ``cited_indices`` is an unconditional
    ``uncited_answer`` refusal in the orchestrator, plus (on an
    ``exact_lookup`` strategy) a retry that costs a second LLM call. That
    trade is what kept #237 out of the #215 PR. Falling back to the raw scan
    keeps such an answer served exactly as it is today.

    The fallback also makes two properties true by construction, which is what
    lets this ship without adding a refusal path:

    * the result is a SUBSET of the raw scan, so the out-of-range hard-fail
      fires on strictly fewer answers, never more;
    * the result is empty if and only if the raw scan is empty, so no new
      refusal and no new retry can exist.
    """
    return _CITATION_RE.findall(_strip_code(answer_text)) or _CITATION_RE.findall(answer_text)


# Substring of the refusal the model is contractually required to emit
# when there is no evidence. Used so trimmed responses still pass.
_NO_ANSWER_SUBSTRING = "do not have credible source material"


class CitationValidationResult(BaseModel):
    """Outcome of :func:`validate_citations`.

    ``valid`` is True when every ``[n]`` in the answer references a real
    evidence bullet — that is, ``1 <= n <= len(evidence)``. The cited
    indices need NOT be contiguous; ``uncited_indices`` reports the
    bullets the model skipped. When ``answer_text`` is the no-answer
    refusal, ``valid`` is True with empty citation lists regardless of
    how many evidence bullets were passed in.
    """

    valid: bool
    cited_indices: list[int] = Field(default_factory=list[int])
    uncited_indices: list[int] = Field(default_factory=list[int])
    reason: str | None = None


def _is_no_answer_refusal(answer_text: str) -> bool:
    """Detect the no-answer refusal.

    Accepts both the exact constant and any string that contains the
    canonical no-answer substring (case-insensitive). The latter lets
    the LLM trim trailing punctuation without tripping the validator.
    """
    stripped = answer_text.strip()
    if stripped == NO_ANSWER_REFUSAL:
        return True
    return _NO_ANSWER_SUBSTRING in stripped.lower()


def validate_citations(
    *,
    answer_text: str,
    evidence: list[EvidenceHit],
) -> CitationValidationResult:
    """Check that ``answer_text`` references ``evidence`` correctly.

    Pure function — no DB, no network. Safe to call from any context
    with arbitrary evidence.
    """
    evidence_count = len(evidence)

    if _is_no_answer_refusal(answer_text):
        return CitationValidationResult(valid=True)

    raw_markers = _cited_markers(answer_text)
    cited_indices = sorted({int(m) for m in raw_markers})

    # Hard-fail: any out-of-range marker (0 or > evidence_count).
    out_of_range = [n for n in cited_indices if n < 1 or n > evidence_count]
    if out_of_range:
        return CitationValidationResult(
            valid=False,
            cited_indices=cited_indices,
            reason=(
                f"citation index out of range: {out_of_range}; "
                f"evidence has {evidence_count} bullet(s)"
            ),
        )

    # NOTE: there is deliberately no contiguity check here (#215).
    #
    # A gap in the cited set (``[1]`` and ``[3]``, no ``[2]``) used to hard-fail
    # and discard the whole answer. It was wrong on three counts: the model is
    # never asked for contiguity (see :mod:`app.llm.prompts`); it contradicted
    # the branch below, which treats an *uncited* bullet as a warning; and
    # hallucination is already caught by the range check above. In production it
    # turned correct, grounded answers into a refusal the user could not
    # distinguish from "the docs don't cover this".
    #
    # A skipped bullet is exactly an uncited bullet, and is reported as such.

    # Warning only: bullets the model never cited.
    cited_set = set(cited_indices)
    uncited = [n for n in range(1, evidence_count + 1) if n not in cited_set]

    return CitationValidationResult(
        valid=True,
        cited_indices=cited_indices,
        uncited_indices=uncited,
    )
