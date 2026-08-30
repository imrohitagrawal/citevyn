"""Citation validator tests.

The validator is pure (no DB, no network) so these tests build
:class:`EvidenceHit` instances by hand. They pin the contract the
LLM client from Slice 4 already honors:

* ``[n]`` markers are 1-indexed. They are **not** required to be
  contiguous — a gap is valid (#215). The contiguity hard-fail was
  discarding correct, grounded answers that cited ``[1]`` and ``[3]``;
  see ``test_gap_in_citation_indices_is_valid`` below.
* Every ``[n]`` must reference an existing evidence bullet — this range
  check is now the only guard against a hallucinated marker, so both of
  its boundaries (``[N]`` valid, ``[N+1]`` rejected) are pinned.
* Uncited evidence is reported as a warning, not a failure.
* The no-answer refusal short-circuits to ``valid=True`` with empty
  citation lists.
"""

from __future__ import annotations

import uuid

from app.llm.prompts import NO_ANSWER_REFUSAL
from app.llm.validation import _CITATION_RE, _cited_markers, validate_citations
from app.models import RetrievalType
from app.retrieval.types import EvidenceHit


def _evidence(*, count: int) -> list[EvidenceHit]:
    """Build ``count`` minimal evidence bullets."""
    hits: list[EvidenceHit] = []
    for i in range(count):
        hits.append(
            EvidenceHit(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                product_area="claude_api",
                source_name="docs.test",
                document_title="Doc",
                section_path="/x",
                heading="H",
                parent_heading=None,
                chunk_text=f"snippet {i + 1}",
                context_summary="summary",
                source_url="https://docs.test/x",
                score=1.0,
                retrieval_type=RetrievalType.hybrid,
                rank=i + 1,
            )
        )
    return hits


def test_valid_answer_with_one_citation() -> None:
    result = validate_citations(
        answer_text="The rate limit is 50 per minute [1].",
        evidence=_evidence(count=1),
    )
    assert result.valid is True
    assert result.cited_indices == [1]
    assert result.uncited_indices == []
    assert result.reason is None


def test_valid_answer_with_multiple_contiguous_citations() -> None:
    result = validate_citations(
        answer_text=(
            "Claude uses a permissions file [1] configured via the CLI [2] "
            "and supports streaming [3]."
        ),
        evidence=_evidence(count=3),
    )
    assert result.valid is True
    assert result.cited_indices == [1, 2, 3]
    assert result.uncited_indices == []
    assert result.reason is None


def test_invalid_citation_index_out_of_range() -> None:
    result = validate_citations(
        answer_text="Per the docs [5].",
        evidence=_evidence(count=2),
    )
    assert result.valid is False
    assert result.cited_indices == [5]
    assert result.reason is not None and "out of range" in result.reason


def test_invalid_citation_index_zero() -> None:
    """``[0]`` is a malformed marker; the contract requires 1-indexing."""
    result = validate_citations(
        answer_text="Per the docs [0].",
        evidence=_evidence(count=1),
    )
    assert result.valid is False
    assert result.cited_indices == [0]
    assert result.reason is not None and "out of range" in result.reason


def test_gap_in_citation_indices_is_valid() -> None:
    """``[1]`` and ``[3]`` with no ``[2]`` is VALID (#215).

    This assertion is inverted from its original form, which required the
    indices to be contiguous from 1. That rule discarded correct answers in
    production — three of them are in the audit trail, with exactly this
    shape:

        citation_validation_failed: citation indices must be contiguous
        from 1; missing [2]

    and the user saw "I couldn't find a grounded answer", indistinguishable
    from a retrieval failure. The rule was never part of the contract the
    model is given: ``prompts.py`` asks only that each ``[n]`` reference the
    matching evidence bullet, never that the set be gap-free. It also
    contradicted the cite-once design, where the branch immediately below
    treats *uncited* bullets as a warning — citing ``[1]`` alone was always
    fine, but citing ``[1]`` and ``[3]`` threw the answer away.

    Genuine hallucination is still caught by ``out_of_range`` above.
    """
    result = validate_citations(
        answer_text="Per the docs [1] and followup [3].",
        evidence=_evidence(count=3),
    )
    assert result.valid is True
    assert result.cited_indices == [1, 3]
    assert result.reason is None
    # The skipped bullet is reported as uncited, not as a failure.
    assert result.uncited_indices == [2]


