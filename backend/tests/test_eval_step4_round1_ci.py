"""The judged CI job after the first review round of #598 Step 4.

A manual dispatch on a release tag is the run the release gate accepts as its judged
result, so the switch-off cap must not reach it: release gating changes only by the
corpus. And the runner's UTC-window refusal for a capped dispatch must cover the
switch-off job's own limit. Rendered with the helpers of ``test_grader_check_ci.py``.

Each test names the change that turns it red.
"""

from __future__ import annotations

import pathlib

from app.core.config import Settings
from tests.eval import grader_check as gc
from tests.test_grader_check_ci import (
    SWITCH_OFF_CAP_USD,
    _conditional,
    _job,
    _run_eval_step,
)

TAG_REF = "refs/tags/v0.12.0"


def test_a_switch_off_dispatch_on_a_release_tag_exports_no_cap(tmp_path: pathlib.Path) -> None:
    """Red when: the switch-off cap is exported on a ``workflow_dispatch`` run of a
    ``refs/tags/*`` ref, so a manual release-tag run (which the release gate accepts)
    runs under a ceiling the tag-push run never had."""
    captured, args = _run_eval_step(tmp_path, "false", TAG_REF)
    assert "tests.eval.runner" in args  # partner: the eval really started
    assert captured.get("GITHUB_EVENT_NAME") == "workflow_dispatch"  # partner: a dispatch
    assert "CITEVYN_COST_HARD_DAILY_USD" not in captured, captured.get(
        "CITEVYN_COST_HARD_DAILY_USD"
    )
    assert "CITEVYN_EVAL_PASSAGE_GRADING" not in captured


def test_the_ordinary_switch_off_dispatch_still_exports_its_cap(tmp_path: pathlib.Path) -> None:
    """Partner of the tag case. Red when: the tag exception also drops the cap from a
    switch-off dispatch on a branch."""
    captured, _args = _run_eval_step(tmp_path, "false", "refs/heads/eval/598-step4")
    assert captured.get("CITEVYN_COST_HARD_DAILY_USD") == SWITCH_OFF_CAP_USD == "0.14"
    default = Settings.model_fields["cost_hard_daily_usd"].default
    assert float(SWITCH_OFF_CAP_USD) < default


def test_the_capped_dispatch_window_covers_the_switch_off_job_limit() -> None:
    """Red when: the runner's minutes-before-00:00-UTC demand for a capped switch-off
    dispatch is below the job's switch-off limit, so a run that starts could still
    cross midnight and the daily cap would reset mid-run."""
    _on, off = _conditional(_job()["timeout-minutes"])
    assert off == 68  # partner: the limit this window is held to
    assert off <= gc.MIN_MINUTES_BEFORE_UTC_MIDNIGHT_CAPPED_DISPATCH
