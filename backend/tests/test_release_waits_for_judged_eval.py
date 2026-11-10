"""A release tag must not publish images until the judged eval has passed (#457).

These read the real workflow files and the real wait script. They are YAML pins,
not a run of the pipeline: what they buy is that weakening the wiring fails the
fast required test job on the pull request that does it. The behaviour of the
wait itself is tested in ``tests/shell/test_wait_for_judged_eval.sh``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_gating_workflows import _disables_the_gate

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
SCRIPT = REPO_ROOT / "scripts" / "wait_for_judged_eval.sh"
GATE = "judged-eval-gate"


def _load(path: Path) -> dict[Any, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path.name} did not parse to a mapping"
    return loaded


def _release_jobs() -> dict[str, Any]:
    return _load(WORKFLOW_DIR / "release.yml")["jobs"]


def _gate() -> dict[str, Any]:
    jobs = _release_jobs()
    assert GATE in jobs, f"release.yml has no `{GATE}` job, so a tag publishes unjudged"
    return jobs[GATE]


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _script_text() -> str:
    assert SCRIPT.exists(), f"{SCRIPT.name} is missing, so the gate has nothing to run"
    return SCRIPT.read_text(encoding="utf-8")


@pytest.mark.parametrize("job_id", ["build", "release"])
def test_publishing_jobs_wait_for_the_gate(job_id: str) -> None:
    """Red when: ``build`` or ``release`` stops needing the gate."""
    _gate()
    needs = _needs(_release_jobs()[job_id])
    assert GATE in needs, f"{job_id} does not need {GATE}, so it runs without the judged eval"
    assert "validate" in needs, f"{job_id} lost its need on the tag validation"


def test_the_gate_runs_after_validation_and_runs_the_script() -> None:
    """Red when: the gate stops running the script, or tolerates its failure."""
    gate = _gate()
    assert "validate" in _needs(gate)
    runs = [str(step["run"]) for step in gate["steps"] if "run" in step]
    assert len(runs) == 1, f"expected exactly one script step, found {runs}"
    assert runs[0].strip() == "bash scripts/wait_for_judged_eval.sh", (
        "the gate's step must be the bare script: anything appended can swallow its exit code"
    )
    assert any(str(step.get("uses", "")).startswith("actions/checkout@") for step in gate["steps"])


def test_the_gate_cannot_be_turned_off() -> None:
    """Red when: ``continue-on-error``, an ``if:`` or a skip input appears on the path.

    An ``if: always()`` on ``build`` would run it after a failed gate, and an ``if:``
    on the gate could skip it, so neither is allowed.
    """
    jobs = _release_jobs()
    gate = _gate()
    assert "continue-on-error" not in gate or not _disables_the_gate(gate["continue-on-error"])
    assert gate["steps"], "the gate has no steps, so the checks below read nothing"
    for step in gate["steps"]:
        assert "continue-on-error" not in step or not _disables_the_gate(step["continue-on-error"])
        assert "if" not in step, f"a gate step is conditional: {step}"
    for job_id in (GATE, "build", "release"):
        assert "if" not in jobs[job_id], f"{job_id} has an if:, which can bypass the gate"

    workflow = _load(WORKFLOW_DIR / "release.yml")
    triggers = workflow.get(True, workflow.get("on"))
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"version"}, (
        "release.yml grew a dispatch input; the judged-eval gate has no skip or override"
    )


def test_the_gate_has_read_only_permissions_and_its_inputs() -> None:
    """Red when: the gate's token widens, or an input the script requires is dropped."""
    gate = _gate()
    assert gate["permissions"] == {"contents": "read", "actions": "read"}
    assert gate["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "SHA": "${{ github.sha }}",
        "REPO": "${{ github.repository }}",
    }


def test_the_gate_outlasts_the_scripts_own_timeout() -> None:
    """Red when: the job is cut below the script's timeout, losing its explanation."""
    match = re.search(r"TOTAL_TIMEOUT_SECONDS-(\d+)", _script_text())
    assert match, "the script no longer defaults TOTAL_TIMEOUT_SECONDS"
    gate = _gate()
    assert gate["timeout-minutes"] == 90
    assert gate["timeout-minutes"] * 60 > int(match.group(1))


def test_the_script_waits_for_the_judged_jobs_real_name() -> None:
    """Red when: the ``name:`` of ``answer-quality-eval`` and the script's name differ.

    The script matches the name exactly. Renaming the job alone would make every
    release fail at the grace period, for a reason nobody would look for.
    """
    match = re.search(r"^JUDGED_JOB_NAME='([^']+)'$", _script_text(), re.MULTILINE)
    assert match, "the script no longer declares JUDGED_JOB_NAME as a plain literal"
    job = _load(WORKFLOW_DIR / "ci.yml")["jobs"]["answer-quality-eval"]
    assert match.group(1) == job["name"]
    assert "${{" not in job["name"], "an expression in the name cannot be matched exactly"
    assert "strategy" not in job, "a matrix appends values to the job's name"


def test_the_script_requires_the_real_eval_steps_name() -> None:
    """Red when: the eval step's ``name:`` in ci.yml and the script's name differ.

    A job marked success proves nothing on its own, because the job stays green
    when its guard skips every step. The script therefore reads this one step, and
    renaming it alone would make every release fail as "the eval did not run".
    """
    match = re.search(r"^JUDGED_STEP_NAME='([^']+)'$", _script_text(), re.MULTILINE)
    assert match, "the script no longer declares JUDGED_STEP_NAME as a plain literal"
    steps = _load(WORKFLOW_DIR / "ci.yml")["jobs"]["answer-quality-eval"]["steps"]
    named = [step for step in steps if step.get("name") == match.group(1)]
    assert len(named) == 1, f"expected one step named {match.group(1)!r}, found {len(named)}"
    assert "tests.eval.runner" in str(named[0]["run"]), (
        "the step the gate reads no longer runs the judged eval"
    )
