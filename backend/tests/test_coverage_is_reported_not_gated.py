"""Coverage is measured, and must stay a REPORT rather than a gate (#308).

The repo's own scar is why this file exists: a line in ``app/worker/promotion_eval.py``
measured **97% covered** and was **executed by the suite**, yet deleting it left all
42 promotion tests green. Coverage shows which lines RAN, never which are CHECKED —
so a coverage threshold would have said nothing about that defect while inviting
tests written to move a number.

#308 therefore ships this deliberately advisory, and `AGENTS.md` says an advisory
gate must state what makes it blocking *in the same commit*. These tests pin both
halves: that the measurement really happens, and that it cannot quietly become a
threshold without someone deleting an assertion that says why not.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_CI = _REPO / ".github" / "workflows" / "ci.yml"
_MAKEFILE = _REPO / "Makefile"
_PYPROJECT = _REPO / "backend" / "pyproject.toml"


def _pytest_step() -> dict:
    jobs = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    steps = jobs["test"]["steps"]
    matches = [s for s in steps if "pytest" in str(s.get("run", "")).lower()]
    assert len(matches) == 1, f"expected exactly one pytest step, found {len(matches)}"
    return matches[0]


def test_ci_measures_coverage_on_the_suite_that_gates_merges() -> None:
    run = _pytest_step()["run"]
    assert "--cov=app" in run, "the merge-gating pytest run no longer measures coverage"
    assert "coverage.xml" in run, "no machine-readable report is produced"
    # Same invocation as the suite itself: a different marker set would measure a
    # different program and the number would describe something nobody runs.
    assert '-m "not postgres"' in run


def test_coverage_cannot_silently_become_a_gate() -> None:
    """The load-bearing one.

    `--cov-fail-under` is the single flag that turns this report into a threshold.
    If someone adds it, they have to delete this test — and the docstring above
    tells them what they are trading away, and what #308 said the blocking
    condition should actually be (no DECREASE vs a baseline stable across three
    consecutive `main` runs, never an absolute floor).
    """
    flag = "--cov-fail-" + "under"  # split so this file is not its own false positive

    # CI: check the parsed `run` values, not the raw file — a COMMENT explaining
    # why the flag is absent must not read as the flag being present. (The first
    # version of this test failed on exactly that.)
    jobs = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            assert flag not in str(step.get("run", "")), (
                f"ci.yml job {job_name!r} sets {flag}, which makes coverage BLOCKING"
            )

    # Makefile and pyproject: strip comment lines for the same reason.
    for path in (_MAKEFILE, _PYPROJECT):
        code = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        assert flag not in code, (
            f"{path.name} sets {flag}, which makes coverage BLOCKING. See #308: an "
            "absolute floor invites tests written to raise a number rather than to "
            "catch a defect."
        )


def test_the_coverage_target_exists_and_mirrors_make_test() -> None:
    makefile = _MAKEFILE.read_text(encoding="utf-8")
    assert "\ncoverage:" in makefile, "`make coverage` is gone"
    # Partner assertion: `make test` is what it must mirror, so if that invocation
    # ever changes shape this comparison is what notices.
    for fragment in ('-m "not postgres"', "env -u CITEVYN_DATABASE_URL"):
        assert makefile.count(fragment) >= 2, (
            f"`make coverage` no longer mirrors `make test` on {fragment!r} — the "
            "reported number would describe a different run than the one people use"
        )


def test_pytest_cov_is_a_declared_dev_dependency() -> None:
    # It was measurable before this only via an ad-hoc `uv run --with pytest-cov`,
    # which is what #308 was filed about.
    assert "pytest-cov" in _PYPROJECT.read_text(encoding="utf-8")
