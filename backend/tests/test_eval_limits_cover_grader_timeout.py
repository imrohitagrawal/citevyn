"""The judged job's limits must cover the slowest measured day under TODAY's grader timeout.

The slowest counted run (2026-11-08, run 42460602450) took 25.0 minutes for the eval
step with the grader timeout at 15 s, and 22 grader calls timed out (counted from that
run's saved report). Replayed under a longer grader timeout T, each of those calls
waits T - 15 s more, and the second grading call each one skipped now runs, for up to
T s. The step limit must cover that estimate, so raising the grader timeout without
raising the limits turns this file red (#634 set the limits from the 15 s run while
the grader waited 60 s).

This is an estimate of one measured day, not a bound on every day; the runner's own
deadline is the lasting fix.
"""

from __future__ import annotations

import math
import re

from tests.eval import grader_check as gc
from tests.test_grader_check_ci import CI_YML, WAIT_SCRIPT, _conditional, _eval_step, _job

#: The slowest counted run: eval step minutes, grader timeouts, and the timeout then.
SLOW_STEP_MINUTES = 25.0
SLOW_TIMEOUTS = 22
SLOW_TIMEOUT_SECONDS = 15
#: The `test` job that runs before the judged job (6 min 57 s measured 2026-11-17).
TEST_JOB_MINUTES = 7


def _grader_timeout_seconds() -> int:
    match = re.search(
        r'CITEVYN_EVAL_JUDGE_TIMEOUT_SECONDS:\s*"(\d+)"', CI_YML.read_text(encoding="utf-8")
    )
    assert match, "ci.yml no longer sets CITEVYN_EVAL_JUDGE_TIMEOUT_SECONDS"
    return int(match.group(1))


def _slow_day_minutes(timeout_seconds: int) -> float:
    extra_wait = SLOW_TIMEOUTS * (timeout_seconds - SLOW_TIMEOUT_SECONDS) / 60
    skipped_calls = SLOW_TIMEOUTS * timeout_seconds / 60
    return SLOW_STEP_MINUTES + extra_wait + skipped_calls


def test_the_estimate_is_the_recorded_one_at_60_seconds() -> None:
    """Red when: the estimate's arithmetic or its recorded inputs change (partner: at a
    60 s grader it is the 63.5 minutes written in ci.yml's comment)."""
    assert _slow_day_minutes(60) == 63.5
    assert "63.5" in CI_YML.read_text(encoding="utf-8")


def test_the_step_limit_covers_the_slow_day_at_the_current_grader_timeout() -> None:
    """Red when: the eval step's limit, on either path, is below the slowest measured
    day replayed under the grader timeout ci.yml sets (the #634 gap: 50 < 63.5)."""
    timeout = _grader_timeout_seconds()
    assert timeout > 0  # partner: the timeout was really read
    need = math.ceil(_slow_day_minutes(timeout))
    on, off = _conditional(_eval_step()["timeout-minutes"])
    assert off >= need, (off, need)
    assert on >= off, (on, off)  # the grader check makes more grader calls


def test_the_job_leaves_room_for_the_upload_and_fits_its_waits() -> None:
    """Red when: the job limit is less than 4 minutes above its step (no time for the
    always() report upload), the switch-off job no longer fits the release wait after
    the `test` job, or a job no longer fits the UTC window its runner demands."""
    step_on, step_off = _conditional(_eval_step()["timeout-minutes"])
    job_on, job_off = _conditional(_job()["timeout-minutes"])
    assert job_off >= step_off + 4 and job_on >= step_on + 4
    match = re.search(r"TOTAL_TIMEOUT_SECONDS-(\d+)", WAIT_SCRIPT.read_text(encoding="utf-8"))
    assert match, "the wait script no longer defaults TOTAL_TIMEOUT_SECONDS"
    assert job_off + TEST_JOB_MINUTES <= int(match.group(1)) / 60
    assert job_off <= gc.MIN_MINUTES_BEFORE_UTC_MIDNIGHT_CAPPED_DISPATCH
    assert job_on <= gc.MIN_MINUTES_BEFORE_UTC_MIDNIGHT
