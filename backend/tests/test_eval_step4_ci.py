"""The judged CI job after #598 Step 4: the switch-off dispatch's cap line and the
grader timeout's measured latency.

Written before the implementation (test designer). The rendered behaviour of every
kind of run (which caps reach Settings, when the spend is exported) is in
``test_grader_check_ci.py``; this module pins the one line a new owner approval may
change, and the comment's measured figures. The job and step limits are 55/50 on
both paths, from measured runs (``test_the_limits_are_the_measured_ones``).

Each test names the change that turns it red.
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, datetime

import pytest

from app.core.config import Settings
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
    """Red when: a switch-off dispatch that sets ``capped_baseline=true`` does not export
    the ``CAP_USD`` line's value (two numbers that can drift apart), or the same dispatch
    without the input is capped too (a release re-run the gate accepts would be capped
    and held to the window)."""
    values = _CAP_LINE.findall(str(_eval_step()["run"]))
    assert len(values) == 1, f"no single CAP_USD line in the eval step: {values}"
    (value,) = values
    (tmp_path / "capped").mkdir()
    (tmp_path / "plain").mkdir()
    capped, _args = _run_eval_step(
        tmp_path / "capped", "false", "refs/heads/eval/598-step4", capped=True
    )
    assert capped.get("CITEVYN_COST_HARD_DAILY_USD") == value
    assert capped.get("CITEVYN_EVAL_CAPPED_DISPATCH") == "true"
    assert "CITEVYN_EVAL_PASSAGE_GRADING" not in capped  # partner: the switch stays off
    plain, _args = _run_eval_step(tmp_path / "plain", "false", "refs/heads/eval/598-step4")
    assert "CITEVYN_COST_HARD_DAILY_USD" not in plain, sorted(plain)
    assert "CITEVYN_EVAL_CAPPED_DISPATCH" not in plain, sorted(plain)


class _Reached(Exception):
    """Raised in place of the eval: the runner let the run start."""


def _main_at(
    monkeypatch: pytest.MonkeyPatch, *, now: datetime, env: dict[str, str]
) -> tuple[int | None, str]:
    """``main()`` with the switch off at ``now``, the eval itself replaced. Returns the
    exit code (``None`` when the eval was reached) and the printed output."""
    from tests.eval import grader_check as gc
    from tests.eval import runner as runner_mod

    for name in ("GITHUB_EVENT_NAME", "GITHUB_REF", "CITEVYN_EVAL_CAPPED_DISPATCH"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(gc, "utc_now", lambda: now)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: Settings(_env_file=None))  # type: ignore[call-arg]

    async def _eval(**_kwargs: object) -> dict[str, object]:
        raise _Reached

    monkeypatch.setattr(runner_mod, "run_eval_async", _eval)
    try:
        code = runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--quiet"])
    except _Reached:
        return None, ""
    return code, ""


LATE = datetime(2026, 11, 17, 23, 30, tzinfo=UTC)  # 30 minutes before 00:00 UTC
NOON = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
DISPATCH = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/eval/x"}


@pytest.mark.parametrize(
    ("env", "now", "refused"),
    [
        ({**DISPATCH, "CITEVYN_EVAL_CAPPED_DISPATCH": "true"}, LATE, True),
        ({**DISPATCH, "CITEVYN_EVAL_CAPPED_DISPATCH": "true"}, NOON, False),
        (DISPATCH, LATE, False),
        ({**DISPATCH, "CITEVYN_EVAL_CAPPED_DISPATCH": "false"}, LATE, False),
        ({"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/tags/v1.0.0"}, LATE, False),
        ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}, LATE, False),
        ({}, LATE, False),
    ],
    ids=[
        "capped-late",
        "capped-noon",
        "dispatch-without-flag-late",
        "flag-false-late",
        "tag-dispatch-late",
        "push-late",
        "local-late",
    ],
)
def test_the_window_refusal_applies_only_to_a_run_marked_capped(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    now: datetime,
    refused: bool,
) -> None:
    """Red when: the runner applies the capped-dispatch window refusal from
    ``GITHUB_EVENT_NAME``/``GITHUB_REF`` alone (an uncapped manual run, such as a
    release re-run, is held to the window), or does not apply it to a run marked
    ``CITEVYN_EVAL_CAPPED_DISPATCH=true`` with under 70 minutes left."""
    code, _ = _main_at(monkeypatch, now=now, env=env)
    out = capsys.readouterr().out
    if refused:
        assert code == 2, out
        assert "nothing was spent" in out and "UTC" in out, out
    else:
        assert code is None, f"refused without the capped flag: {out}"


def test_the_cap_reaches_the_script_never_through_env() -> None:
    """Red when: the cap is set through a job or step ``env:`` entry (an expression
    there renders empty on push and tag runs, and an empty value stops Settings)."""
    for block in (_job().get("env") or {}, _eval_step().get("env") or {}):
        assert "CITEVYN_COST_HARD_DAILY_USD" not in block
        assert "CAP_USD" not in block


def test_the_limits_are_the_measured_ones() -> None:
    """Red when: a limit moves away from the rule in the job's comment in ci.yml
    (twice the slowest counted run, rounded up to 5 minutes, the job at least 5
    above its step: job 55, eval step 50 on both paths)."""
    assert _conditional(_job()["timeout-minutes"]) == (55, 55)
    assert _conditional(_eval_step()["timeout-minutes"]) == (50, 50)


def test_the_grader_timeout_comment_gives_step_3s_measured_latency() -> None:
    """Red when: the grader timeout's comment keeps the old figures (p50 1.4 s, p90
    12.0 s) instead of Step 3's measured p50 1.04 s, p90 2.09 s and max 2.9 s."""
    text = CI_YML.read_text(encoding="utf-8")
    block = text[: text.index("CITEVYN_EVAL_JUDGE_TIMEOUT_SECONDS:")]
    comment = " ".join(block.splitlines()[-8:])
    assert "p50 1.04 s" in comment and "p90 2.09 s" in comment, comment
    assert "2.9 s" in comment, comment
    assert "p90 12.0 s" not in comment
