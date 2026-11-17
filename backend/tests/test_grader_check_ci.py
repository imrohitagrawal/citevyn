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
* #598 Step 4 (plan Revision 4): every ``workflow_dispatch`` run is capped. With
  the input off, the eval step's own script exports ``CITEVYN_COST_HARD_DAILY_USD``
  from a ``CAP_USD`` line (0.14, the approved per-run limit less one answer call);
  with it on, the grader check's 0.43 as before. Push and tag runs export neither.
  The spend export step runs on every dispatch (``github.event_name ==
  'workflow_dispatch'``), never on push or tag runs.
* Limits on the input, as ``${{ inputs.grader_check && ON || OFF }}`` (the
  boolean ``inputs`` context: ``github.event.inputs`` is a string, and the
  string "false" is truthy): job ``timeout-minutes`` 35 on / 35 off, eval step
  30 on / 30 off. The release wait stays 4500 s (tag runs never set the input).
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

import json
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
    """Red when: a limit stops being read from the ``grader_check`` input, or either
    path's value moves away from the measured one (job 35, eval step 30 on both
    today: twice each path's measured duration, rounded up to 5 minutes)."""
    assert _conditional(_job()["timeout-minutes"]) == (35, 35)
    assert _conditional(_eval_step()["timeout-minutes"]) == (30, 30)


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
# The event behind each scenario (#598 Step 4: the switch-off dispatch is told apart
# from push and tag runs by it). Tag runs are ``push`` events too.
_EVENTS = {
    "push-main": "push",
    "tag": "push",
    "dispatch-off": "workflow_dispatch",
    "dispatch-on": "workflow_dispatch",
}
# The cap a switch-off dispatch runs under (plan Revision 4 item 2): the approved
# per-run limit less one answer call at max_tokens. Changes only with a new approval.
SWITCH_OFF_CAP_USD = "0.14"
# The grader check's own cap, unchanged.
SWITCH_ON_CAP_USD = "0.43"


def _event(ref: str, grader_check: str) -> str:
    return "workflow_dispatch" if grader_check else "push"


# The capped baseline (#598 Step 4, round 2): the switch-off cap and the 70-minute
# window apply only to a dispatch that sets the boolean ``capped_baseline`` input. The
# eval step reads it from the event payload (``$GITHUB_EVENT_PATH``, ``.inputs``), never
# through ``env:``, and accepts JSON ``true`` or the string "true". ``_ABSENT`` is a
# dispatch that does not set it (and every push or tag run, which has no inputs).
_ABSENT = object()


def _payload(grader_check: str, ref: str, capped: Any) -> dict[str, Any]:
    """The event payload Actions writes at ``$GITHUB_EVENT_PATH`` for one run."""
    if _event(ref, grader_check) != "workflow_dispatch":
        return {"ref": ref}
    inputs: dict[str, Any] = {"grader_check": grader_check == "true"}
    if capped is not _ABSENT:
        inputs["capped_baseline"] = capped
    return {"ref": ref, "inputs": inputs}


def _render(text: str, *, grader_check: str, ref: str) -> str:
    text = re.sub(r"\$\{\{\s*inputs\.grader_check\s*\}\}", grader_check, text)
    text = re.sub(r"\$\{\{\s*github\.ref\s*\}\}", ref, text)
    text = re.sub(r"\$\{\{\s*github\.event_name\s*\}\}", _event(ref, grader_check), text)
    text = re.sub(r"\$\{\{\s*secrets\.[A-Z0-9_]+\s*\}\}", "placeholder-not-a-key", text)
    assert "${{" not in text, f"an expression this test cannot render: {text!r}"
    return text


def _run_eval_step(
    tmp: pathlib.Path, grader_check: str, ref: str, *, capped: Any = _ABSENT
) -> tuple[dict[str, str], str]:
    """Run the eval step's script as Actions would (bash -e), with a fake ``uv``
    that records the environment and arguments it was started with, and the event
    payload at ``$GITHUB_EVENT_PATH`` (``capped``: the ``capped_baseline`` input)."""
    step = _eval_step()
    event_path = tmp / "event.json"
    event_path.write_text(json.dumps(_payload(grader_check, ref, capped)), encoding="utf-8")
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
        "GITHUB_EVENT_NAME": _event(ref, grader_check),
        "GITHUB_EVENT_PATH": str(event_path),
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


BRANCH = "refs/heads/eval/598-grader-check"
# name: (grader_check input, ref, capped_baseline input, the cap expected in the env).
_RENDERINGS: dict[str, tuple[str, str, Any, str | None]] = {
    "push-main": ("", "refs/heads/main", _ABSENT, None),
    "tag": ("", "refs/tags/v0.12.0", _ABSENT, None),
    "dispatch-off": ("false", BRANCH, _ABSENT, None),
    "dispatch-off-input-false": ("false", BRANCH, False, None),
    "dispatch-off-on-a-tag": ("false", "refs/tags/v0.12.0", _ABSENT, None),
    "dispatch-off-capped": ("false", BRANCH, True, SWITCH_OFF_CAP_USD),
    "dispatch-off-capped-string": ("false", BRANCH, "true", SWITCH_OFF_CAP_USD),
    "dispatch-on": ("true", BRANCH, _ABSENT, SWITCH_ON_CAP_USD),
}


