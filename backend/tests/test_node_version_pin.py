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

Deliberately MAJOR-only. ``.nvmrc`` holds ``26`` and the image tag is
``node:26-bookworm-slim``: both float to the newest 26.x, so patch releases flow
in without a repo edit while the major stays locked together. Pinning a full
``26.x.y`` here would drift against the floating image tag by construction.
(This paragraph said ``22`` until #413, two majors after the repo moved to 26 —
the same rot the guard exists to stop, in the guard's own prose.)

``@types/node`` joined the population in #413. It is not a runtime, so it was
recorded below as a blind spot rather than guarded: "a compile-time types
package, not a runtime pin". That reasoning was wrong in a way worth writing
down. It is the version TypeScript BELIEVES the runtime is, so at ``^20`` while
Node is 26 the compiler checks the SPA's build-time code against a DIFFERENT
API surface from the one that runs — a disagreement no runtime check can reach,
because nothing here executes. It also bounded what the toolchain could take:
vitest 5's ``@types/node`` peer is ``^22.0.0 || >=24.0.0``, so ``^20`` blocked
it (reproduced: ``npm install vitest@5 --dry-run`` exits 1 with ERESOLVE before
this bump, 0 after). Guarded now.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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

# ``FROM [--flag=value ...] [registry[:port]/][namespace/]node:<tag>``.
#
# Anchored at FROM so an unrelated ``node:http`` import or a ``python:3.14``
# base cannot match, and the ``/``-terminated registry groups mean ``mynode:18``
# does not either. The two easy-to-miss real forms are covered deliberately:
# ``FROM --platform=$BUILDPLATFORM node:18`` (common in multi-arch builds) and a
# lowercase ``from`` (Dockerfile keywords are case-insensitive). Both would
# otherwise be a silent hole in exactly the line this guard exists to watch.
_FROM_NODE = re.compile(
    r"^[ \t]*FROM\s+(?:--[\w-]+(?:=\S+)?\s+)*(?:[\w.\-]+(?::\d+)?/)*node:(?P<tag>[\w.\-]+)",
    re.MULTILINE | re.IGNORECASE,
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

# What this file CANNOT enforce. Named so the coverage claim stays honest.
_UNGUARDABLE: tuple[str, ...] = (
    "A job that runs `npm run build` with NO setup-node step: it uses the runner "
    "image's preinstalled Node. Nothing in the YAML says 'node', so no static "
    "scan can find it. Every job that sets Node up TODAY is `ubuntu-latest` "
    "(frontend.yml:build, frontend.yml:demo-e2e, frontend-live-e2e.yml:live-e2e "
    "— enumerated, not assumed), and ubuntu-latest MEASURED as ubuntu-24.04 "
    "(image 20260907.300.1, run 34449722396). NOT every job in the repo: "
    "ci.yml:shell-tests is a matrix over ubuntu-latest AND macos-latest, and "
    "pr-quality.yml:quality-gate declares no runs-on here at all. This entry's "
    "own earlier text put that image's preinstalled Node at 22.23.2, which is "
    "INHERITED, not re-measured. On those facts the mismatch is live TODAY — "
    "the entry used to say it arrived only when ubuntu-latest rolled, which was "
    "true while .nvmrc said 22 and stopped being true at 26.",
    "A Dockerfile that resolves its base through ARG indirection "
    "(`ARG NODE_VERSION=18` + `FROM node:${NODE_VERSION}`). Deliberately not "
    "supported: the literal FROM is what keeps Dependabot tracking the image.",
    "Node inside the external reusable workflow's own repository, and any Node a "
    "third-party action installs internally.",
    "Whether _container_node_images() reads the REAL workflow directory. While "
    "the repo has zero `container:` jobs its only observation over the real tree "
    "is an empty `found`, so a mutant keyed on `directory == WORKFLOW_DIR` that "
    "returns no images AND fabricates the right `examined` list is behaviourally "
    "identical to the finder. The planted-tree test proves the SCAN works, and "
    "the join DOES pin the zero-arg call to whatever `_workflow_files()` returns "
    "— what neither can rule out is a mutant that also FABRICATES `examined` to "
    "match. The day a real `container: node:` job lands, that mutant dies — "
    "until then there is no positive observation to make.",
    "What @types/node RESOLVES to, as opposed to the range package.json declares. "
    "test_types_node_major_matches_the_pin reads the declared range only. Two of "
    "the three ways that could diverge turn out to be closed by the tooling, and "
    "saying so is the point of this register — an overstated blind spot is as "
    "much a bookkeeping error as a missing one. (a) An `overrides` block "
    "redirecting the install: npm FORECLOSES it. Measured during #422 review — "
    'adding `"overrides": {"@types/node": "20.19.43"}` beside the '
    "`^26.5.1` devDependency fails the install outright with `npm error code "
    "EOVERRIDE / Override for @types/node@^26.5.1 conflicts with direct "
    "dependency`. npm accepts an override on a DIRECT dependency only when the "
    "specs match exactly, i.e. only when it cannot redirect anything. "
    "`resolutions` is a yarn key npm ignores entirely. (b) package-lock.json "
    "naming a version outside the range: covered MECHANICALLY — `npm ci` refuses "
    "such a lockfile (`npm error Invalid: lock file's @types/node@20.19.43 does "
    "not satisfy @types/node@26.5.1`), and every job that installs runs `npm ci`: "
    "frontend.yml:47, frontend.yml:101, frontend-live-e2e.yml:108, plus the "
    "external quality-gate (enumerated, not counted). (c) THE REAL RESIDUE: a "
    "local `npm install --force`, which overrides both refusals and is reachable "
    "by no check in this repo.",
)

# Hand-counted off the tuple above. Truthiness alone is not a guard: measured,
# deleting a whole entry left the file at `31 passed, 1 skipped`. Same
# convention as _REAL_SETUP_NODE_STEPS below — update this line deliberately
# when a blind spot is genuinely added or genuinely closed.
#
# Unchanged at 5 across #413, which is a coincidence worth spelling out rather
# than leaving as a silent no-op: one entry CLOSED (@types/node is now enforced
# by test_types_node_major_matches_the_pin and carried in the population of
# test_every_node_version_in_the_repo_agrees) and one OPENED (that new guard
# reads the declared range, not what npm resolves). The opened one is NARROWER
# than it first read: #422 review measured that npm forecloses the `overrides`
# route and `npm ci` forecloses the stale-lockfile route, leaving only a local
# `npm install --force`. It is still a real hole, so the entry stays and the
# number stays — but it is one hole, not three. Closing a blind spot with a
# guard that has its own is the normal case, and the register is only honest if
# the new one goes in the same edit.
_UNGUARDABLE_COUNT = 5

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


def _workflow_files(directory: Path = WORKFLOW_DIR) -> list[Path]:
    """Every workflow file under ``directory``.

    Takes a ``directory`` for the same reason
    ``test_deploy_fly_build_args_are_documented._scanned_files`` takes a ``root``:
    so a test can drive the REAL scan over a copy of the real tree that has a
    planted offender in it. The repo has zero jobs with a ``container:`` image,
    so nothing else can tell a working scan from a dead one.
    """
    files = sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml"))
    assert files, f"no workflow files found under {directory}"
    return files


def _dockerfiles() -> list[Path]:
    """Every Dockerfile in the WORKING TREE, pruning vendored/build trees.

    Working tree, not ``git ls-files``: an untracked Dockerfile still builds, so
    it still has to agree. The cost is that a scratch Dockerfile on a laptop can
    fail this locally; CI checks out clean, so it cannot fire there.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
        found.extend(
            Path(dirpath) / name
            for name in filenames
            if name.startswith("Dockerfile") or name.lower().endswith(".dockerfile")
        )
    assert found, f"no Dockerfile found under {REPO_ROOT}"
    return sorted(found)


def _setup_node_steps(directory: Path = WORKFLOW_DIR) -> list[tuple[Path, str, dict[str, Any]]]:
    """(workflow, job, ``with:`` mapping) for every ``actions/setup-node`` step."""
    steps: list[tuple[Path, str, dict[str, Any]]] = []
    for path in _workflow_files(directory):
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


def _container_node_images(
    directory: Path = WORKFLOW_DIR,
) -> tuple[list[tuple[Path, str, str]], list[tuple[str, str]]]:
    """``(found, examined)`` for every job-level ``container:`` on a node image.

    ``container: node:18`` runs every step of a job inside that image, with no
    ``setup-node`` step to scan. It is a second, entirely separate way to build
    the bundle on an unpinned Node, so it gets the same major rule.

    ``found`` is ``(workflow, job, tag)`` per node image. ``examined`` is every
    ``(workflow name, job name)`` this call actually VISITED, appended inside the
    same loop — the repo has zero node containers, so ``found == []`` is the only
    thing the real tree can observe and it is exactly what a dead scan returns.
    ``examined`` is what a dead scan cannot fake: a separate enumerator would
    prove nothing, so it has to be collected here, by this loop, as it walks.
    """
    found: list[tuple[Path, str, str]] = []
    examined: list[tuple[str, str]] = []
    for path in _workflow_files(directory):
        for job_name, job in (_load_yaml(path).get("jobs") or {}).items():
            examined.append((path.name, job_name))
            if not isinstance(job, dict):
                continue
            container = job.get("container")
            image = container.get("image") if isinstance(container, dict) else container
            if not isinstance(image, str):
                continue
            match = re.match(r"(?:[\w.\-]+(?::\d+)?/)*node:(?P<tag>[\w.\-]+)", image.strip())
            if match:
                found.append((path, job_name, match.group("tag")))
    return found, examined


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
    assert re.fullmatch(r"\d+", lines[0].strip()), (
        f"{NVMRC} must hold a BARE MAJOR (e.g. `26`), got {lines[0]!r}. A full "
        "`26.x.y` would pin CI to one patch release while the Docker tag "
        "`node:26-bookworm-slim` keeps floating to the newest 26.x — the two "
        "would drift by construction, which is what this file exists to prevent."
    )


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
    assert re.fullmatch(r">=\s*\d+", declared.strip()), (
        f"frontend/package.json engines.node is {declared!r}. It must be a FLOOR "
        "of the form `>=<major>`. A ceiling or exact pin (`<=22`, `22.1.0`) reads "
        "as agreement to the major comparison below while actually permitting a "
        "different Node entirely."
    )
    assert _major(declared) == _pinned_major(), (
        f"frontend/package.json engines.node is {declared!r} but "
        f"{NVMRC_WORKFLOW_PATH} pins Node {_pinned_major()}. Two version "
        "declarations that disagree is the defect this guard exists to stop."
    )


# ──────────────────────── the types that describe it ────────────────────────

# Range forms that keep @types/node INSIDE one major: a caret, a tilde, or an
# exact version, at any precision — `^26`, `^26.5`, `^26.5.1`, `26.5.1`,
# `~26.5.1`, `26.5.1-rc.1`. Deliberately NOT `>=<major>`, which is the correct
# shape for `engines.node` (a floor: "this or newer runs it") and the wrong one
# here. `>=26` reads as agreement with the pin while silently accepting 27 and
# 28 — the same class of trap as `node-version` overriding `node-version-file`
# above, where the authoritative-looking value is not the one that applies.
#
# The minor and patch components are OPTIONAL on purpose. An earlier version of
# this pattern demanded all three, which rejected `^26` — the very bare-major
# convention this file MANDATES for `.nvmrc` twenty lines up — and failed it
# with a message asserting the range was not bounded to one major, when it was.
# Measured during #422 review: `^26`, `26.x` and `^26.0.0-rc.1` each resolve
# only inside major 26 and each turned the required `pytest + lint` check red.
# A guard that reddens on a correct manifest is a guard someone deletes.
_TYPES_NODE_RANGE = re.compile(r"[\^~]?\d+(?:\.(?:\d+|x)(?:\.(?:\d+|x)[\w.+-]*)?)?")

# Both manifest sections npm reads for a direct dependency. BOTH, not just
# `devDependencies`: adversarial review measured that a guard reading only
# `devDependencies` stays GREEN on a manifest declaring `@types/node` in
# `dependencies` too, at a DIFFERENT major — npm accepts that manifest, `npm
# ci` accepts the resulting lockfile, and the repo then names two majors with
# every test passing. Reading the union is what makes "the declared major" a
# single value rather than whichever one the guard happened to look at.
_TYPES_NODE_SECTIONS = ("dependencies", "devDependencies")


def _declared_types_node() -> str:
    """The ONE `@types/node` spec in frontend/package.json, or fail saying why."""
    manifest = json.loads(FRONTEND_PACKAGE_JSON.read_text(encoding="utf-8"))
    found = {
        section: (manifest.get(section) or {})["@types/node"]
        for section in _TYPES_NODE_SECTIONS
        if "@types/node" in (manifest.get(section) or {})
    }
    assert found, (
        "frontend/package.json declares @types/node in neither `dependencies` "
        "nor `devDependencies`. Without it TypeScript falls back to whatever "
        "@types/node a transitive dep drags in — an unpinned, unreviewed "
        "version of the very thing this test exists to hold to the runtime "
        "(#413)."
    )
    assert len(found) == 1, (
        f"frontend/package.json declares @types/node twice: {found}. npm accepts "
        "this and so does `npm ci`, so two majors can sit in one manifest with "
        "every check green — measured during the #413 review. Keep exactly one "
        "declaration, in devDependencies."
    )
    return next(iter(found.values()))


def test_types_node_major_matches_the_pin() -> None:
    """@types/node is the version TypeScript believes the runtime is (#413).

    Not a runtime pin, which is why this file recorded it as a blind spot for
    two majors instead of guarding it. But a types package is a claim about the
    runtime, and a false one fails in the direction no runtime check can see:
    the compiler is checking against a DIFFERENT API surface from the one that
    runs, and every test still passes because none of this survives to
    execution. (Stated as the class it is. No such error has been OBSERVED in
    this repo — `tsc` reported zero errors before and after the bump — so the
    hazard is the drift itself, not a defect on record.) It also bounds what
    the toolchain can take: at ^20 the repo could not accept vitest 5, whose
    peer range is `^22.0.0 || >=24.0.0`.

    Which change turns this RED: setting frontend/package.json's
    `@types/node` back to `^20.16.0` (or any major other than .nvmrc's).
    """
    declared = _declared_types_node()
    assert _TYPES_NODE_RANGE.fullmatch(declared.strip()), (
        f"frontend/package.json @types/node is {declared!r}. It must be bounded "
        "to ONE major (`^26.5.1`, `~26.5.1` or an exact `26.5.1`). A floor like "
        "`>=26`, or a multi-major `||` range, satisfies the comparison below "
        "today while letting npm resolve a major nobody reconciled with "
        f"{NVMRC_WORKFLOW_PATH}."
    )
    assert _major(declared) == _pinned_major(), (
        f"frontend/package.json @types/node is {declared!r} but "
        f"{NVMRC_WORKFLOW_PATH} pins Node {_pinned_major()}, so TypeScript is "
        f"checking the frontend against Node {_major(declared)}'s API surface "
        "while CI, the Docker image and production all run "
        f"{_pinned_major()}. Bump @types/node to the same major in this PR."
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


@pytest.mark.parametrize(
    ("path", "job_name", "tag"),
    _container_node_images()[0],
    ids=lambda v: v.name if isinstance(v, Path) else str(v),
)
def test_job_container_node_images_match_the_pin(path: Path, job_name: str, tag: str) -> None:
    """A ``container:`` node image ships the same drift as a Dockerfile FROM."""
    assert _major(tag) == _pinned_major(), (
        f"{path.name}:{job_name} runs in container `node:{tag}` but "
        f"{NVMRC_WORKFLOW_PATH} pins Node {_pinned_major()}."
    )


# ─────────── the finders themselves, driven over a planted workflow ───────────
#
# #381: `test_job_container_node_images_match_the_pin` SKIPS with "got empty
# parameter set" because no job in this repo uses `container:`. That is honest
# today, but nothing proved `_container_node_images()` still WORKS — a dead
# finder returns the same empty list forever and the rule is disabled with no
# visible change. `_setup_node_steps()` had the same hole one layer down: its
# two existing partners only assert that SOME steps are found, so three ways of
# stopping the walk early were all survivable.
#
# The plant below is a real workflow file. Its FIRST job is deliberately
# unscannable, so turning either finder's `continue` into a `break` loses every
# job after it and reddens these tests. It is written into a COPY of the real
# `.github/workflows` directory, under a name that sorts LAST, so the fixture
# tracks the real population and a scan that stops after the first file cannot
# reach it.
_PLANTED_WORKFLOW = """\
name: planted
on: workflow_dispatch
jobs:
  zeroth-job-not-a-mapping: a scalar, not a job mapping
  first-job-plain:
    runs-on: ubuntu-24.04
    steps:
      - run: echo no container at all
  second-job-non-node-container:
    runs-on: ubuntu-24.04
    container: python:3.14-slim
    steps:
      - run: echo a container that is not node
  second-b-job-lookalike-image:
    runs-on: ubuntu-24.04
    container: mynode:18
    steps:
      - run: echo an image whose NAME merely ends in node
  second-c-job-lookalike-behind-a-registry:
    runs-on: ubuntu-24.04
    container: myregistry.io/mynode:18
    steps:
      - run: echo the same lookalike, behind a registry prefix
  third-job-string-container:
    runs-on: ubuntu-24.04
    container: node:18-alpine
    steps:
      - run: echo the string form
  fourth-job-dict-container:
    runs-on: ubuntu-24.04
    container:
      image: docker.io/library/node:19
    steps:
      - run: echo the mapping form
  fifth-job-setup-node:
    runs-on: ubuntu-24.04
    steps:
      - a scalar step, not a mapping
      - uses: actions/setup-node@v6
        with:
          node-version-file: frontend/.nvmrc
      - uses: actions/setup-node@v6
        with:
          cache: npm
"""

# Every job name in _PLANTED_WORKFLOW, in file order. Hand-read off the YAML
# above, NOT parsed out of it — deriving it would make the assertion agree with
# any bug in the parser it is checking.
_PLANTED_JOBS = (
    "zeroth-job-not-a-mapping",
    "first-job-plain",
    "second-job-non-node-container",
    "second-b-job-lookalike-image",
    "second-c-job-lookalike-behind-a-registry",
    "third-job-string-container",
    "fourth-job-dict-container",
    "fifth-job-setup-node",
)

# The three `container:` values in the plant that are NOT the node image. They
# are what makes the scan's pattern a RECOGNISER rather than a substring search:
# the sibling `_FROM_NODE` guard already carries such a table (`FROM mynode:18`
# -> None in test_the_dockerfile_scan_recognises_real_from_syntaxes), and
# without one here, relaxing `re.match(r"(?:[\w.\-]+(?::\d+)?/)*node:...")` to a
# bare `re.search(r"node:...")` was measured to SURVIVE the whole file — so
# `container: mynode:18` would be reported as a Node image and nothing noticed.
_PLANTED_NON_NODE_JOBS = (
    "second-job-non-node-container",  # python:3.14-slim
    "second-b-job-lookalike-image",  # mynode:18
    "second-c-job-lookalike-behind-a-registry",  # myregistry.io/mynode:18
)

# The setup-node steps the real repo has, hand-counted from
# `.github/workflows/` (3, in sorted-filename order). Update this line when a
# workflow gains or loses a setup-node step.
_REAL_SETUP_NODE_STEPS = [
    ("frontend-live-e2e.yml", "live-e2e"),
    ("frontend.yml", "build"),
    ("frontend.yml", "demo-e2e"),
]


def _workflow_dir_copy_with_a_plant(tmp_path: Path) -> Path:
    """A copy of the REAL workflow directory plus one planted workflow file.

    A small synthetic fixture is not enough: in this repo's sibling guard a
    four-file synthetic tree let ``if len(processed) > 50: continue`` survive the
    entire suite. Copying the real directory makes the fixture size track
    reality. The plant is named ``zz-planted.yml`` so it sorts LAST — a plant
    that sorts first cannot catch a scan that stops after the first file.
    """
    real = _workflow_files()
    for path in real:
        shutil.copyfile(path, tmp_path / path.name)
    (tmp_path / "zz-planted.yml").write_text(_PLANTED_WORKFLOW, encoding="utf-8")

    # The plant's sort position is load-bearing, so assert it in the ORDER the
    # scan actually walks — `_workflow_files` puts every `.yaml` after every
    # `.yml`, so comparing names alone would be wrong the day a `.yaml` lands.
    # Measured: renaming the plant `aa-planted.yml` and truncating the scan to
    # `files[:1]` left test_the_container_scan_finds_a_planted_node_image
    # passing; the stop-after-the-first-file mutant is only catchable while the
    # plant genuinely comes last.
    walked = [path.name for path in _workflow_files(tmp_path)]
    assert len(walked) == len(real) + 1, (
        f"the copied tree holds {walked}, want the {len(real)} real workflows plus the plant"
    )
    assert walked[-1] == "zz-planted.yml", (
        f"the plant is no longer the last file the scan walks: {walked}"
    )
    return tmp_path


def test_the_container_scan_finds_a_planted_node_image(tmp_path: Path) -> None:
    """Drive the REAL container scan over a tree that HAS node containers.

    RED if `_container_node_images` returns `[]`, if its `node:` pattern stops
    matching, if the mapping-form `container.image` arm is dropped, if either
    `continue` becomes a `break`, or if it stops honouring `directory`.
    """
    directory = _workflow_dir_copy_with_a_plant(tmp_path)

    found, examined = _container_node_images(directory)

    # Hand-read off _PLANTED_WORKFLOW: exactly two of its eight jobs run a node
    # image, one written as a string and one as a mapping.
    assert [(path.name, job, tag) for path, job, tag in found] == [
        ("zz-planted.yml", "third-job-string-container", "18-alpine"),
        ("zz-planted.yml", "fourth-job-dict-container", "19"),
    ], f"the container scan did not find exactly the two planted node images: {found}"
    # ...and it is not "flags anything with node in it": none of the three
    # non-node containers may appear. Without this, `re.search` in place of
    # `re.match` survives and `container: mynode:18` reads as a Node image.
    flagged = {job for _, job, _ in found}
    assert not flagged & set(_PLANTED_NON_NODE_JOBS), (
        "a container that is not the node image was reported as one: "
        f"{sorted(flagged & set(_PLANTED_NON_NODE_JOBS))}"
    )
    # ...and it walked past the unscannable first job to reach them.
    assert [job for name, job in examined if name == "zz-planted.yml"] == list(_PLANTED_JOBS), (
        "the scan did not visit every job of the planted workflow: "
        f"{[job for name, job in examined if name == 'zz-planted.yml']}"
    )


def test_the_container_scan_examines_every_job_in_every_workflow() -> None:
    """The join between the RULE and the POPULATION, recomputed live.

    A `>= N` floor does not work here and it was measured: with the `node:`
    pattern dead, with the mapping-form arm dropped, or with `if match:` never
    taken, the scan still walks all 7 workflows and all 15 jobs — so a
    population count stays green while the rule does nothing. This is an
    EQUALITY against the jobs recomputed from `_workflow_files()`, and it goes
    through the same zero-arg entry point the parametrize uses: routed through
    an internal helper instead, a wrong-directory mutant survives.

    RED if the scan skips a workflow, skips a job, or truncates either list.

    WHAT IT CANNOT SEE, precisely: `expected` is recomputed from the same
    `_workflow_files()` that `examined` flows through, so a mutation INSIDE
    `_workflow_files` — `return files + files`, `return files[:1]`, a changed
    glob — moves both sides identically and this equality stays green. Its bite
    is against mutants LOCAL to `_container_node_images`'s own walk; the
    population helper itself is pinned elsewhere, by
    `test_the_container_scan_finds_a_planted_node_image` (which drives a tree
    whose contents this test does not control) and by
    `test_the_three_locations_that_drifted_are_all_covered`.
    """
    _found, examined = _container_node_images()

    expected = [
        (path.name, job_name)
        for path in _workflow_files()
        for job_name in (_load_yaml(path).get("jobs") or {})
    ]
    assert expected, "no jobs found in any workflow — this join would pass vacuously"
    assert sorted(examined) == sorted(expected), (
        "the container scan did not visit every job in every workflow. "
        f"missed: {sorted(set(expected) - set(examined))}; "
        f"unexpected: {sorted(set(examined) - set(expected))}"
    )


def test_the_setup_node_scan_finds_every_planted_step(tmp_path: Path) -> None:
    """Same plant, for the finder the two existing partners only half-cover.

    `test_at_least_one_workflow_sets_up_node` and
    `test_the_three_locations_that_drifted_are_all_covered` both pass on a scan
    that stops early, because the steps they name are found first. Measured: the
    job-not-a-mapping `continue`, the step-not-a-mapping `continue`, and
    "stop after the first matching step in a job" all became a `break` without
    either test noticing.

    RED if any of those three walks stops early, or if `_workflow_files` is
    truncated inside this finder.
    """
    directory = _workflow_dir_copy_with_a_plant(tmp_path)

    steps = _setup_node_steps(directory)

    planted = [(job, block) for path, job, block in steps if path.name == "zz-planted.yml"]
    # Hand-read off _PLANTED_WORKFLOW: one scalar step (skipped) then two
    # setup-node steps in the SAME job, given different `with:` blocks so
    # "kept both" is distinguishable from "kept one twice".
    assert planted == [
        ("fifth-job-setup-node", {"node-version-file": "frontend/.nvmrc"}),
        ("fifth-job-setup-node", {"cache": "npm"}),
    ], f"the setup-node scan did not find exactly the two planted steps: {planted}"
    real = [(path.name, job) for path, job, _ in steps if path.name != "zz-planted.yml"]
    assert real == _REAL_SETUP_NODE_STEPS, (
        f"the copied real workflows contributed {real}, want {_REAL_SETUP_NODE_STEPS}"
    )


def test_the_guards_own_blind_spots_are_written_down() -> None:
    """State what this file CANNOT see, so the gap is recorded, not assumed away.

    Adversarial review found ways to build the SPA on an unpinned Node that no
    static scan here can catch. The dangerous one is dated, not hypothetical: a
    job that runs ``npm run build`` with NO ``setup-node`` step uses the runner
    image's preinstalled Node. Every job that sets Node up today runs on
    ``ubuntu-latest``, measured as ubuntu-24.04, whose preinstalled Node this
    register records as 22.x — so such a job would build on the wrong major
    immediately. It does not have to wait for the image to roll, which is what
    this said while .nvmrc still held 22. (Not every job in the repo is
    ubuntu-latest — ``ci.yml``'s ``shell-tests`` is a matrix that includes
    macos-latest — but none of those build the SPA.)

    This test asserts nothing about the workflows. It exists so the limitation
    is in the file a maintainer reads, rather than discovered the hard way.
    """
    assert _UNGUARDABLE, "the blind-spot register must not be silently emptied"
    # Truthiness alone lets any single entry be deleted in silence, which is how
    # a recorded limitation quietly stops being recorded. Pin the hand-count.
    assert len(_UNGUARDABLE) == _UNGUARDABLE_COUNT, (
        f"the blind-spot register holds {len(_UNGUARDABLE)} entries, "
        f"_UNGUARDABLE_COUNT says {_UNGUARDABLE_COUNT}. Adding or closing a "
        "blind spot is a deliberate act: update the count in the same edit."
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
    # #413: the types belong to the population too. The dedicated test above
    # carries the shape rule and the better message; this is what stops the
    # POPULATION from quietly shrinking back to "the runtimes only". Routed
    # through the same resolver, so a deleted key fails on that resolver's
    # named assertion rather than on a bare KeyError from this line.
    majors["frontend/package.json @types/node"] = _major(_declared_types_node())

    assert len(set(majors.values())) == 1, (
        "Node major versions disagree across the repo (#231):\n"
        + "\n".join(f"  {where}: {major}" for where, major in sorted(majors.items()))
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # Forms that MUST be recognised, or a real Node base slips past the guard.
        ("FROM node:22-bookworm-slim AS frontend", "22-bookworm-slim"),
        ("FROM --platform=linux/amd64 node:18", "18"),
        ("FROM --platform=$BUILDPLATFORM node:18-alpine AS build", "18-alpine"),
        ("from node:18", "18"),
        ("FROM docker.io/library/node:18-slim", "18-slim"),
        ("FROM mcr.microsoft.com/devcontainers/node:18", "18"),
        ("FROM localhost:5000/node:20", "20"),
        # Forms that must NOT match, or the guard fails on unrelated images.
        ("FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim", None),
        ("FROM python:3.14-slim-bookworm", None),
        ("FROM mynode:18", None),
        ("# FROM node:14 old comment", None),
        ("RUN echo FROM node:14", None),
    ],
)
def test_the_dockerfile_scan_recognises_real_from_syntaxes(line: str, expected: str | None) -> None:
    """The scan is only as good as its pattern, so pin the pattern itself.

    ``--platform`` and a lowercase ``from`` are both legal Dockerfile syntax and
    both were missed by the first version of this regex. A hole here is
    invisible: the guard would report green while a ``FROM node:18`` sat in the
    tree. Drop the ``--flag`` group or ``re.IGNORECASE`` and this goes red.
    """
    match = _FROM_NODE.search(line)
    assert (match.group("tag") if match else None) == expected
