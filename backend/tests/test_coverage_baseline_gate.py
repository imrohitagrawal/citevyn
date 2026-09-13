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
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backend"
_SCRIPT = _BACKEND / "scripts" / "coverage_baseline.py"
_REAL_BASELINE = _BACKEND / "coverage-baseline.json"


def _baseline(missed: int, *, valid: int = 5585) -> str:
    """A well-formed baseline recording ``missed`` uncovered lines."""
    return json.dumps({"missed": missed, "covered": valid - missed, "valid": valid})


def _report(covered: int, valid: int, *, age_seconds: float = 0.0) -> str:
    """A Cobertura root shaped like coverage.py's, with its own `timestamp`.

    coverage.py stamps epoch MILLISECONDS. The gate refuses a report far from
    now, because pytest-cov writes no file when coverage collects nothing and
    does not fail, so a leftover one would be compared as if it were this run's.
    """
    stamp = int((time.time() - age_seconds) * 1000)
    return (
        f'<?xml version="1.0" ?><coverage lines-valid="{valid}" '
        f'lines-covered="{covered}" timestamp="{stamp}" />'
    )


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


def _write_report(work: Path, covered: int, valid: int, *, age_seconds: float = 0.0) -> None:
    (work / "artifacts" / "coverage.xml").write_text(
        _report(covered, valid, age_seconds=age_seconds), encoding="utf-8"
    )


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
    result = _run(work)
    assert result.returncode == 0, result.stdout
    # ...with a stdout assertion, so an always-exit-0 script cannot satisfy it.
    assert "182 missed" in result.stdout and "no increase" in result.stdout


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
    """Well-formed XML that is not a coverage report — an empty file, a wrong artifact.

    Carries a fresh `timestamp` deliberately, so it reaches the totals check
    rather than being rejected earlier as stale. A test that trips an EARLIER
    guard proves nothing about the one it names.
    """
    (work / "artifacts" / "coverage.xml").write_text(
        f'<coverage timestamp="{int(time.time() * 1000)}" />', encoding="utf-8"
    )
    result = _run(work)
    assert result.returncode == 1
    assert "no line totals" in result.stdout


def test_a_report_with_non_integer_totals_fails(work: Path) -> None:
    (work / "artifacts" / "coverage.xml").write_text(
        f'<coverage lines-valid="lots" lines-covered="most" '
        f'timestamp="{int(time.time() * 1000)}" />',
        encoding="utf-8",
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
    # A REAL commit, resolved through git — not 40 arbitrary characters. The point
    # of recording the SHA is that a reader can check it out and reproduce the
    # number, which a well-formed-but-fictional hex string does not allow.
    sha = str(data["measured_at_sha"])
    kind = subprocess.run(["git", "cat-file", "-t", sha], cwd=_REPO, capture_output=True, text=True)
    assert kind.stdout.strip() == "commit", (
        f"measured_at_sha {sha!r} is not a commit in this repository "
        f"(git says {kind.stdout.strip()!r} / {kind.stderr.strip()!r})"
    )
    assert (
        subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=_REPO).returncode
        == 0
    ), f"measured_at_sha {sha} is not an ancestor of HEAD, so the number is not reproducible here"


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


# ---------------------------------------------------------------------------
# Staleness: a LEFTOVER report is the vacuous case that survives a rerun.
# ---------------------------------------------------------------------------


def test_a_stale_report_fails(work: Path) -> None:
    """The hole an adversarial review found, reproduced as a test.

    pytest-cov writes NO file when coverage collects nothing — rename `app`, omit
    it, break `--cov` — and pytest still exits 0. Whatever report was already in
    `artifacts/` is then read by the gate as if it were this run's, and the gate
    passes having compared a measurement of a different tree. Both writers now
    `rm -f` the report before measuring; this is the guard that bites if either
    ever stops, and it reads the artifact rather than trusting a command line.
    """
    _write_report(work, covered=5403, valid=5585, age_seconds=3 * 60 * 60)
    result = _run(work)
    assert result.returncode == 1, result.stdout
    assert "stale" in result.stdout


def test_a_report_from_this_run_is_not_called_stale(work: Path) -> None:
    """Partner: an ordinary fresh report must NOT trip the freshness check.

    Otherwise "stale" would simply be "always", and every passing test above
    would be passing for a reason the gate no longer has.
    """
    _write_report(work, covered=5403, valid=5585, age_seconds=30 * 60)
    assert _run(work).returncode == 0


def test_a_report_with_no_timestamp_fails(work: Path) -> None:
    """Without the attribute, a leftover report cannot be told from this run's.

    Refused rather than trusted — the alternative is a silent hole that opens
    itself if coverage.py ever stops emitting the field.
    """
    (work / "artifacts" / "coverage.xml").write_text(
        '<coverage lines-valid="5585" lines-covered="5403" />', encoding="utf-8"
    )
    result = _run(work)
    assert result.returncode == 1
    assert "timestamp" in result.stdout