def test_last_evidence_bullet_is_in_range() -> None:
    """``[N]`` for N == len(evidence) is valid — the boundary, not past it.

    Paired with the test below. Until #215 the contiguity rule caught an
    off-by-one in the range check as a side effect; with it gone, mutating
    ``n > evidence_count`` to ``n > evidence_count + 1`` passes every other
    test in this file. These two pin the boundary directly.
    """
    result = validate_citations(
        answer_text="Per the docs [3].",
        evidence=_evidence(count=3),
    )
    assert result.valid is True
    assert result.cited_indices == [3]


def test_one_past_the_last_evidence_bullet_is_out_of_range() -> None:
    """``[N+1]`` is a hallucinated marker and must hard-fail."""
    result = validate_citations(
        answer_text="Per the docs [4].",
        evidence=_evidence(count=3),
    )
    assert result.valid is False
    assert result.cited_indices == [4]
    assert result.reason is not None and "out of range" in result.reason


def test_no_answer_refusal_passes_validation() -> None:
    """An exact no-answer refusal is valid with no citations reported,
    even when evidence was passed in (the orchestrator may have already
    decided to refuse)."""
    result = validate_citations(
        answer_text=NO_ANSWER_REFUSAL,
        evidence=_evidence(count=3),
    )
    assert result.valid is True
    assert result.cited_indices == []
    assert result.uncited_indices == []
    assert result.reason is None


def test_no_answer_refusal_with_trailing_punctuation_passes() -> None:
    """The validator accepts any string that contains the canonical
    no-answer substring so the LLM is free to wrap it with quotes or
    trailing punctuation."""
    result = validate_citations(
        answer_text=f'"{NO_ANSWER_REFUSAL}"',
        evidence=_evidence(count=2),
    )
    assert result.valid is True
    assert result.cited_indices == []
    assert result.uncited_indices == []


def test_uncited_evidence_is_reported_but_does_not_fail() -> None:
    """The model may legitimately not cite every retrieved bullet; we
    surface the unused index in ``uncited_indices`` but keep
    ``valid=True`` so the orchestrator can still serve the answer."""
    result = validate_citations(
        answer_text="Per the docs [1].",
        evidence=_evidence(count=3),
    )
    assert result.valid is True
    assert result.cited_indices == [1]
    assert result.uncited_indices == [2, 3]
    assert result.reason is None


def test_repeated_citation_is_deduplicated_in_cited_indices() -> None:
    """Repeated markers (e.g. ``[1] [1]``) collapse to one entry in
    ``cited_indices`` so the orchestrator's set arithmetic stays
    simple."""
    result = validate_citations(
        answer_text="Per the docs [1] and again [1].",
        evidence=_evidence(count=1),
    )
    assert result.valid is True
    assert result.cited_indices == [1]
    assert result.uncited_indices == []


def test_empty_evidence_and_empty_answer_is_valid() -> None:
    """With no evidence and an empty answer, there are no markers to
    validate and no missing-bullet warnings. (The orchestrator
    would still want a no-answer response, but the validator is
    strict about citations only.)"""
    result = validate_citations(answer_text="", evidence=[])
    assert result.valid is True
    assert result.cited_indices == []
    assert result.uncited_indices == []
    assert result.reason is None


def test_marker_with_text_outside_brackets_is_ignored() -> None:
    """Only ``[n]`` markers count. ``[abc]`` or ``[]`` are not
    citations and must not trigger out-of-range failures."""
    result = validate_citations(
        answer_text="Per the docs [1] (see also [v2] note).",
        evidence=_evidence(count=1),
    )
    assert result.valid is True
    assert result.cited_indices == [1]


# --- #237: a ``[n]`` inside markdown code is not a citation marker ----------
#
# ``_CITATION_RE`` scanned the raw answer, so ``arr[2]`` in a code span and
# ``delays[3]`` in a fenced block counted as citations. Three consequences,
# all measured on ``main``: a real-but-uncited source card was attached and
# labelled with that number; ``confidence`` (``len(cited)/len(evidence)``) was
# inflated by a whole band; and when the bracketed index exceeded the evidence
# count the out-of-range branch discarded a correct, grounded answer.
#
# #258 raised the severity. The contiguity rule it removed had been
# ACCIDENTALLY masking the non-contiguous subset of this defect — an answer
# citing only [1] whose code block contains ``delays[3]`` was refused as
# "missing [2]", and is now served with an authoritative card numbered 3 that
# visually matches the subscript the reader can see two lines up.


