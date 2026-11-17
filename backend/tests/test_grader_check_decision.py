"""The grader check's probe, decision rule, paired table and start refusals
(#598 Step 3). Pure functions; no client, no database.

Written before the implementation (test designer). The contract, in a new
module ``tests/eval/grader_check.py``:

* ``PROBE_CASE_ID = "codex_unprompted_evidence_phrase"``: the probe's question and
  expected gist are that golden case's. It is not a golden case and enters no
  answer-quality figure.
* ``CORRECT_ANSWER``: exactly "The Codex documentation here does not list which
  models Codex supports. It says the --model flag chooses the model Codex uses,
  and `codex --help` lists every flag." ``WRONG_ANSWER``: the same text except
  its first sentence, which claims `codex --help` lists the models Codex
  supports. Neither holds any of the case's ``must_not_contain`` words.
* ``probe_passages() -> list[dict]``: the hermetic corpus fixture's Codex chunks
  (``tests/conftest.py`` ``corpus_fixture_specs()``, ``source_name == "codex"``) as
  evidence dicts (``source``, ``heading``, ``rank`` from 1, ``text``), fixed, so
  the probe does not depend on any case's retrieval.
* ``decide(cells) -> {"decision", "reasons", "medians"}``. ``cells`` is
  ``{"wrong"|"correct": {"blind"|"passages": [s1, s2, s3]}}``, each ``s`` a panel
  score (int) or ``None``. A cell's median is the median of its three repeats,
  and is missing (``None``) unless all three repeats scored. In this order:
  ``"drop"`` if the wrong answer's passage median is above 2, or above its blind
  median, or the correct answer's passage median is below 4 (each only when the
  medians it reads are present); otherwise ``"inconclusive"`` if any of the four
  medians is missing or any cell's repeats differ by 2 or more; otherwise
  ``"keep"``. ``reasons`` is a non-empty list of plain sentences for drop and
  inconclusive.
* ``paired_table(judged, cases) -> dict`` for one pass: ``rows`` (``case_id``,
  ``blind``, ``passages``, ``delta``, ``status`` in raised / lowered / same /
  missing, and ``reason`` for a missing row), ``counts`` (raised, lowered, same,
  missing), ``split`` (``source_in_passages`` and ``source_not_in_passages``, each
  such counts; the source is the case's ``expected_source`` and the passages are
  the ones it was graded with: its evidence, or its cache origin's),
  ``coverage`` (pairs with both scores / cases with a blind score; ``None`` when
  no case has a blind score) and ``coverage_label``: ``"not available"`` when
  coverage is ``None``, ``"too thin to read"`` below 0.9, else ``None``. Only
  cases with a blind score are rows.
* ``report_lines(summary) -> list[str]``: the lines printed for the job summary:
  a ``GRADER CHECK`` line with the decision and its reasons, a ``GRADER PROBE``
  table, each pass's counts, and every case at or below 2 on either score with
  BOTH rationales.
* ``start_refusal(*, ref, now) -> str | None`` and
  ``MIN_MINUTES_BEFORE_UTC_MIDNIGHT = 250``: why a grader-check run must not
  start: on ``refs/heads/main`` or a ``refs/tags/v*`` ref, or when fewer than
  250 minutes remain before 00:00 UTC (the spend cap's window is the UTC day).
  ``None`` means it may start.

Each test names the change that turns it red.
"""

from __future__ import annotations

import dataclasses
import importlib
import statistics
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import corpus_fixture_specs
from tests.eval.cases import load_cases
from tests.eval.paths import GOLDEN_PATH
from tests.eval.runner import JudgedCase

CORRECT = (
    "The Codex documentation here does not list which models Codex supports. It says "
    "the --model flag chooses the model Codex uses, and `codex --help` lists every flag."
)


def _gc() -> ModuleType:
    """The module under test. It does not exist until the builder writes it."""
    return importlib.import_module("tests.eval.grader_check")


def _cells(
    wrong_blind: list[int | None],
    wrong_passages: list[int | None],
    correct_blind: list[int | None],
    correct_passages: list[int | None],
) -> dict[str, dict[str, list[int | None]]]:
    return {
        "wrong": {"blind": wrong_blind, "passages": wrong_passages},
        "correct": {"blind": correct_blind, "passages": correct_passages},
    }


LOW, HIGH = [1, 1, 1], [5, 5, 5]
MISSING: list[int | None] = [None, None, None]


