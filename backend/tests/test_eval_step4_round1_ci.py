"""The judged CI job after the first review round of #598 Step 4.

A manual dispatch at a release commit (on its tag or on a branch) is a run the release
gate accepts as its judged result, so the switch-off cap must not reach it unless the
dispatch sets the ``capped_baseline`` input (round 2): release gating changes only by
the corpus. And the runner's UTC-window refusal for a capped dispatch must cover the
switch-off job's own limit. Rendered with the helpers of ``test_grader_check_ci.py``.

Each test names the change that turns it red.
"""

from __future__ import annotations

import pathlib

import pytest

from app.core.config import Settings
from tests.eval import grader_check as gc
from tests.test_grader_check_ci import (
    SWITCH_OFF_CAP_USD,
    _conditional,
    _job,
    _run_eval_step,
)

TAG_REF = "refs/tags/v0.12.0"
BRANCH_REF = "refs/heads/eval/598-step4"


@pytest.mark.parametrize("ref", [TAG_REF, BRANCH_REF], ids=["tag", "branch"])
def test_a_switch_off_dispatch_without_the_input_exports_no_cap(
    tmp_path: pathlib.Path, ref: str
) -> None:
    """Red when: the switch-off cap, or the capped mark the runner's window reads, is
    exported on a ``workflow_dispatch`` run that does not set ``capped_baseline``, on a
    tag or a branch: a manual re-run of a release (which the release gate accepts)
    would run under a ceiling and a window the tag-push run never had."""
    captured, args = _run_eval_step(tmp_path, "false", ref)
    assert "tests.eval.runner" in args  # partner: the eval really started
    assert captured.get("GITHUB_EVENT_NAME") == "workflow_dispatch"  # partner: a dispatch
    assert "CITEVYN_COST_HARD_DAILY_USD" not in captured, captured.get(
        "CITEVYN_COST_HARD_DAILY_USD"
    )
    assert "CITEVYN_EVAL_CAPPED_DISPATCH" not in captured
    assert "CITEVYN_EVAL_PASSAGE_GRADING" not in captured


def test_a_capped_baseline_dispatch_exports_its_cap_and_mark(tmp_path: pathlib.Path) -> None:
    """Partner of the uncapped cases. Red when: a switch-off dispatch that sets
    ``capped_baseline`` on a branch loses its cap or the mark that holds it to the
    window."""
    captured, _args = _run_eval_step(tmp_path, "false", BRANCH_REF, capped=True)
    assert captured.get("CITEVYN_COST_HARD_DAILY_USD") == SWITCH_OFF_CAP_USD == "0.14"
    assert captured.get("CITEVYN_EVAL_CAPPED_DISPATCH") == "true"
    default = Settings.model_fields["cost_hard_daily_usd"].default
    assert float(SWITCH_OFF_CAP_USD) < default


def test_the_capped_dispatch_window_covers_the_switch_off_job_limit() -> None:
    """Red when: the runner's minutes-before-00:00-UTC demand for a capped switch-off
    dispatch is below the job's switch-off limit, so a run that starts could still
    cross midnight and the daily cap would reset mid-run."""
    _on, off = _conditional(_job()["timeout-minutes"])
    assert off == 55  # partner: the limit this window is held to
    assert off <= gc.MIN_MINUTES_BEFORE_UTC_MIDNIGHT_CAPPED_DISPATCH
