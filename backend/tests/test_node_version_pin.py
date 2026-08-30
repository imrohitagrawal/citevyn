"""Guards the ONE Node version against silent drift (#231).

Before this guard, three places named a Node version and nothing compared them:
CI tested the frontend on Node 20 (``frontend.yml``, ``frontend-live-e2e.yml``)
while the bundle production actually serves was built on Node 22
(``Dockerfile.api``). So CI had never validated the artifact that ships, and
Dependabot PR #227 (``node:22`` -> ``node:26``) would have widened that to six
majors while every check stayed green.

``frontend/.nvmrc`` is now the single source of truth. The workflows read it via
``actions/setup-node``'s ``node-version-file:``; the Dockerfile cannot read a
file in its ``FROM`` line, so this test is what holds it to the same major.

Why a test rather than a comment: the Dockerfile's ``FROM node:`` line is edited
UNILATERALLY by Dependabot (``.github/dependabot.yml`` groups every base image
under ``/infra/docker``), so it is the line that moves on its own. A "keep these
in sync" comment cannot fail a build. This can, and it runs in ``ci.yml``'s
``test`` job, which has no ``paths:`` filter and is a required status check — so
it fires on a Dependabot PR that touches nothing but ``Dockerfile.api``.

Deliberately MAJOR-only. ``.nvmrc`` holds ``22`` and the image tag is
``node:22-bookworm-slim``: both float to the newest 22.x, so patch releases flow
in without a repo edit while the major stays locked together. Pinning a full
``22.x.y`` here would drift against the floating image tag by construction.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
NVMRC = REPO_ROOT / "frontend" / ".nvmrc"
FRONTEND_PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"

# The repo-root-relative path the workflows must hand to ``setup-node``. It is
# NOT affected by a job's ``defaults.run.working-directory`` — that applies to
# ``run:`` steps only, never to an action's ``with:`` inputs.
NVMRC_WORKFLOW_PATH = "frontend/.nvmrc"

# ``FROM [registry/][namespace/]node:<tag>``. Anchored at FROM so an unrelated
# ``node:http`` import or a ``python:3.14`` base cannot match.
_FROM_NODE = re.compile(
    r"^FROM\s+(?:[\w.\-]+(?::\d+)?/)*node:(?P<tag>[\w.\-]+)",
    re.MULTILINE,
)
_LEADING_MAJOR = re.compile(r"^v?(?P<major>\d+)")

# Directories that never hold a Dockerfile this checkout owns.
#
# ``.claude`` matters more than it looks: agent worktrees live under
# ``.claude/worktrees/<id>/``, each a FULL copy of the repo with its own
# Dockerfile. Walking into them would make this guard read a sibling branch's
# in-progress edits and fail on a tree the developer is not even working in —
# a false positive that only ever reproduces on someone's laptop, never in CI.
_PRUNED_DIRS = frozenset(
    {
        ".git",
        ".claude",
        "node_modules",
        "dist",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
    }
)

# Jobs whose Node version this repo genuinely CANNOT pin, each with the reason.
#
# Listing them is the point: an unlisted external Node consumer fails the
# companion test below, so "the guard didn't look there" can never be silent.
_EXTERNAL_NODE_OUT_OF_REACH: dict[tuple[str, str], str] = {
    ("pr-quality.yml", "quality-gate"): (
        "Calls imrohitagrawal/.github/.github/workflows/reusable-pr-quality.yml, "
        "SHA-pinned in ANOTHER repository, which hardcodes node-version: '20'. "
        "This repo cannot change it. Tolerable because that job never builds or "
        "tests the SPA: it runs `npm ci` plus an advisory `npm audit` in "
        "frontend/, and the Make targets it invokes (lint/typecheck/test/ci) are "
        "backend-only. No shipped artifact is produced on that Node."
    ),
}

# Inputs that mean "this external workflow will run something with Node".
_NODE_INPUT_KEYS = frozenset({"node-directory", "node-version", "node-version-file"})


def _major(spec: str) -> int:
    """Leading major of a version spec (``22``, ``v22``, ``22.11.0``, ``>=22``)."""
    match = _LEADING_MAJOR.search(spec.strip().lstrip(">=^~< "))
    assert match, f"cannot read a major version out of {spec!r}"
    return int(match.group("major"))


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} is not a YAML mapping"
    return data


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    assert files, f"no workflow files found under {WORKFLOW_DIR}"
    return files


def _dockerfiles() -> list[Path]:
    """Every tracked Dockerfile in the repo, pruning vendored/build trees."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
        found.extend(Path(dirpath) / name for name in filenames if name.startswith("Dockerfile"))
    assert found, f"no Dockerfile found under {REPO_ROOT}"
    return sorted(found)