# --- the probe's fixed inputs -------------------------------------------------------


def test_the_probe_uses_the_codex_models_question_and_its_gist() -> None:
    """Red when: the probe asks another question than the golden case it is
    modelled on, so its scores say nothing about that known wrong claim."""
    gc = _gc()
    case = {c.id: c for c in load_cases(GOLDEN_PATH)}[gc.PROBE_CASE_ID]
    assert gc.PROBE_CASE_ID == "codex_unprompted_evidence_phrase"
    assert case.question == "What models does Codex support?"


def test_the_correct_answer_is_the_written_one_and_has_no_forbidden_word() -> None:
    """Red when: the correct canned answer is the case's gist (which holds the
    case's forbidden words and an instruction to the answerer) or any other text."""
    gc = _gc()
    case = {c.id: c for c in load_cases(GOLDEN_PATH)}[gc.PROBE_CASE_ID]
    assert gc.CORRECT_ANSWER == CORRECT
    assert case.must_not_contain  # partner: there are forbidden words to check against
    for answer in (gc.CORRECT_ANSWER, gc.WRONG_ANSWER):
        lowered = answer.lower()
        assert not [w for w in case.must_not_contain if w.lower() in lowered], answer


def test_the_wrong_answer_differs_from_the_correct_one_only_in_the_claim() -> None:
    """Red when: the wrong answer does not make the known wrong claim (that
    `codex --help` lists the models), or differs from the correct one in more than
    that first sentence (the probe would then measure something else too)."""
    gc = _gc()
    wrong_first, wrong_rest = gc.WRONG_ANSWER.split(". ", 1)
    correct_first, correct_rest = gc.CORRECT_ANSWER.split(". ", 1)
    assert wrong_rest == correct_rest
    assert wrong_first != correct_first
    assert "codex --help" in wrong_first and "models" in wrong_first
    assert "does not" not in wrong_first


def test_the_probe_passages_are_the_fixtures_codex_chunks() -> None:
    """Red when: the probe's passages come from a case's retrieval (they would
    change run to run) or from anything but the fixture's Codex chunks."""
    expected = [s for s in corpus_fixture_specs() if s["source_name"] == "codex"]
    assert expected  # partner: the fixture has Codex chunks to compare with
    passages = _gc().probe_passages()
    assert [(p["source"], p["heading"], p["text"]) for p in passages] == [
        (s["source_name"], s["chunk_heading"], s["chunk_text"]) for s in expected
    ]
    assert [p["rank"] for p in passages] == list(range(1, len(passages) + 1))


# --- the decision rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("cells", "decision"),
    [
        # Every cell scored, the grader tells wrong from correct with passages.
        (_cells(LOW, LOW, HIGH, HIGH), "keep"),
        # Boundaries that still keep: wrong at 2 (not above 2, not above blind 2),
        # correct at exactly 4, repeats one point apart.
        (_cells([2, 2, 2], [2, 2, 2], HIGH, [4, 4, 4]), "keep"),
        (_cells([1, 2, 1], [1, 1, 2], [5, 4, 5], [4, 5, 4]), "keep"),
        # Drop: passages raise the wrong answer above 2.
        (_cells(LOW, [3, 3, 3], HIGH, HIGH), "drop"),
        # Drop: passages raise the wrong answer above its blind median (still <= 2).
        (_cells(LOW, [2, 2, 2], HIGH, HIGH), "drop"),
        # Drop: passages score the correct answer below 4.
        (_cells(LOW, LOW, HIGH, [3, 3, 3]), "drop"),
        # Inconclusive: a whole cell missing.
        (_cells(LOW, LOW, MISSING, HIGH), "inconclusive"),
        # Inconclusive: one repeat of a cell missing (the median needs all three).
        (_cells(LOW, [1, None, 1], HIGH, HIGH), "inconclusive"),
        # Inconclusive: repeats of a cell disagree by 2 or more.
        (_cells([1, 1, 3], LOW, HIGH, HIGH), "inconclusive"),
        (_cells(LOW, LOW, HIGH, [5, 5, 3]), "inconclusive"),
        # Precedence: an observed drop beats a missing score elsewhere ...
        (_cells(LOW, [4, 4, 4], MISSING, HIGH), "drop"),
        (_cells(MISSING, LOW, HIGH, [2, 2, 2]), "drop"),
        # ... and beats repeats that disagree.
        (_cells(LOW, [3, 3, 5], HIGH, HIGH), "drop"),
    ],
    ids=[
        "keep",
        "keep-at-boundaries",
        "keep-one-point-apart",
        "drop-wrong-above-2",
        "drop-wrong-above-blind",
        "drop-correct-below-4",
        "inconclusive-missing-cell",
        "inconclusive-missing-repeat",
        "inconclusive-disagreement",
        "inconclusive-disagreement-correct",
        "drop-beats-missing",
        "drop-beats-missing-blind",
        "drop-beats-disagreement",
    ],
)
def test_the_decision_rule(cells: dict[str, Any], decision: str) -> None:
    """Red when: any branch of the rule changes: a threshold (2, 4), the "above
    its blind median" comparison, the median over three repeats, the disagreement
    rule (2 points), the missing-score rule, or the precedence (drop first)."""
    result = _gc().decide(cells)
    assert result["decision"] == decision, result
    if decision != "keep":
        assert result["reasons"] and all(isinstance(r, str) and r for r in result["reasons"])


