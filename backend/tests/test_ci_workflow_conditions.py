"""Guards against the ``pull_request``-payload silent-skip trap in CI workflows.

``github.event.pull_request`` is **null** on a ``push`` event. A job ``if:`` that
only tests that payload therefore evaluates false on every push, so the job is
silently skipped on ``main`` while still looking healthy on PRs. Issue #183 (and
#182 before it) were exactly this: the migration gate and the judged eval gate
never ran post-merge.

These tests read the real workflow files — they are the only place this class of
bug is observable, since nothing else in the repo executes GitHub's expression
language.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# The escape hatch that makes a PR-payload condition safe on a push. A push to
# ``main`` is always same-repo (a fork cannot push here), so this does not widen
# fork-secret exposure.
_PUSH_ESCAPE = re.compile(r"github\.event_name\s*==\s*'push'")
_PR_PAYLOAD = re.compile(r"github\.event\.pull_request\b")

# Jobs that are DELIBERATELY not run on a push, keyed by (workflow, job).
#
# The rule above exists because a PR-payload condition in a push-triggered
# workflow is usually an ACCIDENT (#182, #183): the job looks healthy on PRs and
# is silently dead on main. But "does not run on push" is a legitimate design for
# a job that costs real money — there the silence is the point, and the escape
# hatch would reintroduce the charge.
#
# Exemptions are listed one by one, with the reason, so the guard keeps biting for
# every other job. An exempt job must still name a deliberate alternative trigger
# (see the companion test below), so deleting its triggers cannot pass unnoticed.
_INTENTIONALLY_NOT_ON_PUSH: dict[tuple[str, str], str] = {
    ("ci.yml", "answer-quality-eval"): (
        "Costs ~$0.026 of real provider spend per run. Owner policy is that the "
        "paid key is spent when FINALISING a release, not on every merge. Runs on "
        "a v* tag, on workflow_dispatch, or on a `full-eval`-labelled PR."
    ),
}


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} is not a YAML mapping"
    return data


def _triggers(workflow: dict[str, Any]) -> set[str]:
    # PyYAML resolves the bare key ``on`` to the boolean True (YAML 1.1).
    raw = workflow.get(True, workflow.get("on"))
    if isinstance(raw, dict):
        return set(raw)
    if isinstance(raw, list):
        return set(raw)
    return {raw} if isinstance(raw, str) else set()


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    assert files, f"no workflow files found under {WORKFLOW_DIR}"
    return files


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_yaml_parses(path: Path) -> None:
    _load(path)


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_push_triggered_jobs_do_not_gate_on_a_pull_request_payload(path: Path) -> None:
    """A job in a push-triggered workflow must not be gated solely on the PR payload."""
    workflow = _load(path)
    if "push" not in _triggers(workflow):
        pytest.skip(f"{path.name} does not trigger on push")

    offenders: list[str] = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        condition = str(job.get("if") or "")
        if (path.name, job_name) in _INTENTIONALLY_NOT_ON_PUSH:
            continue
        if _PR_PAYLOAD.search(condition) and not _PUSH_ESCAPE.search(condition):
            offenders.append(f"{path.name}:{job_name}: if: {condition!r}")

    assert not offenders, (
        "job(s) gated on github.event.pull_request in a push-triggered workflow — "
        "the payload is null on push, so these are skipped on main. Add "
        "`github.event_name == 'push' ||` to the condition, or — only if the job is "
        "meant to be push-silent — add it to _INTENTIONALLY_NOT_ON_PUSH with the "
        "reason:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("workflow_name", "job_name"),
    sorted(_INTENTIONALLY_NOT_ON_PUSH),
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_push_exempt_jobs_still_have_a_deliberate_trigger(
    workflow_name: str, job_name: str
) -> None:
    """An exempt job must still be reachable, or it is dead rather than deferred.

    Exempting a job from the push rule says "silence on main is intended". It does
    NOT license the job never running at all — that would be #182 again wearing a
    different hat. So every exemption must name at least one deliberate way in: a
    release tag, a manual dispatch, or an opt-in label.
    """
    workflow = _load(WORKFLOW_DIR / workflow_name)
    condition = str((workflow["jobs"][job_name]).get("if") or "")

    deliberate = (
        "refs/tags/" in condition or "workflow_dispatch" in condition or "labels" in condition
    )
    assert deliberate, (
        f"{workflow_name}:{job_name} is exempt from the push rule but names no "
        "deliberate trigger (tag / workflow_dispatch / label), so it can never run"
    )

    triggers = _triggers(workflow)
    assert "workflow_dispatch" in triggers or "push" in triggers, (
        f"{workflow_name} must be dispatchable or push-triggered for "
        f"{job_name} to be reachable at all"
    )


def test_postgres_migrations_runs_on_push_and_still_guards_forks() -> None:
    """Issue #183 regression: the migration gate must run post-merge on main."""
    job = _load(WORKFLOW_DIR / "ci.yml")["jobs"]["postgres-migrations"]
    condition = str(job["if"])
    assert _PUSH_ESCAPE.search(condition), (
        "postgres-migrations must run on push to main; without the push escape the "
        "Alembic/schema-drift gate is silent post-merge"
    )
    fork_guard = "github.event.pull_request.head.repo.full_name == github.repository"
    assert fork_guard in condition, (
        "the fork guard must be kept — fork PRs must not get the Postgres service"
    )


def test_judged_eval_remains_the_release_gate() -> None:
    """The paid eval must still run on a v* tag — that is what gates a release.

    Deferring the judged eval off ordinary merges is deliberate (see
    _INTENTIONALLY_NOT_ON_PUSH), and it is only defensible because the tag still
    runs it: a bad release cannot ship unjudged. Losing the tag trigger would turn
    "deferred to release" into "never runs", which is #182 all over again — and the
    generic reachability check above does NOT catch it, because the `full-eval`
    label alone keeps the job technically reachable.
    """
    workflow = _load(WORKFLOW_DIR / "ci.yml")
    condition = str(workflow["jobs"]["answer-quality-eval"]["if"])
    assert "refs/tags/v" in condition, (
        "the judged eval no longer runs on a v* tag, so a release would ship "
        "without the answer-quality gate"
    )
    assert "workflow_dispatch" in condition, (
        "the judged eval must stay runnable on demand, otherwise the only way to "
        "check answer quality is to cut a tag"
    )

    raw = workflow.get(True, workflow.get("on")) or {}
    push = raw.get("push") or {}
    tags = push.get("tags") or []
    assert any(str(t).startswith("v") for t in tags), (
        "ci.yml does not trigger on v* tags, so the job condition above can never "
        "be reached on a release"
    )
    assert "workflow_dispatch" in raw, (
        "ci.yml is not dispatchable, so the judged eval cannot be run on demand"
    )


def test_the_guard_has_something_to_check() -> None:
    """Non-vacuity: at least one job really is gated on the PR payload."""
    gated = [
        f"{path.name}:{name}"
        for path in _workflow_files()
        for name, job in (_load(path).get("jobs") or {}).items()
        if _PR_PAYLOAD.search(str(job.get("if") or ""))
    ]
    assert gated, "no PR-payload-gated job found — the linter above would pass vacuously"