def test_marker_inside_fenced_block_is_not_a_citation() -> None:
    """The #237 regression, in the exact shape #258 newly exposed.

    Goes red by removing the ``_strip_code`` call from ``_cited_markers``:
    the raw scan yields ``[1, 3]`` and a phantom card numbered 3 is attached.
    """
    result = validate_citations(
        answer_text="Increase the backoff [1].\n\n```python\ndelays[3] = 8\n```\n",
        evidence=_evidence(count=3),
    )
    assert result.valid is True
    assert result.cited_indices == [1]
    assert result.uncited_indices == [2, 3]
    assert result.reason is None


def test_marker_inside_inline_code_span_is_not_a_citation() -> None:
    """``findall("Use `arr[2]` here [1].")`` returned ``['2', '1']`` (#237)."""
    result = validate_citations(
        answer_text="Use `arr[2]` here [1].",
        evidence=_evidence(count=2),
    )
    assert result.valid is True
    assert result.cited_indices == [1]
    assert result.uncited_indices == [2]


def test_marker_inside_tilde_fence_is_not_a_citation() -> None:
    """``~~~`` is a fence too — one of the two forms a naive strip drops.

    Goes red by matching only backtick fences.
    """
    result = validate_citations(
        answer_text="Set the retry count [1].\n\n~~~yaml\nretries[2]: 3\n~~~\n",
        evidence=_evidence(count=2),
    )
    assert result.valid is True
    assert result.cited_indices == [1]


def test_crlf_answer_keeps_the_citation_after_a_fence() -> None:
    """A ``\\r\\n`` answer must not lose everything after its first fence.

    An earlier draft matched the closing fence with ``[ \\t]*$`` against the raw
    line, so the ``\\r`` left the fence unterminated — and an unterminated fence
    runs to the end of the answer, silently discarding ``[2]`` and the source
    card that went with it.

    ``_closer`` compares the STRIPPED line, which is what makes this pass; goes
    red by comparing the raw line instead, or by removing the strip from the
    scanner entirely.
    """
    result = validate_citations(
        answer_text="A [1].\r\n```\r\nx[7]\r\n```\r\nB [2].\r\n",
        evidence=_evidence(count=2),
    )
    assert result.valid is True
    assert result.cited_indices == [1, 2]


def test_unmatched_backtick_cannot_reach_a_later_paragraph() -> None:
    """A stray backtick must cost at most its own line.

    In an earlier draft the lone backtick in the first paragraph paired with
    the OPENING backtick of a span two paragraphs later and ate the ``[1]``
    between them.

    Two independent things now prevent that, and the mutation has to undo BOTH
    to get past this test: the substitution runs per LINE, and the pattern's
    body is ``[^\\n]*?``. Verified — changing either one alone leaves this
    green; applying the substitution to the whole joined text AND widening the
    body to match newlines turns it red.
    """
    result = validate_citations(
        answer_text="First `point [1].\n\nSecond paragraph mentions `code` [2].",
        evidence=_evidence(count=2),
    )
    assert result.valid is True
    assert result.cited_indices == [1, 2]


def test_closing_fence_longer_than_its_opener_still_closes() -> None:
    """CommonMark lets the closer be LONGER than the opener.

    A closer matched by exact length leaves the fence open, so everything
    after it is discarded — losing ``[2]`` — while ``[7]`` leaks out of the
    block and can push the answer out of range into a refusal.
    """
    result = validate_citations(
        answer_text="A [1].\n\n```\nx[7]\n````\n\nB [2].\n",
        evidence=_evidence(count=2),
    )
    assert result.valid is True
    assert result.cited_indices == [1, 2]