@pytest.mark.parametrize("rendering", list(_RENDERINGS))
def test_each_rendering_of_the_eval_step_loads_settings_and_sets_the_switch_only_when_asked(
    rendering: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red when: a push or tag run carries the switch or a cap (or an empty value for
    either, which makes Settings refuse to load); a switch-off dispatch WITHOUT the
    ``capped_baseline`` input (on a branch or on a tag: a release re-run the gate
    accepts) is capped or marked capped; a switch-off dispatch WITH it (JSON true or
    the string "true") is not capped at the CAP_USD value or not marked
    ``CITEVYN_EVAL_CAPPED_DISPATCH=true``; a dispatch-on run does not set both at the
    grader check's cap; or the eval command changes."""
    grader_check, ref, capped, expected = _RENDERINGS[rendering]
    captured, args = _run_eval_step(tmp_path, grader_check, ref, capped=capped)
    assert args.split("\n")[:-1] == EVAL_COMMAND[1:]  # partner: the eval really started
    for name in list(os.environ):
        if name.upper().startswith(PREFIX):
            monkeypatch.delenv(name, raising=False)
    for name, value in captured.items():
        if name.upper().startswith(PREFIX):
            monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    on = rendering == "dispatch-on"
    assert ("CITEVYN_EVAL_PASSAGE_GRADING" in captured) is on, sorted(captured)
    assert captured.get("CITEVYN_COST_HARD_DAILY_USD") == expected, sorted(captured)
    capped_run = expected == SWITCH_OFF_CAP_USD
    assert (captured.get("CITEVYN_EVAL_CAPPED_DISPATCH") == "true") is capped_run, sorted(captured)
    if not capped_run:
        assert "CITEVYN_EVAL_CAPPED_DISPATCH" not in captured, sorted(captured)
    assert settings.eval_passage_grading is on  # type: ignore[attr-defined]
    default = Settings.model_fields["cost_hard_daily_usd"].default
    if expected is not None:
        cap = settings.cost_hard_daily_usd
        assert math.isfinite(cap) and 0 < cap < default, cap
        assert settings.cost_budget_enabled is True
    else:
        assert settings.cost_hard_daily_usd == default


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


def _evaluate_if(condition: str, *, scenario: str, guard: bool = True) -> bool:
    """Evaluate a step's ``if:`` the way Actions would for one scenario. Only the
    names this job's conditions use are known; anything else fails the test."""
    grader_check, ref = _SCENARIOS[scenario]
    names = {
        "always()": "True",
        "success()": "True",
        "github.event_name": repr(_EVENTS[scenario]),
        "github.ref": repr(ref),
        "inputs.grader_check": {"true": "True", "false": "False", "": "None"}[grader_check],
        "steps.guard.outputs.run": repr("true" if guard else "false"),
    }
    expr = condition.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2]
    for name, value in names.items():
        expr = expr.replace(name, value)
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"!(?!=)", " not ", expr)
    expr = re.sub(r"startsWith\(", "_starts(", expr)
    leftover = re.findall(r"[A-Za-z_][\w.]*\.[\w.]+", re.sub(r"'[^']*'", "''", expr))
    assert not leftover, f"a name this test cannot evaluate: {leftover} in {condition!r}"
    return bool(eval(expr, {"__builtins__": {}, "_starts": str.startswith}))  # noqa: S307


def test_the_spend_export_step_runs_after_the_eval_on_every_dispatch() -> None:
    """Red when (#598 Step 4, A8): a switch-off dispatch does not export its spend, a
    tag or push run exports (they gain no step), the export no longer runs after a
    failed or cut-off eval (``always()``), runs without the guard, runs before the
    eval, or runs another command than the one its test runs."""
    steps = _job()["steps"]
    exports = [i for i, s in enumerate(steps) if "tests.eval.spend_export" in str(s.get("run", ""))]
    assert len(exports) == 1, exports
    index = exports[0]
    step = steps[index]
    assert index > steps.index(_eval_step())
    condition = str(step.get("if", ""))
    assert "always()" in condition, condition
    runs = {scenario: _evaluate_if(condition, scenario=scenario) for scenario in _SCENARIOS}
    assert runs == {
        "push-main": False,
        "tag": False,
        "dispatch-off": True,
        "dispatch-on": True,
    }, (condition, runs)
    for scenario in _SCENARIOS:
        assert _evaluate_if(condition, scenario=scenario, guard=False) is False, scenario
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