def _setup_node_steps() -> list[tuple[Path, str, dict[str, Any]]]:
    """(workflow, job, ``with:`` mapping) for every ``actions/setup-node`` step."""
    steps: list[tuple[Path, str, dict[str, Any]]] = []
    for path in _workflow_files():
        for job_name, job in (_load_yaml(path).get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                uses = str(step.get("uses") or "")
                if uses.startswith("actions/setup-node@"):
                    with_block = step.get("with")
                    steps.append(
                        (path, job_name, with_block if isinstance(with_block, dict) else {})
                    )
    return steps


def _from_node_lines() -> list[tuple[Path, str]]:
    """(dockerfile, image tag) for every ``FROM node:<tag>`` line in the repo."""
    return [
        (path, match.group("tag"))
        for path in _dockerfiles()
        for match in _FROM_NODE.finditer(path.read_text(encoding="utf-8"))
    ]


# ─────────────────────────── the source of truth ───────────────────────────


def test_nvmrc_exists_and_names_exactly_one_version() -> None:
    assert NVMRC.is_file(), (
        f"{NVMRC} is missing. It is the single source of truth for the Node "
        "version; without it the workflows' node-version-file: input resolves "
        "to nothing and #231 is back."
    )
    lines = [line for line in NVMRC.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1, f"{NVMRC} must hold exactly one version line, got {lines!r}"
    assert _major(lines[0]) > 0


def _pinned_major() -> int:
    return _major(NVMRC.read_text(encoding="utf-8").strip())


# ─────────────────────────────── CI workflows ───────────────────────────────


def test_at_least_one_workflow_sets_up_node() -> None:
    """Non-vacuity: the workflow rules below must have something to bind to.

    Without this, deleting every ``setup-node`` step would make the two tests
    below pass over an empty list — green CI, no frontend job at all.
    """
    assert _setup_node_steps(), (
        "no actions/setup-node step found in any workflow — the node-version "
        "rules below would pass vacuously"
    )


@pytest.mark.parametrize(
    ("path", "job_name", "with_block"),
    _setup_node_steps(),
    ids=lambda v: v.name if isinstance(v, Path) else str(v),
)
def test_setup_node_reads_the_pin_from_the_nvmrc(
    path: Path, job_name: str, with_block: dict[str, Any]
) -> None:
    declared = with_block.get("node-version-file")
    assert declared == NVMRC_WORKFLOW_PATH, (
        f"{path.name}:{job_name} must set "
        f"node-version-file: {NVMRC_WORKFLOW_PATH} (got {declared!r}). A literal "
        "node-version: here is exactly the drift #231 describes — CI would test "
        "on a version nobody reconciled with the shipped bundle."
    )
    assert (REPO_ROOT / declared).is_file(), (
        f"{path.name}:{job_name} points node-version-file at {declared!r}, which "
        "does not exist. setup-node would fail — or worse, a future default "
        "would silently apply."
    )


@pytest.mark.parametrize(
    ("path", "job_name", "with_block"),
    _setup_node_steps(),
    ids=lambda v: v.name if isinstance(v, Path) else str(v),
)
def test_setup_node_does_not_override_the_file_with_a_literal_version(
    path: Path, job_name: str, with_block: dict[str, Any]
) -> None:
    """``node-version`` WINS over ``node-version-file`` when both are present.

    Straight from actions/setup-node's own README: "If node-version and
    node-version-file are both provided the action will use version from
    node-version." So a stray ``node-version:`` alongside the file does not
    conflict loudly — it makes the .nvmrc decorative and silently restores the
    drift while the file sits there looking authoritative.
    """
    assert "node-version" not in with_block, (
        f"{path.name}:{job_name} sets a literal node-version: "
        f"{with_block.get('node-version')!r}. setup-node gives node-version "
        "precedence over node-version-file, so this is the version that actually "
        f"runs and {NVMRC_WORKFLOW_PATH} is ignored — decorative, not "
        "authoritative. Delete the node-version key."
    )


# ───────────────────────────── shipped artifact ─────────────────────────────


def test_at_least_one_dockerfile_builds_on_node() -> None:
    """Non-vacuity partner for the Dockerfile rule.

    The frontend stage in Dockerfile.api is the ONLY thing that builds the
    bundle production serves. If it disappears, the rule below would pass over
    an empty list while the image quietly shipped no SPA at all.
    """
    assert _from_node_lines(), (
        "no `FROM node:` line found in any Dockerfile — the shipped-bundle rule "
        "below would pass vacuously. Dockerfile.api's frontend stage is what "
        "builds what production serves."
    )


@pytest.mark.parametrize(
    ("path", "tag"),
    _from_node_lines(),
    ids=lambda v: v.name if isinstance(v, Path) else str(v),
)
def test_dockerfile_node_base_matches_the_pin(path: Path, tag: str) -> None:
    pinned = _pinned_major()
    assert _major(tag) == pinned, (
        f"{path.relative_to(REPO_ROOT)} builds on `node:{tag}` but "
        f"{NVMRC_WORKFLOW_PATH} pins Node {pinned}. CI would test the frontend "
        f"on {pinned} and production would ship a bundle built on "
        f"{_major(tag)} — issue #231.\n"
        "If this is a Dependabot base-image bump: the bump is fine, but it must "
        f"move BOTH. Update {NVMRC_WORKFLOW_PATH} to the same major in the same "
        "PR, so the tests that gate the change actually run on the version being "
        "shipped."
    )


# ─────────────────────────── declared engine floor ───────────────────────────


def test_package_json_engine_floor_matches_the_pin() -> None:
    """A second declaration is only safe while something compares it to the first."""
    manifest = json.loads(FRONTEND_PACKAGE_JSON.read_text(encoding="utf-8"))
    declared = (manifest.get("engines") or {}).get("node")
    assert declared, (
        "frontend/package.json has no engines.node. It tells contributors and "
        "npm which Node this package expects; without it the .nvmrc is the only "
        "signal and a local `npm ci` on the wrong major warns about nothing."
    )
    assert _major(declared) == _pinned_major(), (
        f"frontend/package.json engines.node is {declared!r} but "
        f"{NVMRC_WORKFLOW_PATH} pins Node {_pinned_major()}. Two version "
        "declarations that disagree is the defect this guard exists to stop."
    )


# ──────────────────── Node this repo cannot reach, named ────────────────────


def test_external_node_consumers_are_listed_not_silently_missed() -> None:
    """Every external workflow that runs Node must be named, with a reason.

    A reusable workflow is called with ``uses:`` at the JOB level and has no
    ``steps:``, so the ``setup-node`` scan above cannot see its Node version —
    it lives in another repository. That is a legitimate blind spot, but an
    UNRECORDED blind spot is how a guard ends up exempting most of its
    population while still reporting green. So: any job handing Node-ish inputs
    to an external workflow must appear in _EXTERNAL_NODE_OUT_OF_REACH.
    """
    unlisted: list[str] = []
    for path in _workflow_files():
        for job_name, job in (_load_yaml(path).get("jobs") or {}).items():
            if not isinstance(job, dict) or not job.get("uses"):
                continue
            inputs = job.get("with") or {}
            if not (_NODE_INPUT_KEYS & set(inputs)):
                continue
            if (path.name, job_name) not in _EXTERNAL_NODE_OUT_OF_REACH:
                unlisted.append(f"{path.name}:{job_name} -> {job['uses']}")

    assert not unlisted, (
        "job(s) run Node via an external reusable workflow whose version this "
        "repo does not control, and they are not recorded as such. Add them to "
        "_EXTERNAL_NODE_OUT_OF_REACH with the reason it is tolerable, or pin "
        "them:\n" + "\n".join(unlisted)
    )


@pytest.mark.parametrize(
    ("workflow_name", "job_name"),
    sorted(_EXTERNAL_NODE_OUT_OF_REACH),
    ids=lambda v: str(v),
)
def test_recorded_external_node_consumers_still_exist(workflow_name: str, job_name: str) -> None:
    """A stale exemption is a lie about coverage — delete it when the job goes."""
    jobs = _load_yaml(WORKFLOW_DIR / workflow_name).get("jobs") or {}
    assert job_name in jobs, (
        f"_EXTERNAL_NODE_OUT_OF_REACH names {workflow_name}:{job_name}, which no "
        "longer exists. Remove the entry so the exemption list keeps describing "
        "reality."
    )
    assert jobs[job_name].get("uses"), (
        f"{workflow_name}:{job_name} no longer calls an external workflow, so it "
        "is reachable by the setup-node rules above. Remove its exemption."
    )


# ──────────────────────────── the #231 regression ────────────────────────────


def test_the_three_locations_that_drifted_are_all_covered() -> None:
    """Pin the specific defect: the scans must actually reach all three files.

    The generic scans above pass if they find nothing to complain about — which
    includes finding nothing at all. This asserts the exact files named in #231
    are inside the covered population, so a future refactor that quietly moves
    one out of reach fails here instead of going unnoticed.
    """
    workflows_with_setup_node = {path.name for path, _, _ in _setup_node_steps()}
    assert {"frontend.yml", "frontend-live-e2e.yml"} <= workflows_with_setup_node, (
        "the two workflows from #231 no longer have a setup-node step the guard "
        f"can see; covered workflows are {sorted(workflows_with_setup_node)}"
    )

    dockerfiles_on_node = {path.name for path, _ in _from_node_lines()}
    assert "Dockerfile.api" in dockerfiles_on_node, (
        "Dockerfile.api's `FROM node:` frontend stage is no longer visible to "
        f"the guard; covered Dockerfiles are {sorted(dockerfiles_on_node)}"
    )


def test_every_node_version_in_the_repo_agrees() -> None:
    """The whole point, asserted once as a single equality over the population."""
    pinned = _pinned_major()
    majors = {f"{NVMRC_WORKFLOW_PATH}": pinned}
    for path, tag in _from_node_lines():
        majors[str(path.relative_to(REPO_ROOT))] = _major(tag)
    manifest = json.loads(FRONTEND_PACKAGE_JSON.read_text(encoding="utf-8"))
    majors["frontend/package.json engines.node"] = _major(manifest["engines"]["node"])

    assert len(set(majors.values())) == 1, (
        "Node major versions disagree across the repo (#231):\n"
        + "\n".join(f"  {where}: {major}" for where, major in sorted(majors.items()))
    )
