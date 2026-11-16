"""The grader check (#598 Step 3): does grading WITH the passages help?

Behind ``Settings.eval_passage_grading`` (``CITEVYN_EVAL_PASSAGE_GRADING``), off by
default. With it on, the judged run grades every answer a second time with the
passages it was made from, and grades a fixed probe before the first case. All of
it is reported only: the mean and every gate use the blind grade.

The probe grades two canned answers to the question of the golden case
``codex_unprompted_evidence_phrase``: WRONG (it says ``codex --help`` lists the
models) and CORRECT. Each is graded blind and with the fixture's Codex passages,
three times each. Decision, in this order (:func:`decide`):

* ``drop`` if the wrong answer's passage median is above 2, or above its blind
  median, or the correct answer's passage median is below 4;
* otherwise ``inconclusive`` if any of the four medians is missing, or the three
  repeats of a cell differ by 2 or more points;
* otherwise ``keep``. Keep only makes passage grading a candidate for later
  steps; it changes no gate.

This module holds the pure parts (the probe's inputs, the decision, the paired
table, the printed lines and the start refusals). The runner calls them.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from app.llm.errors import CostLimitReached
from tests.conftest import corpus_fixture_specs

PROBE_CASE_ID = "codex_unprompted_evidence_phrase"
CORRECT_ANSWER = (
    "The Codex documentation here does not list which models Codex supports. It says "
    "the --model flag chooses the model Codex uses, and `codex --help` lists every flag."
)
# The same text, except that the first sentence makes the known wrong claim.
WRONG_ANSWER = (
    "The Codex documentation here says `codex --help` lists the models Codex supports. "
    + CORRECT_ANSWER.split(". ", 1)[1]
)
ANSWERS = {"wrong": WRONG_ANSWER, "correct": CORRECT_ANSWER}
MODES = ("blind", "passages")
REPEATS = 3
# Wrong answer: its passage median must not be above this.
WRONG_MAX = 2
# Correct answer: its passage median must not be below this.
CORRECT_MIN = 4
# Repeats of one cell this far apart (or more) make the probe inconclusive.
DISAGREEMENT = 2
# Below this share of pairs, the paired table is too thin to read.
MIN_COVERAGE = 0.9
# A grader-check run needs this much of the UTC day left: the daily spend cap
# counts today's spend only, so a run crossing 00:00 UTC would reset it mid-run.
MIN_MINUTES_BEFORE_UTC_MIDNIGHT = 250

STOPPED_BY_CAP = "stopped by the spend cap"
STOPPED_BY_METER = "stopped: the spend meter could not be read"


def utc_now() -> datetime:
    """The current time in UTC (a function, so a test can fix it)."""
    return datetime.now(UTC)


def probe_passages() -> list[dict[str, Any]]:
    """The hermetic corpus fixture's Codex chunks, as evidence entries in fixture
    order. Fixed, so the probe never depends on any case's retrieval."""
    specs = [s for s in corpus_fixture_specs() if s["source_name"] == "codex"]
    return [
        {
            "source": s["source_name"],
            "heading": s["chunk_heading"],
            "rank": n,
            "text": s["chunk_text"],
        }
        for n, s in enumerate(specs, start=1)
    ]


def empty_cells() -> dict[str, dict[str, list[int | None]]]:
    return {answer: {mode: [] for mode in MODES} for answer in ANSWERS}


def _median(scores: list[int | None]) -> float | int | None:
    """The median of a cell's repeats, or ``None`` unless every repeat scored."""
    if len(scores) != REPEATS or any(s is None for s in scores):
        return None
    return statistics.median([s for s in scores if s is not None])


