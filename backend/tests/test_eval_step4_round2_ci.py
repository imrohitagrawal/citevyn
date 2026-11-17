"""The judged CI job after the second review round of #598 Step 4: the opt-in
``capped_baseline`` input.

Only a paid baseline dispatch sets the input; a release re-run never does. The cap and
the runner's capped mark are set in the eval step's script from the event payload,
never through an ``env:`` block. The rendered behaviour of each kind of run is in
``test_grader_check_ci.py`` and ``test_eval_step4_ci.py``.

Each test names the change that turns it red.
"""

from __future__ import annotations

from tests.test_grader_check_ci import _job, _workflow

_NEVER_IN_ENV = (
    "CAP_USD",
    "CITEVYN_COST_HARD_DAILY_USD",
    "CITEVYN_EVAL_CAPPED_DISPATCH",
    "capped_baseline",
)


def test_workflow_dispatch_has_a_boolean_capped_baseline_input_off_by_default() -> None:
    """Red when: the input is missing, not a boolean (a string "false" would read as
    set), on by default (every manual run, release re-runs included, would be capped
    and held to the UTC window), or its description changes."""
    workflow = _workflow()
    triggers = workflow.get(True, workflow.get("on"))
    capped = (triggers["workflow_dispatch"] or {})["inputs"]["capped_baseline"]
    assert capped["type"] == "boolean"
    assert capped["default"] is False
    assert capped["description"] == (
        "Paid baseline run: apply the per-run spend ceiling and the UTC-midnight window"
    )


def test_no_env_block_of_the_judged_job_carries_the_cap_or_the_capped_mark() -> None:
    """Red when: the cap, the capped mark or the input reaches the judged job through a
    job or step ``env:`` entry (by name or by value): an expression there renders empty
    on push and tag runs, and the cap must come only from the script's ``CAP_USD``
    line on a dispatch that asked for it."""
    job = _job()
    blocks = [("job", job.get("env") or {})]
    blocks += [(str(step.get("name")), step.get("env") or {}) for step in job["steps"]]
    assert len(blocks) > 2  # partner: the job's steps were really read
    assert any(block for _where, block in blocks)  # partner: env blocks exist
    for where, block in blocks:
        for name, value in block.items():
            for banned in _NEVER_IN_ENV:
                assert banned not in str(name), f"{banned} in {where} env: {name}"
                assert banned not in str(value), f"{banned} in {where} env: {name}={value}"
