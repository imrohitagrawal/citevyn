"""The judged CI job's grader-check run (#598 Step 3).

Written before the implementation (test designer). The contract for
``.github/workflows/ci.yml``:

* ``on.workflow_dispatch.inputs.grader_check``: ``type: boolean``, ``default: false``.
  Push and tag runs have no inputs, so the value renders empty there.
* The switch (``CITEVYN_EVAL_PASSAGE_GRADING``) and the spend cap
  (``CITEVYN_COST_HARD_DAILY_USD``) are NEVER in an ``env:`` block: an
  expression that renders empty makes Settings refuse to load. The eval step's
  own script exports both, and only when the input is ``true``; its last line
  is still exactly the eval command. The input may reach the script as
  ``${{ inputs.grader_check }}``, directly or through a step ``env:`` entry that
  is not a CITEVYN_ setting.
* Limits on the input, as ``${{ inputs.grader_check && ON || OFF }}`` (the
  boolean ``inputs`` context: ``github.event.inputs`` is a string, and the
  string "false" is truthy): job ``timeout-minutes`` 245 on / 68 off, eval step
  240 on / 64 off. The release wait stays 4500 s (tag runs never set the input).
* A step after the eval, ``if:`` on ``always()``, the input and the guard,
  ``working-directory: backend``, runs
  ``uv run python -m tests.eval.spend_export --out ../artifacts/spend.json``
  (optionally ``--report ../artifacts/eval_report.json``); the upload step
  uploads ``artifacts/spend.json`` with the report.
* The ref guard (main, ``v*``) and the UTC-window refusal are the runner's
  (``tests/eval/grader_check.py``), at the eval step's start, before any paid
  call; see ``test_grader_check_decision.py`` and ``test_grader_check_runner.py``.

Each test names the change that turns it red.
"""

from __future__ import annotations

import math
import os
import pathlib
import re
import stat
import subprocess
from typing import Any

import pytest
import yaml

from app.core.config import Settings
from tests.eval.parity import PREFIX

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
WAIT_SCRIPT = REPO_ROOT / "scripts" / "wait_for_judged_eval.sh"
JUDGED_JOB = "answer-quality-eval"
EVAL_COMMAND = [
    "uv",
    "run",
    "python",
    "-m",
    "tests.eval.runner",
    "--postgres",
    "--production-parity",
    "--report",
    "../artifacts/eval_report.json",
]
EXPORT_COMMAND = [
    "uv",
    "run",
    "python",
    "-m",
    "tests.eval.spend_export",
    "--out",
    "../artifacts/spend.json",
]
_CONDITIONAL = re.compile(r"^\$\{\{\s*inputs\.grader_check\s*&&\s*(\d+)\s*\|\|\s*(\d+)\s*\}\}$")


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


def _job() -> dict[str, Any]:
    return _workflow()["jobs"][JUDGED_JOB]


def _eval_step() -> dict[str, Any]:
    (step,) = [s for s in _job()["steps"] if "tests.eval.runner" in str(s.get("run", ""))]
    return step


def _conditional(value: Any) -> tuple[int, int]:
    match = _CONDITIONAL.match(str(value).strip())
    assert match, f"not a limit on the grader_check input: {value!r}"
    return int(match.group(1)), int(match.group(2))


# --- the input ------------------------------------------------------------------------


def test_workflow_dispatch_has_a_boolean_grader_check_input_off_by_default() -> None:
    """Red when: the input is missing, not a boolean (a string "false" is truthy in
    an expression), or on by default."""
    triggers = _workflow().get(True, _workflow().get("on"))
    grader_check = (triggers["workflow_dispatch"] or {})["inputs"]["grader_check"]
    assert grader_check["type"] == "boolean"
    assert grader_check["default"] is False


# --- the limits -------------------------------------------------------------------------


def test_the_job_and_step_limits_depend_on_the_input() -> None:
    """Red when: the longer limits apply to every run (a release run could then
    hang 4 hours), or the experiment run keeps the short ones (it would be cut
    off), or the off values change."""
    assert _conditional(_job()["timeout-minutes"]) == (245, 68)
    assert _conditional(_eval_step()["timeout-minutes"]) == (240, 64)


def test_the_release_wait_is_unchanged_and_the_off_limit_fits_inside_it() -> None:
    """Red when: the release wait is raised for the experiment (tag runs never set
    it), or the off-path job limit no longer leaves the 6-minute ``test`` job
    inside the wait."""
    match = re.search(r"TOTAL_TIMEOUT_SECONDS-(\d+)", WAIT_SCRIPT.read_text(encoding="utf-8"))
    assert match, "the wait script no longer defaults TOTAL_TIMEOUT_SECONDS"
    assert int(match.group(1)) == 4500
    _on, off = _conditional(_job()["timeout-minutes"])
    assert off + 6 <= int(match.group(1)) / 60


def test_the_experiment_job_fits_inside_the_utc_window_the_runner_demands() -> None:
    """Red when: the on-path job limit exceeds the minutes the runner requires
    before 00:00 UTC, so a run that starts could still cross midnight (the spend
    cap's daily window would reset mid-run)."""
    from tests.eval import grader_check  # type: ignore[attr-defined]

    on, _off = _conditional(_job()["timeout-minutes"])
    assert on <= grader_check.MIN_MINUTES_BEFORE_UTC_MIDNIGHT


# --- the eval step, rendered for each kind of run --------------------------------------