def test_the_decision_reports_the_four_medians() -> None:
    """Red when: the decision is reported without the medians it was read from, or
    a cell with a missing repeat gets a median from the two that scored."""
    result = _gc().decide(_cells([1, 2, 3], [1, None, 1], HIGH, [4, 5, 5]))
    medians = result["medians"]
    assert medians["wrong"]["blind"] == statistics.median([1, 2, 3])
    assert medians["wrong"]["passages"] is None
    assert medians["correct"] == {"blind": 5, "passages": 5}


# --- the paired table -------------------------------------------------------------------


def _case(
    case_id: str,
    blind: int | None,
    passages: int | None,
    *,
    evidence_source: str | None = "claude_api",
    passage_error: str | None = None,
) -> JudgedCase:
    case = JudgedCase(case_id=case_id, kind="literal", answer="a", no_answer=False, score=blind)
    case.evidence = (
        [{"source": evidence_source, "heading": "h", "rank": 1, "text": "t"}]
        if evidence_source
        else []
    )
    case.passage_score = passages  # type: ignore[attr-defined]
    case.passage_error = passage_error  # type: ignore[attr-defined]
    return case


def _eval_cases(ids: list[str]) -> list[Any]:
    golden = {c.id: c for c in load_cases(GOLDEN_PATH)}
    return [golden[i] for i in ids]


# Real golden ids whose ``expected_source`` is claude_api.
_IDS = ["claude_api_lit_ratelimit", "adv_misspell_ratelimit"]


def test_the_paired_table_counts_raised_lowered_same_and_missing() -> None:
    """Red when: a case without a passage score is counted as "same" (a missing
    score must never read as agreement), or a delta has the wrong sign."""
    assert "passage_score" in {f.name for f in dataclasses.fields(JudgedCase)}
    cases = _eval_cases(_IDS)
    judged = [
        _case(_IDS[0], 3, 5),
        _case(_IDS[1], 4, None, passage_error="judge_parse: no JSON"),
    ]
    table = _gc().paired_table(judged, cases)
    rows = {r["case_id"]: r for r in table["rows"]}
    assert rows[_IDS[0]]["status"] == "raised" and rows[_IDS[0]]["delta"] == 2
    assert rows[_IDS[1]]["status"] == "missing"
    assert "judge_parse" in rows[_IDS[1]]["reason"]
    assert table["counts"] == {"raised": 1, "lowered": 0, "same": 0, "missing": 1}


def test_the_paired_table_is_split_by_whether_the_expected_source_was_a_passage() -> None:
    """Red when: the table does not tell cases whose expected source was among
    their passages from cases graded without it (a lowered score means different
    things in each)."""
    assert "passage_score" in {f.name for f in dataclasses.fields(JudgedCase)}
    cases = _eval_cases(_IDS)
    judged = [
        _case(_IDS[0], 5, 3),  # lowered, its expected source was a passage
        _case(_IDS[1], 4, 4, evidence_source="codex"),  # same, its source was not
    ]
    split = _gc().paired_table(judged, cases)["split"]
    assert split["source_in_passages"] == {"raised": 0, "lowered": 1, "same": 0, "missing": 0}
    assert split["source_not_in_passages"] == {"raised": 0, "lowered": 0, "same": 1, "missing": 0}