def test_indented_prose_constructs_keep_their_citations() -> None:
    """Indented prose is NOT a code block, and must never lose a citation.

    This pins the reason 4-space indented code is deliberately out of scope.
    A line scanner cannot tell an indented code block from indented prose,
    and each of the shapes below was silently stripped by a draft that tried:
    a blockquote, a table row, an ATX heading, a nested bullet whose parent
    marker was not recognised, and an answer that simply opens indented.

    Every one of them DELETED a real source card — the failure direction that
    has no visible symptom, unlike the phantom this fix removes. Leaking an
    indented code block is the accepted cost of never doing that.
    """
    cases = [
        ("blockquote", "A [1].\n\n    > quoted [2]\n\nC [3].", [1, 2, 3]),
        ("table row", "A [1].\n\n    | [2] | x |\n\nC [3].", [1, 2, 3]),
        ("heading", "A [1].\n\n    ## Heading [2]\n\nC [3].", [1, 2, 3]),
        ("nested bullet", "A [1].\n\n    - sub point [2]\n", [1, 2]),
        ("opens indented", "    lead [1]\n\nB [2].\n", [1, 2]),
    ]
    for label, answer, expected in cases:
        result = validate_citations(answer_text=answer, evidence=_evidence(count=3))
        assert result.valid is True, label
        assert result.cited_indices == expected, label


def test_only_citation_inside_a_fence_is_still_counted() -> None:
    """The ``or`` fallback — the reason #237 was NOT fixed alongside #215.

    Stripping code empties this answer's marker set, and an empty
    ``cited_indices`` is an unconditional ``uncited_answer`` refusal in the
    orchestrator PLUS an ``exact_lookup`` retry: a second, PAID LLM call. The
    fallback to the raw scan keeps the answer served exactly as today.

    Not red against the unfixed code — this is a mutation killer, not a
    regression test. It goes red by deleting ``or _CITATION_RE.findall(...)``
    from ``_cited_markers``, and it is the only test in this file that does.
    """
    result = validate_citations(
        answer_text="As shown below:\n\n```\nrefer to [1]\n```\n",
        evidence=_evidence(count=1),
    )
    assert result.valid is True
    assert result.cited_indices == [1]
    assert result.uncited_indices == []


def test_code_only_answer_keeps_its_out_of_range_hard_fail() -> None:
    """The ``or`` fallback covers the INVALID direction too.

    An answer whose only bracketed number is an out-of-range one inside code
    still hard-fails, because the fallback restores the raw scan. This pins a
    deliberate cost of the ``or``: for a code-only answer the false hard-fail
    remains. Serving it instead would mean serving an answer that cited
    nothing, which #174 forbids. Partner to the test above — that one covers
    the valid direction, this one the refusal direction.
    """
    result = validate_citations(
        answer_text="```\narr[9]\n```\n",
        evidence=_evidence(count=1),
    )
    assert result.valid is False
    assert result.cited_indices == [9]
    assert result.reason is not None and "out of range" in result.reason


def test_code_marker_above_evidence_count_no_longer_hard_fails() -> None:
    """``arr[9]`` in a code span used to DISCARD a correctly cited answer.

    The range check is computed from the cited set, so a bracketed index
    inside code that exceeded ``len(evidence)`` turned a grounded, correctly
    cited answer into a ``citation_validation_failed`` refusal — the #215
    failure mode through a different door.
    """
    result = validate_citations(
        answer_text="Read the tenth slot with `arr[9]` after retrying [1].",
        evidence=_evidence(count=1),
    )
    assert result.valid is True
    assert result.cited_indices == [1]
    assert result.reason is None


def test_hallucinated_marker_in_prose_still_hard_fails_alongside_code() -> None:
    """Stripping code must not blunt the range check on PROSE markers.

    Without this, ``_strip_code`` could be mutated to strip the whole answer
    and the suite would stay green while every hallucinated marker sailed
    through. Partner to the test above: that one proves a code marker is
    forgiven, this one proves a prose marker is still caught.
    """
    result = validate_citations(
        answer_text="Use `arr[2]` and see [7].",
        evidence=_evidence(count=3),
    )
    assert result.valid is False
    assert result.cited_indices == [7]
    assert result.reason is not None and "out of range" in result.reason


def test_two_fences_keep_the_citation_written_between_them() -> None:
    """A GREEDY fence regex swallows the prose between two blocks.

    This is the mutation most likely to ship undetected: every other test in
    this file passes against a greedy fence pattern under ``re.DOTALL``. It
    fails silently in the dangerous direction — real citations are DELETED, a
    source card disappears and confidence drops, with no refusal and no audit
    reason. The ``or`` fallback cannot rescue it because the set is still
    non-empty.
    """
    result = validate_citations(
        answer_text="First [1].\n\n```\nx\n```\n\nSecond [2].\n\n```\ny\n```\n\nThird [3].",
        evidence=_evidence(count=3),
    )
    assert result.valid is True
    assert result.cited_indices == [1, 2, 3]
    assert result.uncited_indices == []