def decide(cells: dict[str, dict[str, list[int | None]]]) -> dict[str, Any]:
    """The probe's decision (``keep``, ``drop`` or ``inconclusive``) with its
    reasons and the four medians it was read from. See the module docstring."""
    medians = {
        answer: {mode: _median(list(cells.get(answer, {}).get(mode, []))) for mode in MODES}
        for answer in ANSWERS
    }
    wrong_blind, wrong_passages = medians["wrong"]["blind"], medians["wrong"]["passages"]
    correct_passages = medians["correct"]["passages"]
    drops: list[str] = []
    if wrong_passages is not None and wrong_passages > WRONG_MAX:
        drops.append(f"the wrong answer's passage median {wrong_passages} is above {WRONG_MAX}")
    if wrong_passages is not None and wrong_blind is not None and wrong_passages > wrong_blind:
        drops.append(
            f"the wrong answer's passage median {wrong_passages} is above its blind "
            f"median {wrong_blind}"
        )
    if correct_passages is not None and correct_passages < CORRECT_MIN:
        drops.append(
            f"the correct answer's passage median {correct_passages} is below {CORRECT_MIN}"
        )
    if drops:
        return {"decision": "drop", "reasons": drops, "medians": medians}
    unsure: list[str] = []
    for answer in ANSWERS:
        for mode in MODES:
            scores = list(cells.get(answer, {}).get(mode, []))
            if medians[answer][mode] is None:
                unsure.append(f"the {answer} answer's {mode} median is missing ({scores})")
            scored = [s for s in scores if s is not None]
            if scored and max(scored) - min(scored) >= DISAGREEMENT:
                unsure.append(
                    f"the {answer} answer's {mode} repeats disagree by "
                    f"{max(scored) - min(scored)} points ({scores})"
                )
    if unsure:
        return {"decision": "inconclusive", "reasons": unsure, "medians": medians}
    return {"decision": "keep", "reasons": [], "medians": medians}


def graded_passages(case: Any) -> list[dict[str, Any]] | None:
    """The passages a case is graded with: its cache origin's on a cache hit,
    else its own evidence. ``None`` when they are unknown."""
    origin = getattr(case, "cache_origin", None)
    if origin is not None:
        return origin.get("evidence")
    return getattr(case, "evidence", None)


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"raised": 0, "lowered": 0, "same": 0, "missing": 0}
    for row in rows:
        counts[row["status"]] += 1
    return counts


def paired_table(judged: list[Any], cases: list[Any]) -> dict[str, Any]:
    """One pass's blind-versus-passages table. Only cases with a blind score are
    rows; a row without a passage score is ``missing`` with its reason, never
    ``same``. The counts are split by whether the case's expected source was among
    the passages it was graded with."""
    expected = {c.id: getattr(c, "expected_source", None) for c in cases}
    rows: list[dict[str, Any]] = []
    for case in judged:
        if case.score is None:
            continue
        passage = getattr(case, "passage_score", None)
        row: dict[str, Any] = {
            "case_id": case.case_id,
            "blind": case.score,
            "passages": passage,
            "delta": None,
            "status": "missing",
            "reason": None,
        }
        if passage is None:
            row["reason"] = getattr(case, "passage_error", None) or "no passage score"
        else:
            row["delta"] = passage - case.score
            row["status"] = (
                "raised" if row["delta"] > 0 else "lowered" if row["delta"] < 0 else "same"
            )
        sources = {p.get("source") for p in graded_passages(case) or []}
        row["source_in_passages"] = expected.get(case.case_id) in sources
        rows.append(row)
    paired = sum(1 for r in rows if r["status"] != "missing")
    coverage = paired / len(rows) if rows else None
    if coverage is None:
        label = "not available"
    elif coverage < MIN_COVERAGE:
        label = "too thin to read"
    else:
        label = None
    return {
        "rows": rows,
        "counts": _counts(rows),
        "split": {
            "source_in_passages": _counts([r for r in rows if r["source_in_passages"]]),
            "source_not_in_passages": _counts([r for r in rows if not r["source_in_passages"]]),
        },
        "coverage": coverage,
        "coverage_label": label,
    }