_SCENARIOS = {
    "push-main": ("", "refs/heads/main"),
    "tag": ("", "refs/tags/v0.12.0"),
    "dispatch-off": ("false", "refs/heads/eval/598-grader-check"),
    "dispatch-on": ("true", "refs/heads/eval/598-grader-check"),
}


def _render(text: str, *, grader_check: str, ref: str) -> str:
    text = re.sub(r"\$\{\{\s*inputs\.grader_check\s*\}\}", grader_check, text)
    text = re.sub(r"\$\{\{\s*github\.ref\s*\}\}", ref, text)
    text = re.sub(r"\$\{\{\s*secrets\.[A-Z0-9_]+\s*\}\}", "placeholder-not-a-key", text)
    assert "${{" not in text, f"an expression this test cannot render: {text!r}"
    return text


def _run_eval_step(tmp: pathlib.Path, grader_check: str, ref: str) -> tuple[dict[str, str], str]:
    """Run the eval step's script as Actions would (bash -e), with a fake ``uv``
    that records the environment and arguments it was started with."""
    step = _eval_step()
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text(
        '#!/usr/bin/env bash\nenv > "$CAPTURE_ENV"\nprintf "%s\\n" "$@" > "$CAPTURE_ARGS"\n',
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(tmp),
        "GITHUB_REF": ref,
        "CAPTURE_ENV": str(tmp / "env.txt"),
        "CAPTURE_ARGS": str(tmp / "args.txt"),
    }
    for block in (_job().get("env") or {}, step.get("env") or {}):
        for name, value in block.items():
            env[str(name)] = _render(str(value), grader_check=grader_check, ref=ref)
    script = tmp / "step.sh"
    script.write_text(
        _render(str(step["run"]), grader_check=grader_check, ref=ref), encoding="utf-8"
    )
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    captured: dict[str, str] = {}
    for line in (tmp / "env.txt").read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        captured[name] = value
    return captured, (tmp / "args.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
def test_each_rendering_of_the_eval_step_loads_settings_and_sets_the_switch_only_when_asked(
    scenario: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red when: a push, tag or dispatch-off run carries the switch or the cap (or
    an empty value for either, which makes Settings refuse to load), or a
    dispatch-on run does not set both, or the eval command changes."""
    grader_check, ref = _SCENARIOS[scenario]
    captured, args = _run_eval_step(tmp_path, grader_check, ref)
    assert args.split("\n")[:-1] == EVAL_COMMAND[1:]  # partner: the eval really started
    for name in list(os.environ):
        if name.upper().startswith(PREFIX):
            monkeypatch.delenv(name, raising=False)
    for name, value in captured.items():
        if name.upper().startswith(PREFIX):
            monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    on = scenario == "dispatch-on"
    assert ("CITEVYN_EVAL_PASSAGE_GRADING" in captured) is on, sorted(captured)
    assert ("CITEVYN_COST_HARD_DAILY_USD" in captured) is on, sorted(captured)
    assert settings.eval_passage_grading is on  # type: ignore[attr-defined]
    if on:
        cap = settings.cost_hard_daily_usd
        assert (
            math.isfinite(cap) and 0 < cap < Settings.model_fields["cost_hard_daily_usd"].default
        ), cap
    else:
        assert settings.cost_hard_daily_usd == Settings.model_fields["cost_hard_daily_usd"].default


def test_the_eval_steps_last_line_is_exactly_the_eval_command() -> None:
    """Red when: anything follows the eval command or is chained to it (``|| true``,
    ``; exit 0``), or the step stops failing on the command's exit code."""
    step = _eval_step()
    lines = [ln.strip() for ln in str(step["run"]).strip().splitlines() if ln.strip()]
    assert " ".join(lines[-1].split()) == " ".join(EVAL_COMMAND)
    before = "\n".join(lines[:-1])
    for banned in ("||", "exit 0", "trap", "set +e"):
        assert banned not in before, banned
    assert "inputs.grader_check" in str(step["run"]) + str(step.get("env", {}))


# --- the spend export step -----------------------------------------------------------------


def test_a_spend_export_step_runs_after_the_eval_only_for_a_grader_check() -> None:
    """Red when: the spend is not exported when the eval fails or is cut off
    (``always()``), is exported on every tag run, runs before the eval, or runs
    another command than the one its test runs."""
    steps = _job()["steps"]
    exports = [i for i, s in enumerate(steps) if "tests.eval.spend_export" in str(s.get("run", ""))]
    assert len(exports) == 1, exports
    index = exports[0]
    step = steps[index]
    assert index > steps.index(_eval_step())
    condition = str(step.get("if", ""))
    for part in ("always()", "inputs.grader_check", "steps.guard.outputs.run == 'true'"):
        assert part in condition, condition
    assert step.get("working-directory") == "backend"
    tokens = str(step["run"]).split()
    assert tokens[: len(EXPORT_COMMAND)] == EXPORT_COMMAND
    assert tokens[len(EXPORT_COMMAND) :] in ([], ["--report", "../artifacts/eval_report.json"])
    assert "continue-on-error" not in step or step["continue-on-error"] is False


def test_the_upload_step_uploads_the_spend_export_with_the_report() -> None:
    """Red when: spend.json is written but never uploaded, so the measured spend
    is lost with the runner."""
    steps = _job()["steps"]
    (upload,) = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))]
    paths = str(upload["with"]["path"]).split()
    assert "artifacts/eval_report.json" in paths  # partner: the report still goes up
    assert "artifacts/spend.json" in paths
    export = next(s for s in steps if "tests.eval.spend_export" in str(s.get("run", "")))
    assert steps.index(upload) > steps.index(export)