def test_two_inline_spans_keep_the_citation_written_between_them() -> None:
    """The inline-span twin of the greedy-fence case above.

    Goes red against a greedy backtick-span regex, which eats ``[2]``.
    """
    result = validate_citations(
        answer_text="Set `a[7]` then cite [2] and set `b[8]` and cite [1].",
        evidence=_evidence(count=2),
    )
    assert result.valid is True
    assert result.cited_indices == [1, 2]
    assert result.uncited_indices == []


def test_list_continuation_lines_keep_their_citations() -> None:
    """An indented list continuation is prose, not a code block.

    A naive ``^ {4}`` strip eats these, deleting a REAL source card. That is
    strictly worse than the phantom being fixed: the ``or`` fallback rescues
    only an answer whose markers vanish ENTIRELY, so a PARTIAL loss is
    silent. Goes red by dropping the list-awareness from
    ``_drop_indented_code``.
    """
    result = validate_citations(
        answer_text="Two points [1]:\n\n- top level\n    - nested point [2]\n",
        evidence=_evidence(count=2),
    )
    assert result.valid is True
    assert result.cited_indices == [1, 2]
    assert result.uncited_indices == []


def test_stripping_code_never_fabricates_a_marker() -> None:
    """Inline spans collapse to a SPACE, never to the empty string.

    Deleting a span outright welds its neighbours together, so ``[`x`2]``
    becomes ``[2]`` — a citation the model never wrote, manufactured from
    untrusted output, which the range check then turns into a refusal. The
    index is 2 with ONE evidence bullet precisely so the fabrication is
    visible: a fabricated ``[1]`` would be indistinguishable from the real
    one.
    """
    result = validate_citations(
        answer_text="See [`x`2] and the guide [1].",
        evidence=_evidence(count=1),
    )
    assert result.valid is True
    assert result.cited_indices == [1]
    assert result.reason is None


def test_unmatched_backtick_does_not_swallow_the_rest_of_the_answer() -> None:
    """A lone backtick opens no span, so the trailing real marker survives."""
    result = validate_citations(
        answer_text="Use `arr to configure retries [1].",
        evidence=_evidence(count=1),
    )
    assert result.valid is True
    assert result.cited_indices == [1]


def test_unterminated_fence_runs_to_the_end_of_the_answer() -> None:
    """Per CommonMark an unclosed fence extends to end of document.

    Truncation by ``llm_max_tokens`` makes this a real shape rather than a
    theoretical one — the closing fence is exactly what gets cut off.
    """
    result = validate_citations(
        answer_text="Start here [1].\n\n```python\nretries[9] = 1\n",
        evidence=_evidence(count=1),
    )
    assert result.valid is True
    assert result.cited_indices == [1]


def test_code_stripping_can_only_refuse_less_never_more() -> None:
    """Property: the fix cannot add a refusal that does not exist today.

    This discharges — mechanically, rather than by argument — the exact
    objection that deferred #237 from the #215 PR. Over every 1- and
    2-fragment answer built from the shapes below:

    * the cited set is a SUBSET of the raw scan, so the out-of-range
      hard-fail fires on strictly fewer answers, never more;
    * the set is empty IFF the raw scan is empty, so no new
      ``uncited_answer`` refusal and no new paid ``exact_lookup`` retry can
      exist.

    The ``changed`` counter is the partner that stops this from passing
    vacuously: it proves the corpus actually contains answers the strip
    alters, so the invariants are asserted over live code paths.
    """
    fragments = [
        "prose about retries",
        "cite [1].",
        "cite [2].",
        "bad [0].",
        "big [9].",
        "inline `arr[2]` span",
        "```\nx[3] = 1\n```",
        "~~~\nz[7] = 1\n~~~",
        "    indented[8] = 1",
        "- item [2]\n    cont [3]",
        "```\nonly [1] here\n```",
    ]
    answers = list(fragments)
    answers += [f"{a}\n\n{b}" for a in fragments for b in fragments]

    changed = 0
    for text in answers:
        raw = sorted({int(m) for m in _CITATION_RE.findall(text)})
        cited = sorted({int(m) for m in _cited_markers(text)})
        assert set(cited) <= set(raw), text
        assert bool(cited) == bool(raw), text
        if cited != raw:
            changed += 1
        for count in (1, 2, 3, 4):
            fails_now = any(n < 1 or n > count for n in raw)
            fails_after = any(n < 1 or n > count for n in cited)
            assert fails_after <= fails_now, (text, count)

    assert changed > 0


