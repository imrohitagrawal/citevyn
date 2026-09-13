"""#321: the coverage gate must BITE in both directions, and never pass vacuously.

Every test here RUNS ``backend/scripts/coverage_baseline.py`` — the same file CI
runs — as a subprocess in a temporary directory, against reports and baselines
written for the occasion. Nothing greps the script for ``sys.exit``; asserting a
string is present is the mistake the sibling policy guard was rewritten to stop
making, and a partial edit that broke one exit while leaving another would satisfy
a substring check.

The script resolves ``artifacts/coverage.xml`` and ``coverage-baseline.json``
relative to the CURRENT DIRECTORY, so running it with ``cwd=tmp`` exercises the
exact invocation CI uses (``working-directory: backend``) rather than a
test-only code path with injected paths.

THE TWO DIRECTIONS, and why both are needed. A gate that always fails would
satisfy every "this must fail" test on its own; a gate that always passes would
satisfy every "this must pass" one. Neither half is evidence without the other.
And the vacuous cases matter MORE under a no-regression gate than they did under
#308's report: a report that measured nothing satisfies "uncovered lines did not
increase" trivially, so silence has to be an error rather than a pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backend"
_SCRIPT = _BACKEND / "scripts" / "coverage_baseline.py"
_REAL_BASELINE = _BACKEND / "coverage-baseline.json"


def _baseline(missed: int, *, valid: int = 5585) -> str:
    """A well-formed baseline recording ``missed`` uncovered lines."""
    return json.dumps({"missed": missed, "covered": valid - missed, "valid": valid})


def _report(covered: int, valid: int) -> str:
    return f'<?xml version="1.0" ?><coverage lines-valid="{valid}" lines-covered="{covered}" />'


def _run(work: Path, *, summary: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the real script in ``work``.

    ``GITHUB_STEP_SUMMARY`` is stripped from the inherited environment unless a
    test asks for one. Without that, this suite running INSIDE CI would append a
    dozen coverage verdicts to the real job summary — and the "no summary
    variable" test below would silently exercise the wrong branch.
    """
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
    if summary is not None:
        env["GITHUB_STEP_SUMMARY"] = str(summary)
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=work,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """A directory shaped like ``backend/`` mid-CI: a baseline and a report path."""
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "coverage-baseline.json").write_text(_baseline(182), encoding="utf-8")
    return tmp_path


def _write_report(work: Path, covered: int, valid: int) -> None:
    (work / "artifacts" / "coverage.xml").write_text(_report(covered, valid), encoding="utf-8")


# ---------------------------------------------------------------------------
# It bites: uncovered lines went UP.
# ---------------------------------------------------------------------------


def test_more_uncovered_lines_than_the_baseline_fails(work: Path) -> None:
    """ONE extra uncovered line reds the build. The gate is a count, not a percentage.

    5585 - 5402 = 183 missed against a baseline of 182. Note the PERCENTAGE barely
    moves (96.7413% -> 96.7234%) and would clear any plausible floor — which is the
    argument for counting lines instead.
    """
    _write_report(work, covered=5402, valid=5585)
    result = _run(work)
    assert result.returncode == 1, result.stdout
    assert "went UP by 1" in result.stdout


def test_a_large_regression_fails_and_names_the_size(work: Path) -> None:
    _write_report(work, covered=5300, valid=5585)
    result = _run(work)
    assert result.returncode == 1
    assert "went UP by 103" in result.stdout


# ---------------------------------------------------------------------------
# It does NOT bite: at or below the baseline.
# ---------------------------------------------------------------------------


def test_exactly_the_baseline_passes(work: Path) -> None:
    """The partner that stops "it fails" from being satisfied by a script that always fails."""
    _write_report(work, covered=5403, valid=5585)
    result = _run(work)
    assert result.returncode == 0, result.stdout
    assert "no increase" in result.stdout


def test_fewer_uncovered_lines_passes_and_says_the_baseline_can_be_lowered(work: Path) -> None:
    """An improvement must pass — AND tell the operator to lock it in.

    Without that sentence the baseline drifts upward in effect: lines covered by
    this PR can go uncovered again in the next one and the gate stays green,
    because it is still comparing against the older, looser number.
    """
    _write_report(work, covered=5410, valid=5585)
    result = _run(work)
    assert result.returncode == 0, result.stdout
    assert "7 better than the baseline" in result.stdout
    assert "coverage-baseline.json" in result.stdout


def test_growing_the_codebase_with_covered_lines_passes(work: Path) -> None:
    """`lines-valid` DRIFTS as `app/` grows — the whole reason this is not a floor.

    500 new lines, all covered: `missed` is unchanged at 182 and the gate passes,
    while the percentage rose. A "no decrease in percentage" gate would also pass
    here — but it would FAIL on 500 new lines of which 499 are covered, even though
    that too is a strict improvement in tested code. The count does not.
    """
    _write_report(work, covered=5903, valid=6085)
    assert _run(work).returncode == 0


# ---------------------------------------------------------------------------
# Vacuity: nothing measured must FAIL, never "compare" nothing and pass.
# ---------------------------------------------------------------------------


def test_a_missing_report_fails(work: Path) -> None:
    """Rename `app` and pytest still exits 0, writing no XML at all."""
    result = _run(work)
    assert result.returncode == 1
    assert "No coverage report" in result.stdout


