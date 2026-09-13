"""Fail the build when the number of UNCOVERED lines goes UP (#321, follow-up to #308).

WHAT THIS GATE IS, AND WHAT IT DELIBERATELY IS NOT
--------------------------------------------------
It compares one integer — ``missed`` = ``lines-valid`` - ``lines-covered``, read
from the real ``artifacts/coverage.xml`` — against a baseline recorded in
``backend/coverage-baseline.json``. Above the baseline fails. At it passes.
Below it passes and says the baseline can be lowered.

It is NOT a percentage floor, and adding one would defeat the point. #308 shipped
coverage advisory with the condition "blocking only once a baseline is stable
across three consecutive `main` runs, and then as no-decrease against it — never
an absolute floor", and ``tests/test_coverage_gate_is_a_baseline_not_a_floor.py``
holds that line: it resolves coverage.py's EFFECTIVE ``fail_under`` and goes red
on any spelling of a threshold.

WHY A COUNT RATHER THAN A PERCENTAGE, measured rather than assumed. Across six
consecutive successful ``main`` CI runs the report was bit-identical at
``lines-valid=5585 / lines-covered=5403`` — but ``lines-valid`` DRIFTS as ``app/``
grows (5562 -> 5567 -> 5585 over the runs before those), so a percentage floor
moves for reasons that have nothing to do with test quality. An integer count of
uncovered lines only moves when uncovered lines are added or removed.

WHAT THIS GATE CANNOT SEE, stated rather than implied:
  * Coverage is not assertion. It shows which lines RAN, never which are CHECKED.
    A line in ``app/worker/promotion_eval.py`` once measured 97% covered AND was
    executed by the suite, and deleting it still left all 42 promotion tests
    green. Mutation testing is what proves a test bites; this only proves no test
    even executes a line. Where the two disagree, mutation wins.
  * Deleting uncovered code lowers ``missed`` just as adding a test does, so a
    change that deletes N uncovered lines and adds N new uncovered ones passes.
    That is inherent to a count and is accepted.
  * Shrinking the measured program lowers ``missed`` without covering anything,
    and this script cannot see it: it reads the report, never the invocation or
    the source that produced it. The zero-lines check below catches only a TOTAL
    collapse. The partial cases live in
    ``tests/test_coverage_gate_is_a_baseline_not_a_floor.py``, and an adversarial
    review proved the first version of that list incomplete — three lines of
    ``[tool.coverage.report] exclude_also`` collapsed ``lines-valid`` 5,585 ->
    2,222 with every test green, and a ``# pragma: no cover`` hid a genuinely
    untested helper outright. So the guard now pins FOUR things, not two: the
    exact ``--cov=app`` token, the resolved ``omit``/``include``/``source``, the
    resolved exclusion lists against coverage.py's own defaults, and a recorded
    count of ``pragma: no cover`` lines under ``app/``. That list is what it
    covers today — it is not a proof that no further route exists.
  * Code outside ``app/`` is not measured at all, this script included. Logic
    parked in ``backend/scripts/`` contributes nothing to the number it produces.

NON-VACUITY. pytest exits 0 when coverage collects NOTHING: rename ``app``, or
omit it, and you get a green job, a CoverageWarning nobody reads, and no XML at
all. So every path where the measurement is absent, unreadable or empty exits 1
loudly rather than "comparing" nothing and passing. This repo's scar is a check
that counted nothing and passed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPORT = Path("artifacts/coverage.xml")
BASELINE = Path("coverage-baseline.json")

# How far coverage.py's own `timestamp` may be from now before the report is
# treated as LEFTOVER rather than fresh. See `_read_report`: pytest-cov does not
# write the file at all when coverage collects nothing, and does not fail either,
# so an older report sitting in the directory would be compared as if it were
# this run's. Two hours is far beyond any real gap (CI writes it in the step
# immediately before; `make coverage-gate` seconds before) and far under
# "yesterday's run", which is the case worth catching.
_MAX_REPORT_AGE_SECONDS = 2 * 60 * 60

# A plain non-negative decimal integer, and nothing else. `int()` alone accepts
# `1_585`, `+5585`, surrounding whitespace and non-ASCII digits, and truncates a
# float — spellings coverage.py never emits, so accepting them only widens what a
# hand-edited file can say.
_PLAIN_INT_RE = re.compile(r"\A[0-9]+\Z")

_WHERE = (
    "\n\n_Baseline lives in `backend/coverage-baseline.json`; the comparison is "
    "`backend/scripts/coverage_baseline.py`. See #321._"
)


def _emit(markdown: str) -> None:
    """Print to the job log AND append to the GitHub step summary, when there is one.

    Both, deliberately. The summary is where a human looks; the log is where a
    FAILING step is read. The previous shape redirected stdout into the summary
    file, which left the job log silent on exactly the runs that matter.

    The summary write CANNOT change the verdict. Letting an OSError out of here
    would turn a passing run red whenever `$GITHUB_STEP_SUMMARY` is unwritable —
    a coverage gate that fails because a log file could not be appended to.
    """
    print(markdown, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    try:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    except OSError as exc:
        print(f"(could not append to GITHUB_STEP_SUMMARY: {exc})", flush=True)


def _fail(body: str) -> int:
    _emit("### Coverage\n\n" + body + _WHERE)
    return 1


def _read_baseline() -> tuple[int, str] | str:
    """The recorded ``missed`` count, or a human-readable reason it is unusable."""
    if not BASELINE.exists():
        return (
            f"**No baseline file.** `{BASELINE}` is missing, so there is nothing "
            "to compare against."
        )
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"**The baseline file is not valid JSON** (`{BASELINE}`): {exc}."
    except OSError as exc:
        return f"**The baseline file could not be read** (`{BASELINE}`): {exc}."
    if not isinstance(data, dict):
        return f"**The baseline file is not a JSON object** (`{BASELINE}`)."
    values: list[int] = []
    for key in ("missed", "covered", "valid"):
        value = data.get(key)
        # `isinstance(True, int)` is True, and `int("1_585")` is 1585 — so require
        # a real JSON integer rather than anything int() would swallow.
        if not isinstance(value, int) or isinstance(value, bool):
            return (
                f"**The baseline file is malformed** (`{BASELINE}`): `{key}` must be "
                f"a JSON integer, got {value!r}."
            )
        values.append(value)
    missed, covered, valid = values
    if missed < 0 or valid <= 0 or covered < 0 or covered > valid:
        return f"**The baseline file holds impossible numbers**: {covered=}, {valid=}, {missed=}."
    # Cross-check the recorded arithmetic. A hand-edited baseline that lowers
    # `missed` without touching `covered`/`valid` is the obvious way to fake a
    # pass, and it leaves exactly this inconsistency behind.
    if missed != valid - covered:
        return (
            f"**The baseline file is internally inconsistent**: it records "
            f"`missed={missed}` but `valid - covered` = {valid} - {covered} = "
            f"{valid - covered}. One of the three was edited without the others."
        )
    sha = str(data.get("measured_at_sha", "unrecorded"))
    return missed, sha


def _read_report() -> tuple[int, int] | str:
    """``(covered, valid)`` from the real report, or a reason it is unusable."""
    if not REPORT.exists():
        return (
            f"**No coverage report was produced.** `{REPORT}` does not exist — the "
            "suite ran but measured nothing. Check that `--cov=app` still names a "
            "real package."
        )
    try:
        root = ET.parse(REPORT).getroot()
    except ET.ParseError as exc:
        return f"**The coverage report is not parseable XML** (`{REPORT}`): {exc}."
    except OSError as exc:
        return f"**The coverage report could not be read** (`{REPORT}`): {exc}."

    # FRESHNESS, before anything is compared. pytest-cov writes no file at all
    # when coverage collects nothing, and does not fail — so an EARLIER report
    # left in the directory would be read as this run's, and the gate would pass
    # having compared a measurement of a different tree. Both writers now delete
    # the file before measuring; this is the guard that bites if either ever
    # stops, and it reads the artifact rather than trusting a command line.
    raw_stamp = root.get("timestamp")
    if raw_stamp is None or not _PLAIN_INT_RE.match(raw_stamp):
        return (
            f"**The coverage report has no usable `timestamp`** (`{REPORT}`): "
            f"got {raw_stamp!r}. Without it a leftover report from an earlier run "
            "cannot be told from this one's, so it is refused rather than trusted."
        )
    age = abs(time.time() - int(raw_stamp) / 1000)
    if age > _MAX_REPORT_AGE_SECONDS:
        return (
            f"**The coverage report is stale** (`{REPORT}`, written "
            f"{age / 3600:.1f} hours from now). Coverage measured nothing this run "
            "and an older report was left behind, or the clock moved. Re-measure: "
            "`make coverage-gate`."
        )

    raw_covered = root.get("lines-covered")
    raw_valid = root.get("lines-valid")
    if raw_covered is None or raw_valid is None:
        return (
            f"**The coverage report has no line totals** (`{REPORT}`): expected "
            "`lines-covered` and `lines-valid` attributes on the root element."
        )
    if not _PLAIN_INT_RE.match(raw_covered) or not _PLAIN_INT_RE.match(raw_valid):
        return (
            f"**The coverage report's line totals are not integers** (`{REPORT}`): "
            f"lines-covered={raw_covered!r}, lines-valid={raw_valid!r}."
        )
    covered, valid = int(raw_covered), int(raw_valid)
    if valid <= 0:
        return (
            "**0 lines measured.** Coverage collected no data, so there is nothing "
            "to compare. This is a failure, not a pass — a report that measures "
            "nothing would otherwise satisfy any no-regression check trivially."
        )
    if covered < 0 or covered > valid:
        return f"**The coverage report is self-contradictory**: {covered=} against {valid=}."
    return covered, valid


def main() -> int:
    baseline = _read_baseline()
    if isinstance(baseline, str):
        return _fail(baseline)
    baseline_missed, baseline_sha = baseline

    report = _read_report()
    if isinstance(report, str):
        return _fail(report)
    covered, valid = report

    # A baseline claiming more uncovered lines than the program HAS is not a
    # loose baseline, it is a disabled gate — and the arithmetic cross-check in
    # `_read_baseline` does not catch it, because editing all three numbers
    # consistently is no harder than editing one. Bound it against reality.
    if baseline_missed > valid:
        return _fail(
            f"**The baseline is impossible against this run**: it records "
            f"`missed={baseline_missed:,}`, but the whole measured program is only "
            f"{valid:,} lines. A baseline above the line count can never be "
            "exceeded, so the gate would be permanently green."
        )

    missed = valid - covered
    headline = (
        f"**{covered / valid:.1%}** of {valid:,} lines covered — "
        f"**{missed:,} missed** against a baseline of **{baseline_missed:,}** "
        f"(recorded at `{baseline_sha}`)."
    )

    if missed > baseline_missed:
        return _fail(
            headline + "\n\n**FAILED: uncovered lines went UP by "
            f"{missed - baseline_missed:,}.** Every line this change added is not "
            "executed by any test. Cover them, or — if the new code is genuinely "
            "untestable here — set `missed`, `covered` AND `valid` in "
            "`backend/coverage-baseline.json` to this run's numbers "
            f"(`missed: {missed}`, `covered: {covered}`, `valid: {valid}`) in the "
            "same commit and say why. All three, together: editing `missed` alone "
            "leaves the file internally inconsistent and fails the next run. This "
            "gate is a count of uncovered lines, never a percentage floor "
            "(#308, #321)."
        )

    if missed < baseline_missed:
        _emit(
            "### Coverage\n\n"
            + headline
            + f"\n\nPASSED, and **{baseline_missed - missed:,} better than the "
            "baseline.** Lower `missed` (and `covered`/`valid`) in "
            "`backend/coverage-baseline.json` to lock the improvement in — until "
            "you do, those lines can silently go uncovered again." + _WHERE
        )
        return 0

    _emit("### Coverage\n\n" + headline + "\n\nPASSED: no increase in uncovered lines." + _WHERE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
