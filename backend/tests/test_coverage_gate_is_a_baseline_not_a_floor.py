"""Coverage gates on a BASELINE, and must never become an absolute floor (#308, #321).

RENAMED, and the rename is the point. This file was
``test_coverage_is_reported_not_gated.py`` while #308's coverage step was advisory.
#321 made it blocking, so the old name became false, and keeping it would have been
exactly the drift this repo files issues about. What survives the rename is the
load-bearing half: coverage must not become a PERCENTAGE THRESHOLD. The tests that
enforce that are unchanged below, down to the assertions.

The repo's own scar is why this file exists: a line in ``app/worker/promotion_eval.py``
measured **97% covered** and was **executed by the suite**, yet deleting it left the
whole promotion suite green. (That defect has since been fixed and is now guarded by
``test_promotion_gate_mirrors_the_orchestrator_query_pipeline`` — it is history, not
a live hole. The lesson is what survives.) Coverage shows which lines RAN, never which are CHECKED —
so a coverage threshold would have said nothing about that defect while inviting
tests written to move a number.

WHY THE NEW GATE DOES NOT VIOLATE THE REASONING THIS FILE ENCODES. #308 shipped the
measurement advisory and stated its own promotion condition in the same commit — the
project practice being that a gate shipped advisory says what would make it blocking
(a working convention, NOT a rule in `AGENTS.md`, which an earlier version of this
docstring wrongly claimed). That condition was: "only once a baseline is stable across
three consecutive `main` runs, and then as no-regression against it — never an
absolute floor."

#321 satisfied it — SIX consecutive successful `main` runs, bit-identical at
``lines-covered=5403 / lines-valid=5585`` — and shipped the gate in the shape the
condition names: an integer count of UNCOVERED lines that must not INCREASE, recorded
in ``backend/coverage-baseline.json`` and compared by
``backend/scripts/coverage_baseline.py``.

The difference between that and a floor is not cosmetic. A percentage floor is a
TARGET, and a target can be raised by writing tests that execute code without checking
it — the promotion_eval failure exactly. ``lines-valid`` also DRIFTS as ``app/`` grows
(5562 -> 5567 -> 5585 across successive `main` runs), so a floor moves for reasons that
have nothing to do with test quality. A count of lines no test executes moves only when
such lines are added or removed, offers no number to chase upward, and makes no claim
about whether a covered line is CHECKED — which is the claim this file exists to stop
coverage from making.

This file owns the POLICY: that no absolute threshold exists in any spelling, and that
the merge-gating job still measures the suite that gates merges. The BEHAVIOUR of the
comparison is proven in ``test_coverage_baseline_gate.py``.

WHAT THESE TESTS LEARNED THE HARD WAY
-------------------------------------
The first version checked only for the ``--cov-fail-under`` CLI flag and called
that "the single flag that turns this report into a threshold". It is not.
``pytest_cov/plugin.py`` adopts coverage.py's OWN config::

    if self.options.cov_fail_under is None and hasattr(cov_config, 'fail_under'):
        self.options.cov_fail_under = cov_config.fail_under

so a ``[tool.coverage.report] fail_under`` in ``pyproject.toml`` — or a
``.coveragerc`` / ``setup.cfg`` / ``tox.ini`` — makes coverage blocking with the
guard fully green. Verified end to end: the guard passed 4/4 while the run
reported "Required test coverage of 99.0% not reached". These tests now check the
EFFECTIVE configuration, not one spelling of it.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_CI = _REPO / ".github" / "workflows" / "ci.yml"
_WORKFLOWS = sorted((_REPO / ".github" / "workflows").glob("*.yml"))
_MAKEFILE = _REPO / "Makefile"
_PYPROJECT = _REPO / "backend" / "pyproject.toml"
_BACKEND = _REPO / "backend"
_GATE_SCRIPT = "scripts/coverage_baseline.py"
_BASELINE = _BACKEND / "coverage-baseline.json"


# `--cov=app` as a whole token. Anchored on a word boundary so `--cov=app/api`
# and `--cov=app.worker` do NOT satisfy it.
_COV_TARGET_RE = re.compile(r"--cov=app(?![\w./])")


def _mentions_threshold(text: str) -> bool:
    """True if `text` imposes a coverage threshold, however it is spelled.

    Line continuations are joined first: a Makefile recipe can split the flag over
    `\\` + newline and the shell still receives one token, so the literal never
    appears contiguously in the file.
    """
    joined = re.sub(r"\\\s*\n\s*", "", text)
    return "cov-fail-under" in joined or "--fail-under" in joined


def _pyproject_addopts(path: Path) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", ""))


def _ini_addopts(path: Path) -> str:
    parser = configparser.ConfigParser()
    parser.read(path)
    return " ".join(parser[section].get("addopts", "") for section in parser.sections())


def _pytest_step() -> dict:
    jobs = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    steps = jobs["test"]["steps"]
    # Match the INVOCATION, not the word: a step that merely mentions pytest
    # (`pip install pytest`) must not red a required check by looking like a
    # second test run.
    matches = [s for s in steps if "uv run pytest" in str(s.get("run", ""))]
    assert len(matches) == 1, f"expected exactly one `uv run pytest` step, found {len(matches)}"
    return matches[0]


def _cannot_fail_the_job(step: dict) -> str | None:
    """Why ``step`` could not red its job, or ``None`` if it can.

    A step that RUNS but cannot fail anything is the "blocking on day one" claim
    made false while every wiring assertion agrees. `tests/test_gating_workflows.py`
    already catches `continue-on-error` across the gating jobs; this adds the three
    shapes specific to one step's own command, and keeps the reason local to the
    test that depends on it.
    """
    if step.get("continue-on-error"):
        return "the step sets `continue-on-error`, so its failure cannot red the job"
    if "if" in step:
        return f"the step is conditional (`if: {step['if']}`), so it may not run at all"
    run = str(step.get("run", ""))
    # `cmd || true`, `cmd ; true`, `set +e`, or the whole body commented out. The
    # default shell is `bash -e`, so a trailing `|| true` is all it takes.
    for needle in ("|| true", "||true", "; true", "set +e", "|| :"):
        if needle in run:
            return f"the command is neutered with {needle!r}, so a failure is swallowed"
    if not [line for line in run.splitlines() if line.strip() and not line.strip().startswith("#")]:
        return "the command body is empty or entirely commented out"
    return None


def _coverage_recipe() -> str:
    """The body of the ``coverage:`` target, and only that target.

    Counting fragments file-wide was decoration: ``env -u CITEVYN_DATABASE_URL``
    appears three times in this Makefile, so deleting it from THIS recipe still
    left two and the assertion passed.
    """
    text = _MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^coverage:.*?(?=^\S)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, "no `coverage:` target found in the Makefile"
    return match.group(0)


def test_ci_measures_coverage_on_the_suite_that_gates_merges() -> None:
    run = _pytest_step()["run"]
    # The WHOLE token, not a substring. `--cov=app/api` contains `--cov=app`, and
    # narrowing the measured program that way lowers `missed` without covering a
    # single line — the cheapest way to walk under the #321 baseline gate while
    # every check here stays green.
    assert _COV_TARGET_RE.search(run), (
        "the merge-gating pytest run no longer measures coverage of exactly `app`"
    )
    assert "coverage.xml" in run, "no machine-readable report is produced"
    # Same invocation as the suite itself: a different marker set would measure a
    # different program and the number would describe something nobody runs.
    assert '-m "not postgres"' in run


def test_no_coverage_threshold_is_configured_anywhere() -> None:
    """The load-bearing one — and it asks coverage.py, rather than matching text.

    A review found EIGHT working ways to make coverage blocking while a
    string-matching version of this test stayed green: ``[tool.coverage.report]
    fail_under`` in pyproject, a ``.coveragerc``, ``setup.cfg``, ``tox.ini``, a
    ``pytest.ini`` ``addopts``, a ``PYTEST_ADDOPTS`` env var on the CI step, a
    separate ``coverage report --fail-under`` step, and splitting the flag across
    a Makefile line continuation so the literal never appears contiguously.

    The lesson is the same one this whole change is about: checking that a STRING
    is absent is not checking that the BEHAVIOUR is absent. So the first assertion
    below resolves the real configuration through coverage.py itself, which closes
    every config-file route at once including ones nobody has invented yet.
    """
    import coverage

    # 1. EFFECTIVE coverage.py config. Resolved from backend/ exactly as a test
    #    run would: .coveragerc, setup.cfg, tox.ini and pyproject are all read
    #    here, so this one assertion covers every config-file spelling.
    cwd = os.getcwd()
    try:
        os.chdir(_BACKEND)
        config = coverage.Coverage().config
        resolved = config.fail_under
        narrowing = {
            "run:omit": config.run_omit,
            "run:include": config.run_include,
            "run:source": config.source,
            "report:omit": config.report_omit,
            "report:include": config.report_include,
        }
        exclusions = {
            "report:exclude_lines/exclude_also": list(config.exclude_list),
            "report:partial_branches/partial_also": list(config.partial_list),
        }
    finally:
        os.chdir(cwd)
    assert resolved == 0, (
        f"coverage.py resolves fail_under={resolved}. pytest-cov adopts that as "
        "--cov-fail-under (see pytest_cov/plugin.py), which makes coverage BLOCKING."
    )
    # NEW WITH THE #321 GATE, and it belongs with the resolution above rather than
    # in the gate's own tests: the gate compares a COUNT of uncovered lines, so
    # SHRINKING the measured program lowers that count without covering anything.
    # An `omit` is the config-file spelling of it, `--cov=app/api` the CLI one
    # (pinned separately by `_COV_TARGET_RE` above). Resolved through coverage.py
    # for the same reason as `fail_under`: it closes .coveragerc, setup.cfg,
    # tox.ini and pyproject at once.
    assert not any(narrowing.values()), (
        f"coverage.py resolves a narrowed measurement scope: "
        f"{ {k: v for k, v in narrowing.items() if v} }. That lowers the uncovered-line "
        "count the #321 baseline gate compares, without a single line being covered."
    )
    # ...and EXCLUSION is the second, sharper way to shrink it — sharper because it
    # needs no path at all. An adversarial review landed three lines of
    # `[tool.coverage.report] exclude_also = ["^def ", "^    def ", "^class "]` in
    # `pyproject.toml` and watched `lines-valid` collapse 5,585 -> 2,222 while the
    # gate printed "98.2% ... 39 missed against a baseline of 182" and exited 0,
    # with every test in this file green. The five attributes above did not see it,
    # and the comment that used to sit here claimed they closed "every config-file
    # route at once" — which was FALSE, and is corrected rather than narrowed away.
    #
    # Asserted EQUAL to coverage.py's own defaults, not merely non-empty: the
    # defaults already contain `pragma: no cover`, an `...` body and
    # `if TYPE_CHECKING:`, so "is empty" would be wrong and "is truthy" would
    # accept any addition. Equality accepts exactly what shipped with the library.
    resolved_excludes = exclusions["report:exclude_lines/exclude_also"]
    assert resolved_excludes == list(coverage.config.DEFAULT_EXCLUDE), (
        "coverage.py resolves a NON-DEFAULT exclusion list: "
        f"{exclusions['report:exclude_lines/exclude_also']}. Every line an exclusion "
        "matches leaves `lines-valid` entirely, so it lowers the uncovered-line count "
        "the #321 gate compares without one line being covered."
    )
    assert exclusions["report:partial_branches/partial_also"] == list(
        coverage.config.DEFAULT_PARTIAL
    ), (
        "coverage.py resolves a NON-DEFAULT partial-branch list: "
        f"{exclusions['report:partial_branches/partial_also']}."
    )

    # 2. pytest's own addopts, which coverage.py cannot see.
    for path, getter in (
        (_PYPROJECT, _pyproject_addopts),
        (_BACKEND / "pytest.ini", _ini_addopts),
        (_BACKEND / "setup.cfg", _ini_addopts),
        (_BACKEND / "tox.ini", _ini_addopts),
    ):
        if path.exists():
            assert not _mentions_threshold(getter(path)), f"{path.name} addopts sets a threshold"

    # 3. CI: every step's `run` AND its `env` — a PYTEST_ADDOPTS env var imposes a
    #    threshold without appearing in any command. Line continuations are joined
    #    first, because `--cov-fail\<newline>-under=95` reaches the shell as one
    #    token while the literal never appears contiguously in the file.
    #    WHAT THIS CANNOT SEE, stated rather than implied: a threshold inside a
    #    shell script invoked from a `run:`, or inside the external reusable
    #    workflow behind `pr-quality.yml` (it lives in another repo). Neither is
    #    closed by pretending otherwise.
    for workflow in _WORKFLOWS:
        data = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
        for name, job in (data.get("jobs") or {}).items():
            for step in job.get("steps", []) or []:
                assert not _mentions_threshold(str(step.get("run", ""))), (
                    f"{workflow.name} job {name!r} runs a coverage threshold"
                )
                for key, value in (step.get("env") or {}).items():
                    assert not _mentions_threshold(str(value)), (
                        f"{workflow.name} job {name!r} sets {key}, imposing a threshold"
                    )

    # 4. The Makefile, comment lines stripped. The repo writes recipe comments as
    #    `@#`, not just `#` — a strip that only handled `#` would go red on a
    #    comment explaining why the flag is absent.
    code = "\n".join(
        line
        for line in _MAKEFILE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().lstrip("@").startswith("#")
    )
    assert not _mentions_threshold(code), "the Makefile imposes a coverage threshold"


def test_the_threshold_check_would_actually_notice() -> None:
    """Partner assertions: prove each detector above can SEE a threshold.

    Without these, any of them could be passing because a parser returns nothing —
    which is exactly how the string-matching version passed while eight routes
    were open.
    """
    assert _mentions_threshold("pytest --cov-fail-under=95")
    assert _mentions_threshold("coverage report --fail-under=95")
    # ...including when a line continuation splits the flag in the source text.
    assert _mentions_threshold("pytest --cov-fail\\\n-under=95")
    # ...and it does NOT fire on ordinary coverage usage, or this would be a
    # tripwire that forbids the feature itself.
    assert not _mentions_threshold("pytest --cov=app --cov-report=xml:artifacts/coverage.xml")

    # The `--cov=app` token matcher accepts the real invocation and REJECTS the
    # narrowings that would walk under #321's count gate.
    assert _COV_TARGET_RE.search('uv run pytest -m "not postgres" --cov=app --cov-report=xml')
    assert _COV_TARGET_RE.search("pytest --cov=app")
    assert not _COV_TARGET_RE.search("pytest --cov=app/api")
    assert not _COV_TARGET_RE.search("pytest --cov=app.worker")
    assert not _COV_TARGET_RE.search("pytest --cov=apps")

    # The Makefile recipe extractor returns a real recipe, not an empty string.
    assert "pytest" in _coverage_recipe()
    # The CI parser finds the job and steps it is supposed to scan.
    jobs = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    assert "test" in jobs and len(jobs["test"]["steps"]) > 3


def test_the_narrowing_check_would_actually_notice() -> None:
    """Partner for the resolved narrowing/exclusion assertions above.

    Those assertions read seven coverage.py attributes. If any were misnamed, the
    lookup would still return something falsy — ``None`` — and the check would pass
    by looking at nothing. So resolve a config that DOES narrow or exclude and
    prove the same expressions see it.

    Discovered through a BARE ``Coverage()`` with the cwd set to a directory
    holding a ``pyproject.toml``, not through ``config_file=``. That is the exact
    path the real assertion uses, and ``pyproject.toml`` is the file this repo
    actually has — an earlier version passed a ``.coveragerc`` explicitly, which
    would have stayed green if coverage.py ever stopped reading pyproject by
    default, leaving the real check blind.
    """
    import coverage

    def _resolve(toml_body: str, attribute: str) -> object:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pyproject.toml").write_text(toml_body, encoding="utf-8")
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                return getattr(coverage.Coverage().config, attribute)
            finally:
                os.chdir(cwd)

    for section, setting, attribute in (
        ("run", "omit", "run_omit"),
        ("run", "include", "run_include"),
        ("run", "source", "source"),
        ("report", "omit", "report_omit"),
        ("report", "include", "report_include"),
    ):
        body = f'[tool.coverage.{section}]\n{setting} = ["app/api/*"]\n'
        assert _resolve(body, attribute), (
            f"coverage.py's `{attribute}` did not pick up a [{section}] {setting} from "
            "a pyproject.toml — the narrowing check above reads an attribute that "
            "never populates, or pyproject discovery has stopped working."
        )

    # The exclusion pair, which is asserted EQUAL to the defaults rather than
    # empty — so the partner has to show the resolved value actually CHANGES.
    widened = _resolve('[tool.coverage.report]\nexclude_also = ["^def "]\n', "exclude_list")
    assert widened != list(coverage.config.DEFAULT_EXCLUDE), (
        "an `exclude_also` in pyproject.toml did not change the resolved exclude_list"
    )
    widened_partial = _resolve(
        '[tool.coverage.report]\npartial_also = ["^while "]\n', "partial_list"
    )
    assert widened_partial != list(coverage.config.DEFAULT_PARTIAL), (
        "a `partial_also` in pyproject.toml did not change the resolved partial_list"
    )


def _pragma_suppressions() -> list[str]:
    """Every ``pragma: no cover`` / ``no branch`` line under ``app/``, as file:line.

    Matched with coverage.py's OWN regexes, selected from the resolved exclusion
    lists by naming "pragma" — so the scanner cannot drift from what coverage
    actually honours, and a hand-written approximation of the pragma syntax cannot
    quietly disagree with it. The structural exclusions in the same lists (an
    ``...`` body, ``if TYPE_CHECKING:``) are deliberately NOT counted: they are
    not deliberate suppressions, adding a Protocol method should not need a
    baseline bump, and they are pinned by the equality assertion above anyway.
    """
    import coverage

    cwd = os.getcwd()
    try:
        os.chdir(_BACKEND)
        config = coverage.Coverage().config
        patterns = [
            re.compile(p)
            for p in list(config.exclude_list) + list(config.partial_list)
            if "pragma" in p.lower()
        ]
    finally:
        os.chdir(cwd)
    assert patterns, "no pragma-shaped exclusion regex in coverage.py's resolved config"

    hits: list[str] = []
    for path in sorted((_BACKEND / "app").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(pattern.search(line) for pattern in patterns):
                hits.append(f"{path.relative_to(_BACKEND)}:{number}")
    return hits


def test_coverage_suppressions_do_not_grow_past_the_recorded_count() -> None:
    """The cheapest walk under the #321 gate, and the one nothing else catches.

    Proven end to end by an adversarial review on a real copy of this branch: add
    a genuinely untested helper to `app/api/routes/health.py` and the count goes
    182 -> 185 and the gate FAILS, as designed. Append ` # pragma: no cover` to its
    `def` line and the very same helper measures 182 again — the lines leave
    `lines-valid` entirely — and the gate prints "PASSED: no increase in uncovered
    lines" and exits 0. Ruff, pyright and every other test stay green.

    `pragma: no cover` is a DEFAULT coverage.py exclusion, so the equality
    assertion in `test_no_coverage_threshold_is_configured_anywhere` cannot help:
    nothing about the configuration changes. What changes is the source. So the
    suppressions get the same treatment as the uncovered lines themselves — a
    recorded count, in the same file, that must not INCREASE.

    This is not a ban. Every one of the existing suppressions is a defensible
    `except Exception:` on a shutdown path. It is a requirement that adding one be
    DELIBERATE: bump `exclusion_pragmas` in `backend/coverage-baseline.json` in the
    same commit and say which line and why, exactly as raising `missed` requires.
    """
    recorded = json.loads(_BASELINE.read_text(encoding="utf-8"))["exclusion_pragmas"]
    found = _pragma_suppressions()
    assert len(found) <= recorded, (
        f"{len(found)} coverage suppressions under app/, against "
        f"{recorded} recorded in {_BASELINE.name}. A `pragma: no cover` removes its "
        "line from `lines-valid` entirely, so uncovered code added under one is "
        "INVISIBLE to the #321 gate — the count stays put and the build goes green. "
        "If the suppression is genuinely right, raise `exclusion_pragmas` in the same "
        f"commit and say why.\nFound:\n  " + "\n  ".join(found)
    )
    # The other direction is a note, not a failure: removing one is an improvement,
    # and the recorded number should follow it down.
    if len(found) < recorded:
        print(
            f"NOTE: {len(found)} suppressions against {recorded} recorded — lower "
            f"`exclusion_pragmas` in {_BASELINE.name} to lock the improvement in."
        )


def test_the_suppression_scanner_finds_the_suppressions_that_are_there() -> None:
    """Partner: a scanner that matched nothing would satisfy `<= recorded` forever.

    This repo HAS suppressions, all of them `except Exception:` on shutdown or
    defensive paths, so the scanner must return a non-empty list of real file:line
    references — and each one must actually contain the pragma when read back.
    """
    found = _pragma_suppressions()
    assert found, (
        "the suppression scanner found NOTHING. Either every `pragma: no cover` was "
        "removed from app/ (in which case lower `exclusion_pragmas` to 0 and delete "
        "this partner), or the scanner is broken and the guard above is vacuous."
    )
    for reference in found:
        path, _, number = reference.rpartition(":")
        line = (_BACKEND / path).read_text(encoding="utf-8").splitlines()[int(number) - 1]
        assert "pragma" in line.lower(), f"{reference} does not hold a pragma: {line!r}"

    # ...and it must NOT fire on ordinary code, or it would be a tripwire that
    # forbids the word rather than the suppression.
    import coverage

    cwd = os.getcwd()
    try:
        os.chdir(_BACKEND)
        patterns = [
            re.compile(p)
            for p in list(coverage.Coverage().config.exclude_list)
            if "pragma" in p.lower()
        ]
    finally:
        os.chdir(cwd)
    for benign in ("x = 1", "# a comment about pragmas", 'raise ValueError("no cover")'):
        assert not any(p.search(benign) for p in patterns), benign


def test_the_merge_gating_job_runs_the_baseline_comparison_after_measuring() -> None:
    """#321's gate must be WIRED into the job that gates merges, and wired after pytest.

    Three separate ways this goes quietly wrong, so three assertions:

    * the step is deleted or renamed away — then nothing compares anything;
    * it is moved into a NEW job, which blocks nothing until the owner adds a
      required status context in the repository settings. Living inside the
      already-required ``pytest + lint`` check is what makes it blocking on day
      one, so the job it sits in is part of the contract, not an implementation
      detail;
    * it runs BEFORE the pytest step that writes ``artifacts/coverage.xml``, in
      which case it reads a stale report from the runner's cache or, more likely,
      fails every run on a missing file.

    What the gate DOES is proven in ``test_coverage_baseline_gate.py`` by running
    the real script against crafted reports. This asserts only that CI calls it.
    """
    steps = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]["test"]["steps"]
    runs = [str(s.get("run", "")) for s in steps]
    gates = [i for i, run in enumerate(runs) if _GATE_SCRIPT in run]
    assert len(gates) == 1, (
        f"expected exactly one `{_GATE_SCRIPT}` step in the `pytest + lint` job, "
        f"found {len(gates)}. Without it nothing compares coverage against the "
        "baseline, and #321's gate is inert."
    )
    measures = [i for i, run in enumerate(runs) if "uv run pytest" in run]
    assert measures and gates[0] > measures[0], (
        "the coverage gate runs BEFORE the pytest step that writes the report it reads"
    )
    assert not _mentions_threshold(runs[gates[0]]), "the baseline gate grew into a threshold"
    # The script it names must exist, or the step fails for the wrong reason and
    # this test would still pass.
    assert (_BACKEND / _GATE_SCRIPT).is_file(), f"{_GATE_SCRIPT} does not exist"

    # The step must also be ABLE to fail the job. Found by review: with only the
    # assertions above, FIVE separate neuterings left the whole suite green —
    # `continue-on-error: true`, `if: ${{ false }}`, appending `|| true`,
    # commenting the body out, and the same `|| true` on the Makefile recipe. A
    # gate that runs and cannot red anything is the "BLOCKING on day one" claim
    # made false while every test agrees.
    assert _cannot_fail_the_job(steps[gates[0]]) is None, _cannot_fail_the_job(steps[gates[0]])
    # ...and the same for the step that MEASURES. Neutering pytest disables the
    # gate just as completely, since a gate with no report to read is a gate that
    # never disagrees with anything.
    assert _cannot_fail_the_job(steps[measures[0]]) is None, _cannot_fail_the_job(
        steps[measures[0]]
    )

    # `defaults.run.working-directory: backend` is what makes the step's relative
    # paths resolve. Without it the script is not found and the failure is a
    # confusing one, not the gate speaking.
    workflow = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    assert workflow["jobs"]["test"]["defaults"]["run"]["working-directory"] == "backend"


def test_ci_uploads_the_report_from_the_path_it_writes() -> None:
    """The report half, which nothing pinned before.

    Deleting the upload step, or pointing it at a path relative to the workspace
    root instead of `backend/`, both leave the job GREEN — `if-no-files-found`
    defaults to `warn` — while nothing is ever uploaded.
    """
    steps = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]["test"]["steps"]
    uploads = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))]
    assert len(uploads) == 1, "the coverage upload step is gone"
    upload_path = uploads[0]["with"]["path"]

    # `defaults.run.working-directory` applies to `run:` steps only, never to an
    # action's inputs — so the written path and the uploaded path are expressed
    # relative to DIFFERENT roots, and must be reconciled explicitly.
    written = _pytest_step()["run"].split("xml:")[1].split()[0]
    assert upload_path == f"backend/{written}", (
        f"pytest writes backend/{written} but the artifact uploads {upload_path!r}"
    )


def test_the_coverage_target_mirrors_make_test() -> None:
    recipe = _coverage_recipe()
    for fragment in ('-m "not postgres"', "env -u CITEVYN_DATABASE_URL"):
        assert fragment in recipe, (
            f"`make coverage` no longer mirrors `make test` on {fragment!r} — the "
            "reported number would describe a different run than the one people use. "
            "(`env -u` is what makes it hermetic: a local backend/.env changes which "
            "tests pass, and so changes the number.)"
        )
    assert _COV_TARGET_RE.search(recipe) and "coverage.xml" in recipe


def test_a_local_target_runs_the_same_comparison_ci_runs() -> None:
    """`make coverage-gate` must call the same script, so CI is not the only place.

    Deliberately a SEPARATE target from `make coverage`: a local checkout holding a
    `backend/.env` fails several tests for an environment reason unrelated to any
    change, which leaves lines unreached and would red the gate for nobody's
    mistake. `coverage` stays the plain report; this is the opt-in parity check.
    """
    text = _MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^coverage-gate:.*?(?=^\S)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, "the `coverage-gate:` target is gone — CI is now the only place it runs"
    recipe = match.group(0)
    assert _GATE_SCRIPT in recipe, "`make coverage-gate` no longer runs the comparison script"
    assert not _mentions_threshold(recipe), "`make coverage-gate` grew a threshold"
    # ...and it must still be able to FAIL. `cd backend && uv run ... || true` runs
    # the gate, prints the verdict, and exits 0.
    assert _cannot_fail_the_job({"run": recipe}) is None, _cannot_fail_the_job({"run": recipe})


def test_the_defusal_detector_would_actually_notice() -> None:
    """Partner for `_cannot_fail_the_job`: prove each shape it claims to catch is caught.

    Without this it could be returning ``None`` for everything, and every assertion
    that uses it would pass by agreeing with nothing. These are the five neuterings
    an adversarial review landed on the real workflow while the whole suite stayed
    green.
    """
    real = {"run": "uv run python scripts/coverage_baseline.py"}
    assert _cannot_fail_the_job(real) is None, "the detector rejects the REAL step"

    for neutered in (
        {**real, "continue-on-error": True},
        {**real, "if": "${{ false }}"},
        {"run": "uv run python scripts/coverage_baseline.py || true"},
        {"run": "uv run python scripts/coverage_baseline.py ; true"},
        {"run": "set +e\nuv run python scripts/coverage_baseline.py"},
        {"run": "# uv run python scripts/coverage_baseline.py"},
        {"run": ""},
    ):
        assert _cannot_fail_the_job(neutered) is not None, neutered

    # `continue-on-error: false` is the explicit, harmless spelling and must pass.
    assert _cannot_fail_the_job({**real, "continue-on-error": False}) is None


def test_pytest_cov_is_a_declared_dev_dependency() -> None:
    # It was measurable before this only via an ad-hoc `uv run --with pytest-cov`,
    # which is what #308 was filed about. Read the parsed dependency LIST rather
    # than grepping the file, so a comment mentioning the package cannot satisfy it.
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    dev = pyproject["dependency-groups"]["dev"]
    assert any(d.startswith("pytest-cov") for d in dev), dev
