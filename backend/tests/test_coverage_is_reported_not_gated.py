"""Coverage is measured, and must stay a REPORT rather than a gate (#308).

The repo's own scar is why this file exists: a line in ``app/worker/promotion_eval.py``
measured **97% covered** and was **executed by the suite**, yet deleting it left all
42 promotion tests green. Coverage shows which lines RAN, never which are CHECKED —
so a coverage threshold would have said nothing about that defect while inviting
tests written to move a number.

#308 therefore ships this deliberately advisory. The project practice is that a gate
shipped advisory says, in the same commit, what would make it blocking — that is a
working convention, NOT a rule in `AGENTS.md`, which an earlier version of this
docstring wrongly claimed. The condition is recorded in
`.github/workflows/ci.yml` beside the step, and is:
only once a baseline is stable across three consecutive `main` runs, and then as
no-DECREASE against it — never an absolute floor.

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
import os
import re
import tomllib
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_CI = _REPO / ".github" / "workflows" / "ci.yml"
_MAKEFILE = _REPO / "Makefile"
_PYPROJECT = _REPO / "backend" / "pyproject.toml"
_BACKEND = _REPO / "backend"


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
    matches = [s for s in steps if "pytest" in str(s.get("run", "")).lower()]
    assert len(matches) == 1, f"expected exactly one pytest step, found {len(matches)}"
    return matches[0]


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
    assert "--cov=app" in run, "the merge-gating pytest run no longer measures coverage"
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
        resolved = coverage.Coverage().config.fail_under
    finally:
        os.chdir(cwd)
    assert resolved == 0, (
        f"coverage.py resolves fail_under={resolved}. pytest-cov adopts that as "
        "--cov-fail-under (see pytest_cov/plugin.py), which makes coverage BLOCKING."
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
    for name, job in yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"].items():
        for step in job.get("steps", []):
            assert not _mentions_threshold(str(step.get("run", ""))), (
                f"ci.yml job {name!r} runs a coverage threshold"
            )
            for key, value in (step.get("env") or {}).items():
                assert not _mentions_threshold(str(value)), (
                    f"ci.yml job {name!r} sets {key}, imposing a coverage threshold"
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

    # The Makefile recipe extractor returns a real recipe, not an empty string.
    assert "pytest" in _coverage_recipe()
    # The CI parser finds the job and steps it is supposed to scan.
    jobs = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    assert "test" in jobs and len(jobs["test"]["steps"]) > 3


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
    assert "--cov=app" in recipe and "coverage.xml" in recipe


def test_pytest_cov_is_a_declared_dev_dependency() -> None:
    # It was measurable before this only via an ad-hoc `uv run --with pytest-cov`,
    # which is what #308 was filed about. Read the parsed dependency LIST rather
    # than grepping the file, so a comment mentioning the package cannot satisfy it.
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    dev = pyproject["dependency-groups"]["dev"]
    assert any(d.startswith("pytest-cov") for d in dev), dev
