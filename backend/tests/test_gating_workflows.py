"""Guards the workflows that gate a merge against silently stopping gating (#379).

`Live-mode Playwright (stub backend)` runs a whole browser suite — the only job
that EXECUTES #62 (the composer refusing a second in-flight question) and the
refused-submit half of #356 (``behavior.spec.ts:629-630``) — and it was not a
required status check.

Measured 2026-09-08, correcting an earlier claim in this file: #356's
ARRIVING-answer announcement is not live-only. ``behavior.spec.ts:1453`` asserts
it unskipped in the REQUIRED ``Demo-mode Playwright`` job, and
``ChatView.test.tsx`` covers it in vitest inside the REQUIRED
``type-check + unit tests + build`` job. Re-derived 2026-09-09:
``grep -rn "Answer ready" frontend/src`` → 20 hits, 5 of them in
``ChatView.tsx`` and 15 in ``ChatView.test.tsx``; ``frontend/tests`` → 4.
(An earlier version of this note, and of docs/TEST_STRATEGY.md §12.2, read
"15 hits under ``frontend/src``" — that 15 is the TEST file's share, not the
directory's.)

The live workflow could not become required, because it was PATH-FILTERED to
``frontend/**``: GitHub leaves a required check that never triggers at
"Expected — waiting for status to be reported", so on 13 of the last 30 merged
PRs (re-derived 2026-09-08; a 30-PR window that MOVES — an earlier reading said
14, and #333 has since fallen out of it) it would have blocked the merge from
the day it was promoted until someone touched a ``frontend/**`` file to make the
workflow fire at all.
(GitHub's own doc: "Handling skipped but required checks",
https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks)

Why tests rather than comments. Every rule below is a two-place agreement that
nothing else compares:

* a ``paths:`` filter re-added to a gating workflow reads as a harmless
  optimisation and is invisible until a docs-only PR wedges;
* the job ``name:`` is the exact string the owner types into branch protection,
  so a one-character edit makes the documented promotion command silently wrong
  — it would add a context nothing ever reports, wedging every PR;
* the live job's selection guard counts what ``--list`` selects, and ``--list``
  COUNTS SKIPPED TESTS, so a run that executes nothing still satisfies it. Only
  a step that reads the run's own JSON ``stats`` can tell the difference;
* the regex literal in ``frontend.yml``'s demo-side rename trap has to stay
  equal to ``playwright.live.config.ts``'s ``grep:``, or that trap checks a
  selection nobody uses;
* every rule above is defused by three words. ``continue-on-error: true`` on a
  gating job's assertion step, or a job-level ``if: false``, leaves the check
  GREEN while it asserts nothing — a review-round mutant put
  ``continue-on-error: true`` on ``frontend.yml``'s "Assert the suite actually
  executed" step (a REQUIRED context today) and it survived this whole file,
  the backend suite and ruff. ``test_a_gating_job_is_not_defused`` is what sees
  that now.

These run in ``ci.yml``'s ``test`` job ("pytest + lint"), which is a required
context and has ``pull_request: branches: [main]`` with NO ``paths:`` filter —
so they fire on a PR that touches nothing but a workflow file.

What this file CANNOT see is recorded in ``_UNGUARDABLE`` below.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
FRONTEND = REPO_ROOT / "frontend"
LIVE_WORKFLOW = WORKFLOW_DIR / "frontend-live-e2e.yml"
DEMO_WORKFLOW = WORKFLOW_DIR / "frontend.yml"
LIVE_PW_CONFIG = FRONTEND / "playwright.live.config.ts"
TEST_STRATEGY = REPO_ROOT / "docs" / "TEST_STRATEGY.md"

# The status-check CONTEXT string. Branch protection stores contexts as opaque
# strings and never validates them, so a typo here is indistinguishable from a
# check that has not reported yet.
LIVE_JOB_NAME = "Live-mode Playwright (stub backend)"

# Every required status check on `main`, mapped to the workflow that produces
# it. Measured 2026-09-08:
#
#   gh api repos/imrohitagrawal/citevyn/branches/main/protection \
#     --jq .required_status_checks.contexts
#
# and cross-checked against the check-runs on PR #386
# (`gh pr view 386 --json statusCheckRollup`), which is where the
# `.workflowName` for each context comes from.
_REQUIRED_CONTEXT_WORKFLOWS: dict[str, str] = {
    "Analyze (python)": "codeql.yml",
    "alembic + postgres integration tests": "ci.yml",
    "pytest + lint": "ci.yml",
    "quality-gate / quality-gate": "pr-quality.yml",
    "type-check + unit tests + build": "frontend.yml",
    "Demo-mode Playwright (no visual snapshots)": "frontend.yml",
    "shell suites (bash 3.2 + 5.x) (ubuntu-latest)": "ci.yml",
    "shell suites (bash 3.2 + 5.x) (macos-latest)": "ci.yml",
}

# Required contexts that NO workflow in this repo produces, with the reason.
# Listing them keeps the mapping above honest: an unexplained gap would look
# identical to a workflow this guard forgot to cover.
_REQUIRED_CONTEXTS_NOT_FROM_A_WORKFLOW: dict[str, str] = {
    "CodeQL": (
        "Reported by GitHub's code-scanning app once analysis results are "
        "uploaded, not by a job in codeql.yml. On PR #386 its check-run carries "
        "an empty `workflowName` while `Analyze (python)` carries `CodeQL`. "
        "There is no YAML in this repo to path-filter, so nothing to assert."
    ),
}

# The workflow files this guard holds to the no-path-filter rule: everything
# hosting a required context today, PLUS the live workflow, which #379 is about
# making eligible. Sorted so the population is stable and the first item
# (`ci.yml`) is one that has always been compliant — a rule that only ever saw
# the offender would look identical to a rule that never ran.
GATING_WORKFLOWS: tuple[str, ...] = tuple(
    sorted({*_REQUIRED_CONTEXT_WORKFLOWS.values(), LIVE_WORKFLOW.name})
)

# The individual JOBS held to the "not defused" rule below, as
# ``(workflow file, job id)``. This is a hand-picked list rather than "every job
# in GATING_WORKFLOWS", because two jobs in ``ci.yml`` carry a DELIBERATE
# job-level ``if:`` (see _NOT_HELD_TO_THE_DEFUSING_RULE) and a blanket rule would
# have to be weakened until it caught nothing.
#
# Ordered so the FIRST entry is `ci.yml`'s `test` — the longest-standing gating
# job here and not the one #379 is about, so the rule is exercised against a
# known-good control as well as against the new candidate.
GATING_JOBS: tuple[tuple[str, str], ...] = (
    # `pytest + lint`. Required, and it is the job that RUNS this file: one
    # `continue-on-error: true` here turns every rule in it advisory at once.
    ("ci.yml", "test"),
    # `type-check + unit tests + build`. Required; carries the vitest suite and
    # the bundle budget gate.
    ("frontend.yml", "build"),
    # `Demo-mode Playwright (no visual snapshots)`. Required; carries the
    # #379 title-rename trap, which is the only check that bites TODAY.
    ("frontend.yml", "demo-e2e"),
    # `Live-mode Playwright (stub backend)`. Not required yet; #379 is about
    # keeping it eligible, which a defused job is not.
    (LIVE_WORKFLOW.name, "live-e2e"),
)

# Gating jobs deliberately NOT in the population above, with the reason. An
# unexplained omission would look identical to one this guard forgot.
_NOT_HELD_TO_THE_DEFUSING_RULE: dict[str, str] = {
    "ci.yml:postgres-migrations": (
        "`alembic + postgres integration tests` IS a required context but carries "
        "a deliberate job-level `if:` restricting it to same-repo pushes and PRs "
        "(forks get no Postgres service and no secrets). Holding it to `no job "
        "`if:`` would either fail on `main` or force an exemption so specific it "
        "would re-admit `if: false`."
    ),
    "codeql.yml:analyze / pr-quality.yml:quality-gate": (
        "Required contexts produced outside #379's scope. Adding them is a "
        "separate change with its own review; recorded so the gap is visible."
    ),
}

# The ONLY step-level ``if:`` values tolerated inside a gating job, keyed by
# ``(workflow, job id, step name)`` and compared BYTE-EXACTLY, never by
# substring. Every one is an artifact-upload step: it carries no assertion, so
# skipping it cannot weaken the gate. Values read out of the YAML on 2026-09-08.
_SANCTIONED_STEP_CONDITIONS: dict[tuple[str, str, str], str] = {
    ("ci.yml", "test", "Upload coverage report"): "${{ !cancelled() }}",
    ("frontend.yml", "build", "Upload dist artifact"): "success()",
    ("frontend.yml", "demo-e2e", "Upload report + traces on failure"): "failure()",
    (LIVE_WORKFLOW.name, "live-e2e", "Upload HTML report on failure"): "failure()",
}

# The five tests the live suite selects, pinned as (spec file, test title).
#
# Measured with `npx playwright test --config=playwright.live.config.ts --list`
# (Total: 5 tests in 2 files) and reproduced by the static scan below.
#
# Why pin the SET and not just a count: four of the five self-skip in demo mode,
# and `frontend.yml`'s rename trap covers those. The fifth
# (`behavior.spec.ts:1272`, the #302 landing re-ask) has NO `if (!isLive)` guard
# and PASSES in demo mode, so renaming its title away from the grep would drop
# it from the live suite with every job still green. This is what catches that.
_LIVE_SUITE_MEMBERSHIP: frozenset[tuple[str, str]] = frozenset(
    {
        (
            "answer-format.spec.ts",
            "a multi-line answer renders as multiple lines, not one blob (live only)",
        ),
        (
            "answer-format.spec.ts",
            "renders the subset, chips the markers, collapses cards per document (live only)",
        ),
        (
            "behavior.spec.ts",
            "loading indicator: pending-bubble renders 3 dots + label when state.pending "
            "is true (live only)",
        ),
        (
            "behavior.spec.ts",
            "composer refuses a second question while one is in flight (live only)",
        ),
        (
            "behavior.spec.ts",
            "re-asking from a landing section scrolls to the existing answer and STAYS "
            "there (regression is live only)",
        ),
    }
)

# What these guards CANNOT see. Named so the coverage claim stays honest.
_UNGUARDABLE: tuple[str, ...] = (
    "Branch protection itself. This file cannot read it (that needs a token and "
    "a network call from a test), so it cannot detect a context being REMOVED "
    "from the required list, nor confirm one was added. The measured snapshot in "
    "_REQUIRED_CONTEXT_WORKFLOWS is a 2026-09-08 reading, not a live query.",
    "A live-only test selected by its DESCRIBE title rather than its own test "
    'title. The static scan below reads `test("...")` titles only, so such a '
    "test would be missing from the membership set and would fail the pin — "
    "loudly, but with a misleading message. All five today match on their own "
    "title.",
    "A test whose title is built from a variable or a template literal with a "
    "substitution. The scan reads string literals; `test(`... ${x}`)` is skipped. "
    "`visual.spec.ts` is the live example: one templated declaration that "
    "Playwright expands to 22 selected tests.",
    "JavaScript this file's comment stripper does not lex. `_strip_comments` "
    "understands string literals and comments, not regex literals or `${…}` "
    "substitutions. TWO CORRECTIONS, both re-derived 2026-09-09. (1) An earlier "
    "version of this entry said no regex literal appears in "
    "frontend/tests/*.spec.ts. Three do — `grep -rn '\\\\/\\\\/' "
    "frontend/tests/*.spec.ts` finds fonts-offline.spec.ts:53 and :217 and "
    "harness.spec.ts:57, all `/^https:\\/\\/…/` route matchers. (2) The example "
    "that entry gave for the hazard, `/a\\/\\/b/`, is not an instance of it: "
    "the source characters are `\\`,`/`,`\\`,`/`, which contain no `//` bigram, "
    "so `//[^\\n]*` cannot match them — and neither can it match any of the "
    "three real literals, for the same reason. The genuine shape would be a "
    "BARE `//` inside a regex literal (only reachable inside a character class, "
    "`/[//]/`) or a quote character inside one; `grep -n '/\\[[^]]*//' "
    "frontend/tests/*.spec.ts` finds none today. What is asserted rather than "
    "argued is the OUTCOME: "
    "test_stripping_comments_swallows_no_real_declaration checks PER FILE that "
    "the stripped scan finds every declaration the raw text does (159 = 159 "
    "across all nine spec files), which is the direction that matters — a "
    "mis-lexed region can only blank characters, so it can only LOSE "
    "declarations.",
    "A declaration disabled by surrounding CODE rather than by a comment. "
    '`if (false) { test("… (live only)", …) }` leaves the title in the source '
    "and in `--list`'s eyes it is simply not selected, so the scan reads it as "
    "present, the membership pin stays green and the selection differential "
    "moves on both sides at once. This is the original design of the scan (it "
    "reads declarations, not reachability) and closing it needs a JavaScript "
    "parser, not a regex. OUT OF SCOPE for #379, recorded so the coverage "
    "claim above is not read as more than it is.",
    "`env: CI: ''` on the demo job's RUN step. A review mutant set it and "
    "survived this file. Its exploitability is UNVERIFIED and deliberately not "
    "guarded here: the two `--list` steps in the same job still execute under "
    "the runner's own `CI=true`, so a stray `.only` still errors there, and "
    "Playwright's `retries`/`forbidOnly` are read from the config this repo "
    "pins rather than from the bare variable. Settling command: run the demo "
    "suite twice, once with `CI=''` and once without, and diff the JSON "
    "report's `stats`. Not run — it is a full browser suite.",
    "Branch protection's `checks[]` app_id pins. All nine existing contexts are "
    "bound to an app (15368 github-actions, 57789 github-advanced-security); a "
    "context added through the `contexts` endpoint lands with `app_id: null` "
    "and would accept a same-named check from any app. Reading that needs a "
    "token, so it is documented in docs/TEST_STRATEGY.md §12.5, not asserted.",
    "Whether the live suite's assertions are any good. These guards prove it "
    "RUNS and that nothing silently drops out of it, not that it tests the right "
    "thing.",
)

_PATHS_KEYS = ("paths", "paths-ignore")

# `pull_request: branches:` values a gating workflow may declare, compared as a
# LIST and byte-exactly. `main` is this repository's default branch
# (`gh api repos/imrohitagrawal/citevyn --jq .default_branch` → `main`,
# 2026-09-09) and the only branch carrying protection, so a filter admitting
# exactly `main` cannot stop a gated PR from reporting: every PR whose merge
# these guards exist to gate targets `main` by definition.
_SANCTIONED_PULL_REQUEST_BRANCHES: tuple[str, ...] = ("main",)

# The `pull_request` activity types a gating workflow must still react to.
# GitHub's default is [opened, synchronize, reopened]; declaring `types:` at all
# REPLACES that default rather than adding to it, so `types: [labeled]` stops
# the workflow firing when a PR is opened or pushed to — and a workflow that
# never fires reports nothing, which is #379's bug.
#
# `reopened` is deliberately NOT required: reopening a PR does not create a new
# head commit, and status checks attach to a commit, so the results from before
# the close are still the ones branch protection reads. `opened` and
# `synchronize` are the two events that DO produce a head SHA with no status
# yet.
_REQUIRED_PULL_REQUEST_TYPES: tuple[str, ...] = ("opened", "synchronize")


def _load_workflow(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name} is not a YAML mapping"
    return data


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """The ``on:`` block.

    YAML 1.1 resolves a bare ``on`` key to the BOOLEAN True, which is why every
    naive workflow parser reads ``on:`` as missing. Handle both.
    """
    raw = workflow.get(True, workflow.get("on"))
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return dict.fromkeys(raw)
    assert isinstance(raw, dict), "workflow has no usable `on:` block"
    return raw


def _live_job() -> dict[str, Any]:
    jobs = _load_workflow(LIVE_WORKFLOW.name).get("jobs") or {}
    assert "live-e2e" in jobs, (
        f"{LIVE_WORKFLOW.name} no longer defines a `live-e2e` job — every rule "
        "below would be looking at nothing."
    )
    return jobs["live-e2e"]


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in (job.get("steps") or []) if isinstance(step, dict)]


# ───────────────────── no gating workflow is path-filtered ─────────────────────


def test_the_gating_workflow_population_is_what_it_claims_to_be() -> None:
    """Non-vacuity partner for the two parametrized rules below.

    They pass over whatever ``GATING_WORKFLOWS`` happens to hold, including an
    empty tuple. This asserts the population is multi-item, that every file in
    it exists, and that the live workflow #379 is about is actually inside it.
    """
    assert len(GATING_WORKFLOWS) >= 2, (
        f"GATING_WORKFLOWS has {len(GATING_WORKFLOWS)} entries; the rules below "
        "are close to vacuous"
    )
    assert LIVE_WORKFLOW.name in GATING_WORKFLOWS, (
        f"{LIVE_WORKFLOW.name} dropped out of the guarded population — #379's "
        "whole point is that this workflow must stay eligible to be required"
    )
    missing = [name for name in GATING_WORKFLOWS if not (WORKFLOW_DIR / name).is_file()]
    assert not missing, f"guarded workflow file(s) do not exist: {missing}"
    # NOT an ordering pin. An earlier draft asserted `GATING_WORKFLOWS[0] ==
    # "ci.yml"` and justified it as short-circuit protection, which it is not:
    # the rules below are `@pytest.mark.parametrize`, so pytest generates one
    # INDEPENDENT test per workflow and there is no loop to short-circuit
    # (`--collect-only` shows one case per entry). All that pin really bought
    # was "a long-compliant control is in the population", and it would have
    # gone red for any future gating workflow that sorts before `ci.yml`.
    # Assert the thing itself, without the order.
    assert "ci.yml" in GATING_WORKFLOWS, (
        "`ci.yml` dropped out of the guarded population. It is the control: a "
        "workflow that has never been path-filtered, so the rules below are "
        "exercised against a known-good case and not only against the "
        "candidate #379 changed."
    )


@pytest.mark.parametrize("name", GATING_WORKFLOWS)
def test_a_gating_workflow_is_not_path_filtered(name: str) -> None:
    triggers = _triggers(_load_workflow(name))
    offenders: list[str] = []
    for event in ("push", "pull_request"):
        config = triggers.get(event)
        if not isinstance(config, dict):
            continue
        offenders.extend(f"{event}.{key}" for key in _PATHS_KEYS if key in config)

    assert not offenders, (
        f"{name} filters by path on {offenders} while hosting (or being a "
        "candidate for) a REQUIRED status check. GitHub reports NOTHING for a "
        "workflow that path-filtering skipped, so the required context sits at "
        '"Expected — waiting for status to be reported" and the PR can never '
        "merge. 13 of the last 30 merged PRs touched zero `frontend/**` files "
        "(2026-09-08; the window moves). "
        "See the header comment in .github/workflows/frontend.yml (#326) and "
        "frontend-live-e2e.yml (#379)."
    )


@pytest.mark.parametrize("name", GATING_WORKFLOWS)
def test_a_gating_workflow_is_not_branch_or_type_filtered(name: str) -> None:
    """`paths:` is not the only key with #379's wedge; three siblings have it too.

    Each of these was applied as a one-line mutant and SURVIVED the
    ``paths``/``paths-ignore`` rule above, because that rule reads two key names
    and these are three others. The effect is identical in every case — the
    workflow does not trigger, GitHub reports NOTHING (not a failure, not a
    skip), and the required context sits at "Expected — waiting for status to be
    reported" until someone notices:

    * ``pull_request: branches: [never-a-real-branch]`` on the live workflow;
    * ``pull_request: types: [labeled]`` on the live workflow — declaring
      ``types:`` REPLACES GitHub's [opened, synchronize, reopened] default
      rather than adding to it, so this fires on a label and on nothing else;
    * ``pull_request: branches-ignore: ['**']`` on ``frontend.yml``, which hosts
      TWO required contexts.

    ``branches: [main]`` is ALLOWED, and only that exact list. It is what
    ``ci.yml`` (the workflow that RUNS this file) and ``codeql.yml`` already
    declare, and it is safe here for a specific reason rather than by
    convention: ``main`` is the repository's default branch and the only branch
    with protection on it, so every PR whose merge these guards gate targets
    ``main``. The narrow limit, stated rather than hidden — if this repo ever
    grows a second protected branch, those two workflows would not fire for a PR
    targeting it and this rule would not notice.

    ``branches-ignore`` gets no exemption at all: it is an exclusion, so no
    value provably admits every protected-branch PR.

    Turns red if: a gating workflow's ``pull_request:`` gains a ``branches:``
    other than exactly ``["main"]``, gains ``branches-ignore:`` at all, or
    declares a ``types:`` list missing ``opened`` or ``synchronize``.
    """
    config = _triggers(_load_workflow(name)).get("pull_request")
    if not isinstance(config, dict):
        # `pull_request:` with no narrowing keys at all — the widest, safest
        # shape, and what `frontend.yml` and the live workflow declare. That the
        # trigger EXISTS is the next test's job, not this one's.
        return

    violations: list[str] = []
    if "branches-ignore" in config:
        violations.append(
            f"`pull_request: branches-ignore: {config['branches-ignore']!r}`. An "
            "exclusion can never be shown to admit every protected-branch PR, so "
            "there is no sanctioned value for this key on a gating workflow."
        )
    if "branches" in config:
        declared = config["branches"]
        branches = tuple(declared) if isinstance(declared, list) else (declared,)
        if branches != _SANCTIONED_PULL_REQUEST_BRANCHES:
            violations.append(
                f"`pull_request: branches: {list(branches)!r}`, but the only "
                f"sanctioned value is {list(_SANCTIONED_PULL_REQUEST_BRANCHES)!r} "
                "— the default and only protected branch. A PR targeting `main` "
                "would not trigger this workflow, and GitHub reports nothing for "
                "a workflow that did not trigger."
            )
    if "types" in config:
        declared_types = config["types"] or []
        missing = [t for t in _REQUIRED_PULL_REQUEST_TYPES if t not in declared_types]
        if missing:
            violations.append(
                f"`pull_request: types: {list(declared_types)!r}` omits {missing}. "
                "Declaring `types:` REPLACES GitHub's [opened, synchronize, "
                "reopened] default, so this workflow no longer runs when a PR is "
                f"{' or '.join(missing)}d — the head SHA gets no status at all."
            )

    assert not violations, (
        f"{name} narrows its `pull_request:` trigger while hosting (or being a "
        "candidate for) a REQUIRED status check:\n  " + "\n  ".join(violations) + "\n"
        "This is the same wedge as a `paths:` filter through a different key. "
        "See the rule above and #379."
    )


def test_the_branch_and_type_rule_has_something_to_compare() -> None:
    """Non-vacuity partner: the rule above only compares keys that are DECLARED.

    If no gating workflow declared ``branches:`` or ``types:`` at all, every
    comparison in it would be skipped and the rule would be indistinguishable
    from one that never ran. Both keys are live on the real repo today, which is
    also what proves the sanctioned value is a real shape and not a guess.

    Turns red if: no gating workflow declares a ``pull_request: branches:``, or
    none declares a ``pull_request: types:``.
    """
    with_branches: list[str] = []
    with_types: list[str] = []
    for name in GATING_WORKFLOWS:
        config = _triggers(_load_workflow(name)).get("pull_request")
        if not isinstance(config, dict):
            continue
        if "branches" in config:
            with_branches.append(name)
        if "types" in config:
            with_types.append(name)

    assert with_branches, (
        "no gating workflow declares `pull_request: branches:`, so the "
        "sanctioned-value comparison in the rule above evaluates on nothing. "
        "`ci.yml` and `codeql.yml` both declared it on 2026-09-09."
    )
    assert with_types, (
        "no gating workflow declares `pull_request: types:`, so the "
        "opened/synchronize check in the rule above evaluates on nothing. "
        "`ci.yml` and `pr-quality.yml` both declared it on 2026-09-09."
    )


@pytest.mark.parametrize("name", GATING_WORKFLOWS)
def test_a_gating_workflow_actually_triggers_on_pull_request(name: str) -> None:
    """Partner to the rule above: "no path filter" is trivially satisfied by a
    workflow with no PR trigger at all — which reports nothing on a PR for a
    completely different reason, with the identical wedging effect.
    """
    triggers = _triggers(_load_workflow(name))
    assert "pull_request" in triggers, (
        f"{name} has no `pull_request:` trigger, so it reports no status on a PR "
        f"at all. Its `on:` block declares {sorted(map(str, triggers))}."
    )


# ──────────────────── no gating job is defused into a no-op ────────────────────


def _disables_the_gate(value: Any) -> bool:
    """Is this ``continue-on-error:`` value anything other than a literal false?

    GitHub accepts a boolean, the STRING ``"true"``/``"false"``, or an
    expression that evaluates to one — so ``value is True`` misses two of the
    three forms. Conservative on purpose: an expression could evaluate either
    way at run time and cannot be settled from the YAML, so it counts as
    disabling and has to be argued for in review.
    """
    if value is False:
        return False
    return not (isinstance(value, str) and value.strip().lower() == "false")


@pytest.mark.parametrize(("workflow", "job_id"), GATING_JOBS, ids=lambda v: str(v))
def test_a_gating_job_is_not_defused(workflow: str, job_id: str) -> None:
    """Three words turn any gate in this repo green-and-empty, and nothing saw it.

    Reproduced in review: ``continue-on-error: true`` on ``frontend.yml``'s
    "Assert the suite actually executed" step — a REQUIRED context today —
    survived every other rule in this file, the whole backend suite and ruff,
    while defusing the pinned skip count, the full account, the flaky check AND
    the #379 rename trap at once. A job-level ``if: false`` is the same defect
    one level up, and GitHub reports a SKIPPED job as SUCCESS to branch
    protection, so the check still shows green.

    ``if:`` is judged BYTE-EXACTLY against ``_SANCTIONED_STEP_CONDITIONS``, not
    by substring: ``failure() || true`` contains ``failure()``.

    Turns red if: any gating job gains an ``if:``, gains a
    ``continue-on-error:`` that is not literal ``false``, or any of its steps
    does — except the four artifact-upload steps listed in
    ``_SANCTIONED_STEP_CONDITIONS`` with exactly their recorded condition.
    """
    jobs = _load_workflow(workflow).get("jobs") or {}
    assert job_id in jobs, (
        f"{workflow} no longer defines a `{job_id}` job, so this rule is "
        "looking at nothing. If it was renamed, its status-check context "
        "changed too — which wedges every PR that requires it."
    )
    job = jobs[job_id]
    steps = _steps(job)
    # Partner: the step loop below is vacuous over an empty list, and a job that
    # lost its steps is exactly as defused as one with `continue-on-error`.
    assert len(steps) >= 3, (
        f"{workflow}:{job_id} declares only {len(steps)} step(s); the per-step "
        "rule below has almost nothing to iterate"
    )

    violations: list[str] = []
    if "if" in job:
        violations.append(
            f"job-level `if: {job['if']!r}`. A job GitHub skips reports SUCCESS "
            "to branch protection, so the required context goes green having "
            "run nothing."
        )
    if "continue-on-error" in job and _disables_the_gate(job["continue-on-error"]):
        violations.append(
            f"job-level `continue-on-error: {job['continue-on-error']!r}` — the "
            "job can fail and still report success."
        )

    for index, step in enumerate(steps):
        label = str(step.get("name") or step.get("uses") or f"step #{index}")
        if "continue-on-error" in step and _disables_the_gate(step["continue-on-error"]):
            violations.append(
                f"step {label!r} sets `continue-on-error: "
                f"{step['continue-on-error']!r}`, so whatever it asserts can "
                "fail without failing the job."
            )
        if "if" in step:
            sanctioned = _SANCTIONED_STEP_CONDITIONS.get((workflow, job_id, label))
            if sanctioned is None:
                violations.append(
                    f"step {label!r} carries `if: {step['if']!r}`, which is not "
                    "in _SANCTIONED_STEP_CONDITIONS. A step that skips asserts "
                    "nothing. If this step genuinely cannot weaken the gate "
                    "(an artifact upload), add it there with its exact value."
                )
            elif str(step["if"]).strip() != sanctioned:
                violations.append(
                    f"step {label!r} carries `if: {step['if']!r}`, but "
                    f"_SANCTIONED_STEP_CONDITIONS records {sanctioned!r}. The "
                    "comparison is byte-exact on purpose — `failure() || true` "
                    "contains `failure()`."
                )

    assert not violations, (
        f"{workflow}:{job_id} is defused:\n  " + "\n  ".join(violations) + "\n"
        "This job gates a merge. See _NOT_HELD_TO_THE_DEFUSING_RULE for the "
        "jobs deliberately outside this rule."
    )


@pytest.mark.parametrize(("workflow", "job_id"), GATING_JOBS, ids=lambda v: str(v))
def test_a_gating_job_does_not_matrix_its_status_check_context(workflow: str, job_id: str) -> None:
    """A ``strategy.matrix`` RENAMES the check GitHub reports — #379's wedge again.

    GitHub appends the matrix combination to the job's name, and this repo's own
    required-context list is the proof: ``ci.yml``'s ``shell-tests`` declares
    ``name: shell suites (bash 3.2 + 5.x)`` and branch protection holds
    ``shell suites (bash 3.2 + 5.x) (ubuntu-latest)`` and ``… (macos-latest)``.
    The BARE name is reported by nothing. So adding a matrix to a job whose bare
    name is a required context leaves that context waiting for a status forever
    while every run is green — no error message anywhere.

    ``test_the_recorded_required_context_still_names_a_real_job`` cannot see
    this. It matches on ``startswith(prefix)``, which is precisely what has to
    be true for the two shell contexts to resolve, so a matrix added anywhere
    else passes it too. Mutants that survived it:
    ``strategy.matrix`` on ``frontend.yml:demo-e2e`` (required today), on
    ``ci.yml:test`` (``pytest + lint``, required, and the job that runs this
    file) and on ``frontend-live-e2e.yml:live-e2e``.

    Scope is ``GATING_JOBS``, deliberately NOT "every job whose name is a
    required context": ``ci.yml:shell-tests`` is matrixed ON PURPOSE — bash 3.2
    vs 5.x is the defect class it exists to catch — and its two expansions are
    the strings branch protection already holds, so the broader rule would go
    red on the real repo on day one. None of the four gating jobs is matrixed
    today; a future one that genuinely needs a matrix has to record its expanded
    names in ``_REQUIRED_CONTEXT_WORKFLOWS`` and be argued for here.

    Turns red if: any job in ``GATING_JOBS`` gains a ``strategy:`` key.
    """
    jobs = _load_workflow(workflow).get("jobs") or {}
    assert job_id in jobs, (
        f"{workflow} no longer defines a `{job_id}` job, so this rule is looking at nothing."
    )
    job = jobs[job_id]
    assert "strategy" not in job, (
        f"{workflow}:{job_id} declares `strategy: {job['strategy']!r}`. GitHub "
        "appends the matrix combination to the job name, so the context this job "
        f"reports is no longer {str(job.get('name') or job_id)!r} but that name "
        "plus a suffix per combination. Branch protection stores the old string "
        "and waits for it forever. If the matrix is genuinely wanted, the "
        "expanded names have to go into _REQUIRED_CONTEXT_WORKFLOWS and branch "
        "protection has to be updated in the same change — see "
        "`ci.yml:shell-tests`, which is matrixed on purpose and pinned that way."
    )


def test_the_matrix_rule_names_a_key_github_really_uses() -> None:
    """Partner: ``assert "strategy" not in job`` passes just as well misspelled.

    A rule that looks for a key nothing ever sets is green forever and
    indistinguishable from one that works. ``ci.yml:shell-tests`` is genuinely
    matrixed, so it pins both the key name and the renaming mechanism the rule
    above is about: its bare ``name:`` appears in NO required context, and both
    matrix expansions do.

    Turns red if: ``ci.yml:shell-tests`` stops declaring ``strategy.matrix``, or
    its expanded names stop being the recorded required contexts (at which point
    the rule above is asserting against a mechanism this repo no longer
    demonstrates, and the exemption for it needs re-arguing).
    """
    job = (_load_workflow("ci.yml").get("jobs") or {}).get("shell-tests") or {}
    strategy = job.get("strategy")
    assert isinstance(strategy, dict) and "matrix" in strategy, (
        "ci.yml:shell-tests no longer declares `strategy.matrix`. It is the only "
        "worked example in this repo of a matrix renaming a status-check "
        f"context, and the rule above is written against it. Got {strategy!r}."
    )
    bare = str(job.get("name") or "shell-tests")
    assert bare not in _REQUIRED_CONTEXT_WORKFLOWS, (
        f"{bare!r} is recorded as a required context, but a matrixed job never "
        "reports its bare name — GitHub reports one context per combination. "
        "Either the matrix went away or the recorded list is wrong."
    )
    expanded = [
        context for context in _REQUIRED_CONTEXT_WORKFLOWS if context.startswith(f"{bare} (")
    ]
    assert len(expanded) >= 2, (
        f"expected at least two matrix-expanded required contexts starting with "
        f"{bare + ' ('!r}, found {expanded}. That expansion IS the mechanism the "
        "rule above guards against."
    )


def test_every_sanctioned_step_condition_still_names_a_real_step() -> None:
    """The exemption list must not rot into a licence nothing uses.

    Two directions matter. A stale entry (the step was renamed or deleted) is
    dead weight that quietly widens what a future edit may claim; and an entry
    whose recorded value drifts from the YAML would let the rule above accept a
    condition nobody re-read. Both are caught by resolving every key against
    the real workflow.

    Turns red if: an entry in ``_SANCTIONED_STEP_CONDITIONS`` names a step that
    no longer exists, or records an ``if:`` value the YAML does not have.
    """
    assert _SANCTIONED_STEP_CONDITIONS, (
        "the sanctioned-condition list was emptied; the rule above would then "
        "reject the artifact-upload steps that legitimately carry `if:`"
    )
    unresolved: list[str] = []
    for (workflow, job_id, step_name), expected in sorted(_SANCTIONED_STEP_CONDITIONS.items()):
        job = (_load_workflow(workflow).get("jobs") or {}).get(job_id) or {}
        found = [step for step in _steps(job) if str(step.get("name") or "") == step_name]
        if len(found) != 1:
            unresolved.append(f"{workflow}:{job_id} has {len(found)} step(s) named {step_name!r}")
            continue
        actual = str(found[0].get("if", "<no if:>")).strip()
        if actual != expected:
            unresolved.append(
                f"{workflow}:{job_id}:{step_name} has `if: {actual!r}`, recorded as {expected!r}"
            )
    assert not unresolved, "sanctioned step conditions no longer match the YAML:\n  " + "\n  ".join(
        unresolved
    )


def test_the_jobs_left_out_of_the_defusing_rule_are_recorded_with_a_reason() -> None:
    """An unexplained omission looks identical to one this guard forgot.

    Turns red if: ``_NOT_HELD_TO_THE_DEFUSING_RULE`` is emptied, or a job is
    listed both as guarded and as exempt.
    """
    assert _NOT_HELD_TO_THE_DEFUSING_RULE, (
        "the register of gating jobs outside the defusing rule must not be silently emptied"
    )
    guarded = {f"{workflow}:{job_id}" for workflow, job_id in GATING_JOBS}
    overlap = guarded & set(_NOT_HELD_TO_THE_DEFUSING_RULE)
    assert not overlap, f"job(s) recorded as both guarded and exempt: {sorted(overlap)}"


@pytest.mark.parametrize(
    ("context", "workflow"), sorted(_REQUIRED_CONTEXT_WORKFLOWS.items()), ids=lambda v: str(v)
)
def test_the_recorded_required_context_still_names_a_real_job(context: str, workflow: str) -> None:
    """A stale mapping is a lie about which files this guard is protecting.

    Matrix jobs expand their `name:` per combination (``shell suites … (macos-latest)``),
    so the recorded context only has to START with a declared job name.
    """
    jobs = _load_workflow(workflow).get("jobs") or {}
    declared = {
        str(job.get("name") or job_id) for job_id, job in jobs.items() if isinstance(job, dict)
    }
    # `Analyze (${{ matrix.language }})` and the shell matrix cannot be resolved
    # statically, so compare on the literal prefix before any expression.
    prefixes = {name.split("${{")[0].strip() for name in declared} | declared
    assert any(context.startswith(prefix) and prefix for prefix in prefixes), (
        f"required context {context!r} is recorded as coming from {workflow}, "
        f"which declares {sorted(declared)}. Either the job was renamed — which "
        "wedges every PR, because branch protection still waits for the old "
        "context — or this mapping is stale."
    )


def test_required_contexts_with_no_workflow_are_recorded_with_a_reason() -> None:
    """Keeps the "which workflow produces this" mapping from quietly under-covering."""
    assert _REQUIRED_CONTEXTS_NOT_FROM_A_WORKFLOW, (
        "the register of required contexts that no workflow produces must not be "
        "silently emptied — an unexplained gap looks like an oversight"
    )
    overlap = set(_REQUIRED_CONTEXTS_NOT_FROM_A_WORKFLOW) & set(_REQUIRED_CONTEXT_WORKFLOWS)
    assert not overlap, f"context(s) recorded in both directions: {sorted(overlap)}"


# ──────────────────── the context string the owner has to type ────────────────────


def test_the_live_job_name_is_the_exact_branch_protection_context() -> None:
    """One character of drift here makes the documented promotion command wrong.

    Branch protection does not validate context strings. Adding
    ``Live-mode Playwright (stub backend)`` when the job is called something
    else does not error — it wedges every PR on a check that will never report.
    """
    assert _live_job().get("name") == LIVE_JOB_NAME, (
        f"{LIVE_WORKFLOW.name}'s live-e2e job is named "
        f"{_live_job().get('name')!r}, but {LIVE_JOB_NAME!r} is the context "
        "string documented in docs/TEST_STRATEGY.md §12 for branch protection."
    )


def _documented_promotion_contexts() -> list[str]:
    """Every ``-f 'contexts[]=…'`` argument inside a fenced block in §12.

    Not a whole-file search. The doc names the context string more than once —
    in §12.2's prose and in §12.5's command — and only ONE of those is executed:
    the one the owner selects and pastes into a shell.
    """
    doc = TEST_STRATEGY.read_text(encoding="utf-8")
    fenced = re.findall(r"^```[^\n]*\n(.*?)^```", doc, re.M | re.S)
    return [
        match.group("context")
        for block in fenced
        for match in re.finditer(r"contexts\[\]=(?P<context>[^'\"\n]+)", block)
    ]


def test_the_documented_promotion_command_sends_the_real_job_name() -> None:
    """Assert what the CONSUMER receives — the ARGUMENT, not the file.

    An earlier version of this test read ``LIVE_JOB_NAME in doc``. The string
    appears twice in ``docs/TEST_STRATEGY.md``, so a review-round mutant that
    changed ONLY the ``contexts[]=`` value in §12.5 — the copy the owner
    actually pastes — left the test green (44 passed). Branch protection does
    not validate context strings, so that command would have added a context no
    job ever reports and wedged every pull request: the exact failure §12 exists
    to prevent.

    Turns red if: the ``contexts[]=`` argument in §12.5's fenced command stops
    matching the live job's ``name:`` by even one character.
    """
    sent = _documented_promotion_contexts()
    # The partner. A regex that finds nothing would make the comparison below
    # vacuously true over an empty list — "a check that counts nothing needs a
    # partner proving the thing counted exists".
    assert len(sent) == 1, (
        f"expected exactly one `contexts[]=` argument in a fenced code block in "
        f"docs/TEST_STRATEGY.md, found {len(sent)}: {sent}. Either the promotion "
        "command was removed (the owner has no command to run), or the "
        "extraction regex has stopped seeing it — in which case the equality "
        "below would pass over nothing."
    )
    assert sent[0] == _live_job().get("name"), (
        f"docs/TEST_STRATEGY.md §12.5 tells the owner to run "
        f"`contexts[]={sent[0]}`, but {LIVE_WORKFLOW.name}'s live-e2e job is "
        f"named {_live_job().get('name')!r}. That command would add a context "
        "nothing ever reports, and every pull request would then wait on it "
        "forever."
    )
    assert sent[0] == LIVE_JOB_NAME, (
        f"the documented command sends {sent[0]!r} but this file pins "
        f"{LIVE_JOB_NAME!r}; the two records of the same string have drifted"
    )


def test_the_documented_context_string_matches_the_job_name() -> None:
    """The §12.1 table must list every context a full-object write would replace.

    Separate from the rule above on purpose: that one guards the ARGUMENT of the
    one command the owner runs, this one guards the LIST they would have to
    re-send if they ever reached for `PATCH`/`PUT` instead.
    """
    doc = TEST_STRATEGY.read_text(encoding="utf-8")
    assert LIVE_JOB_NAME in doc, (
        f"docs/TEST_STRATEGY.md does not contain the exact context string "
        f"{LIVE_JOB_NAME!r}. That is the string the owner pastes into branch "
        "protection; a doc that names it approximately is worse than no doc."
    )
    for context in _REQUIRED_CONTEXT_WORKFLOWS:
        assert context in doc, (
            f"docs/TEST_STRATEGY.md §12 claims to list the required contexts but "
            f"omits {context!r}. An incomplete list is the one that gets pasted "
            "into a full-protection PUT, silently clearing the rest."
        )


# ─────────────────── the live job asserts EXECUTION, not selection ───────────────────


def test_the_live_job_reads_the_runs_own_json_stats() -> None:
    """A ``--list`` count is not evidence that anything ran.

    ``playwright test --list`` COUNTS SKIPPED TESTS, and Playwright exits 0 when
    every selected test skips. Four of the five live tests self-skip via
    ``if (!isLive) test.skip(...)`` where ``isLive`` is read from the DOM badge —
    i.e. from the DEV SERVER's env. A server that comes up in demo mode leaves
    4 skipped, 1 passed, exit 0 and zero live coverage, and the selection guard
    happily reports 5. Only the run's own JSON ``stats`` can tell them apart.
    """
    steps = _steps(_live_job())

    json_env = [
        step
        for step in steps
        if isinstance(step.get("env"), dict) and "PLAYWRIGHT_JSON_OUTPUT_NAME" in step["env"]
    ]
    assert len(json_env) == 1, (
        "exactly one step in the live job must set PLAYWRIGHT_JSON_OUTPUT_NAME "
        f"(found {len(json_env)}); without it there is no JSON report to assert on"
    )
    run_step = json_env[0]
    report_path = str(run_step["env"]["PLAYWRIGHT_JSON_OUTPUT_NAME"])

    command = str(run_step.get("run") or "")
    reporters = re.search(r"--reporter=(?P<list>[\w,]+)", command)
    assert reporters, f"the live run step has no --reporter= flag: {command!r}"
    named = set(reporters.group("list").split(","))
    # `--reporter` REPLACES the config's reporters
    # (playwright/lib/runner/index.js:301), so both have to be named here or the
    # JSON below and the uploaded HTML report silently do not exist.
    assert {"json", "html"} <= named, (
        f"the live run step's --reporter names {sorted(named)}. `--reporter` "
        "REPLACES the config's reporters rather than adding to them, so `json` "
        "(this assertion's input) and `html` (the failure artifact) must both "
        "be listed."
    )

    asserting = [
        step
        for step in steps
        if report_path in str(step.get("run") or "") and "stats" in str(step.get("run") or "")
    ]
    assert asserting, (
        f"no step in the live job reads {report_path} and looks at its `stats`. "
        "The selection guard alone is satisfied by a run that executed nothing."
    )

    body = "\n".join(str(step.get("run")) for step in asserting)
    assert re.search(r'stats\["skipped"\]', body), (
        "the live job's execution assertion no longer looks at "
        '`stats["skipped"]`. That line is what catches a dev server booted in '
        "DEMO mode, where 4 of the 5 live tests self-skip and the job still "
        "exits 0."
    )
    assert "LIVE_TOTAL" in body, (
        "the live job's execution assertion no longer compares against "
        "LIVE_TOTAL, so it no longer accounts for every selected test"
    )


def test_the_live_job_caps_its_own_runtime() -> None:
    """Without a cap a hung dev server burns the 360-minute default."""
    timeout = _live_job().get("timeout-minutes")
    assert isinstance(timeout, int), (
        f"{LIVE_WORKFLOW.name}:live-e2e has no integer `timeout-minutes:` "
        f"(got {timeout!r}). The job has measured at 97 s max over its last 15 "
        "runs; the default is 360 minutes."
    )
    # FLOOR, not just a ceiling. `0 < timeout` accepts `timeout-minutes: 1`,
    # which is BELOW the job's own measured worst case once checkout, `npm ci`
    # and the browser download are counted — every run would be cancelled at 60
    # seconds and the required check would be permanently red. 5 is ~3x the
    # 97 s max measured over the job's last 15 runs, and still 4x under the 20
    # the demo job uses. Severity is low precisely because a too-low value
    # announces itself in red rather than passing green — which is why the
    # bound is set with headroom rather than tight to the measurement.
    assert 5 <= timeout <= 30, (
        f"`timeout-minutes: {timeout}` is outside the range this job can "
        "justify — it measured 97 s max over its last 15 runs, and the demo "
        "job, ~5x longer, uses 20. Below 5 the cap is under the job's own "
        "worst case and every run is cancelled; above 30 it is not a cap."
    )


def test_the_live_html_report_lands_where_the_upload_step_looks() -> None:
    """The exact bug ``frontend.yml`` already hit, one workflow over.

    ``--reporter=...,html`` drops the config's ``outputFolder:
    playwright-report-live``, because the CLI reporter form cannot carry
    options. The report then lands in ``playwright-report`` while the upload
    step points at ``playwright-report-live`` — and with the default
    ``if-no-files-found: warn`` the artifact is silently empty.
    """
    steps = _steps(_live_job())
    run_steps = [
        step
        for step in steps
        if isinstance(step.get("env"), dict) and "PLAYWRIGHT_HTML_OUTPUT_DIR" in step["env"]
    ]
    assert len(run_steps) == 1, (
        "exactly one step must set PLAYWRIGHT_HTML_OUTPUT_DIR (found "
        f"{len(run_steps)}); it is what puts the HTML report back where the "
        "upload step looks after --reporter dropped the config's outputFolder"
    )
    html_dir = str(run_steps[0]["env"]["PLAYWRIGHT_HTML_OUTPUT_DIR"])

    uploads = [step for step in steps if str(step.get("uses") or "").startswith("actions/upload-")]
    assert len(uploads) == 1, f"expected one upload step in the live job, found {len(uploads)}"
    with_block = uploads[0].get("with") or {}

    # The run step's cwd is `frontend` (defaults.run.working-directory) and
    # PLAYWRIGHT_HTML_OUTPUT_DIR resolves against it, but upload-artifact's
    # `path:` is ALWAYS repo-root-relative — `working-directory` applies to
    # `run:` steps only, never to an action's `with:` inputs.
    assert str(with_block.get("path", "")).strip() == f"frontend/{html_dir}", (
        f"the live job writes its HTML report to frontend/{html_dir} but uploads "
        f"{with_block.get('path')!r}. The artifact would collect nothing."
    )
    assert with_block.get("if-no-files-found") == "error", (
        "the live job's upload step must set `if-no-files-found: error`. The "
        "default is `warn`, which is precisely why the dead-artifact bug in "
        "frontend.yml went unnoticed: a debugging artifact that collects "
        "nothing is worth knowing about on the first run, not the tenth."
    )


def test_the_live_selection_guard_keeps_its_non_vacuity_partner() -> None:
    """The differential alone is satisfied by selecting nothing.

    ``live_total -eq all_live_titled`` is true when both are 0 — which is
    exactly the state where the job runs no tests at all. The ``-gt 0`` line is
    the partner that makes the differential mean something, and it is the sort
    of line that gets deleted as redundant.

    This is a TEXT check on a shell body, and it is the weaker of the two kinds
    here: unlike the execution assertion below, the selection guard cannot be
    executed without a Playwright install, so nothing proves the comparison is
    still wired to the right variables.
    """
    body = _step_body(LIVE_WORKFLOW, "live-e2e", "Guard against a vacuous selection")
    assert re.search(r'"\$live_total"\s+-gt\s+0', body), (
        "the live job's selection guard lost its `-gt 0` partner. Without it the "
        "differential `live_total -eq all_live_titled` is satisfied by 0 == 0, "
        "i.e. by a job that selects and runs nothing."
    )
    assert re.search(r'"\$live_total"\s+-eq\s+"\$all_live_titled"', body), (
        "the live job's selection guard no longer compares the live config's "
        "selection against the default config's live-titled tests, so "
        "testDir/testIgnore drift can silently drop a live-only test"
    )
    assert 'echo "LIVE_TOTAL=$live_total" >> "$GITHUB_ENV"' in body, (
        "the selection guard no longer exports LIVE_TOTAL, which the execution "
        "assertion reads to account for every selected test"
    )


# ───────────────────────── the two live greps must agree ─────────────────────────


def _config_grep() -> tuple[str, str]:
    """``(pattern, flags)`` from ``playwright.live.config.ts``'s ``grep:``."""
    source = LIVE_PW_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^\s*grep:\s*/(?P<pattern>[^/\n]+)/(?P<flags>[a-z]*)", source, re.M)
    assert match, (
        f"{LIVE_PW_CONFIG.name} has no `grep: /.../` line. It is what restricts "
        "the live config to the live-only tests; without it the live job would "
        "run the whole demo suite against a live server."
    )
    return match.group("pattern"), match.group("flags")


