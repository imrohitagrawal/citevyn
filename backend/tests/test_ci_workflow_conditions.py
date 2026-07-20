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
        if _PR_PAYLOAD.search(condition) and not _PUSH_ESCAPE.search(condition):
            offenders.append(f"{path.name}:{job_name}: if: {condition!r}")

    assert not offenders, (
        "job(s) gated on github.event.pull_request in a push-triggered workflow — "
        "the payload is null on push, so these are skipped on main. Add "
        "`github.event_name == 'push' ||` to the condition:\n" + "\n".join(offenders)
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


def test_the_guard_has_something_to_check() -> None:
    """Non-vacuity: at least one job really is gated on the PR payload."""
    gated = [
        f"{path.name}:{name}"
        for path in _workflow_files()
        for name, job in (_load(path).get("jobs") or {}).items()
        if _PR_PAYLOAD.search(str(job.get("if") or ""))
    ]
    assert gated, "no PR-payload-gated job found — the linter above would pass vacuously"