def stopped_line(stops: list[BaseException]) -> str | None:
    """Why the run stopped making paid calls, from the first ``CostLimitReached``
    it saw: the cap reached (no cause), or the meter unreadable (a cause)."""
    for exc in stops:
        if isinstance(exc, CostLimitReached):
            return STOPPED_BY_CAP if exc.cause is None else STOPPED_BY_METER
    return None


def budget_stop(exc: BaseException | None) -> CostLimitReached | None:
    """The ``CostLimitReached`` behind ``exc`` (itself or along its causes)."""
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, CostLimitReached):
            return current
        seen.add(id(current))
        current = current.__cause__ or getattr(current, "cause", None)
    return None


def _low(case: dict[str, Any]) -> bool:
    return any(
        isinstance(case.get(key), int) and case[key] <= 2 for key in ("score", "passage_score")
    )


def report_lines(summary: dict[str, Any]) -> list[str]:
    """The grader check's lines for the job summary: the decision and its reasons,
    the probe table, each pass's paired counts, and every case at or below 2 on
    either score with both rationales."""
    check = summary.get("grader_check") or {}
    reasons = check.get("reasons") or []
    lines = [
        f"GRADER CHECK: {check.get('decision')}"
        + (f" ({'; '.join(reasons)})" if reasons else "")
        + " (reported only; no gate reads it)"
    ]
    if check.get("stopped"):
        lines.append(f"GRADER CHECK: {check['stopped']}")
    cells = (check.get("probe") or {}).get("cells") or {}
    medians = decide(cells)["medians"]
    lines.append("GRADER PROBE (scores of the three repeats, then their median):")
    for answer in ANSWERS:
        for mode in MODES:
            scores = (cells.get(answer) or {}).get(mode, [])
            lines.append(f"  {answer:7s} {mode:8s} {scores} median {medians[answer][mode]}")
    for name, table in (check.get("paired") or {}).items():
        coverage = table.get("coverage")
        lines.append(
            f"{name} pass, blind vs passages: {table.get('counts')}; "
            f"coverage {'n/a' if coverage is None else f'{coverage:.2f}'}"
            + (f" ({table['coverage_label']})" if table.get("coverage_label") else "")
        )
        split = table.get("split") or {}
        lines.append(
            f"  expected source among the passages: {split.get('source_in_passages')}; "
            f"not among them: {split.get('source_not_in_passages')}"
        )
    passes = [("main", (summary.get("judge") or {}).get("cases") or [])]
    fallback = summary.get("fallback")
    if fallback:
        passes.append(("fallback", (fallback.get("judge") or {}).get("cases") or []))
    for name, cases in passes:
        for case in cases:
            if _low(case):
                lines.append(
                    f"  {name} pass low score: {case.get('case_id')}: blind "
                    f"{case.get('score')} ({case.get('rationale', '')}); passages "
                    f"{case.get('passage_score')} "
                    f"({case.get('passage_rationale') or case.get('passage_error') or ''})"
                )
    spend = check.get("spend")
    if spend:
        lines.append(f"grader calls and tokens by role: {spend}")
    if check.get("commit"):
        lines.append(f"measured commit: {check['commit']}")
    return lines


def start_refusal(*, ref: str | None, now: datetime) -> str | None:
    """Why a grader-check run must not start, or ``None``: never on main or a
    release tag, and not with fewer than ``MIN_MINUTES_BEFORE_UTC_MIDNIGHT``
    minutes left before 00:00 UTC."""
    if ref == "refs/heads/main" or (ref or "").startswith("refs/tags/v"):
        return f"the grader check does not run on {ref}; dispatch it on a branch"
    now = now.astimezone(UTC)
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    left = (midnight - now).total_seconds() / 60
    if left < MIN_MINUTES_BEFORE_UTC_MIDNIGHT:
        return (
            f"only {left:.0f} minutes are left before 00:00 UTC, and the grader check "
            f"needs {MIN_MINUTES_BEFORE_UTC_MIDNIGHT} (the daily spend cap resets at "
            "00:00 UTC)"
        )
    return None