def _demo_workflow_grep() -> tuple[str, str]:
    """``(pattern, flags)`` from ``frontend.yml``'s rename-trap regex literal."""
    source = DEMO_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r'LIVE_GREP\s*=\s*re\.compile\(\s*r"(?P<pattern>[^"\n]+)"\s*,\s*re\.(?P<flag>\w+)\s*\)',
        source,
    )
    assert match, (
        f'{DEMO_WORKFLOW.name} no longer defines `LIVE_GREP = re.compile(r"...", '
        "re.IGNORECASE)` in its 'Assert the suite actually executed' step. That "
        "is the guard that catches a live-only title being renamed out of the "
        "live suite (#379)."
    )
    return match.group("pattern"), match.group("flag")


def _live_workflow_grep() -> tuple[str, str]:
    """``(pattern, grep option cluster)`` from the live job's selection guard."""
    source = LIVE_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"grep -(?P<opts>[a-zA-Z]+) '(?P<pattern>[^'\n]+)' /tmp/all_list.txt", source)
    assert match, (
        f"{LIVE_WORKFLOW.name}'s selection guard no longer greps the default "
        "config's listing for the live pattern, so its differential can no "
        "longer notice a live-only test that the live config stopped selecting."
    )
    return match.group("pattern"), match.group("opts")


