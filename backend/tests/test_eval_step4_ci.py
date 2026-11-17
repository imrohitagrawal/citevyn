"""The judged CI job after #598 Step 4: the switch-off dispatch's cap line and the
grader timeout's measured latency.

Written before the implementation (test designer). The rendered behaviour of every
kind of run (which caps reach Settings, when the spend is exported) is in
``test_grader_check_ci.py``; this module pins the one line a new owner approval may
change, and the comment's measured figures. The job and step limits stay 68/64 off
and 245/240 on (``test_the_job_and_step_limits_depend_on_the_input``).

Each test names the change that turns it red.
"""

from __future__ import annotations

import pathlib
import re

from tests.test_grader_check_ci import (
    CI_YML,
    SWITCH_OFF_CAP_USD,
    _conditional,
    _eval_step,
    _job,
    _run_eval_step,
)

_CAP_LINE = re.compile(r"^\s*(?:export\s+)?CAP_USD=(\S+)\s*$", re.MULTILINE)


def test_the_switch_off_cap_is_one_cap_usd_line() -> None:
    """Red when: the switch-off dispatch's cap is not one ``CAP_USD=`` line in the eval
    step's script (an owner's new number must be a one-line change), or its value is
    not the reviewed 0.14."""
    script = str(_eval_step()["run"])
    assert _CAP_LINE.findall(script) == [SWITCH_OFF_CAP_USD], script


def test_the_switch_off_dispatch_runs_under_the_cap_usd_value(tmp_path: pathlib.Path) -> None:
    """Red when: the value exported on a switch-off dispatch is not the ``CAP_USD`` line's
    (two numbers that can drift apart)."""
    values = _CAP_LINE.findall(str(_eval_step()["run"]))
    assert len(values) == 1, f"no single CAP_USD line in the eval step: {values}"
    (value,) = values
    captured, _args = _run_eval_step(tmp_path, "false", "refs/heads/eval/598-step4")
    assert captured.get("CITEVYN_COST_HARD_DAILY_USD") == value
    assert "CITEVYN_EVAL_PASSAGE_GRADING" not in captured  # partner: the switch stays off


def test_the_cap_reaches_the_script_never_through_env() -> None:
    """Red when: the cap is set through a job or step ``env:`` entry (an expression
    there renders empty on push and tag runs, and an empty value stops Settings)."""
    for block in (_job().get("env") or {}, _eval_step().get("env") or {}):
        assert "CITEVYN_COST_HARD_DAILY_USD" not in block
        assert "CAP_USD" not in block


def test_the_limits_are_unchanged() -> None:
    """Red when: this package moves a limit (plan Revision 4 item 3: re-set from the
    measured run in a separate pull request)."""
    assert _conditional(_job()["timeout-minutes"]) == (245, 68)
    assert _conditional(_eval_step()["timeout-minutes"]) == (240, 64)


def test_the_grader_timeout_comment_gives_step_3s_measured_latency() -> None:
    """Red when: the grader timeout's comment keeps the old figures (p50 1.4 s, p90
    12.0 s) instead of Step 3's measured p50 1.04 s, p90 2.09 s and max 2.9 s."""
    text = CI_YML.read_text(encoding="utf-8")
    block = text[: text.index("CITEVYN_EVAL_JUDGE_TIMEOUT_SECONDS:")]
    comment = " ".join(block.splitlines()[-8:])
    assert "p50 1.04 s" in comment and "p90 2.09 s" in comment, comment
    assert "2.9 s" in comment, comment
    assert "p90 12.0 s" not in comment
