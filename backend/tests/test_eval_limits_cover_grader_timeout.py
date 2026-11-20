"""The judged job's limits: the runner's deadline must stop the run before the step limit.

Written before the implementation (test designer). Every value is read the way the
runner reads it: the eval step's script is run for each kind of run
(``_run_eval_step`` in ``test_grader_check_ci.py``, a fake ``uv`` capturing the
environment), and Settings is loaded from that environment. No value is read from
ci.yml's text.

On every rendering (all of them are judged runs):

    D + S + C + T <= step limit x 60

* D: ``eval_deadline_seconds``, as Settings loaded it for that rendering.
* S: start-up slack before the runner's clock starts (shell, settings, the parity
  check). The builder measures it from the step log timestamps of a recorded judged
  run and adds 60 s; it is the one constant ``runner.DEADLINE_STARTUP_SLACK_SECONDS``.
* C: the worst case of the one case still running at D: the most answer calls one
  case made in a recorded judged report (the one constant
  ``runner.DEADLINE_MAX_ANSWER_CALLS_PER_CASE``, set by the builder from that
  report) x ``eval_call_total_timeout_seconds`` (as loaded), + G.
* G: ``eval_grading_budget_seconds`` (as loaded), at least the largest single
  panel's calls (``panel_size() + 1``, the panel as the rendering sets it) x the
  grader timeout (as loaded) + 60 s.
* T: the teardown reserve, the one constant
  ``runner.DEADLINE_TEARDOWN_RESERVE_SECONDS``, at least 120 s.

This replaces the estimate of the slowest measured day (#637): with every paid
call capped, the run's overrun past D is one case plus teardown, so the step limit
is only a backstop.

Each test carries one ``RED WHEN`` line naming the change that turns it red.
"""

from __future__ import annotations

import math
import os
import pathlib
import re

import pytest

from app.core.config import Settings
from tests.eval import grader_check as gc
from tests.eval import judge as judge_mod
from tests.eval import runner as runner_mod
from tests.eval.parity import PREFIX
from tests.test_grader_check_ci import (
    _RENDERINGS,
    CI_YML,
    WAIT_SCRIPT,
    _conditional,
    _eval_step,
    _job,
    _run_eval_step,
)

#: The `test` job that runs before the judged job (6 min 57 s measured 2026-11-17).
TEST_JOB_MINUTES = 7
#: The plan's teardown reserve and the margin the budget keeps over one panel.
MIN_TEARDOWN_SECONDS = 120
BUDGET_MARGIN_SECONDS = 60
#: The builder adds this to the measured start-up time.
STARTUP_MARGIN_SECONDS = 60


def _loaded(
    rendering: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Settings, int]:
    """Settings as the runner would load them for one rendering, and that
    rendering's step limit in minutes (the grader-check limit only on dispatch-on)."""
    grader_check, ref, capped, _cap = _RENDERINGS[rendering]
    captured, _args = _run_eval_step(tmp_path, grader_check, ref, capped=capped)
    for name in list(os.environ):
        if name.upper().startswith(PREFIX):
            monkeypatch.delenv(name, raising=False)
    for name, value in captured.items():
        if name.upper().startswith(PREFIX):
            monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    on, off = _conditional(_eval_step()["timeout-minutes"])
    return settings, on if rendering == "dispatch-on" else off


def _constant(name: str) -> float:
    value = getattr(runner_mod, name)
    assert isinstance(value, int | float) and math.isfinite(value) and value > 0, (name, value)
    return float(value)