def test_every_copy_of_the_live_selection_regex_agrees() -> None:
    """Three files hardcode the same pattern; nothing else compares them.

    If ``frontend.yml``'s literal drifts from the config's ``grep:``, the rename
    trap keeps passing while checking a selection no job uses — the guard is
    still there, still green, and no longer connected to anything.
    """
    config_pattern, config_flags = _config_grep()
    demo_pattern, demo_flag = _demo_workflow_grep()
    live_pattern, live_opts = _live_workflow_grep()

    assert config_pattern == demo_pattern == live_pattern, (
        "the live-selection pattern disagrees across the three files that "
        f"hardcode it: {LIVE_PW_CONFIG.name}={config_pattern!r}, "
        f"{DEMO_WORKFLOW.name}={demo_pattern!r}, "
        f"{LIVE_WORKFLOW.name}={live_pattern!r}"
    )
    assert "i" in config_flags, (
        f"{LIVE_PW_CONFIG.name}'s grep lost its /i flag; the other two copies "
        "are case-insensitive, so they would now select a different set"
    )
    assert demo_flag == "IGNORECASE", (
        f"{DEMO_WORKFLOW.name}'s LIVE_GREP uses re.{demo_flag}, but the config's "
        "grep is case-INSENSITIVE"
    )
    assert "i" in live_opts, (
        f"{LIVE_WORKFLOW.name}'s selection grep uses `-{live_opts}` — without "
        "`i` it is case-sensitive while the config's grep is not"
    )