def test_coverage_py_really_emits_the_timestamp_the_gate_depends_on(tmp_path: Path) -> None:
    """Partner for the two above: the attribute is one coverage.py ACTUALLY writes.

    Generated by running coverage.py over a real module, not asserted from memory.
    Without this, `test_a_report_with_no_timestamp_fails` could be demanding a
    field the library never produces — the gate would then fail every real run,
    and nothing here would say so.

    IN A SUBPROCESS, and that is not incidental. The first version called
    `coverage.Coverage().start()` inside the pytest process, which suspends the
    outer pytest-cov collector for the rest of the session: the suite stayed
    green and the measured count silently went 182 -> 337. The #321 gate is what
    caught it — the first thing it ever caught — so the lesson is recorded here
    rather than just fixed. Never start a second collector in-process.
    """
    module = tmp_path / "probe.py"
    module.write_text("def f():\n    return 1\n\n\nf()\n", encoding="utf-8")
    out = tmp_path / "generated.xml"
    env = {**os.environ, "COVERAGE_FILE": str(tmp_path / ".coverage")}
    run = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "--source", str(tmp_path), str(module)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    report = subprocess.run(
        [sys.executable, "-m", "coverage", "xml", "-o", str(out)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert report.returncode == 0, report.stderr
    root = ET.parse(out).getroot()
    assert root.get("timestamp"), "coverage.py's XML report carries no `timestamp` attribute"
    assert root.get("timestamp", "").isdigit()
    # ...and the two totals the gate reads, from the same generated report.
    assert root.get("lines-valid", "").isdigit() and root.get("lines-covered", "").isdigit()


# ---------------------------------------------------------------------------
# A baseline can be loose. It cannot be so loose the gate is off.
# ---------------------------------------------------------------------------


def test_a_baseline_above_the_whole_line_count_fails(work: Path) -> None:
    """The internal cross-check catches a ONE-field edit. This catches a consistent one.

    `{"missed": 99817, "covered": 183, "valid": 100000}` adds up perfectly and
    disables the gate forever — it printed "PASSED, and 99,635 better than the
    baseline" before this. A baseline above the measured program's line count can
    never be exceeded.
    """
    (work / "coverage-baseline.json").write_text(
        json.dumps({"missed": 99817, "covered": 183, "valid": 100000}), encoding="utf-8"
    )
    _write_report(work, covered=5403, valid=5585)
    result = _run(work)
    assert result.returncode == 1, result.stdout
    assert "impossible against this run" in result.stdout


def test_a_baseline_slightly_above_the_current_count_still_passes(work: Path) -> None:
    """Partner: a LOOSE baseline is legitimate and must not be rejected.

    Only one above the whole line count is refused. Rejecting any baseline above
    the measurement would forbid the "improved, lower it when you like" path the
    gate deliberately offers.
    """
    (work / "coverage-baseline.json").write_text(
        json.dumps({"missed": 300, "covered": 5285, "valid": 5585}), encoding="utf-8"
    )
    _write_report(work, covered=5403, valid=5585)
    result = _run(work)
    assert result.returncode == 0, result.stdout
    assert "better than the baseline" in result.stdout


# ---------------------------------------------------------------------------
# Number spellings, and errors that must not go quiet.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"missed": "1_585", "covered": "4_000", "valid": "5_585"},
        {"missed": 1585.9, "covered": 3999.1, "valid": 5585.0},
        {"missed": True, "covered": 5584, "valid": 5585},
    ],
)
def test_a_baseline_whose_numbers_are_not_json_integers_fails(
    work: Path, payload: dict[str, object]
) -> None:
    """`int()` swallows underscores, signs and floats; JSON integers are what is meant.

    `int("1_585")` is 1585 and `int(1585.9)` is 1585, so a string or float baseline
    parsed cleanly AND passed the internal arithmetic check. coverage.py never
    emits those spellings, so accepting them only widened what a hand-edited file
    could say. `True` is included because `isinstance(True, int)` is also True.
    """
    (work / "coverage-baseline.json").write_text(json.dumps(payload), encoding="utf-8")
    _write_report(work, covered=5403, valid=5585)
    result = _run(work)
    assert result.returncode == 1, result.stdout
    assert "must be a JSON integer" in result.stdout


def test_a_report_with_underscored_totals_fails(work: Path) -> None:
    (work / "artifacts" / "coverage.xml").write_text(
        f'<coverage lines-valid="5_585" lines-covered="5_403" '
        f'timestamp="{int(time.time() * 1000)}" />',
        encoding="utf-8",
    )
    result = _run(work)
    assert result.returncode == 1
    assert "not integers" in result.stdout


def test_an_unreadable_baseline_fails_with_a_message_not_a_traceback(work: Path) -> None:
    """A directory where the baseline should be raised IsADirectoryError: exit 1, stdout EMPTY.

    Exit 1 is the right verdict, but an operator reading the step got a traceback
    and no statement of what the gate concluded.
    """
    (work / "coverage-baseline.json").unlink()
    (work / "coverage-baseline.json").mkdir()
    _write_report(work, covered=5403, valid=5585)
    result = _run(work)
    assert result.returncode == 1
    assert "could not be read" in result.stdout


def test_an_unreadable_report_fails_with_a_message_not_a_traceback(work: Path) -> None:
    (work / "artifacts" / "coverage.xml").mkdir()
    result = _run(work)
    assert result.returncode == 1
    assert "could not be read" in result.stdout


def test_an_unwritable_step_summary_does_not_turn_a_pass_into_a_failure(
    work: Path, tmp_path: Path
) -> None:
    """The inverted silent failure: a coverage gate reddening over a LOG file.

    `_emit` appends to `$GITHUB_STEP_SUMMARY`. Letting an OSError out of there
    means a run that PASSED exits 1 whenever that path is unwritable — the log
    says PASSED and the job is red, with no explanation anywhere.
    """
    _write_report(work, covered=5403, valid=5585)
    result = _run(work, summary=tmp_path / "no" / "such" / "dir" / "summary.md")
    assert result.returncode == 0, result.stdout
    assert "PASSED" in result.stdout
    assert "could not append" in result.stdout