@pytest.mark.parametrize("rendering", list(_RENDERINGS))
def test_every_judged_rendering_loads_a_deadline_a_budget_and_a_call_total(
    rendering: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RED WHEN: a judged rendering loads no deadline, budget or total.
    settings, _step = _loaded(rendering, tmp_path, monkeypatch)
    assert settings.eval_judge_model  # partner: the rendering is a judged run
    assert settings.eval_deadline_seconds is not None  # type: ignore[attr-defined]
    assert settings.eval_grading_budget_seconds is not None  # type: ignore[attr-defined]
    assert settings.eval_call_total_timeout_seconds is not None  # type: ignore[attr-defined]


@pytest.mark.parametrize("rendering", list(_RENDERINGS))
def test_the_deadline_leaves_room_for_start_up_one_case_and_teardown(
    rendering: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RED WHEN: D + S + C + T exceeds the step limit on a rendering.
    settings, step_minutes = _loaded(rendering, tmp_path, monkeypatch)
    deadline = settings.eval_deadline_seconds  # type: ignore[attr-defined]
    budget = settings.eval_grading_budget_seconds  # type: ignore[attr-defined]
    call_total = settings.eval_call_total_timeout_seconds  # type: ignore[attr-defined]
    startup = _constant("DEADLINE_STARTUP_SLACK_SECONDS")
    calls = _constant("DEADLINE_MAX_ANSWER_CALLS_PER_CASE")
    teardown = _constant("DEADLINE_TEARDOWN_RESERVE_SECONDS")
    one_case = calls * call_total + budget
    assert deadline > 0 and budget > 0 and call_total > 0  # partner: each value was read
    assert deadline + startup + one_case + teardown <= step_minutes * 60, (
        rendering,
        {"D": deadline, "S": startup, "C": one_case, "T": teardown, "step": step_minutes * 60},
    )


@pytest.mark.parametrize("rendering", list(_RENDERINGS))
def test_the_budget_holds_the_largest_panel_and_a_minute(
    rendering: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RED WHEN: G falls below the largest panel's calls x timeout + 60 s.
    settings, _step = _loaded(rendering, tmp_path, monkeypatch)
    timeout = settings.eval_judge_timeout_seconds
    assert timeout is not None and timeout > 0  # partner: the timeout was read
    panel_calls = judge_mod.panel_size() + 1  # the panel as this rendering sets it
    need = panel_calls * timeout + BUDGET_MARGIN_SECONDS
    assert settings.eval_grading_budget_seconds >= need, (rendering, need)  # type: ignore[attr-defined]


def test_the_runner_constants_are_derived_as_the_plan_says() -> None:
    # RED WHEN: a constant is removed or set below its derivation's floor.
    assert _constant("DEADLINE_STARTUP_SLACK_SECONDS") > STARTUP_MARGIN_SECONDS
    assert _constant("DEADLINE_TEARDOWN_RESERVE_SECONDS") >= MIN_TEARDOWN_SECONDS
    calls = _constant("DEADLINE_MAX_ANSWER_CALLS_PER_CASE")
    assert calls >= 1 and calls == int(calls)


def _job_header_comment() -> str:
    """The comment lines between the judged job's name and its `timeout-minutes`."""
    lines = CI_YML.read_text(encoding="utf-8").splitlines()
    start = lines.index("  answer-quality-eval:")
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("    timeout-minutes:"))
    return "\n".join(ln.strip() for ln in lines[start:end] if ln.strip().startswith("#"))


def test_the_job_comment_states_each_paths_deadline_as_loaded(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RED WHEN: a path's deadline changes without the job comment.
    comment = _job_header_comment()
    assert comment  # partner: the comment was found
    for rendering in ("push-main", "dispatch-on"):
        sub = tmp_path / rendering
        sub.mkdir()
        settings, _step = _loaded(rendering, sub, monkeypatch)
        deadline = settings.eval_deadline_seconds  # type: ignore[attr-defined]
        assert deadline is not None
        assert re.search(rf"(?<!\d){deadline}(?!\d)", comment), (rendering, deadline)


# --- kept from #637: the job limit around the step ------------------------------------


def test_the_job_leaves_room_for_the_upload_and_fits_its_waits() -> None:
    """Red when: the job limit is less than 4 minutes above its step (no time for the
    always() report upload), the switch-off job no longer fits the release wait after
    the `test` job, or a job no longer fits the UTC window its runner demands."""
    # RED WHEN: the job limit loses its 4-minute margin or outgrows a wait.
    step_on, step_off = _conditional(_eval_step()["timeout-minutes"])
    job_on, job_off = _conditional(_job()["timeout-minutes"])
    assert job_off >= step_off + 4 and job_on >= step_on + 4
    match = re.search(r"TOTAL_TIMEOUT_SECONDS-(\d+)", WAIT_SCRIPT.read_text(encoding="utf-8"))
    assert match, "the wait script no longer defaults TOTAL_TIMEOUT_SECONDS"
    assert job_off + TEST_JOB_MINUTES <= int(match.group(1)) / 60
    assert job_off <= gc.MIN_MINUTES_BEFORE_UTC_MIDNIGHT_CAPPED_DISPATCH
    assert job_on <= gc.MIN_MINUTES_BEFORE_UTC_MIDNIGHT