# ────────────────────── the live suite's membership is pinned ──────────────────────

# ``test("title", ...)``, ``test.only(...)``, ``test.skip(...)`` — a double- or
# single-quoted literal only. Backticks are deliberately excluded: a template
# literal may carry a substitution, and a half-resolved title is worse than a
# recorded blind spot (see _UNGUARDABLE).
_TEST_TITLE = re.compile(
    r"""\btest(?:\.\w+)*\(\s*(?P<q>["'])(?P<title>(?:\\.|(?!(?P=q)).)*)(?P=q)""",
    re.S,
)


# Strings and comments in ONE alternation, scanned left to right in one pass.
# What makes a `//` inside a string literal (a URL in a test title) safe is that
# the string alternative matches at the OPENING QUOTE, several characters
# earlier, and consumes the whole literal — so the scan is already past the `//`
# when it resumes.
#
# It is NOT the listing order, and an earlier version of this comment said it
# was ("order is the whole trick"). Measured 2026-09-09: moving the two comment
# alternatives to the front changes nothing — 159 declarations before and after,
# equal in every file — because alternation order only decides between branches
# that could begin at the SAME position, and `//` and a quote never can. What
# IS load-bearing is that the string alternatives are PRESENT: drop the
# double-quoted one and the count falls to 156.
_JS_STRING_OR_COMMENT = re.compile(
    r'"(?:\\.|[^"\\\n])*"'  # double-quoted
    r"|'(?:\\.|[^'\\\n])*'"  # single-quoted
    r"|`(?:\\.|[^`\\])*`"  # template literal
    r"|/\*.*?\*/"  # block comment
    r"|//[^\n]*",  # line comment
    re.S,
)