def test_a_report_measuring_zero_lines_fails(work: Path) -> None:
    """The case that would otherwise satisfy "uncovered lines did not increase" trivially.

    0 valid, 0 covered -> 0 missed, which is <= any baseline. Under a naive
    comparison this is not merely a pass, it is a pass that REPORTS AN IMPROVEMENT.
    """
    _write_report(work, covered=0, valid=0)
    result = _run(work)
    assert result.returncode == 1
    assert "0 lines measured" in result.stdout


def test_a_malformed_report_fails(work: Path) -> None:
    (work / "artifacts" / "coverage.xml").write_text("<coverage lines-valid=", encoding="utf-8")
    result = _run(work)
    assert result.returncode == 1
    assert "not parseable XML" in result.stdout


def test_a_report_without_line_totals_fails(work: Path) -> None:
    """Well-formed XML that is not a coverage report — an empty file, a wrong artifact."""
    (work / "artifacts" / "coverage.xml").write_text("<coverage />", encoding="utf-8")
    result = _run(work)
    assert result.returncode == 1
    assert "no line totals" in result.stdout


def test_a_report_with_non_integer_totals_fails(work: Path) -> None:
    (work / "artifacts" / "coverage.xml").write_text(
        '<coverage lines-valid="lots" lines-covered="most" />', encoding="utf-8"
    )
    result = _run(work)
    assert result.returncode == 1
    assert "not integers" in result.stdout


def test_a_report_claiming_more_covered_than_valid_fails(work: Path) -> None:
    _write_report(work, covered=6000, valid=5585)
    result = _run(work)
    assert result.returncode == 1
    assert "self-contradictory" in result.stdout


# ---------------------------------------------------------------------------
# The baseline file itself.
# ---------------------------------------------------------------------------


def test_a_missing_baseline_file_fails(tmp_path: Path) -> None:
    """Delete the baseline and the gate must stop, not wave everything through."""
    (tmp_path / "artifacts").mkdir()
    _write_report(tmp_path, covered=5403, valid=5585)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "No baseline file" in result.stdout


def test_a_baseline_that_is_not_json_fails(work: Path) -> None:
    (work / "coverage-baseline.json").write_text("missed = 182", encoding="utf-8")
    _write_report(work, covered=5403, valid=5585)
    result = _run(work)
    assert result.returncode == 1
    assert "not valid JSON" in result.stdout


def test_a_baseline_missing_its_keys_fails(work: Path) -> None:
    (work / "coverage-baseline.json").write_text('{"note": "tbd"}', encoding="utf-8")
    _write_report(work, covered=5403, valid=5585)
    result = _run(work)
    assert result.returncode == 1
    assert "malformed" in result.stdout


def test_a_baseline_whose_arithmetic_does_not_add_up_fails(work: Path) -> None:
    """The obvious way to fake a pass: lower `missed`, leave `covered`/`valid` alone.

    A baseline is three numbers where one is derived from the other two, so an
    edit that touches only the one the gate reads leaves a visible contradiction.
    """
    (work / "coverage-baseline.json").write_text(
        json.dumps({"missed": 400, "covered": 5403, "valid": 5585}), encoding="utf-8"
    )
    _write_report(work, covered=5200, valid=5585)  # 385 missed: would pass under 400
    result = _run(work)
    assert result.returncode == 1
    assert "internally inconsistent" in result.stdout


def test_the_repos_real_baseline_is_usable_and_self_consistent() -> None:
    """The shipped file, not a fixture. A baseline the gate cannot read fails every run.

    Also pins the SHA, so the number is auditable: a reader can check out that
    commit and reproduce it. `measured_at_sha` is recorded, not derived, because
    the point is to say where the number came from.
    """
    data = json.loads(_REAL_BASELINE.read_text(encoding="utf-8"))
    assert data["missed"] == data["valid"] - data["covered"]
    assert data["valid"] > 0 and data["missed"] >= 0
    assert len(str(data["measured_at_sha"])) == 40


# ---------------------------------------------------------------------------
# The report's reader.
# ---------------------------------------------------------------------------


def test_the_verdict_reaches_the_github_step_summary(work: Path, tmp_path: Path) -> None:
    """Written by the SCRIPT, not by a shell redirect on the step.

    A redirect (`script >> "$GITHUB_STEP_SUMMARY"`) puts the verdict in the summary
    and NOWHERE ELSE — the job log stays silent on exactly the runs that fail. This
    asserts both destinations, on a run that FAILS, which is the case that matters.
    """
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    _write_report(work, covered=5000, valid=5585)
    result = _run(work, summary=summary)
    assert result.returncode == 1
    written = summary.read_text(encoding="utf-8")
    assert "went UP" in written, "the failing verdict never reached the step summary"
    assert "went UP" in result.stdout, "the failing verdict never reached the job log"


def test_it_runs_without_a_step_summary_variable(work: Path) -> None:
    """No `GITHUB_STEP_SUMMARY` outside CI. `make coverage-gate` must not crash on that."""
    _write_report(work, covered=5403, valid=5585)
    result = _run(work)
    assert result.returncode == 0, result.stderr
    assert "PASSED" in result.stdout