@pytest.mark.parametrize(
    ("judged", "coverage", "label"),
    [
        ([], None, "not available"),
        ([("a", 4, 4), ("b", 4, 4)], 1.0, None),
        ([("a", 4, 4), ("b", 4, None)], 0.5, "too thin to read"),
    ],
    ids=["no-graded-case", "full", "thin"],
)
def test_passage_coverage_and_its_label(
    judged: list[tuple[str, int, int | None]], coverage: float | None, label: str | None
) -> None:
    """Red when: coverage with no graded case is computed as 0/0 (or 0) instead of
    "not available", or a table with under 90% of pairs is not labelled "too thin
    to read"."""
    ids = {"a": _IDS[0], "b": _IDS[1]}
    rows = [_case(ids[k], b, p) for k, b, p in judged]
    table = _gc().paired_table(rows, _eval_cases(_IDS))
    assert table["coverage"] == coverage
    assert table["coverage_label"] == label
    assert len(table["rows"]) == len(judged)  # partner: every graded case is a row


# --- the printed lines ----------------------------------------------------------------


def test_report_lines_show_the_decision_the_probe_and_every_low_case_with_both_rationales() -> None:
    """Red when: the job summary lacks the decision and its reasons, the probe
    table, or a case at or below 2 on either score with both rationales."""
    gc = _gc()
    decision = gc.decide(_cells(LOW, [3, 3, 3], HIGH, HIGH))
    low_case = {
        "case_id": "claude_api_lit_ratelimit",
        "score": 4,
        "rationale": "matches the gist",
        "passage_score": 2,
        "passage_rationale": "not supported by passage 1",
    }
    fine_case = {
        "case_id": "codex_lit_modelflag",
        "score": 5,
        "rationale": "fine",
        "passage_score": 5,
        "passage_rationale": "fine too",
    }
    summary = {
        "judge": {"cases": [low_case, fine_case]},
        "grader_check": {
            "probe": {"cells": _cells(LOW, [3, 3, 3], HIGH, HIGH)},
            "decision": decision["decision"],
            "reasons": decision["reasons"],
        },
    }
    lines = gc.report_lines(summary)
    text = "\n".join(lines)
    assert any("GRADER CHECK" in ln and "drop" in ln for ln in lines), text
    assert any("GRADER PROBE" in ln for ln in lines), text
    assert decision["reasons"][0] in text
    low = [ln for ln in lines if "claude_api_lit_ratelimit" in ln]
    assert low and any(
        "matches the gist" in ln and "not supported by passage 1" in ln for ln in low
    ), text
    assert not any("codex_lit_modelflag" in ln for ln in lines), text


# --- refusals before any paid call -------------------------------------------------------


_SAFE = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)  # 720 minutes before midnight


@pytest.mark.parametrize(
    "ref", ["refs/heads/main", "refs/tags/v0.12.0", "refs/tags/v1"], ids=["main", "tag", "tag-1"]
)
def test_a_grader_check_on_main_or_a_release_tag_is_refused(ref: str) -> None:
    """Red when: the experiment can run on main or a release tag, where it would
    pay for a second grading on every release."""
    reason = _gc().start_refusal(ref=ref, now=_SAFE)
    assert reason and isinstance(reason, str)


@pytest.mark.parametrize(
    "ref", ["refs/heads/eval/598-grader-check", None, "refs/heads/mainline"], ids=str
)
def test_a_grader_check_on_a_branch_may_start(ref: str | None) -> None:
    """Partner. Red when: the ref guard refuses a branch run (the only place the
    check is meant to run), a local run with no ref, or matches by prefix."""
    assert _gc().start_refusal(ref=ref, now=_SAFE) is None


@pytest.mark.parametrize(
    ("minutes_left", "refused"),
    [(249, True), (250, False), (720, False), (1, True)],
)
def test_a_grader_check_too_close_to_utc_midnight_is_refused(
    minutes_left: int, refused: bool
) -> None:
    """Red when: a run can start with fewer than 250 minutes before 00:00 UTC (the
    daily spend window would reset mid-run, so the cap would stop counting), or
    the boundary moves."""
    gc = _gc()
    assert gc.MIN_MINUTES_BEFORE_UTC_MIDNIGHT == 250
    midnight = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    now = midnight - timedelta(minutes=minutes_left)
    reason = gc.start_refusal(ref="refs/heads/eval/598-grader-check", now=now)
    assert (reason is not None) is refused, reason
    if refused:
        assert "UTC" in reason