def _strip_comments(source: str) -> str:
    """Blank out ``//`` and ``/* … */`` comments, preserving line structure.

    Commenting a test out is invisible to every CI job (#379): the live
    ``--list`` count drops on BOTH sides of the selection differential, the test
    stops skipping so the demo rename trap never sees it, and a scan of the raw
    text still finds the title inside the comment — so the membership pin below
    stayed green while the test ran in NO job at all.

    Each comment is replaced by the same number of characters (newlines kept,
    everything else a space) so offsets and line numbers are unchanged.

    KNOWN LIMIT, stated rather than papered over: this is a lexer for strings
    and comments only, not for JavaScript. A regex literal containing ``//``
    (e.g. ``/a\\/\\/b/``) or a ``${…}`` substitution containing a backtick would
    confuse it.

    TWO CORRECTIONS, re-derived 2026-09-09. An earlier version of this docstring
    said neither appears in ``frontend/tests/*.spec.ts``. Three regex literals
    DO — ``fonts-offline.spec.ts:53``, ``:217`` and ``harness.spec.ts:57``, all
    ``/^https:\\/\\/…/`` route matchers. But its worked example ``/a\\/\\/b/``
    is not an instance of the hazard it names: those source characters are
    ``\\``, ``/``, ``\\``, ``/`` with no ``//`` bigram anywhere, so ``//[^\\n]*``
    cannot match them, and the same is true of all three real literals. The
    shape that would actually bite is a BARE ``//`` inside a regex literal — only
    reachable inside a character class, ``/[//]/`` — or a quote character inside
    one. Neither is present today.

    That same earlier version also leaned on the declaration-count FLOOR to
    notice if this ever changed. It would not have: the floor sits at 120
    against a measured 159, so it tolerates a 39-declaration loss, and hiding
    whole spec files was measured leaving the suite green.

    What checks it now is ``test_stripping_comments_swallows_no_real_declaration``,
    which asserts PER FILE that the stripped scan finds exactly as many
    declarations as the raw text (159 = 159 across all nine spec files). That is
    the direction that matters and the only one this function can get wrong — a
    mis-lexed region can only blank characters, so it can only LOSE
    declarations, never invent one.
    """

    def blank(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith(("//", "/*")):
            return "".join("\n" if char == "\n" else " " for char in token)
        return token

    return _JS_STRING_OR_COMMENT.sub(blank, source)


def _scan_titles(source: str) -> list[str]:
    """Every ``test("…")`` title in ``source``, ignoring commented-out ones."""
    return [match.group("title") for match in _TEST_TITLE.finditer(_strip_comments(source))]


# Every spec file the scans below read, pinned as a SET. The declaration FLOOR
# alone does not notice a file vanishing: it is 120 against a measured 159, so
# hiding `fidelity.spec.ts` (20 declarations), `focus-ring.spec.ts` (11) or all
# of fidelity + focus-ring + lazy-strip (39) was each measured leaving the suite
# green. A spec file appearing or disappearing is a deliberate act; the floor is
# there for regex rot, and this is there for the population.
_SPEC_FILES: frozenset[str] = frozenset(
    {
        "answer-format.spec.ts",
        "behavior.spec.ts",
        "faint-contrast.spec.ts",
        "fidelity.spec.ts",
        "focus-ring.spec.ts",
        "fonts-offline.spec.ts",
        "harness.spec.ts",
        "landing.spec.ts",
        "lazy-strip.spec.ts",
        "visual.spec.ts",
    }
)


def _spec_files() -> list[Path]:
    specs = sorted((FRONTEND / "tests").glob("*.spec.ts"))
    assert specs, f"no *.spec.ts found under {FRONTEND / 'tests'}"
    return specs


def _declared_test_titles() -> list[tuple[str, str]]:
    specs = _spec_files()
    return [
        (spec.name, title)
        for spec in specs
        for title in _scan_titles(spec.read_text(encoding="utf-8"))
    ]


def test_the_test_title_scan_finds_the_whole_suite() -> None:
    """Non-vacuity partner: the membership pin is only meaningful over a real scan.

    A scan that suddenly sees a handful would still satisfy an equality against
    a five-item set if those five happened to survive — this catches the regex
    (or the comment stripper) rotting under a syntax change.

    The number counts DECLARATIONS, not the selection. Measured 2026-09-08:
    159 ``test("…")`` declarations in 9 spec files, which Playwright expands to
    202 selected tests (`visual.spec.ts` builds its 22 from one templated
    declaration, which this scan deliberately does not read). The earlier
    ``>= 150`` floor sat nine below the real number while its message quoted the
    ~200 SELECTION count — two different things, and nine tests of headroom is a
    rot trap for ordinary test deletion. The floor below is ~25% under the
    measurement: comfortably clear of churn, and still far above the handful a
    broken regex leaves.

    What this floor does NOT buy, measured 2026-09-09 rather than assumed: at
    120 against 159 it tolerates a 39-declaration loss, and hiding
    ``fidelity.spec.ts`` (20), ``focus-ring.spec.ts`` (11) or all of
    fidelity + focus-ring + lazy-strip (39) each left the suite green. That
    population is held by ``_SPEC_FILES`` in
    ``test_the_scanned_spec_files_are_the_whole_population``, not here.

    Turns red if: `_TEST_TITLE` or `_strip_comments` stops matching real
    declarations (drop the ``(?:\\.\\w+)*`` group, or blank a string literal as
    a comment, and this collapses).
    """
    titles = _declared_test_titles()
    assert len(titles) >= 120, (
        f'the test-title scan found only {len(titles)} `test("…")` '
        f"DECLARATIONS across {len(_spec_files())} spec files; "
        "there were 159 on 2026-09-09. The regex or the comment stripper has "
        "stopped matching real declarations."
    )


def test_the_scanned_spec_files_are_the_whole_population() -> None:
    """The floor above cannot see a whole spec file leaving the scan.

    Measured 2026-09-09: hiding ``fidelity.spec.ts`` (20 declarations),
    ``focus-ring.spec.ts`` (11) or all of fidelity + focus-ring + lazy-strip
    (39) each left the suite fully green, because 159 − 39 = 120 is still at the
    floor. Only a 42-declaration loss reddened it. Every scan in this file —
    including the live-suite membership pin, the one thing covering
    ``behavior.spec.ts:1272`` — reads whatever ``glob("*.spec.ts")`` returns, so
    a file dropping out of that glob silently narrows all of them at once.

    Turns red if: a spec file is added, renamed, deleted or moved out of
    ``frontend/tests/`` without ``_SPEC_FILES`` being updated in the same change.
    """
    found = frozenset(spec.name for spec in _spec_files())
    assert found == _SPEC_FILES, (
        "the set of scanned spec files has changed.\n"
        f"  missing (every scan in this file just got narrower): {sorted(_SPEC_FILES - found)}\n"
        f"  new (add to _SPEC_FILES if intended): {sorted(found - _SPEC_FILES)}"
    )


def test_stripping_comments_swallows_no_real_declaration() -> None:
    """Asserts the claim ``_strip_comments``' docstring makes, PER FILE.

    ``_strip_comments`` lexes strings and comments, not JavaScript, so a
    construct it mis-reads can be blanked as a comment and take a real
    ``test("…")`` declaration with it. The direction is one-way, and that is
    what makes the check cheap: blanking can only REMOVE declarations, never add
    one, so raw-count == stripped-count per file is exactly "nothing real was
    swallowed". Per FILE rather than in total, because a total hides a
    compensating pair — one file gaining a commented-out declaration while
    another loses a real one nets to zero.

    Measured 2026-09-09: 159 = 159, equal in all nine files individually.

    A red here has TWO possible readings and both want a human. Either the
    stripper swallowed something real, or a ``test("…")`` declaration was
    genuinely commented out — and the second is #379's own round-2 finding: a
    commented-out test runs in NO job, the live ``--list`` count drops on both
    sides of the selection differential, and it stops skipping so the demo
    rename trap cannot see it either.

    Turns red if (both VERIFIED 2026-09-09): the ``r'"(?:\\\\.|[^"\\\\\\n])*"'``
    double-quoted alternative is dropped from ``_JS_STRING_OR_COMMENT``, so the
    ``//`` in a title like ``opens https://example.com`` is read as a comment
    (159 → 156; ``answer-format.spec.ts`` 4 → 2, ``behavior.spec.ts`` 83 → 82);
    or any single ``test("…")`` declaration is commented out — line 35 of
    ``fidelity.spec.ts`` reddens THIS TEST ALONE, which is what it buys over the
    120-declaration floor, the five-title membership pin and ``_SPEC_FILES``.

    NOT red for: reordering the alternatives in ``_JS_STRING_OR_COMMENT`` to put
    comments first. Measured, that is a no-op — alternation order only decides
    between branches that could start at the SAME position, and ``//`` and a
    quote never can. The "strings FIRST" comment on that pattern describes the
    intent, not a behaviour any test can distinguish.
    """
    mismatched: list[str] = []
    raw_total = stripped_total = 0
    for spec in _spec_files():
        raw = spec.read_text(encoding="utf-8")
        raw_count = len(_TEST_TITLE.findall(raw))
        stripped_count = len(_scan_titles(raw))
        raw_total += raw_count
        stripped_total += stripped_count
        if raw_count != stripped_count:
            mismatched.append(
                f"{spec.name}: {raw_count} declaration(s) in the raw text, "
                f"{stripped_count} after stripping comments"
            )

    assert not mismatched, (
        'comment stripping is swallowing real `test("…")` declarations:\n  '
        + "\n  ".join(mismatched)
        + "\nA mis-lexed regex literal or template substitution blanks the "
        "declaration that follows it, which silently narrows the live-suite "
        "membership pin and every other scan in this file."
    )
    # Partner: the loop above is vacuous over zero declarations, and an equality
    # between two zeros is the shape a broken `_TEST_TITLE` produces.
    assert raw_total == stripped_total >= 120, (
        f"the raw scan found {raw_total} declarations and the stripped scan "
        f"{stripped_total}; there were 159 each on 2026-09-09. Equality between "
        "two collapsed counts is not evidence that stripping is safe."
    )


def test_the_live_only_titles_are_exactly_the_pinned_five() -> None:
    """Catches a rename that no CI job can see (#379).

    ``frontend.yml``'s rename trap only covers tests that SKIP in demo mode.
    ``behavior.spec.ts:1272`` does not — it has no ``if (!isLive)`` guard and
    passes in demo mode — so renaming its title out of ``/live only/i`` would
    drop it from the live suite with every job still green. This pin is the only
    thing that fails on that.

    Adding a genuine new live-only test is meant to land here in the same
    change, the same way ``stats["skipped"] != 4`` is maintained.
    """
    pattern, _ = _config_grep()
    live_grep = re.compile(pattern, re.IGNORECASE)
    found = frozenset(
        (spec, title) for spec, title in _declared_test_titles() if live_grep.search(title)
    )

    assert found == _LIVE_SUITE_MEMBERSHIP, (
        "the set of tests titled for the live suite has changed.\n"
        "  dropped (these would now run in NO job): "
        f"{sorted(_LIVE_SUITE_MEMBERSHIP - found)}\n"
        "  added (update _LIVE_SUITE_MEMBERSHIP if intended): "
        f"{sorted(found - _LIVE_SUITE_MEMBERSHIP)}\n"
        "A title that stops matching the live config's grep silently leaves the "
        "live suite; the demo job cannot see it, because a rename does not "
        "change whether a test skips."
    )


def test_the_title_scan_recognises_the_real_declaration_forms() -> None:
    """The pin is only as good as its regex, so pin the regex itself.

    Dropping the ``(?:\\.\\w+)*`` group or the single-quote alternative would
    silently stop seeing real tests, and the membership set would go red for
    the wrong reason — or, worse, a dropped test would look like a rename.
    """
    cases: list[tuple[str, str | None]] = [
        ('test("plain (live only)", async ({ page }) => {', "plain (live only)"),
        ("test('single quoted (live only)', async () => {", "single quoted (live only)"),
        ('test.skip("skipped (live only)", async () => {', "skipped (live only)"),
        ('  test(\n    "wrapped (live only)",\n', "wrapped (live only)"),
        ('test("has \\"escaped\\" quotes", () => {', 'has \\"escaped\\" quotes'),
        # A `//` INSIDE a string literal is not a comment. Without a
        # double-quoted-string alternative in _JS_STRING_OR_COMMENT this title
        # would be blanked from `https:` onward and the declaration would
        # vanish (measured 2026-09-09: dropping that alternative takes the real
        # suite from 159 declarations to 156). The ORDER of the alternatives is
        # not what saves it — see the note on the pattern.
        (
            'test("opens https://example.com (live only)", () => {',
            "opens https://example.com (live only)",
        ),
        # Must NOT match: a describe block is not a test declaration, and a
        # template literal may hide a substitution.
        ('describe("a group (live only)", () => {', None),
        ("test(`templated (live only)`, () => {", None),
        # A COMMENTED-OUT declaration must not count (#379). It runs in no job:
        # the live `--list` count drops on both sides of the selection
        # differential, and a commented test does not skip, so the demo rename
        # trap cannot see it either. Reading it as present is what made the
        # membership pin below green over a deleted test.
        ('// test("commented out (live only)", () => {', None),
        ('  //test("no space after the slashes (live only)", () => {', None),
        ('/* test("block-commented (live only)", () => { */', None),
        ('/*\n  test("multi-line block (live only)", () => {\n*/', None),
    ]
    for source, expected in cases:
        found = _scan_titles(source)
        actual = found[0] if found else None
        assert actual == expected, (
            f"the title scan read {actual!r} out of {source!r}, expected {expected!r}"
        )


# ────────────── the two CI assertions, EXECUTED rather than grepped ──────────────
#
# Everything above reads the workflow YAML as text. A guard asserted by grep is
# a guard nobody has ever run: the string can be present and the logic wrong, or
# the logic right and the string moved. So these lift the two `run:` bodies out
# of the YAML verbatim and EXECUTE them against synthetic Playwright reports,
# once per failure mode they exist to catch.
#
# The one substitution is the report path: the bodies hardcode
# /tmp/live-results.json and /tmp/demo-results.json, which the real jobs write
# and a test must not. The substitution is asserted to have actually applied,
# so a renamed path fails loudly instead of testing an unmodified script.


def _step_body(workflow: Path, job_id: str, step_name: str) -> str:
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    steps = [step for step in jobs[job_id]["steps"] if isinstance(step, dict)]
    matching = [str(step["run"]) for step in steps if step.get("name") == step_name]
    assert len(matching) == 1, (
        f"expected exactly one step named {step_name!r} in {workflow.name}:{job_id}, "
        f"found {len(matching)}. The executable checks below would test nothing."
    )
    return matching[0]


def _run_assertion(
    body: str, hardcoded_path: str, report: dict[str, Any], tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    report_file = tmp_path / "results.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    assert hardcoded_path in body, (
        f"{hardcoded_path!r} no longer appears in the step body, so the "
        "substitution below would run the script against the REAL report path "
        "(or against nothing) and prove nothing"
    )
    script = body.replace(hardcoded_path, str(report_file))
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def _live_assertion_body() -> str:
    return _step_body(LIVE_WORKFLOW, "live-e2e", "Assert the suite actually executed")


def _demo_assertion_body() -> str:
    return _step_body(DEMO_WORKFLOW, "demo-e2e", "Assert the suite actually executed")


def _stats(expected: int, skipped: int = 0, unexpected: int = 0, flaky: int = 0) -> dict[str, int]:
    return {
        "expected": expected,
        "skipped": skipped,
        "unexpected": unexpected,
        "flaky": flaky,
    }


@pytest.mark.parametrize(
    ("label", "stats", "live_total", "should_pass"),
    [
        # The happy path, and the partner that keeps the four failures below
        # from being satisfied by a script that rejects everything.
        ("a clean live run", _stats(expected=5), 5, True),
        # THE load-bearing case: a dev server that booted in DEMO mode. Four of
        # the five live tests self-skip on `if (!isLive)`, the fifth passes, and
        # Playwright exits 0. Without this line the job is green with no live
        # coverage at all.
        ("a dev server in demo mode", _stats(expected=1, skipped=4), 5, False),
        ("a single unexplained skip", _stats(expected=4, skipped=1), 5, False),
        ("a real failure", _stats(expected=4, unexpected=1), 5, False),
        ("a green-by-flake", _stats(expected=4, flaky=1), 5, False),
        # Selection said 5, the run accounts for 3. This is what a `>= 1`
        # threshold in place of the full account would let through.
        ("two tests that never ran", _stats(expected=3), 5, False),
    ],
)
def test_the_live_execution_assertion_actually_fires(
    label: str, stats: dict[str, int], live_total: int, should_pass: bool, tmp_path: Path
) -> None:
    result = _run_assertion(
        _live_assertion_body(),
        "/tmp/live-results.json",
        {"stats": stats},
        tmp_path,
        {"LIVE_TOTAL": str(live_total)},
    )
    if should_pass:
        assert result.returncode == 0, (
            f"the live execution assertion rejected {label}, which is a healthy "
            f"run:\n{result.stdout}\n{result.stderr}"
        )
    else:
        assert result.returncode != 0, (
            f"the live execution assertion PASSED on {label} ({stats}). That is "
            f"a silent-green live job.\n{result.stdout}\n{result.stderr}"
        )


# ──────────── the two SELECTION guards, EXECUTED rather than grepped ────────────
#
# These two step bodies shell out to `npx playwright test --list`, which a unit
# test must not run (it is a browser toolchain and it takes ~20 s). So the `npx`
# BINARY is shadowed by a bash function printing a synthetic `--list` fixture,
# and the two `/tmp/*_list.txt` capture paths are redirected into tmp_path.
# Everything else — the greps, the arithmetic, `set -euo pipefail`, the order of
# the assertions — is the workflow's own text, lifted verbatim.

# A test TITLE that contains the summary line's own prefix. This is what makes
# the `^` anchor load-bearing rather than cosmetic, and it is not a contrived
# shape: `answer-format.spec.ts` already titles tests after what the answer
# renders, and "cites Total: 7 sources" is an ordinary thing to assert.
_POISONED_TITLE = "behavior.spec.ts:17:3 › Chat › cites Total: 7 sources under the answer"


def _listing(lines: list[str], total: int, files: int = 2) -> str:
    """A ``playwright test --list --reporter=list`` rendering.

    Shape verified against a real run on 2026-09-09
    (``npx playwright test -c playwright.demo-ci.config.ts --list
    --reporter=list`` → ``Total: 180 tests in 8 files``): a header, one INDENTED
    line per selected test, and the summary at column 0. That indentation is the
    entire reason ``^`` disambiguates.
    """
    body = "".join(f"  [chromium] › {line}\n" for line in lines)
    return f"Listing tests:\n{body}Total: {total} tests in {files} files\n"


def _run_selection_guard(
    body: str, fixtures: list[tuple[str, str, str]], tmp_path: Path
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Execute a "Guard against a vacuous selection" body against fake ``--list`` output.

    ``fixtures`` carries one ``(config filename, hardcoded /tmp capture path,
    listing text)`` per ``npx`` invocation in the body. Both halves of every
    entry are asserted PRESENT before the substitution, so a renamed config or a
    moved capture path fails loudly instead of silently testing an unmodified
    script (or, worse, writing to the path a real CI job uses).

    Returns the completed process and whatever the script appended to
    ``$GITHUB_ENV``.
    """
    script = body
    branches: list[str] = []
    for index, (config, capture_path, listing) in enumerate(fixtures):
        assert config in script, (
            f"{config!r} no longer appears in the step body, so the `npx` stub "
            "would not recognise the invocation and this test would prove nothing"
        )
        assert capture_path in script, (
            f"{capture_path!r} no longer appears in the step body; the "
            "substitution below would leave the script reading and WRITING the "
            "real path a CI job uses"
        )
        source = tmp_path / f"listing-{index}.txt"
        source.write_text(listing, encoding="utf-8")
        script = script.replace(capture_path, shlex.quote(str(tmp_path / f"captured-{index}.txt")))
        branches.append(f"*{config}*) cat {shlex.quote(str(source))} ;;")

    # `case` takes the FIRST matching branch, and the fixtures are ordered so the
    # longer config name (`playwright.demo-ci.config.ts`,
    # `playwright.live.config.ts`) is tried before the shorter
    # `playwright.config.ts`. Neither long name CONTAINS the short one, so this
    # is belt-and-braces, but the ordering is what a reader should be able to
    # rely on.
    stub = (
        'npx() { case "$*" in\n  '
        + "\n  ".join(branches)
        + '\n  *) echo "unexpected npx invocation: $*" >&2; return 99 ;;\n  esac; }\n'
    )
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", stub + script],
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_ENV": str(github_env)},
        check=False,
    )
    return result, github_env.read_text(encoding="utf-8")


def _demo_selection_body() -> str:
    return _step_body(DEMO_WORKFLOW, "demo-e2e", "Guard against a vacuous selection")


def _live_selection_body() -> str:
    return _step_body(LIVE_WORKFLOW, "live-e2e", "Guard against a vacuous selection")


_DEMO_KEEPS_FOCUS = "behavior.spec.ts:31:3 › Hero › keeps focus on the composer"
_DEMO_ORDINARY = "behavior.spec.ts:22:3 › Hero › cycles through the canned Q&As"
_DEMO_VISUAL = "visual.spec.ts:9:3 › visual regression › the landing page at 1280px"


def _demo_fixtures(ci: str, all_: str) -> list[tuple[str, str, str]]:
    return [
        ("playwright.demo-ci.config.ts", "/tmp/ci_list.txt", ci),
        ("playwright.config.ts", "/tmp/all_list.txt", all_),
    ]


@pytest.mark.parametrize(
    ("label", "ci_listing", "all_listing"),
    [
        (
            "a clean listing",
            _listing([_DEMO_ORDINARY, _DEMO_KEEPS_FOCUS], 2, files=1),
            _listing([_DEMO_ORDINARY, _DEMO_KEEPS_FOCUS, _DEMO_VISUAL], 3),
        ),
        (
            "a test titled with the summary prefix",
            _listing([_POISONED_TITLE, _DEMO_KEEPS_FOCUS], 2, files=1),
            _listing([_POISONED_TITLE, _DEMO_KEEPS_FOCUS, _DEMO_VISUAL], 3),
        ),
    ],
    ids=["clean", "poisoned-title"],
)
def test_the_demo_selection_guard_accepts_a_healthy_listing(
    label: str, ci_listing: str, all_listing: str, tmp_path: Path
) -> None:
    """Passing partner AND the direct statement of what ``^Total:`` buys.

    Without it the two failure cases below are satisfied by a script that
    rejects everything. And ``CI_TOTAL`` is asserted BYTE-EXACT, not merely
    "the step exited 0": un-anchored, the poisoned listing makes
    ``ci_total`` the two lines ``7\\n2`` and the step exports a two-line
    ``CI_TOTAL`` into ``$GITHUB_ENV`` (reproduced 2026-09-09), which is a
    corrupt env file for every later step in the job.

    Turns red if: ``^Total:`` is un-anchored to ``Total:`` in
    ``.github/workflows/frontend.yml`` — the poisoned case then exports
    ``CI_TOTAL=7\\n2``.
    """
    result, github_env = _run_selection_guard(
        _demo_selection_body(), _demo_fixtures(ci_listing, all_listing), tmp_path
    )
    assert result.returncode == 0, (
        f"the demo selection guard rejected {label}, which is a healthy "
        f"selection:\n{result.stdout}\n{result.stderr}"
    )
    assert github_env == "CI_TOTAL=2\n", (
        f"the demo selection guard exported {github_env!r} for {label}; expected "
        "exactly 'CI_TOTAL=2\\n'. A multi-line value here means the `Total:` "
        "capture matched a test TITLE as well as the summary line, and it "
        f"corrupts $GITHUB_ENV for every later step.\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("label", "ci_listing", "all_listing"),
    [
        # The control: the differential works on an ordinary listing.
        (
            "a plain shortfall",
            _listing([_DEMO_ORDINARY], 1, files=1),
            _listing([_DEMO_ORDINARY, _DEMO_KEEPS_FOCUS, _DEMO_VISUAL], 3),
        ),
        # THE load-bearing case. Un-anchored, this exits 0 — reproduced.
        (
            "a shortfall under a title containing the summary prefix",
            _listing([_POISONED_TITLE], 1, files=1),
            _listing([_POISONED_TITLE, _DEMO_KEEPS_FOCUS, _DEMO_VISUAL], 3),
        ),
    ],
    ids=["plain-shortfall", "poisoned-shortfall"],
)
def test_the_demo_selection_guard_still_fires_through_a_poisoned_title(
    label: str, ci_listing: str, all_listing: str, tmp_path: Path
) -> None:
    """The demonstrated exit-0 silent pass this anchor exists to close.

    Un-anchored, ``ci_total`` becomes the two lines ``7\\n1`` and ``all_total``
    ``7\\n3``. ``$((all_total - all_visual))`` is then a bash arithmetic SYNTAX
    ERROR — and, reproduced 2026-09-09 under ``set -euo pipefail``, that does
    NOT trip ``set -e``: the script runs to the end and exits **0**, having
    exported ``CI_TOTAL=7\\n1``. So a genuine selection shortfall — the exact
    thing this guard exists to catch — reports green.

    Turns red if: ``^Total:`` is un-anchored to ``Total:`` in
    ``.github/workflows/frontend.yml``.
    """
    result, _ = _run_selection_guard(
        _demo_selection_body(), _demo_fixtures(ci_listing, all_listing), tmp_path
    )
    assert result.returncode != 0, (
        f"the demo selection guard PASSED on {label}. The CI config selects 1 "
        "test while the default suite minus its visual tests is 2, so this is a "
        f"silent-green demo job.\n{result.stdout}\n{result.stderr}"
    )


_LIVE_REFUSES = "behavior.spec.ts:24:3 › Chat › composer refuses a second question (live only)"
_LIVE_PENDING = "behavior.spec.ts:31:3 › Chat › pending bubble renders 3 dots (live only)"
_LIVE_POISONED = "behavior.spec.ts:17:3 › Chat › cites Total: 7 sources (live only)"
_DEMO_TITLED = "landing.spec.ts:9:3 › Landing › renders the hero"


def _live_fixtures(live: str, all_: str) -> list[tuple[str, str, str]]:
    return [
        ("playwright.live.config.ts", "/tmp/live_list.txt", live),
        ("playwright.config.ts", "/tmp/all_list.txt", all_),
    ]


@pytest.mark.parametrize(
    ("label", "live_listing", "all_listing"),
    [
        (
            "a clean listing",
            _listing([_LIVE_REFUSES, _LIVE_PENDING], 2, files=1),
            _listing([_LIVE_REFUSES, _LIVE_PENDING, _DEMO_TITLED], 3),
        ),
        (
            "a live test titled with the summary prefix",
            _listing([_LIVE_POISONED, _LIVE_PENDING], 2, files=1),
            _listing([_LIVE_POISONED, _LIVE_PENDING, _DEMO_TITLED], 3),
        ),
    ],
    ids=["clean", "poisoned-title"],
)
def test_the_live_selection_guard_accepts_a_healthy_listing(
    label: str, live_listing: str, all_listing: str, tmp_path: Path
) -> None:
    """The live workflow fails DIFFERENTLY un-anchored, so the biting case is here.

    Reproduced 2026-09-09: with ``Total:`` un-anchored, the poisoned listing
    makes ``live_total`` the two lines ``7\\n2``; ``test "$live_total" -gt 0``
    then reports "integer expression expected" and the step exits 1 printing
    ``the live config selects 0 tests — this whole job is vacuous``. That is a
    FALSE RED naming the wrong cause on a healthy selection — an operator
    would go looking at ``testDir``, not at a test title. Anchored, the same
    listing passes and exports a single-line ``LIVE_TOTAL``.

    Turns red if: ``^Total:`` is un-anchored to ``Total:`` in
    ``.github/workflows/frontend-live-e2e.yml``.
    """
    result, github_env = _run_selection_guard(
        _live_selection_body(), _live_fixtures(live_listing, all_listing), tmp_path
    )
    assert result.returncode == 0, (
        f"the live selection guard rejected {label}, which is a healthy "
        f"selection:\n{result.stdout}\n{result.stderr}"
    )
    assert github_env == "LIVE_TOTAL=2\n", (
        f"the live selection guard exported {github_env!r} for {label}; expected "
        "exactly 'LIVE_TOTAL=2\\n'. LIVE_TOTAL is the denominator the EXECUTION "
        "assertion later compares the run's `stats` against, so a corrupt value "
        f"here disables that check too.\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("label", "live_listing", "all_listing"),
    [
        (
            "a plain selection shortfall",
            _listing([_LIVE_REFUSES], 1, files=1),
            _listing([_LIVE_REFUSES, _LIVE_PENDING, _DEMO_TITLED], 3),
        ),
        (
            "a shortfall under a title containing the summary prefix",
            _listing([_LIVE_POISONED], 1, files=1),
            _listing([_LIVE_POISONED, _LIVE_PENDING, _DEMO_TITLED], 3),
        ),
    ],
    ids=["plain-shortfall", "poisoned-shortfall"],
)
def test_the_live_selection_guard_fails_a_selection_shortfall(
    label: str, live_listing: str, all_listing: str, tmp_path: Path
) -> None:
    """Failing partner: the two cases above are not satisfied by a script that
    accepts everything. A live-only test that the live config stops selecting
    runs in NO job at all, which is #379's whole subject.
    """
    result, _ = _run_selection_guard(
        _live_selection_body(), _live_fixtures(live_listing, all_listing), tmp_path
    )
    assert result.returncode != 0, (
        f"the live selection guard PASSED on {label}: the live config selects 1 "
        "test while 2 tests in the default config are titled for the live grep, "
        f"so one live-only test runs nowhere.\n{result.stdout}\n{result.stderr}"
    )


def _demo_report(skipped_titles: list[str], passed: int) -> dict[str, Any]:
    """A Playwright JSON report shaped like the real one.

    The nesting is what the real reporter emits and what the workflow walks:
    an outer suite titled with the SPEC FILE, an inner suite per `describe`,
    and `specs[].tests[].status`. Verified against a real run of
    `playwright.demo-ci.config.ts`.
    """
    return {
        "stats": _stats(expected=passed, skipped=len(skipped_titles)),
        "suites": [
            {
                "title": "behavior.spec.ts",
                "suites": [
                    {
                        "title": "Chat",
                        "specs": [
                            {
                                "title": title,
                                "tests": [{"projectName": "chromium", "status": "skipped"}],
                            }
                            for title in skipped_titles
                        ],
                    }
                ],
            }
        ],
    }


_FOUR_LIVE_SKIPS = [
    "pending-bubble renders 3 dots (live only)",
    "composer refuses a second question while one is in flight (live only)",
    "a multi-line answer renders as multiple lines (live only)",
    "renders the subset, chips the markers (live only)",
]


def test_the_demo_jobs_pinned_skip_count_is_still_the_literal_four() -> None:
    """A PRE-EXISTING line that this change made load-bearing for a second rule.

    ``if stats["skipped"] != 4:`` used to stand alone: relaxing it to ``> 99``
    only widened what the demo job tolerated, and a review-round mutant doing
    exactly that survived everything. It now also feeds the rename trap's
    completeness partner (``len(skipped) != stats["skipped"]``), so the same
    relaxation would take the partner with it — the walk could find one skipped
    test out of four and still agree with a stats value nothing constrains.

    Static rather than executable because the number is the SUBJECT here, not an
    input: the executable cases below all supply their own stats.

    Turns red if: the literal ``4`` in ``frontend.yml``'s skip pin changes, or
    the comparison stops being ``!=``.
    """
    body = _demo_assertion_body()
    assert re.search(r'if\s+stats\["skipped"\]\s*!=\s*4\s*:', body), (
        'frontend.yml\'s demo job no longer pins `stats["skipped"] != 4`. Four '
        "tests self-skip in demo mode via `if (!isLive)`; a threshold in place "
        "of the exact count lets new skips accumulate silently, AND unpins the "
        "value the rename trap's completeness partner compares its walk "
        "against. If a fifth demo skip is genuinely correct, change the literal "
        "here and there in the same commit, with the reason."
    )


def test_the_demo_rename_trap_passes_on_a_healthy_report(tmp_path: Path) -> None:
    """The partner. Without it, every red case below is satisfied by a script
    that fails on everything — which would be a broken guard, not a working one.
    """
    result = _run_assertion(
        _demo_assertion_body(),
        "/tmp/demo-results.json",
        _demo_report(_FOUR_LIVE_SKIPS, passed=176),
        tmp_path,
        {"CI_TOTAL": "180"},
    )
    assert result.returncode == 0, (
        f"the demo assertion rejected a healthy report:\n{result.stdout}\n{result.stderr}"
    )


def test_the_demo_rename_trap_catches_a_title_renamed_out_of_the_live_suite(
    tmp_path: Path,
) -> None:
    """The headline case for #379.

    Rename ``(live only)`` to the hyphenated ``(live-only)`` — a tidy-up nobody
    would flag in review — and the test keeps skipping in demo mode (so the
    pinned skip count is unchanged and that job stays green) while
    ``grep: /live only/i`` stops selecting it. It then runs in NO job at all.
    This is the only check that sees it.
    """
    renamed = list(_FOUR_LIVE_SKIPS)
    renamed[1] = "composer refuses a second question while one is in flight (live-only)"
    result = _run_assertion(
        _demo_assertion_body(),
        "/tmp/demo-results.json",
        _demo_report(renamed, passed=176),
        tmp_path,
        {"CI_TOTAL": "180"},
    )
    assert result.returncode != 0, (
        "the demo assertion PASSED with a demo-skipped test whose title no "
        f"longer matches the live grep:\n{result.stdout}\n{result.stderr}"
    )
    assert "live-only" in result.stdout, (
        "the failure does not name the test that now runs nowhere, which is the "
        f"only thing that makes it actionable:\n{result.stdout}"
    )


@pytest.mark.parametrize(
    ("label", "walked"),
    [
        # The original case: the walk finds nothing at all.
        ("an empty skip list", 0),
        # The case `if not skipped:` could not see. Demonstrated in review: stats
        # say 4 skipped, the walk reaches 1, the other 3 are renamed out of the
        # live grep — and the rule exited 0 printing "all 1 demo-skipped tests
        # are selected". A silent pass over exactly the tests it exists to catch.
        ("a PARTIAL skip list (1 of 4 walked)", 1),
        ("a PARTIAL skip list (3 of 4 walked)", 3),
    ],
)
def test_the_demo_rename_trap_refuses_to_run_over_an_incomplete_skip_list(
    label: str, walked: int, tmp_path: Path
) -> None:
    """ "Every skipped test is live-selected" is vacuously true over no tests,
    and only PARTLY true over some of them.

    The realistic way to get here is a Playwright bump changing the JSON schema,
    after which the walk finds nothing (or, worse, part of the tree) and the rule
    silently stops checking. ``stats["skipped"]`` still says four, so the
    disagreement is detectable — which is what the completeness comparison uses.

    Turns red if: ``frontend.yml``'s ``len(skipped) != stats["skipped"]``
    reverts to ``if not skipped:`` — the 1-of-4 and 3-of-4 cases then pass.
    """
    # Keep the walkable specs and the stats deliberately out of step: stats
    # always report 4 (which the pinned `!= 4` line requires), the tree carries
    # `walked`.
    report = _demo_report(_FOUR_LIVE_SKIPS[:walked], passed=176)
    report["stats"] = _stats(expected=176, skipped=4)
    result = _run_assertion(
        _demo_assertion_body(),
        "/tmp/demo-results.json",
        report,
        tmp_path,
        {"CI_TOTAL": "180"},
    )
    assert result.returncode != 0, (
        f"the demo assertion PASSED on {label} while stats reported 4 skipped. "
        "It is checking a fraction of the list and reporting success over the "
        f"whole of it:\n{result.stdout}\n{result.stderr}"
    )
    assert "4" in result.stdout and str(walked) in result.stdout, (
        "the failure must name BOTH numbers — what stats claimed and what the "
        f"walk actually found — or it is not actionable:\n{result.stdout}"
    )


def test_the_demo_rename_trap_reads_the_project_name_into_the_grep_string(
    tmp_path: Path,
) -> None:
    """Pins the ``projectName`` element of the rebuilt grep string.

    Playwright greps the JOINED path and it STARTS with the project name —
    re-measured on 1.61.0 in this repo: ``--grep "^chromium behavior.spec.ts
    Chat "`` selects 15 tests, ``--grep "spec.ts"`` selects all 202. The
    workflow rebuilds that string, but dropping ``projectName`` from the join
    changed no other test here, so a review-round mutant doing so survived.

    The fixture is deliberately artificial: a project whose NAME carries the
    live marker while neither the suite titles nor the spec title do. Nothing in
    this repo is configured that way today; what it pins is the mechanism —
    Playwright would select this test, so the trap must not call it an orphan,
    and it can only know that by including the project segment.

    Turns red if: ``[test.get("projectName", "")]`` is dropped from the join in
    ``frontend.yml``'s rename trap.
    """
    report = _demo_report(_FOUR_LIVE_SKIPS[:3], passed=176)
    report["stats"] = _stats(expected=176, skipped=4)
    report["suites"].append(
        {
            "title": "harness.spec.ts",
            "suites": [
                {
                    "title": "browser matrix",
                    "specs": [
                        {
                            "title": "renders the badge",
                            "tests": [{"projectName": "chromium (live only)", "status": "skipped"}],
                        }
                    ],
                }
            ],
        }
    )
    result = _run_assertion(
        _demo_assertion_body(),
        "/tmp/demo-results.json",
        report,
        tmp_path,
        {"CI_TOTAL": "180"},
    )
    assert result.returncode == 0, (
        "the trap flagged a test whose live marker sits on the PROJECT NAME as "
        "running nowhere. Playwright's grep string starts with the project "
        f"name, so it does select it:\n{result.stdout}\n{result.stderr}"
    )


def test_the_demo_rename_trap_matches_the_same_string_playwright_greps(tmp_path: Path) -> None:
    """Playwright's grep path includes the project name and the SPEC FILE PATH.

    Measured on Playwright 1.61.0 in this repo:
    ``--grep "^chromium behavior.spec.ts Chat "`` selects 15 tests and
    ``--grep "spec.ts"`` selects the whole 202-test suite. So a test whose own
    title says nothing about live mode, but which sits under a describe block
    that does, IS selected by the live config — and the trap must not report it
    as an orphan.
    """
    # Four skipped, because the pinned `stats["skipped"] != 4` line runs first.
    # Three carry "(live only)" in their own title; the fourth carries it only
    # on its enclosing describe, which is the case under test.
    report = _demo_report(_FOUR_LIVE_SKIPS[:3], passed=176)
    report["stats"] = _stats(expected=176, skipped=4)
    report["suites"].append(
        {
            "title": "behavior.spec.ts",
            "suites": [
                {
                    "title": "pending-bubble behaviour (live only)",
                    "specs": [
                        {
                            "title": "renders 3 dots",
                            "tests": [{"projectName": "chromium", "status": "skipped"}],
                        }
                    ],
                }
            ],
        }
    )
    result = _run_assertion(
        _demo_assertion_body(),
        "/tmp/demo-results.json",
        report,
        tmp_path,
        {"CI_TOTAL": "180"},
    )
    assert result.returncode == 0, (
        "the trap flagged a test selected by its DESCRIBE title as running "
        f"nowhere; it is rebuilding the grep string wrongly:\n{result.stdout}\n{result.stderr}"
    )


def test_the_guards_own_blind_spots_are_written_down() -> None:
    """State what this file cannot see, so the gap is recorded, not assumed away.

    The biggest one is that branch protection itself is unreadable from here:
    these guards make the live workflow ELIGIBLE to be required and keep it that
    way, but nothing in CI can confirm the owner ever promoted it, or notice it
    being demoted again.
    """
    assert _UNGUARDABLE, "the blind-spot register must not be silently emptied"


def test_the_ci_workflow_that_runs_these_guards_is_itself_unfiltered() -> None:
    """These rules are only enforced if the job carrying them runs on every PR.

    ``ci.yml``'s ``test`` job ("pytest + lint") is a required context. If it
    gained a ``paths:`` filter, this whole file would become advisory on exactly
    the PRs it exists to police — a workflow-only change.
    """
    triggers = _triggers(_load_workflow("ci.yml"))
    pull_request = triggers.get("pull_request")
    assert isinstance(pull_request, dict), "ci.yml has no `pull_request:` trigger"
    assert not (set(_PATHS_KEYS) & set(pull_request)), (
        "ci.yml's pull_request trigger gained a path filter; the `pytest + lint` "
        "job carrying these guards would stop running on the PRs they cover"
    )
    jobs = _load_workflow("ci.yml").get("jobs") or {}
    assert (jobs.get("test") or {}).get("name") == "pytest + lint", (
        "ci.yml's `test` job is no longer named `pytest + lint`, which is a "
        "REQUIRED status check — renaming it wedges every PR"
    )


def test_the_backlog_row_and_this_guard_agree_on_the_issue() -> None:
    """Cheap pin that the tracked follow-up index still points at this work.

    ``docs/BACKLOG.md`` is the in-repo index AGENTS.md requires reading at the
    start of every session. A row that no longer mentions the file that fixed it
    sends the next session looking in the wrong place.
    """
    backlog = (REPO_ROOT / "docs" / "BACKLOG.md").read_text(encoding="utf-8")
    row = [line for line in backlog.splitlines() if "/issues/379" in line]
    assert len(row) == 1, f"expected exactly one #379 row in docs/BACKLOG.md, found {len(row)}"
    assert Path(__file__).name in row[0], (
        f"docs/BACKLOG.md's #379 row does not mention {Path(__file__).name}, the "
        "file that carries these guards"
    )


# `test_the_pinned_required_contexts_are_json_serialisable_strings` was DELETED
# here. It asserted `json.loads(json.dumps(X)) == X` over literals defined a few
# lines above it: no edit anywhere in this repository could turn it red, so by
# CLAUDE.md's rule ("if reverting the fix leaves it green, it is not a test") it
# was not one. What it was reaching for — that the string the owner sends is the
# string the job reports — is now actually checked, by
# `test_the_documented_promotion_command_sends_the_real_job_name`.