def test_prose_line_starting_with_backticks_only_opens_a_fence_if_one_closes() -> None:
    """A multi-word info string opens a fence ONLY when a closer follows.

    ``_strip_code`` decides this with the ``reachable`` reverse pass: a bare or
    one-token opener (``` or ```python) is truncated output and opens
    unconditionally, but a line whose "info string" is a SENTENCE is prose that
    merely begins with backticks, so it opens a fence only if a real closing
    fence appears later.

    Both directions are pinned here because the whole reverse pass is otherwise
    dead weight: with ``reachable`` deleted (``if simple:``) the entire suite
    stayed green at 138 passed, so nothing tested the largest block of this
    change. The first case goes red without it (``[1]`` leaks out of a genuine
    fence); the second goes red if the opener is allowed to swallow the rest of
    the answer, which would DELETE two real citations.
    """
    closed = validate_citations(
        answer_text="```prose with spaces here\ntext [1]\n```\n\nMore [2].",
        evidence=_evidence(count=2),
    )
    assert closed.valid is True
    assert closed.cited_indices == [2]

    # A real citation sits BEFORE the stray opener on purpose. Without it the
    # strip empties the set, the ``or`` fallback restores the raw scan, and the
    # assertion passes even when the opener wrongly swallowed everything --
    # verified: an ``if True:`` mutant survives the version without ``[1]``.
    never_closed = validate_citations(
        answer_text="Answer [1].\n```see the docs for [2]\nmore [3]",
        evidence=_evidence(count=3),
    )
    assert never_closed.valid is True
    assert never_closed.cited_indices == [1, 2, 3]


def test_malformed_closing_fences_still_close_rather_than_eat_the_answer() -> None:
    """A sloppy closer must not swallow every citation after it.

    CommonMark says a closing fence must be bare and at least as long as its
    opener. Applied strictly, both rules turn a malformed fence into silent
    citation LOSS -- the answer keeps saying "[2]" and "[3]" while no such
    cards are rendered, which is the #215 defect in the opposite direction and
    the exact failure this module's docstring promises to resolve against.

    Measured before ``_fence_run`` was made forgiving: both shapes returned
    ``[1]``, dropping two real source cards and deflating confidence a band.
    Goes red by requiring a bare closer (``set(body) == {body[0]}``) or by
    requiring the closer to be at least as long as the opener.
    """
    trailing_text = validate_citations(
        answer_text="Limit is 50/min [1].\n```\nx = 1\n``` (that is all)\nBackoff [2].\nSSE [3].\n",
        evidence=_evidence(count=3),
    )
    assert trailing_text.valid is True
    assert trailing_text.cited_indices == [1, 2, 3]

    shorter_closer = validate_citations(
        answer_text="Limit [1].\n````\nx = 1\n```\nBackoff [2].\nSSE [3].\n",
        evidence=_evidence(count=3),
    )
    assert shorter_closer.valid is True
    assert shorter_closer.cited_indices == [1, 2, 3]


def test_a_backtick_pair_on_one_line_is_a_real_code_span() -> None:
    """Two backticks on a line DO delimit a span, and that is correct.

    The module comment used to claim "an unmatched lone backtick matches
    nothing, so it cannot swallow the rest of the line". True of a LONE tick,
    and false of a pair: ``Use ` to quote a value like [2] in the shell ` here``
    is a genuine CommonMark code span, and every markdown renderer agrees, so
    dropping ``[2]`` is right rather than a bug.

    Pinned because it looks like the loss cases above and is not one; the
    partner assertion is that a citation OUTSIDE the span survives, proving the
    span is bounded rather than eating the line.
    """
    result = validate_citations(
        answer_text="Use ` to quote a value like [2] in the shell ` here. See [1].",
        evidence=_evidence(count=2),
    )
    assert result.valid is True
    assert result.cited_indices == [1]
