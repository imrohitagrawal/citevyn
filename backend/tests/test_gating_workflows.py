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
# it. THIS DICT IS THE AUTHORITATIVE RECORD in this repo: docs/TEST_STRATEGY.md
# §12.1's table and its stated count are compared against it by
# `test_the_strategy_doc_lists_exactly_the_recorded_contexts`, so the two can no
# longer drift the way they had by 2026-09-16.
#
# Re-measured 2026-09-16 (previous reading 2026-09-08):
#
#   gh api repos/imrohitagrawal/citevyn/branches/main/protection \
#     --jq '.required_status_checks.contexts[]'
#
# TEN contexts, not nine. `Live-mode Playwright (stub backend)` HAS BEEN
# PROMOTED — #379's whole objective — some time between the two readings. The
# 2026-09-08 snapshot outlived that change and this file went on describing the
# live job as "not required yet" in two comments, which is what a snapshot with
# no mechanical check does. `.github/dependabot.yml` already carried the correct
# 10 (its auto-merge note), so the repo disagreed with itself; this is now the
# side that is asserted.
#
# Also measured 2026-09-16, because §12.5 predicts otherwise: every one of the
# ten carries an app_id pin — nine are `15368` (github-actions) and `CodeQL` is
# `57789`. NONE is null, the live context included. Read with:
#
#   gh api repos/imrohitagrawal/citevyn/branches/main/protection/required_status_checks \
#     --jq '.checks[] | "\(.app_id)  \(.context)"'
#
# RE-DERIVED INDEPENDENTLY on 2026-09-24 by the #448 work, which reached the same
# ten. Worth keeping both readings: a completeness test is only as complete as the
# population it is complete over, nothing in CI can read branch protection
# (`_UNGUARDABLE` records that), so this list is a manual step and
# `test_every_required_context_is_in_one_of_the_two_registers` is only as good as
# the last person to run the command above.
_REQUIRED_CONTEXT_WORKFLOWS: dict[str, str] = {
    "Analyze (python)": "codeql.yml",
    "alembic + postgres integration tests": "ci.yml",
    "pytest + lint": "ci.yml",
    "quality-gate / quality-gate": "pr-quality.yml",
    "type-check + unit tests + build": "frontend.yml",
    "Demo-mode Playwright (no visual snapshots)": "frontend.yml",
    "Live-mode Playwright (stub backend)": LIVE_WORKFLOW.name,
    "shell suites (bash 3.2 + 5.x) (ubuntu-latest)": "ci.yml",
    "shell suites (bash 3.2 + 5.x) (macos-latest)": "ci.yml",
}

# The size of the two registers AS MEASURED, compared as a BASELINE that must not
# shrink silently — the same shape as `backend/coverage-baseline.json`'s `missed`,
# and for the same reason. A floor of "at least 2" against a real population of
# nine is a partner that barely partners: six rows could be deleted, and with them
# the completeness rule's whole reach, while every assertion here stayed green.
#
# Re-derive the first with:
#   gh api repos/imrohitagrawal/citevyn/branches/main/protection \
#     --jq '.required_status_checks.contexts | length'
# → 10 on 2026-09-24, of which one (`CodeQL`) is produced by no workflow and lives
# in `_REQUIRED_CONTEXTS_NOT_FROM_A_WORKFLOW`, leaving 9 here.
#
# A context the owner genuinely DEMOTES, or a gating job genuinely retired, is a
# deliberate one-line update to these numbers in the same change — which is the
# point. Shrinking either register is how the rules below quietly stop reaching.
_MEASURED_REQUIRED_CONTEXTS_FROM_A_WORKFLOW = 9
_MEASURED_GATING_JOBS = 5

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
# in GATING_WORKFLOWS", because some jobs hosting a required context carry a
# DELIBERATE job-level ``if:``, or no ``steps:`` at all (see
# _NOT_HELD_TO_THE_DEFUSING_RULE), and a blanket rule would have to be weakened
# until it caught nothing. Being hand-picked is what made an OMISSION possible,
# so `test_every_required_context_is_in_one_of_the_two_registers` now requires
# every required context to land in this tuple or in that register, with a
# reason — never in neither (#448).
#
# Ordered so the FIRST entry is `ci.yml`'s `test` — the longest-standing gating
# job here and not the one #379 is about, so the rule is exercised against a
# known-good control as well as against the new candidate.
GATING_JOBS: tuple[tuple[str, str], ...] = (
    # `pytest + lint`. Required, and it is the job that RUNS this file: one
    # `continue-on-error: true` here turns every rule in it advisory at once.
    ("ci.yml", "test"),
    # `shell suites (bash 3.2 + 5.x) (ubuntu-latest)` and `… (macos-latest)`. TWO
    # required contexts from one job, and the ONLY jobs that execute
    # `tests/shell/*` — the rollback, env-guard, deploy_verify and bundle-key
    # regression suites. `continue-on-error: true` on its single assertion step
    # ("Run every tests/shell suite") turns both contexts advisory and takes
    # every one of those suites with them (#448). It was absent from this
    # population AND from the exemption register below, which is the state
    # `test_every_required_context_is_in_one_of_the_two_registers` now forbids.
    # Matrixed on purpose, so it is exempt from the matrix rule alone — see
    # `_MATRIXED_ON_PURPOSE`.
    ("ci.yml", "shell-tests"),
    # `type-check + unit tests + build`. Required; carries the vitest suite and
    # the bundle budget gate.
    ("frontend.yml", "build"),
    # `Demo-mode Playwright (no visual snapshots)`. Required; carries the
    # #379 title-rename trap, which is the only check that bites TODAY.
    ("frontend.yml", "demo-e2e"),
    # `Live-mode Playwright (stub backend)`. REQUIRED as of the 2026-09-16
    # reading above — #379 achieved what it set out to do. This comment said
    # "not required yet" until then, which is what an un-checked snapshot decays
    # into.
    # A defused job here now WEDGES merges rather than merely forfeiting a
    # candidate, which is the practical difference promotion made.
    (LIVE_WORKFLOW.name, "live-e2e"),
    # `Visual snapshots (Linux baselines, advisory)` (#325). ADVISORY, and the
    # ONLY advisory member of this tuple — the four above are all required as of
    # 2026-09-16. Being in this list does not promote it: this list is the
    # population of ONE rule, "a job in it may not be defused", and it is not the
    # same list as `_REQUIRED_CONTEXT_WORKFLOWS`. Branch protection is the only
    # thing that makes a check required, and changing it is the owner's call.
    #
    # An earlier version of this comment justified the entry by pointing at
    # `live-e2e` "sitting here while advisory". That precedent was FALSE when it
    # was written — live-e2e was already required — so the entry is argued on its
    # own terms instead: a defused advisory job is not a weak signal, it is a
    # fake one. `continue-on-error: true` here would leave a green check beside
    # 22 screenshot comparisons that never ran, which reads as "the baselines are
    # fine" to exactly the person who would otherwise go and look. An advisory
    # check is read by a human deciding whether to investigate; that is precisely
    # the audience a false green misleads.
    ("frontend.yml", "visual-e2e"),
)

# Jobs inside ``GATING_JOBS`` held to every rule EXCEPT the no-matrix one, with
# the reason. Recorded here rather than by keeping the job OUT of
# ``GATING_JOBS``, because the defusing rule is the one that matters most and
# dropping the job would have exempted it from that too — which is exactly the
# hole #448 is about.
_MATRIXED_ON_PURPOSE: dict[str, str] = {
    "ci.yml:shell-tests": (
        "`strategy.matrix: os: [ubuntu-latest, macos-latest]` is the POINT of "
        "this job: `${v:1:-1}` is bash 4.2+ and errors on the bash 3.2 macOS "
        "ships, so a Linux-only job reports green on exactly the bug #161 "
        "describes. Branch protection already holds both expanded names "
        "(`shell suites (bash 3.2 + 5.x) (ubuntu-latest)` and "
        "`… (macos-latest)`), so the renaming hazard the matrix rule guards "
        "against has already been paid for and recorded in "
        "`_REQUIRED_CONTEXT_WORKFLOWS`."
    ),
}

# The population the no-matrix rule is parametrized over: every gating job whose
# status-check context is NOT already pinned in its expanded form.
_JOBS_HELD_TO_THE_MATRIX_RULE: tuple[tuple[str, str], ...] = tuple(
    (workflow, job_id)
    for workflow, job_id in GATING_JOBS
    if f"{workflow}:{job_id}" not in _MATRIXED_ON_PURPOSE
)

# Gating jobs deliberately NOT in the population above, with the reason. An
# unexplained omission would look identical to one this guard forgot — and for
# `ci.yml:shell-tests` that is not hypothetical: it sat in NEITHER register
# until #448, so nothing in this file would have noticed either of its two
# required contexts being defused.
#
# Keys are ``"<workflow file>:<job id>"`` and MUST resolve to a real job, because
# `test_every_required_context_is_in_one_of_the_two_registers` looks required
# contexts up in this dict by that key. The codeql + pr-quality entry used to be
# a single key naming two jobs at once, which resolved to neither.
_NOT_HELD_TO_THE_DEFUSING_RULE: dict[str, str] = {
    "ci.yml:postgres-migrations": (
        "`alembic + postgres integration tests` IS a required context but carries "
        "a deliberate job-level `if:` restricting it to same-repo pushes and PRs "
        "(forks get no Postgres service and no secrets). Holding it to `no job "
        "`if:`` would either fail on `main` or force an exemption so specific it "
        "would re-admit `if: false`. ONE of its silent-green failure modes — the "
        "whole `postgres` suite skipping when `CITEVYN_PG_TEST_URL` is gone — is "
        "guarded instead by `tests/test_postgres_suite_is_not_vacuous.py` (#449), "
        "which runs INSIDE the job's own `-m postgres` selection. Its PYTEST STEP "
        "is separately pinned by `test_ci_workflow_conditions.py` against "
        "`|| true` / `--ignore` / `--deselect` / `-k`, against step-level "
        "`continue-on-error:`, and against an `env:` override of `CI` or either "
        "database URL. The job's OTHER steps are still uncaught, because this "
        "exemption is whole-job: closing that needs an exemption shaped per-rule "
        "rather than per-job, which is a separate change. (An earlier version of "
        "this paragraph said the pytest step was uncaught too — false about code "
        "added in the same commit, and exactly the stale-exemption prose these "
        "rules exist to prevent.)"
    ),
    "codeql.yml:analyze": (
        "`Analyze (python)` IS a required context, produced outside #379's scope "
        "and matrixed by language. Adding it is a separate change with its own "
        "review; recorded so the gap is visible."
    ),
    "pr-quality.yml:quality-gate": (
        "`quality-gate / quality-gate` IS a required context, but the job is a "
        "`uses:` call into a reusable workflow in ANOTHER repository "
        "(`imrohitagrawal/.github`). It declares no `steps:` of its own and "
        "carries a deliberate job-level `if: github.event.pull_request.draft == "
        "false`, so both halves of the defusing rule (no job `if:`, at least "
        "three steps) would fail on the real repo. What the called workflow does "
        "is not readable from this repository at all."
    ),
}

# The ONLY step-level ``if:`` values tolerated inside a gating job, keyed by
# ``(workflow, job id, step name)`` and compared BYTE-EXACTLY, never by
# substring. Every one is an artifact-upload step: it carries no assertion, so
# skipping it cannot weaken the gate. Values read out of the YAML on 2026-09-08,
# and re-read 2026-09-16 for the two entries #325 moved.
#
# ``frontend.yml:build:Upload dist artifact`` was REMOVED here in #325 because
# the step itself was removed. Nothing consumed the `frontend-dist` artifact:
# no ``actions/download-artifact`` step and no ``workflow_run`` trigger exists
# anywhere under ``.github/``, and ``release.yml`` builds its image from source.
# ``test_every_sanctioned_step_condition_still_names_a_real_step`` is what makes
# leaving a stale entry behind impossible — it resolves every key against the
# real YAML.
_SANCTIONED_STEP_CONDITIONS: dict[tuple[str, str, str], str] = {
    ("ci.yml", "test", "Upload coverage report"): "${{ !cancelled() }}",
    ("frontend.yml", "demo-e2e", "Upload report + traces on failure"): "failure()",
    ("frontend.yml", "visual-e2e", "Upload report + snapshot diffs on failure"): "failure()",
    (LIVE_WORKFLOW.name, "live-e2e", "Upload HTML report on failure"): "failure()",
}

# Shell idioms that swallow a failure or narrow a selection INSIDE a step's
# ``run:`` body. `continue-on-error:` is not the only three words that defuse a
# gate — `run: make test-shell || true` turns both `shell suites …` contexts green
# with every ops-script suite failing, and the ``if``/``continue-on-error`` rule
# below reads two KEYS and cannot see it. Found by an adversarial review of #448,
# whose headline risk is "turned advisory by a one-line YAML edit"; this was still
# a one-line YAML edit that does it.
#
# Matched as substrings on purpose (these are shell text, not YAML values), so the
# rule is deliberately broad and every legitimate use has to be recorded below.
#
# Three near-misses are deliberately ABSENT, each checked against the real step
# bodies rather than reasoned about:
#   * a bare `exit 0` — it WOULD swallow a verdict at the end of a body, but the
#     string already appears inside PROSE in `frontend-live-e2e.yml`'s "Assert the
#     suite actually executed" step ("it would otherwise exit 0 with no live
#     coverage"), so the rule would go red on a comment. `|| exit 0` IS listed,
#     which is the form that actually swallows;
#   * `&& true` — it does not swallow. `false && true` exits 1, because `&&`
#     short-circuits and the status is the left-hand command's;
#   * `2>/dev/null` — hides stderr, not the exit code.
# `set +e` IS listed and `set -e` is NOT: four gating steps open with
# `set -euo pipefail`, which is the opposite of defusing.
_DEFUSING_RUN_IDIOMS: tuple[str, ...] = (
    "|| true",
    "|| :",
    "|| exit 0",
    "; true",
    "set +e",
    # The long form of `set +e`. A verification round pointed out that listing
    # only the short one is a rule that reads a spelling rather than a behaviour.
    "set +o errexit",
    "set +o pipefail",
    # `if ! make test-shell; then echo "::warning::"; fi` exits 0 with the suite
    # failing. MEASURED as a bypass of the earlier list under GitHub's `bash -e`,
    # and no gating step uses `if !` today, so listing it costs nothing.
    "if !",
    "--ignore=",
    "--ignore-glob",
    "--deselect",
    "continue-on-error",
)

# The ONLY lines inside a gating job's ``run:`` that may carry one of the idioms
# above, keyed by ``(workflow, job id, step name)`` and compared BYTE-EXACTLY
# against the stripped line, never by substring. Read out of the YAML on
# 2026-09-24.
#
# All three are `grep -c` COUNTERS inside a command substitution: `grep` exits 1
# when it matches nothing, which is a legitimate zero and not a failure, and the
# count is asserted on immediately afterwards. They are sanctioned because they
# cannot swallow the step's own verdict.
#
# What they DO hide is already known and tracked rather than re-discovered here: a
# `|| true` on a producer conceals a CRASHED producer, not merely a zero count, so
# a `grep` reading a file that was never written reports 0 and looks like a clean
# count. That is a property of these three lines, not a reason to widen the rule.
_SANCTIONED_RUN_IDIOM_LINES: dict[tuple[str, str, str], tuple[str, ...]] = {
    ("frontend.yml", "demo-e2e", "Guard against a vacuous selection"): (
        "ci_visual=$(grep -c 'visual regression' /tmp/ci_list.txt || true)",
        "all_visual=$(grep -c 'visual regression' /tmp/all_list.txt || true)",
    ),
    (LIVE_WORKFLOW.name, "live-e2e", "Guard against a vacuous selection"): (
        "all_live_titled=$(grep -ciE 'live only' /tmp/all_list.txt || true)",
    ),
    # #325 added `visual-e2e` to GATING_JOBS, which brought it under this rule
    # for the first time. Same shape as the demo job two entries up: `grep -c`
    # exits 1 when the count is ZERO while still printing `0`, so without
    # `|| true` the step would die on the very reading it exists to take. The
    # `|| true` therefore cannot hide a weak gate here, because the zero it
    # preserves is exactly what the next line refuses:
    # `test "$visual_total" -gt 0`, with `test "$visual_total" -eq "$all_visual"`
    # beside it. Sanctioned verbatim rather than by pattern, so a line that
    # merely LOOKS like a counter does not inherit the exemption.
    ("frontend.yml", "visual-e2e", "Guard against a vacuous selection"): (
        "all_visual=$(grep -c 'visual regression' /tmp/all_list.txt || true)",
        "selected_visual=$(grep -c 'visual regression' /tmp/visual_list.txt || true)",
    ),
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
    "That HALF the partition assertion now lives in an ADVISORY job. The "
    "REQUIRED demo job proves `ci_total == all_total - all_visual` and "
    "`all_visual > 0`; only `visual-e2e` proves `visual_total == all_visual` and "
    "`selected_visual == visual_total`. So if `playwright.visual-ci.config.ts` "
    "breaks, the pull request still merges — the tests below EXECUTE both guard "
    "bodies, but against synthetic listings, so nothing required ever runs them "
    "on the real numbers. That is the price of the job being advisory, recorded "
    "rather than argued away (#325).",
    "Whether `visual-e2e` is still OUTSIDE branch protection. Everything here "
    "treats it as advisory — the defusing rule, docs/TEST_STRATEGY.md §12.2, "
    "and the deliberate absence of a `contexts[]=` command for it. If the owner "
    "promoted it, nothing in this file would notice, and the next font-rendering "
    "shift would block every merge with no warning. Same root cause as the "
    "branch-protection entry below: a test cannot read that API (#325).",
    "Whether the `*-chromium-linux.png` baselines were drawn by the browser the "
    "container tag names, or on the right CPU architecture. "
    "(They were reviewed image by image and re-run in the job's own container "
    "against the merged tree before landing, but that is a one-off check by a "
    "person, not a rule this file enforces.) "
    "`test_the_visual_job_runs_the_browser_that_drew_its_baselines` pins the TAG "
    "against the lockfile; it cannot open a PNG and ask what rendered it. "
    "Measured while generating them: arm64 and amd64 renders differ for 12 of "
    "the 22, so a regeneration on an Apple-silicon machine without "
    "`--platform linux/amd64` would commit 12 wrong images and pass every rule "
    "here. What catches it is the advisory job itself going red (#325).",
    "Branch protection itself. This file cannot read it (that needs a token and "
    "a network call from a test), so it cannot detect a context being REMOVED "
    "from the required list, nor confirm one was added. The snapshot in "
    "_REQUIRED_CONTEXT_WORKFLOWS is a 2026-09-16 hand reading, not a live query "
    "— and the cost of that is now demonstrated rather than hypothetical: the "
    "previous 2026-09-08 snapshot missed the promotion of `Live-mode Playwright "
    "(stub backend)` entirely, and two comments in this file went on calling it "
    "advisory until #325 re-measured. Anything asserted from this dict is only "
    "as fresh as the last hand reading; re-run the `gh api` command above when "
    "in doubt.",
    "A live-only test selected by its DESCRIBE title rather than its own test "
    'title. The static scan below reads `test("...")` titles only, so such a '
    "test would be missing from the membership set and would fail the pin — "
    "loudly, but with a misleading message. All five today match on their own "
    "title.",
    "A test whose title is built from a variable or a template literal with a "
    "substitution. The scan reads string literals; `test(`... ${x}`)` is skipped. "
    "`visual.spec.ts` is the live example. CORRECTED 2026-09-16: this entry said "
    '"one templated declaration that Playwright expands to 22 selected tests". '
    "The file declares TWO — the templated ``test(`${section.name}`)`` at :38, "
    "which expands to 20 (10 sections x 2 themes), and a LITERAL "
    '``test("chat-empty")`` at :51, which the scan reads perfectly well and '
    "expands to 2. The blind spot is 20 of the 22, not all of them.",
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
    "Branch protection's `checks[]` app_id pins. All TEN contexts are bound to an "
    "app — nine to 15368 (github-actions) and CodeQL to 57789 "
    "(github-advanced-security), measured 2026-09-16. This entry said NINE, and "
    "went on to state as a live worry that a context added through the "
    "`contexts` endpoint lands with `app_id: null`; §12.5 was rewritten in the "
    "same change to record that this did NOT happen to the one context added "
    "since, so the file was giving two answers to one question. What remains "
    "unguardable is unchanged: reading these pins needs a token, so nothing "
    "asserts them, and nothing here can tell which route the owner used.",
    "Whether the live suite's assertions are any good. These guards prove it "
    "RUNS and that nothing silently drops out of it, not that it tests the right "
    "thing.",
    "A job name SHORTENED to a prefix of its required context. "
    "`_job_ids_for_context` matches on `context.startswith(prefix)`, because that "
    "is what makes `Analyze (python)` and the two matrix-expanded shell contexts "
    "resolve at all — so renaming `ci.yml:test` from `pytest + lint` to `pytest` "
    "still resolves, and both the completeness rule and "
    "`test_the_recorded_required_context_still_names_a_real_job` stay green while "
    "GitHub reports `pytest` and branch protection waits for `pytest + lint` "
    "forever. Found by an adversarial review of #448. NOT silent (it wedges every "
    "PR, loudly) but the stated purpose of those two rules is unmet. "
    "`ci.yml:shell-tests` is incidentally covered, by "
    "`test_the_matrix_rule_names_a_key_github_really_uses`'s "
    '`startswith(f"{bare} (")`; the other three gating jobs have no equivalent. '
    "Closing it needs an exact-name pin per non-matrixed context, which would "
    "have to be argued against the matrix cases.",
    "A `strategy.matrix` SHRUNK rather than added. `ci.yml:shell-tests` reduced to "
    "`os: [ubuntu-latest]` still declares `strategy`, so the matrix-exemption "
    "register's `not_matrixed` check passes, and `_REQUIRED_CONTEXT_WORKFLOWS` "
    "still lists both expansions, so nothing compares the matrix VALUES to the "
    "recorded expansions. bash 3.2 coverage — #161's whole defect class — would be "
    "gone. Consequence is a permanent wedge on the never-reported macos context, "
    "so loud rather than silent.",
    "A `postgres`-suite skip moved somewhere this file and #449's guard cannot "
    "see: `@pytest.mark.skip` on each of the 15 test FUNCTIONS rather than the "
    "module, `collect_ignore` in a conftest, or a "
    "`pytest_collection_modifyitems` that skips `postgres` items. Each gives a "
    "green `alembic + postgres integration tests` context over zero executed "
    "tests. The settling mechanism is the same one the live job already has — read "
    "the RUN's own outcome (`--junit-xml` plus a step that asserts on it), which "
    "is a workflow edit and so needs the owner. Recorded in "
    "`tests/test_postgres_suite_is_not_vacuous.py` too.",
    "Anything under `tests/shell/`. Nothing in this repository asserts which shell "
    "suites exist or that any given case executes — `Makefile`'s runner only fails "
    "when it finds ZERO suites, so deleting one, or re-adding an early `skip` "
    "branch inside one, is green. #448 made `test_rollback_policy_warn.sh`'s case "
    "3 hermetic; nothing pins that it stays so. A shell-suite inventory guard is a "
    "separate change.",
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


def _scannable_run_lines(body: str) -> list[str]:
    """A step's ``run:`` body, prepared so a per-line idiom scan is not fooled.

    Two corrections from a verification round, both of which it MEASURED rather
    than argued:

    * a line ending in ``||`` or ``&&`` CONTINUES onto the next one, so
      ``make test-shell ||`` / ``true`` split across two lines swallows the failure
      (exit 0, confirmed under ``bash -e``) while neither line on its own contains
      a listed idiom. Continuations are joined before scanning;
    * a SHELL COMMENT cannot defuse anything, but a comment MENTIONING ``|| true``
      would match and go red. Not hypothetical: the four gating steps carry 106
      comment lines inside their ``run:`` bodies between them, and the two most
      likely to gain such a comment already have one just outside the block.
      Comment lines are dropped.

    Join first, then drop comments, so a continuation is not stitched to a comment.
    """
    joined: list[str] = []
    pending = ""
    for raw in body.split("\n"):
        stripped = raw.strip()
        if pending:
            stripped = f"{pending} {stripped}"
            pending = ""
        if stripped.endswith(("||", "&&")):
            pending = stripped
            continue
        joined.append(stripped)
    if pending:
        joined.append(pending)
    return [line for line in joined if not line.startswith("#")]


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

    Three words are not the only way. ``run: make test-shell || true`` turns both
    ``shell suites …`` contexts green with every ops-script suite failing, and the
    ``if``/``continue-on-error`` checks read two KEYS, so they cannot see it. The
    ``run:`` body is therefore scanned for ``_DEFUSING_RUN_IDIOMS``, with the
    three legitimate ``grep -c`` counters recorded byte-exactly in
    ``_SANCTIONED_RUN_IDIOM_LINES``.

    Turns red if: any gating job gains an ``if:``, gains a
    ``continue-on-error:`` that is not literal ``false``, or any of its steps
    does — except the four artifact-upload steps listed in
    ``_SANCTIONED_STEP_CONDITIONS`` with exactly their recorded condition — or if
    any step's ``run:`` body gains an unsanctioned failure-swallowing idiom.
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
        # `run: make test-shell || true` defuses a gate just as completely as
        # `continue-on-error: true`, and the two key checks above cannot see it.
        allowed_lines = _SANCTIONED_RUN_IDIOM_LINES.get((workflow, job_id, label), ())
        for line in _scannable_run_lines(str(step.get("run") or "")):
            stripped = line.strip()
            if stripped in allowed_lines:
                continue
            for idiom in _DEFUSING_RUN_IDIOMS:
                if idiom in stripped:
                    violations.append(
                        f"step {label!r} has `{idiom}` in its `run:` body, on the "
                        f"line {stripped!r}. That swallows a failure (or narrows a "
                        "selection) inside the step, so the step exits 0 and the "
                        "gate is green having asserted nothing. If this line "
                        "genuinely cannot weaken the gate — a `grep -c` counter, "
                        "say — add it to _SANCTIONED_RUN_IDIOM_LINES verbatim."
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


@pytest.mark.parametrize(
    ("workflow", "job_id"), _JOBS_HELD_TO_THE_MATRIX_RULE, ids=lambda v: str(v)
)
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

    Scope is ``_JOBS_HELD_TO_THE_MATRIX_RULE`` — ``GATING_JOBS`` minus the jobs
    recorded in ``_MATRIXED_ON_PURPOSE``. ``ci.yml:shell-tests`` is the one
    entry there: it is matrixed on purpose (bash 3.2 vs 5.x is the defect class
    it exists to catch) and its two expansions are the strings branch protection
    already holds, so this rule would go red on the real repo on day one. It is
    still held to every OTHER rule in this file, which is #448's point — a job
    dropped from ``GATING_JOBS`` to dodge this rule would have dodged the
    defusing rule with it.
    ``GATING_JOBS`` holds five jobs since #325 added ``frontend.yml:visual-e2e``;
    of the four left after the exemption, none is matrixed today. A future gating
    job that genuinely needs a matrix has to record its expanded names in
    ``_REQUIRED_CONTEXT_WORKFLOWS`` and be argued for here.


    Turns red if: any job in ``_JOBS_HELD_TO_THE_MATRIX_RULE`` gains a
    ``strategy:`` key.
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


def test_every_sanctioned_run_idiom_line_is_still_in_the_yaml() -> None:
    """A stale exemption is a licence nothing uses — and a widening one.

    ``_SANCTIONED_RUN_IDIOM_LINES`` names exact shell lines. If one is edited or
    deleted, the entry stops matching and becomes dead weight that a future
    reviewer reads as "these idioms are fine here". Resolve every entry against
    the real ``run:`` body instead.

    Turns red if: an entry names a step that no longer exists, or a line the
    step's ``run:`` body no longer contains verbatim; or the register is emptied
    (the rule above would then reject the three legitimate ``grep -c`` counters
    and go red on the real repo).
    """
    assert _SANCTIONED_RUN_IDIOM_LINES, (
        "the sanctioned-run-idiom register was emptied; the three `grep -c || "
        "true` counters in the two selection guards are legitimate and the rule "
        "above would go red on the real repo"
    )
    unresolved: list[str] = []
    for (workflow, job_id, step_name), lines in sorted(_SANCTIONED_RUN_IDIOM_LINES.items()):
        job = (_load_workflow(workflow).get("jobs") or {}).get(job_id) or {}
        found = [s for s in _steps(job) if str(s.get("name") or "") == step_name]
        if len(found) != 1:
            unresolved.append(f"{workflow}:{job_id} has {len(found)} step(s) named {step_name!r}")
            continue
        body = {ln.strip() for ln in str(found[0].get("run") or "").split("\n")}
        for line in lines:
            if line not in body:
                unresolved.append(f"{workflow}:{job_id}:{step_name} no longer contains {line!r}")
    assert not unresolved, "sanctioned run-idiom lines no longer match the YAML:\n  " + "\n  ".join(
        unresolved
    )


def test_the_jobs_left_out_of_the_defusing_rule_are_recorded_with_a_reason() -> None:
    """An unexplained omission looks identical to one this guard forgot.

    Turns red if: ``_NOT_HELD_TO_THE_DEFUSING_RULE`` is emptied, a job is listed
    both as guarded and as exempt, or an exemption names a job no workflow
    declares.
    """
    assert _NOT_HELD_TO_THE_DEFUSING_RULE, (
        "the register of gating jobs outside the defusing rule must not be silently emptied"
    )
    guarded = {f"{workflow}:{job_id}" for workflow, job_id in GATING_JOBS}
    overlap = guarded & set(_NOT_HELD_TO_THE_DEFUSING_RULE)
    assert not overlap, f"job(s) recorded as both guarded and exempt: {sorted(overlap)}"

    # Every exemption must RESOLVE. The completeness test below looks required
    # contexts up in this dict by `"<workflow>:<job id>"`, so a key that names no
    # real job silently fails to cover the context it was written for — which is
    # how `"codeql.yml:analyze / pr-quality.yml:quality-gate"`, one key naming two
    # jobs, covered neither.
    unresolved: list[str] = []
    for key in sorted(_NOT_HELD_TO_THE_DEFUSING_RULE):
        workflow, _, job_id = key.partition(":")
        if not job_id or job_id not in (_load_workflow(workflow).get("jobs") or {}):
            unresolved.append(key)
    assert not unresolved, (
        "exemption key(s) do not name a `<workflow file>:<job id>` pair this repo "
        f"declares: {unresolved}. The completeness test looks contexts up by that "
        "exact key, so an unresolvable one exempts nothing."
    )


def test_the_matrix_exemption_register_names_real_gating_jobs() -> None:
    """``_MATRIXED_ON_PURPOSE`` narrows a rule, so it must not rot into a licence.

    Two directions. A key naming a job that is NOT in ``GATING_JOBS`` narrows
    nothing and hides that the job escaped the defusing rule as well; a key
    naming a job that is no longer matrixed silently exempts a compliant job
    from a rule it would now pass.

    Turns red if: an entry names a job outside ``GATING_JOBS``, or one that no
    longer declares ``strategy:``; or the rule's own population shrinks to fewer
    than two jobs.
    """
    assert _MATRIXED_ON_PURPOSE, (
        "the matrix-exemption register was emptied; `ci.yml:shell-tests` is "
        "genuinely matrixed and the no-matrix rule would go red on the real repo"
    )
    guarded = {f"{workflow}:{job_id}" for workflow, job_id in GATING_JOBS}
    stray = sorted(set(_MATRIXED_ON_PURPOSE) - guarded)
    assert not stray, (
        f"matrix-exemption key(s) {stray} are not in GATING_JOBS. An exemption "
        "for a job that is not guarded at all narrows nothing and hides that the "
        "job is outside the DEFUSING rule too — #448's actual defect."
    )
    not_matrixed: list[str] = []
    for key in sorted(_MATRIXED_ON_PURPOSE):
        workflow, _, job_id = key.partition(":")
        job = (_load_workflow(workflow).get("jobs") or {}).get(job_id) or {}
        if "strategy" not in job:
            not_matrixed.append(key)
    assert not not_matrixed, (
        f"{not_matrixed} no longer declare `strategy:`, so exempting them from "
        "the no-matrix rule exempts a job that would now pass it. Drop the "
        "exemption."
    )
    # Partner: the rule above is `@pytest.mark.parametrize`d over this tuple, and
    # an empty (or single-item) population makes it near-vacuous. Its FIRST entry
    # is `ci.yml:test`, which has never been matrixed — a compliant control.
    assert len(_JOBS_HELD_TO_THE_MATRIX_RULE) >= 2, (
        f"_JOBS_HELD_TO_THE_MATRIX_RULE has {len(_JOBS_HELD_TO_THE_MATRIX_RULE)} "
        "entries; the no-matrix rule is close to vacuous"
    )
    assert ("ci.yml", "test") in _JOBS_HELD_TO_THE_MATRIX_RULE, (
        "`ci.yml:test` dropped out of the no-matrix population. It is the "
        "control: a required, never-matrixed job, so the rule is exercised "
        "against a known-good case and not only against candidates."
    )


def _job_ids_for_context(context: str, workflow: str) -> list[str]:
    """Every job id in ``workflow`` whose declared name is a prefix of ``context``.

    Matrix jobs expand their ``name:`` per combination, so a required context is
    the declared name PLUS a suffix — the same prefix match
    ``test_the_recorded_required_context_still_names_a_real_job`` uses. A name
    carrying an expression (``Analyze (${{ matrix.language }})``) is truncated at
    the ``${{``, because it cannot be resolved statically. A job with no
    ``name:`` reports its job id, which is how ``quality-gate`` resolves.
    """
    jobs = _load_workflow(workflow).get("jobs") or {}
    found: list[str] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        declared = str(job.get("name") or job_id)
        prefix = declared.split("${{")[0].strip()
        if prefix and context.startswith(prefix):
            found.append(job_id)
    return found


def test_every_required_context_is_in_one_of_the_two_registers() -> None:
    """#448: absent from BOTH registers is how a required gate goes unguarded.

    ``GATING_JOBS`` says "held to the defusing rule".
    ``_NOT_HELD_TO_THE_DEFUSING_RULE`` says "deliberately not, and here is why".
    A job in NEITHER is indistinguishable from one this guard forgot — and that
    was not hypothetical. ``ci.yml:shell-tests`` produces TWO required contexts
    and is the only job that executes ``tests/shell/*``; it appeared in neither
    register, so ``continue-on-error: true`` on its one assertion step would have
    turned both contexts advisory with nothing in this file, the backend suite or
    ruff going red.

    Turns red if: a required context in ``_REQUIRED_CONTEXT_WORKFLOWS`` resolves
    to a job listed in neither register — e.g. delete ``("ci.yml",
    "shell-tests")`` from ``GATING_JOBS`` without adding it to the exemption
    register — or if a recorded context resolves to no job at all.
    """
    # Partner: everything below iterates `_REQUIRED_CONTEXT_WORKFLOWS`, so a
    # SHRUNK mapping quietly shrinks this rule's reach. A floor of "at least 2"
    # against a real nine would have let six rows be deleted — and with them the
    # coverage of the jobs they name — while every assertion here stayed green.
    # Compared as a baseline, not a floor: see _MEASURED_REQUIRED_CONTEXTS_FROM_A_WORKFLOW.
    assert len(_REQUIRED_CONTEXT_WORKFLOWS) >= _MEASURED_REQUIRED_CONTEXTS_FROM_A_WORKFLOW, (
        f"_REQUIRED_CONTEXT_WORKFLOWS holds {len(_REQUIRED_CONTEXT_WORKFLOWS)} "
        f"entries, below the {_MEASURED_REQUIRED_CONTEXTS_FROM_A_WORKFLOW} measured "
        "on 2026-09-24. Removing a row removes this rule's coverage of that "
        "context. If the owner genuinely demoted one, re-derive the live list and "
        "update the measured number in the same change."
    )
    # Same reasoning on the other side: moving jobs OUT of GATING_JOBS (into the
    # exemption register, or nowhere) is how the defusing rule stops reaching
    # them, and nothing else here caps that.
    assert len(GATING_JOBS) >= _MEASURED_GATING_JOBS, (
        f"GATING_JOBS holds {len(GATING_JOBS)} entries, below the "
        f"{_MEASURED_GATING_JOBS} measured on 2026-09-24. Each removal takes a "
        "gating job out of the defusing rule. If a job was genuinely retired, "
        "update the measured number in the same change."
    )

    guarded = {f"{workflow}:{job_id}" for workflow, job_id in GATING_JOBS}
    exempt = set(_NOT_HELD_TO_THE_DEFUSING_RULE)

    unresolved: list[str] = []
    unregistered: list[str] = []
    hit_guarded: list[str] = []
    hit_exempt: list[str] = []
    for context, workflow in sorted(_REQUIRED_CONTEXT_WORKFLOWS.items()):
        job_ids = _job_ids_for_context(context, workflow)
        if not job_ids:
            unresolved.append(f"{context!r} is recorded as coming from {workflow}")
            continue
        for job_id in job_ids:
            key = f"{workflow}:{job_id}"
            if key in guarded:
                hit_guarded.append(key)
            elif key in exempt:
                hit_exempt.append(key)
            else:
                unregistered.append(f"{key} (required context {context!r})")

    # Resolution is the other half of the partner: if `_job_ids_for_context`
    # returned nothing for everything, `unregistered` would be empty and this
    # rule would pass having checked nothing.
    assert not unresolved, (
        "required context(s) resolve to no job at all, so this rule cannot say "
        f"whether they are registered: {unresolved}. Either a job was renamed — "
        "which wedges every PR, because branch protection still waits for the "
        "old context — or _REQUIRED_CONTEXT_WORKFLOWS is stale."
    )
    # Both registers must actually be consulted, or one of the two branches above
    # is dead and a whole class of omission goes unseen.
    assert hit_guarded, "no required context resolved to a GATING_JOBS entry"
    assert hit_exempt, "no required context resolved to an exemption entry"

    assert not unregistered, (
        "required status context(s) resolve to a job in NEITHER register:\n  "
        + "\n  ".join(unregistered)
        + "\nAdd the job to GATING_JOBS (so the defusing rule covers it), or to "
        "_NOT_HELD_TO_THE_DEFUSING_RULE with the reason it cannot be. Being in "
        "neither means a one-line `continue-on-error: true` turns a required "
        "gate advisory and nothing here notices."
    )


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


# English number words the §12.1 sentence may use. Digits are accepted too. The
# range is deliberately short: a repo with more than twenty required contexts has
# a different problem than this rule is for.
_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
}


def _strategy_section(heading: str) -> str:
    """The text of one ``### x.y`` section of docs/TEST_STRATEGY.md."""
    doc = TEST_STRATEGY.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(heading)}[^\n]*\n(?P<body>.*?)(?=^### |\Z)", doc, re.M | re.S)
    assert match, f"docs/TEST_STRATEGY.md has no `{heading}` section"
    return match.group("body")


def test_the_strategy_doc_lists_exactly_the_recorded_contexts() -> None:
    """One authoritative record, mechanically compared — not two hand copies.

    The required-context list existed in THREE places by 2026-09-16 and two of
    them were wrong: this file's ``_REQUIRED_CONTEXT_WORKFLOWS`` said eight
    workflow contexts, ``docs/TEST_STRATEGY.md`` §12.1 said "Nine contexts are
    required" over a nine-row table, README said the live job was advisory —
    while ``gh api`` returned TEN and ``.github/dependabot.yml`` said 10 in its
    auto-merge note. ``Live-mode Playwright (stub backend)`` had been promoted
    (which is #379 succeeding) and every hand copy outlived the change.

    A doc cannot be asserted against GitHub from a test — that needs a token and
    a network call. What it CAN be asserted against is the record in this file,
    so there is one place to update when branch protection moves and one rule
    that reds if only some of them get updated.

    Turns red if: a row is added to or removed from §12.1's table without the
    same change here, or the count in the sentence above the table stops
    matching the table.
    """
    body = _strategy_section("### 12.1")
    # Table rows look like `| \`Context\` | produced by |`. Take the first cell,
    # backticks stripped, skipping the header and the `|---|---|` separator.
    rows = [
        match.group("context").strip()
        for match in re.finditer(r"^\|\s*`(?P<context>[^`]+)`\s*\|", body, re.M)
    ]
    recorded = set(_REQUIRED_CONTEXT_WORKFLOWS) | set(_REQUIRED_CONTEXTS_NOT_FROM_A_WORKFLOW)
    # PARTNER. Both comparisons below are satisfied by an empty table if the row
    # regex stops matching, and by an empty record if both dicts are emptied.
    assert len(rows) >= 2, (
        f"§12.1's context table parsed to {len(rows)} row(s) — the row regex has "
        "stopped matching the table's markdown, so the comparison below would "
        "pass over nothing"
    )
    assert len(recorded) >= 2, "the recorded required-context set is empty or near-empty"

    assert set(rows) == recorded, (
        "docs/TEST_STRATEGY.md §12.1's table and this file's recorded required "
        "contexts disagree.\n"
        f"  in the doc only:    {sorted(set(rows) - recorded)}\n"
        f"  in this file only:  {sorted(recorded - set(rows))}\n"
        "Branch protection is the truth; re-read it with `gh api "
        "repos/imrohitagrawal/citevyn/branches/main/protection --jq "
        "'.required_status_checks.contexts[]'` and update BOTH."
    )
    assert len(rows) == len(set(rows)), f"§12.1's table lists a context twice: {rows}"

    # ...and the prose count above the table, which is the part a reader
    # actually quotes. It said "Nine" over a nine-row table while ten were
    # required, so the table and the sentence were consistent with each other
    # and wrong together — only the comparison above catches that. This line
    # catches the cheaper half: a row added without touching the sentence.
    stated = re.search(r"^\*\*(?P<count>\w+)\*\* contexts are required", body, re.M)
    assert stated, (
        "§12.1 no longer opens with `**<count>** contexts are required`. That "
        "sentence is what a reader quotes; without it this rule checks only the "
        "table."
    )
    word = stated.group("count").lower()
    value = int(word) if word.isdigit() else _NUMBER_WORDS.get(word)
    assert value is not None, f"cannot read a number out of §12.1's {word!r}"
    assert value == len(recorded), (
        f"§12.1 says {stated.group('count')!r} contexts are required, but "
        f"{len(recorded)} are recorded. The sentence and the table have to move "
        "together."
    )


README = REPO_ROOT / "README.md"


def test_the_readme_gate_table_does_not_call_a_required_check_advisory() -> None:
    """The third drifted copy, and the only one that was still unguarded.

    README's gate table is the most-read description of what blocks a merge, and
    on 2026-09-16 its live-mode row said "advisory, not a required check" about a
    context that branch protection had required for some time. §12.1 is held to
    the record by ``test_the_strategy_doc_lists_exactly_the_recorded_contexts``;
    this is the cheap equivalent for README.

    It is deliberately NARROW. README's table is free prose with one row per
    gate, so asserting its full contents against the record would fight the
    document rather than guard it. What it catches is the one drift that
    happened: a row naming a REQUIRED job while describing it as advisory.

    The backticked job names are removed from the cell before the words are
    looked for, so `Visual snapshots (Linux baselines, advisory)` — a job whose
    NAME contains the word — cannot trip it, today or if it is ever promoted.

    Turns red if: a README row names a context in _REQUIRED_CONTEXT_WORKFLOWS and
    calls it advisory or not required.
    """
    lines = [
        line for line in README.read_text(encoding="utf-8").splitlines() if line.startswith("|")
    ]
    offenders: list[str] = []
    checked = 0
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        cell = cells[-1]
        named = [n for n in re.findall(r"`([^`]+)`", cell) if n in _REQUIRED_CONTEXT_WORKFLOWS]
        if not named:
            continue
        checked += 1
        # Strip every backticked span, so a JOB NAME containing "advisory" is
        # not mistaken for a claim about that job.
        prose = re.sub(r"`[^`]*`", " ", cell).lower()
        if "advisory" in prose or "not a required check" in prose:
            offenders.append(f"{named} -> {cell}")
    # PARTNER. The loop is "nothing is wrong" over whatever it happens to find;
    # a table that stops parsing, or job names that stop matching the record,
    # would satisfy it for free.
    assert checked >= 2, (
        f"README's gate table yielded {checked} row(s) naming a recorded "
        "required context — the table or the backtick convention has changed, so "
        "the rule below is checking almost nothing"
    )
    assert not offenders, (
        "README's gate table calls a REQUIRED status check advisory:\n  "
        + "\n  ".join(offenders)
        + "\nBranch protection is the truth; docs/TEST_STRATEGY.md §12.1 is the "
        "list, and backend/tests/test_gating_workflows.py is the record."
    )


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
        # #403's contrast-FLOOR sweep. `faint-contrast.spec.ts` below is #396's
        # token BAN — different questions over the same rendered page, and both
        # are scanned here.
        "contrast-floor.spec.ts",
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
    159 ``test("…")`` declarations in 9 spec files, which Playwright expanded to
    202 selected tests; re-measured 2026-09-16 the default config selects **252**
    (`visual.spec.ts` builds 20 of its 22 from one templated declaration, which
    this scan deliberately does not read). Both readings are of the SELECTION,
    which this floor is not about — it counts DECLARATIONS — but leaving 202
    standing beside the 252 that #325 asserts in two other files would be one
    number with two values. The earlier
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


# ──────────── the three CI assertions, EXECUTED rather than grepped ────────────
#
# Everything above reads the workflow YAML as text. A guard asserted by grep is
# a guard nobody has ever run: the string can be present and the logic wrong, or
# the logic right and the string moved. So these lift the `run:` bodies out of
# the YAML verbatim and EXECUTE them against synthetic Playwright reports, once
# per failure mode they exist to catch. Three jobs have one: `live-e2e`,
# `demo-e2e`, and `visual-e2e` (#325, further down with the rest of that job's
# rules).
#
# The one substitution is the report path: the bodies hardcode
# /tmp/live-results.json, /tmp/demo-results.json and /tmp/visual-results.json,
# which the real jobs write and a test must not. The substitution is asserted to
# have actually applied, so a renamed path fails loudly instead of testing an
# unmodified script.


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


# ─────────── the three SELECTION guards, EXECUTED rather than grepped ───────────
#
# These step bodies shell out to `npx playwright test --list`, which a unit test
# must not run (it is a browser toolchain and it takes ~20 s). So the `npx`
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


# ──────────────────── the visual-snapshot job (#325) ────────────────────
#
# `visual-e2e` is the OTHER half of the demo job's `ci_visual -eq 0` line. That
# line used to mean "the visual specs run in no CI job at all"; it now means "not
# in the REQUIRED job", and the difference is only real if something asserts the
# advisory job runs exactly the tests the required one drops. These rules are
# that something.
#
# The job is ADVISORY. Being guarded here does not promote it: branch protection
# is the only thing that makes a check required, and changing it is the owner's
# call. (An earlier version of this note offered `live-e2e` as the precedent for
# "in GATING_JOBS while advisory". That was false — live-e2e was already a
# required context by then, measured 2026-09-16. `visual-e2e` is the only
# advisory member of that tuple.)

VISUAL_PW_CONFIG = FRONTEND / "playwright.visual-ci.config.ts"
FRONTEND_PACKAGE_LOCK = FRONTEND / "package-lock.json"


def _visual_job() -> dict[str, Any]:
    jobs = _load_workflow(DEMO_WORKFLOW.name).get("jobs") or {}
    assert "visual-e2e" in jobs, (
        f"{DEMO_WORKFLOW.name} no longer defines a `visual-e2e` job. The 22 "
        "screenshot comparisons in frontend/tests/visual.spec.ts would then run "
        "in NO job again — which is #325 — while the demo job's `ci_visual -eq 0` "
        "line keeps passing, because it asserts their ABSENCE."
    )
    return jobs["visual-e2e"]


#: The lockfile entries that decide which browser this job runs. ``npx playwright``
#: resolves through ``@playwright/test`` -> ``playwright`` -> ``playwright-core``,
#: and it is ``playwright-core`` that owns ``browsers.json`` — the revision-to-
#: version table the container image is built from. npm pins transitives exactly,
#: so all three move together in practice; asserting it is what makes "the image
#: tag equals the installed Playwright" a statement about the BROWSER rather than
#: about a package that merely re-exports one.
_PLAYWRIGHT_LOCK_ENTRIES: tuple[str, ...] = (
    "node_modules/@playwright/test",
    "node_modules/playwright",
    "node_modules/playwright-core",
)


def _installed_playwright_version() -> str:
    """The ``playwright-core`` version ``npm ci`` actually installs.

    Read from ``package-lock.json``, not from ``package.json``. The manifest
    declares a RANGE (``^1.63.0``); the lockfile names the one version ``npm ci``
    installs. The two can diverge without anyone editing the manifest — ``npm
    update`` moves the lock to 1.64.0 and ``^1.63.0`` still accepts it, so a
    guard reading the manifest would compare the container tag against a floor
    nobody bumped while the browser underneath had already changed.
    """
    packages = json.loads(FRONTEND_PACKAGE_LOCK.read_text(encoding="utf-8")).get("packages") or {}
    resolved: dict[str, Any] = {}
    for name in _PLAYWRIGHT_LOCK_ENTRIES:
        version = (packages.get(name) or {}).get("version")
        assert isinstance(version, str) and version, (
            f"frontend/package-lock.json has no resolved version for {name}, so "
            "the pin below would compare the container image against nothing"
        )
        resolved[name] = version
    # `playwright-core` is the one that actually ships `browsers.json`. If the
    # three ever disagreed, `@playwright/test`'s number would name a package that
    # merely re-exports a browser chosen by a different version.
    assert len(set(resolved.values())) == 1, (
        "the three Playwright packages in frontend/package-lock.json resolve to "
        f"different versions: {resolved}. `playwright-core` owns browsers.json, "
        "so the container tag has to follow THAT one — decide which is right and "
        "regenerate the baselines in the matching image."
    )
    return resolved["node_modules/playwright-core"]


def test_the_visual_job_runs_the_browser_that_drew_its_baselines() -> None:
    """A screenshot suite whose browser floats is a scheduled false red.

    ``tests/visual.spec.ts-snapshots/*-chromium-linux.png`` are pixels drawn by
    one exact browser. Playwright pins it per release — 1.63.0 ships revision
    1243, ``Google Chrome for Testing 153.0.8010.12``, read out of the image
    itself while generating the baselines (from ``chromium_headless_shell-1243``,
    which is what a headless run launches; the headful ``chromium-1243`` reports
    the same version) — so the container tag and the installed Playwright are two
    records of the same decision, in two files, with nothing else comparing them.

    This rule does not depend on the baselines existing — it compares a tag
    against a lockfile — so it held before they were committed and holds now.

    Dependabot bumps ``@playwright/test`` on its own schedule (which drags
    ``playwright-core`` with it) and has no idea this tag exists. Left unpinned,
    the next minor bump silently swaps the renderer under 22 committed baselines
    and the whole job reads as "the design regressed".

    THE COST OF THIS RULE, stated because it lands on a REQUIRED check (#325,
    item 9). This test runs in ``pytest + lint``. So the next routine
    ``frontend-test`` dependabot PR turns a required check RED, and the fix is
    not a one-line tag edit: it needs an amd64 regeneration of all 22 baselines
    plus the owner looking at the images. The failure message below is written to
    say that, because the dependabot PR is where a maintainer meets it. The
    alternative — leaving the pin advisory — is worse: the baselines would
    silently stop matching the browser, which is the defect #325 exists to close.

    Turns red if: ``frontend/package-lock.json``'s ``playwright-core`` version
    and ``frontend.yml``'s ``visual-e2e`` container tag stop matching, or the
    three Playwright packages in the lockfile stop agreeing with each other. The fix is
    to regenerate the baselines in the NEW image and move both together.
    """
    container = _visual_job().get("container")
    image = container.get("image") if isinstance(container, dict) else container
    assert isinstance(image, str) and image, (
        f"{DEMO_WORKFLOW.name}:visual-e2e declares no `container:` image (got "
        f"{container!r}). Without it the job installs whatever browser "
        "`playwright install` fetches on the day, and the committed -linux "
        "baselines stop being reproducible."
    )
    # The digest form is ACCEPTED (#325, item 8). `...-noble@sha256:<digest>` is
    # strictly stronger than the tag: it is immutable, and the tag is not —
    # Microsoft republishes these tags when the base image is patched, and the
    # base's font packages are a render input for a screenshot suite. Refusing
    # the digest would have put this required test between the repo and the best
    # available pin, which is not a guard's job. The version is still read from
    # the tag part, so the lockstep comparison works either way.
    match = re.fullmatch(
        r"mcr\.microsoft\.com/playwright:v(?P<version>[0-9.]+)-\w+"
        r"(?:@sha256:[0-9a-f]{64})?",
        image.strip(),
    )
    assert match, (
        f"{DEMO_WORKFLOW.name}:visual-e2e runs in {image!r}, which is not an "
        "`mcr.microsoft.com/playwright:v<version>-<distro>` tag. The pin below "
        "cannot read a version out of it, and a floating or `latest` tag is "
        "exactly what this rule exists to refuse."
    )
    installed = _installed_playwright_version()
    assert match.group("version") == installed, (
        f"{DEMO_WORKFLOW.name}:visual-e2e runs in Playwright image "
        f"v{match.group('version')} while frontend/package-lock.json installs "
        f"playwright-core {installed}. The image's bundled Chromium is the one "
        "that drew tests/visual.spec.ts-snapshots/*-chromium-linux.png; a "
        "mismatch means the job compares those pixels against a different "
        "renderer, and every diff it reports is about the version skew rather "
        "than about this repo.\n"
        "\n"
        "IF YOU GOT HERE FROM A DEPENDABOT PR, this is not a one-line fix and "
        "bumping the tag alone is the wrong move — it would leave 22 baselines "
        "drawn by the old browser. The sequence is:\n"
        "  1. edit the `image:` tag in .github/workflows/frontend.yml's "
        "`visual-e2e` job to match the new version;\n"
        "  2. regenerate every baseline in the NEW image, on amd64 — the command "
        "is in the header of frontend/playwright.visual-ci.config.ts, and "
        "`--platform linux/amd64` is not optional (arm64 renders differ for 12 "
        "of the 22);\n"
        "  3. have the owner look at the regenerated images before they are "
        "committed, the same gate #325 used for the first set;\n"
        "  4. commit the tag and the PNGs together.\n"
        "If that is more than the bump is worth today, close the dependabot PR "
        "rather than editing this test."
    )


def test_the_regeneration_command_and_the_ci_container_agree() -> None:
    """A baseline drawn in one container and compared in another is two environments.

    ``playwright.visual-ci.config.ts`` documents the ``docker run`` that produces
    the ``*-chromium-linux.png`` files; ``frontend.yml``'s ``visual-e2e`` declares
    the container that compares them. They are the same decision written twice,
    and nothing else compares the two — which is how the job shipped for one
    review round with ``--ipc=host`` in the documented command and not in the
    job (found by review, 2026-09-16; the mutant that removed it from the YAML
    survived the whole suite).

    ``--ipc=host`` is the one runtime flag that matters here: Playwright
    documents it for Chromium because the default 64 MB ``/dev/shm`` can kill a
    tab mid-screenshot. It is not believed to move a pixel — the driver already
    passes ``--disable-dev-shm-usage`` — which is precisely why a divergence
    would never announce itself, and why it is asserted rather than trusted.

    WHAT IT HOLDS, precisely. ``--ipc=host`` must be PRESENT in both places.
    That single rule subsumes the agreement one it replaced: two things that must
    both be present cannot diverge, so the case that actually happened (the flag
    in the documented command, absent from the job) fails on the job's side, and
    the case an agreement-only check let through (absent from both) fails on
    both. The name still says "agree" because the file-level agreement — the
    documented command must name the SAME IMAGE the job runs — is asserted
    separately, by the ``len(candidates) == 1`` line.

    Turns red if: ``options: --ipc=host`` leaves the job, or the documented
    regeneration command stops passing it, or both do.
    """
    container = _visual_job().get("container")
    image = str((container or {}).get("image") or "") if isinstance(container, dict) else ""
    options = str((container or {}).get("options") or "") if isinstance(container, dict) else ""
    assert image, (
        f"{DEMO_WORKFLOW.name}:visual-e2e declares no container image, so there "
        "is nothing for the documented command to agree with"
    )

    # The COMMAND, not the whole file, and identified by the IMAGE it runs.
    #
    # Searching the file would let `--ipc=host` be deleted from the `docker run`
    # while a sentence elsewhere in the docblock still mentioned it: the string
    # present, the command not passing it, this rule reporting agreement.
    # Matching on `"docker run" in line` alone is not enough either — that was
    # the first version of this and it was WRONG, caught by running it: the
    # docblock also contains the prose "Apple-silicon machine needs `docker run
    # --platform linux/amd64`", which sorts first and carries no flags, so the
    # rule compared the CI container against a sentence.
    #
    # Requiring the image makes the selection unambiguous AND adds a third
    # two-place agreement for free: the command a human runs to DRAW a baseline
    # must name the same image the job that COMPARES it runs.
    source = VISUAL_PW_CONFIG.read_text(encoding="utf-8")
    joined = re.sub(r"\\\n\s*\*?\s*", " ", source)  # un-wrap the backslash continuations
    candidates = [line for line in joined.splitlines() if "docker run" in line and image in line]
    # PARTNER. Everything below is a substring check; over zero candidates the
    # comparison would agree about nothing, and over two it would silently pick
    # one. Exactly one is also the assertion that the documented command and the
    # CI job name the SAME image.
    assert len(candidates) == 1, (
        f"expected exactly one documented `docker run ... {image} ...` command in "
        f"{VISUAL_PW_CONFIG.name}, found {len(candidates)}. Either the "
        "regeneration command is gone — it is the only record of how the -linux "
        "baselines are produced — or it names a different image from the one "
        "visual-e2e runs, which means baselines drawn by one browser and "
        "compared by another."
    )
    command = candidates[0]
    assert "--platform linux/amd64" in command, (
        f"the documented regeneration command in {VISUAL_PW_CONFIG.name} no "
        "longer passes `--platform linux/amd64`. GitHub's ubuntu-latest is "
        "x86_64 and arm64 renders differ for 12 of the 22 snapshots, so without "
        f"it an Apple-silicon machine commits 12 wrong images:\n  {command}"
    )
    in_command = "--ipc=host" in command
    in_job = "--ipc=host" in options
    # PRESENCE, not merely agreement (#325, item 4). `in_command == in_job` alone
    # is satisfied by the flag being absent from BOTH, and nothing else in this
    # repo asserts it exists at all — so the pair could quietly lose Playwright's
    # documented Chromium setting with every test green. Asserting presence makes
    # dropping it a decision someone has to argue for here, which is the right
    # bar for a setting that only shows up as a crashed tab mid-screenshot.
    assert in_command and in_job, (
        f"`--ipc=host` must be present in BOTH the documented regeneration "
        f"command (found: {in_command}) and {DEMO_WORKFLOW.name}:visual-e2e's "
        f"`container.options` (found: {in_job}, value {options!r}). Playwright "
        "documents it for Chromium because the default 64 MB /dev/shm can kill a "
        "tab mid-screenshot. If dropping it is genuinely wanted, change this "
        "assertion deliberately and say why — do not let it lapse from one side."
    )
    # The `in_command == in_job` assertion that used to stand here is DELETED
    # (#325, item 7). Once presence is asserted on both sides, equality can never
    # fire: True == True is the only reachable state. Keeping it would have been
    # a line no edit anywhere could turn red, which this repo deletes on sight —
    # the same reason `test_the_pinned_required_contexts_are_json_serialisable_
    # strings` went. Divergence is still caught, and by a STRICTER rule: the
    # assertion above fails on either side losing the flag, naming which one.


def test_the_visual_job_does_not_replace_the_browser_the_image_ships() -> None:
    """Partner to the pin above: the tag is only a pin if nothing overrides it.

    ``npx playwright install`` inside the container downloads a second copy of
    the browser into ``$PLAYWRIGHT_BROWSERS_PATH`` and Playwright then uses THAT.
    The container tag would still read v1.63.0, this file would still be green,
    and the browser drawing the comparison would be whichever one the install
    step fetched. The demo and live jobs legitimately run that step — they have
    no container — so the rule is scoped to this job.

    Turns red if: a ``playwright install`` step is added to ``visual-e2e``.
    """

    def install_steps(job: dict[str, Any]) -> list[str]:
        return [
            str(step.get("name") or step.get("run") or "")
            for step in _steps(job)
            if "playwright install" in str(step.get("run") or "")
        ]

    # THE PARTNER. `assert not offenders` is satisfied by a job with no steps at
    # all, and by a detector that has stopped matching anything. `demo-e2e`
    # legitimately runs `npx playwright install --with-deps chromium` — it has no
    # container — so it is the positive control: if this scan cannot see it
    # there, it cannot see it here either and the rule below is decorative.
    demo_job = (_load_workflow(DEMO_WORKFLOW.name).get("jobs") or {}).get("demo-e2e")
    # `.get`, not `[...]`. A bare subscript raises a bare `KeyError: 'demo-e2e'`
    # from inside a test about the VISUAL job, naming the wrong culprit for a
    # rename whose real consequence is elsewhere: that job's `name:` is a
    # required status check.
    assert demo_job is not None, (
        f"{DEMO_WORKFLOW.name} no longer defines a `demo-e2e` job, so this rule "
        "has no positive control to calibrate its detector against."
    )
    # `>= 1`, not `== 1`. The control's job is to prove the detector can SEE a
    # real `playwright install` step. demo-e2e legitimately adding a second one
    # (a browser beyond chromium) is not a defect in the VISUAL job and must not
    # redden a rule about it.
    demo_installs = install_steps(demo_job)
    assert len(demo_installs) >= 1, (
        "the `playwright install` scan found no such step in "
        "frontend.yml:demo-e2e, which runs one. The detector has stopped "
        "matching, so the rule below would pass over nothing."
    )
    offenders = install_steps(_visual_job())
    assert not offenders, (
        f"{DEMO_WORKFLOW.name}:visual-e2e runs `playwright install` ({offenders}). "
        "The container image already carries the browsers, and a second download "
        "silently replaces the pinned one — which defeats "
        "test_the_visual_job_runs_the_browser_that_drew_its_baselines without "
        "changing anything it reads."
    )


def test_the_visual_job_uploads_the_diff_images_not_just_the_report() -> None:
    """The artifact IS the deliverable for this job, and one path carries it.

    When a screenshot comparison fails, the only thing that separates "the design
    moved" from "the renderer moved" is the pair of images Playwright writes to
    ``test-results/``: ``<name>-actual.png`` and ``<name>-diff.png``. The HTML
    report alone is navigation around them. Uploading only ``playwright-report``
    would look complete and leave the reviewer with nothing to compare.

    ``path:`` is repo-root-relative even though the run steps use
    ``working-directory: frontend`` — ``working-directory`` applies to ``run:``
    steps only, never to an action's ``with:`` inputs.
    """
    uploads = [
        step
        for step in _steps(_visual_job())
        if str(step.get("uses") or "").startswith("actions/upload-")
    ]
    assert len(uploads) == 1, (
        f"expected exactly one upload step in visual-e2e, found {len(uploads)}"
    )
    with_block = uploads[0].get("with") or {}
    paths = [line.strip() for line in str(with_block.get("path", "")).splitlines() if line.strip()]
    assert "frontend/test-results" in paths, (
        f"visual-e2e uploads {paths}, which does not include "
        "`frontend/test-results` — the directory holding every -actual.png and "
        "-diff.png. Without it a failed run hands the reviewer a report with no "
        "images to look at."
    )
    assert "frontend/playwright-report" in paths, (
        f"visual-e2e uploads {paths}, which does not include "
        "`frontend/playwright-report`. The run step passes `--reporter=list,json,"
        "html`, and `--reporter` REPLACES the config's reporters, so the HTML "
        "report lands in the base config's `playwright-report` — this path."
    )
    assert with_block.get("if-no-files-found") == "error", (
        "visual-e2e's upload step must set `if-no-files-found: error`. The "
        "default is `warn`, which is exactly how frontend.yml's dead "
        "`frontend-dist` artifact survived unnoticed until #325 removed it."
    )


def test_the_visual_config_selects_by_file_path_not_by_test_title() -> None:
    """A title-based selection here empties the job the day someone renames a describe.

    ``playwright.demo-ci.config.ts`` records the same reasoning for its
    ``testIgnore``. The two are complements — one excludes the file, the other
    matches it — and if either switched to a ``grep`` over titles, a rename would
    move one side of the partition without moving the other.
    """
    source = VISUAL_PW_CONFIG.read_text(encoding="utf-8")
    assert re.search(r"^\s*testMatch:\s*/visual\\\.spec\\\.ts\$/", source, re.M), (
        f"{VISUAL_PW_CONFIG.name} no longer declares "
        "`testMatch: /visual\\.spec\\.ts$/`. That regex is what makes this "
        "config the exact complement of playwright.demo-ci.config.ts's "
        "`testIgnore`; a `grep:` in its place selects by TITLE and empties the "
        "job on a describe-block rename."
    )
    # NOT `"grep:" not in source` (#325, item 5). This repo has a written scar
    # about guards that check strings, and that one had both failure modes at
    # once: `grepInvert: /hero/` narrows the selection just as effectively and
    # contains no `grep:`, while the WORD appears legitimately in this file's own
    # docblock ("the grep form silently stops matching"), so writing `grep:` in a
    # comment would have reddened it for a reason unrelated to selection.
    #
    # So: strip comments and string literals with the same lexer the rest of this
    # file uses, then look for the KEY rather than the word. `grep` and
    # `grepInvert` are the only two Playwright options that narrow a selection by
    # title, so the pair is the whole population.
    stripped = _strip_comments(source)
    # Quoted keys count (#325, item 8): `"grep": /hero/` and `'grep': /hero/` are
    # the same declaration to JavaScript, and a bare-word-only pattern read them
    # as prose. The quote characters are optional and must MATCH, so `"grep':`
    # is not treated as a key.
    narrowing = sorted(
        {
            match.group("key")
            for match in re.finditer(
                r"""(?P<q>["']?)(?P<key>grepInvert|grep)(?P=q)\s*:""", stripped
            )
        }
    )
    assert not narrowing, (
        f"{VISUAL_PW_CONFIG.name} declares {narrowing}, which narrows the "
        "selection by test TITLE on top of the file match. The workflow's "
        "`visual_total -eq all_visual` differential would then go red for a "
        "reason that has nothing to do with the baselines — and a describe-block "
        "rename would silently change which snapshots are compared."
    )
    # PARTNER for the absence check above: prove the lexer did not simply blank
    # the whole file, which would make `narrowing` empty for free. `testMatch`
    # survives stripping because it is real code, not a comment.
    assert "testMatch" in stripped, (
        f"comment-stripping {VISUAL_PW_CONFIG.name} removed its `testMatch` "
        "declaration too, so the check above passed over an empty file"
    )

    # WHAT THIS CANNOT SEE, stated rather than implied: a selection narrowed at
    # RUN time (a `--grep` appended to the workflow's command line), a key built
    # dynamically (`[k]: /hero/`), or one arriving through a spread from another
    # module. The run-time case is caught one layer out — the job's execution
    # assertion accounts for every selected test against `VISUAL_TOTAL`, so a
    # narrowed run fails the full account. The other two are not covered by
    # anything. Quoted keys WERE in this list and are now caught instead.


def _visual_selection_body() -> str:
    return _step_body(DEMO_WORKFLOW, "visual-e2e", "Guard against a vacuous selection")


def _visual_assertion_body() -> str:
    return _step_body(DEMO_WORKFLOW, "visual-e2e", "Assert the suite actually executed")


_VISUAL_HERO = "visual.spec.ts:38:7 › [light] visual regression › hero"
_VISUAL_FOOTER = "visual.spec.ts:38:7 › [dark] visual regression › footer"
_VISUAL_NONVISUAL = "landing.spec.ts:9:3 › Landing › renders the hero"
# A visual test whose TITLE starts with the summary line's own prefix. It has to
# carry "visual regression" as well, or it would trip the `selected_visual`
# comparison for an unrelated reason and prove nothing about the anchor.
_VISUAL_POISONED = "visual.spec.ts:38:7 › [light] visual regression › Total: 7 sources strip"


def _visual_fixtures(visual: str, all_: str) -> list[tuple[str, str, str]]:
    return [
        ("playwright.visual-ci.config.ts", "/tmp/visual_list.txt", visual),
        ("playwright.config.ts", "/tmp/all_list.txt", all_),
    ]


@pytest.mark.parametrize(
    ("label", "visual_listing", "all_listing"),
    [
        (
            "a clean listing",
            _listing([_VISUAL_HERO, _VISUAL_FOOTER], 2, files=1),
            _listing([_VISUAL_HERO, _VISUAL_FOOTER, _VISUAL_NONVISUAL], 3),
        ),
        (
            "a visual test titled with the summary prefix",
            _listing([_VISUAL_POISONED, _VISUAL_FOOTER], 2, files=1),
            _listing([_VISUAL_POISONED, _VISUAL_FOOTER, _VISUAL_NONVISUAL], 3),
        ),
    ],
    ids=["clean", "poisoned-title"],
)
def test_the_visual_selection_guard_accepts_a_healthy_listing(
    label: str, visual_listing: str, all_listing: str, tmp_path: Path
) -> None:
    """Passing partner AND the direct statement of what ``^Total:`` buys.

    Without a passing case, every failure case below is satisfied by a script
    that rejects everything.

    The ``poisoned-title`` case is what makes the anchor load-bearing here, and
    it works differently from the demo job's twin. That one turns the shortfall
    case green through a bash arithmetic syntax error that ``set -euo pipefail``
    does not catch; this guard does no arithmetic, so the anchor's failure shows
    up on the HEALTHY side instead. Measured 2026-09-16 by removing the two ``^``
    characters from ``.github/workflows/frontend.yml``: ``visual_total`` becomes
    the two lines ``7\\n2``, ``test "$visual_total" -gt 0`` dies with "integer
    expression expected", and this case goes red — so an un-anchored guard
    rejects a perfectly healthy selection and the job can never pass.

    ``VISUAL_TOTAL`` is asserted BYTE-EXACT rather than "the step exited 0": a
    multi-line value written into ``$GITHUB_ENV`` corrupts the file for every
    later step in the job.

    Turns red if: ``^Total:`` is un-anchored to ``Total:`` in
    ``.github/workflows/frontend.yml``'s ``visual-e2e`` selection guard.
    """
    result, github_env = _run_selection_guard(
        _visual_selection_body(), _visual_fixtures(visual_listing, all_listing), tmp_path
    )
    assert result.returncode == 0, (
        f"the visual selection guard rejected {label}, which is a healthy "
        f"selection:\n{result.stdout}\n{result.stderr}"
    )
    assert github_env == "VISUAL_TOTAL=2\n", (
        f"the visual selection guard exported {github_env!r} for {label}; "
        "expected exactly 'VISUAL_TOTAL=2\\n'. A multi-line value means the "
        "`Total:` capture matched a test TITLE as well as the summary line, and "
        "it corrupts $GITHUB_ENV for every later step."
    )


@pytest.mark.parametrize(
    ("label", "visual_listing", "all_listing"),
    [
        # Nothing selected at all, while the default config still has visual
        # tests. CORRECTION, reproduced in review 2026-09-16: an earlier version
        # of this comment credited the `visual_total -eq all_visual`
        # differential. It does not get there — `visual_total -gt 0` is the
        # FIRST assertion and this case dies on it, emitting "selects 0 tests —
        # this whole job is vacuous". That is not a duplicate of the case below:
        # with `-gt 0` deleted this one still reddens on `0 -eq 2`, which is
        # exactly why it did NOT expose the `-gt 0` gap and why
        # `visual-spec-deleted` had to be added.
        (
            "a config that selects nothing",
            _listing([], 0, files=0),
            _listing([_VISUAL_HERO, _VISUAL_FOOTER, _VISUAL_NONVISUAL], 3),
        ),
        # THE case only `visual_total -gt 0` can see, and the reason that line is
        # not redundant. `visual.spec.ts` is gone, so BOTH differentials read
        # 0 == 0 and the job reports green having compared nothing. Measured
        # 2026-09-16 by deleting the `-gt 0` line: every other case here stayed
        # red and this one turned green — which is what "defence in depth" looks
        # like right up until it is the only line left standing. (`--list` also
        # exits 1 on an empty selection in a real run; the stub does not, which
        # is what lets this case isolate the one comparison.)
        (
            "visual.spec.ts deleted, so both differentials read 0 == 0",
            _listing([], 0, files=0),
            _listing([_VISUAL_NONVISUAL], 1, files=1),
        ),
        # A visual test the visual config no longer selects. demo-ci IGNORES the
        # same file, so this test now runs in NO job — #325 reopening itself, one
        # test at a time.
        (
            "a visual test dropped from the visual config",
            _listing([_VISUAL_HERO], 1, files=1),
            _listing([_VISUAL_HERO, _VISUAL_FOOTER, _VISUAL_NONVISUAL], 3),
        ),
        # THE case `visual_total -eq all_visual` alone cannot see: one visual
        # test dropped and one non-visual test pulled in. The totals still
        # balance at 2 == 2; only `selected_visual -eq visual_total` catches it.
        (
            "a visual test swapped for a non-visual one",
            _listing([_VISUAL_HERO, _VISUAL_NONVISUAL], 2, files=2),
            _listing([_VISUAL_HERO, _VISUAL_FOOTER, _VISUAL_NONVISUAL], 3),
        ),
        # The same shortfall, under a title carrying the summary prefix. The
        # anchor's own proof lives on the passing side (see the docstring there);
        # this is here so the failure path is exercised through that title shape
        # too rather than only through ordinary ones.
        (
            "a shortfall under a title containing the summary prefix",
            _listing([_VISUAL_POISONED], 1, files=1),
            _listing([_VISUAL_POISONED, _VISUAL_FOOTER, _VISUAL_NONVISUAL], 3),
        ),
    ],
    ids=[
        "empty",
        "visual-spec-deleted",
        "dropped-visual",
        "swapped-for-non-visual",
        "poisoned-shortfall",
    ],
)
def test_the_visual_selection_guard_fires_on_every_way_the_partition_breaks(
    label: str, visual_listing: str, all_listing: str, tmp_path: Path
) -> None:
    result, _ = _run_selection_guard(
        _visual_selection_body(), _visual_fixtures(visual_listing, all_listing), tmp_path
    )
    assert result.returncode != 0, (
        f"the visual selection guard PASSED on {label}. The visual specs would "
        f"then be partly or wholly uncompared while the job reports "
        f"green.\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("label", "stats", "visual_total", "should_pass"),
    [
        # The happy path, and the partner that stops the four failures below
        # from being satisfied by a script that rejects everything.
        ("a clean visual run", _stats(expected=22), 22, True),
        # Nothing in visual.spec.ts self-skips today, so a skip is always new.
        # A mass `test.skip(true, ...)` is invisible to the selection guard,
        # because `--list` COUNTS skipped tests.
        ("the whole suite skipped", _stats(expected=0, skipped=22), 22, False),
        ("a single unexplained skip", _stats(expected=21, skipped=1), 22, False),
        # A missing -chromium-linux.png baseline lands here: Playwright writes
        # the actual and fails the test.
        ("a baseline that no longer matches", _stats(expected=21, unexpected=1), 22, False),
        # `retries: 2` under CI. For a pixel suite a retry-only pass is the
        # signature of a layout that has not settled — #325's own `how-it-works`
        # symptom — so it must not report green.
        ("a green-by-flake", _stats(expected=21, flaky=1), 22, False),
        # Selection said 22, the run accounts for 20. This is what a threshold in
        # place of the full account would let through.
        ("two snapshots that never ran", _stats(expected=20), 22, False),
    ],
)
def test_the_visual_execution_assertion_actually_fires(
    label: str, stats: dict[str, int], visual_total: int, should_pass: bool, tmp_path: Path
) -> None:
    result = _run_assertion(
        _visual_assertion_body(),
        "/tmp/visual-results.json",
        {"stats": stats},
        tmp_path,
        {"VISUAL_TOTAL": str(visual_total)},
    )
    if should_pass:
        assert result.returncode == 0, (
            f"the visual execution assertion rejected {label}, which is a "
            f"healthy run:\n{result.stdout}\n{result.stderr}"
        )
    else:
        assert result.returncode != 0, (
            f"the visual execution assertion PASSED on {label} ({stats}). That "
            f"is a silent-green visual job.\n{result.stdout}\n{result.stderr}"
        )


@pytest.mark.parametrize(
    ("label", "ci_listing", "all_listing", "only_line"),
    [
        # ONLY `ci_visual -eq 0` can see this. A visual test leaked into the
        # REQUIRED job's selection, and the totals still balance: the CI config
        # selects 2, the default selects 3 with 1 visual, and 2 == 3 - 1. The
        # differential is satisfied, `all_visual > 0` is satisfied. Delete the
        # `ci_visual` line and this listing sails through — with the demo job now
        # running baseline-less screenshots on ubuntu, which is the failure
        # `playwright.demo-ci.config.ts` was created to prevent.
        (
            "a visual test leaking into the required job",
            _listing([_DEMO_ORDINARY, _DEMO_VISUAL], 2, files=2),
            _listing([_DEMO_ORDINARY, _DEMO_KEEPS_FOCUS, _DEMO_VISUAL], 3),
            'test "$ci_visual" -eq 0',
        ),
        # ONLY `all_visual -gt 0` can see this. `visual.spec.ts` is gone
        # entirely, so `ci_visual` is 0 (satisfied), `all_visual` is 0, and
        # `ci_total == all_total - 0` is satisfied. Delete the `all_visual` line
        # and deleting the whole spec file becomes a green way to satisfy "zero
        # visual tests in the CI run" — the exclusion made vacuous rather than
        # honoured.
        (
            "visual.spec.ts deleted, making the exclusion vacuous",
            _listing([_DEMO_ORDINARY, _DEMO_KEEPS_FOCUS], 2, files=1),
            _listing([_DEMO_ORDINARY, _DEMO_KEEPS_FOCUS], 2),
            'test "$all_visual" -gt 0',
        ),
    ],
    ids=["visual-leaked-into-ci", "visual-spec-deleted"],
)
def test_each_demo_partition_line_bites_on_its_own(
    label: str, ci_listing: str, all_listing: str, only_line: str, tmp_path: Path
) -> None:
    """The two lines whose MEANING #325 re-purposed, each proved separately.

    `ci_visual -eq 0` used to mean "the visual specs run in no CI job at all".
    It now means "not in the REQUIRED job", with `visual-e2e`'s
    `visual_total -eq all_visual` as its partner. Re-purposing a line is a good
    moment to check it still does anything, and neither it nor `all_visual > 0`
    had an executable proof — the new job's three equivalent lines did, which is
    the wrong way round, since these two are the older and the load-bearing pair.

    Each case is built so that EXACTLY ONE line rejects it: every other
    assertion in the guard is satisfied. Measured 2026-09-16 by deleting each
    line in turn from ``.github/workflows/frontend.yml`` — the matching case
    turned green and the other stayed red, and the whole backend gating suite
    stayed green with either line deleted, which is what made them worth pinning.

    Turns red if: the line named in ``only_line`` is deleted or weakened.
    """
    # The guard must still CONTAIN the line this case is about, or the case is
    # proving something about a script that no longer has it.
    body = _demo_selection_body()
    assert only_line in body, (
        f"the demo selection guard no longer contains `{only_line}`, so this "
        "case is not testing what its id says it is"
    )
    result, _ = _run_selection_guard(body, _demo_fixtures(ci_listing, all_listing), tmp_path)
    assert result.returncode != 0, (
        f"the demo selection guard PASSED on {label}. `{only_line}` is the only "
        f"line that rejects it, so it is now doing nothing.\n"
        f"{result.stdout}\n{result.stderr}"
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
    Chat "`` selects 15 tests, ``--grep "spec.ts"`` selected all 202 when that
    was measured on 2026-09-08 (the suite is 252 as of 2026-09-16 — the point is
    that the grep path includes the file, not the size of the suite). The
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
    ``--grep "spec.ts"`` selected the whole suite — 202 tests when measured on
    2026-09-08, 252 on 2026-09-16; what carries the point is that it matches
    every test, not the number. So a test whose own
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


# `_BASH_ONLY_SYNTAX` is deliberately narrow: each entry is syntax that `dash`
# (which is what `/bin/sh` is on the Ubuntu images these containers use) rejects
# outright, not merely something bash does better. An entry that dash ACCEPTS
# would make the rule below fire on a step that works, which is the false-red
# half of the same mistake.
_BASH_ONLY_SYNTAX: tuple[tuple[str, str], ...] = (
    ("set -euo pipefail", "`-o pipefail` is a bash option; dash exits 2 on it"),
    ("pipefail", "`pipefail` is a bash option; dash has no equivalent"),
    ("=(", "array assignment `arr=(...)`; dash has no arrays"),
    ("${#", "`${#arr[@]}` array length; dash has no arrays"),
    ("[[", "`[[ ... ]]` is a bash keyword; dash has only `[`"),
    ("<(", "process substitution; dash does not implement it"),
)


def test_a_container_job_that_uses_bash_syntax_declares_bash() -> None:
    """A `container:` job defaults to `sh`, not `bash`, and dash rejects bashisms.

    THE DEFECT, measured on a real runner rather than reasoned about. Every job
    in this repository that runs on the host gets ``bash -e {0}`` for its
    ``run:`` steps. A job with a ``container:`` gets ``sh -e {0}`` instead, and
    on these images ``/bin/sh`` is dash. The first push of ``visual-e2e`` died
    at its FIRST step with::

        /__w/_temp/....sh: 1: set: Illegal option -o pipefail
        ##[error]Process completed with exit code 2.

    before a single snapshot was compared, and because the step that failed was
    the browser probe rather than the suite, the job reported red for a reason
    that had nothing to do with the pixels it exists to check. Nothing in the
    repository could have caught it: the YAML is valid, the shell script is
    valid bash, and no local run uses the container's default shell.

    Turns red if: a `container:` job carrying bash-only syntax loses its
    ``shell: bash``, or a new one is added without it.

    Partner: the population is asserted non-empty, and each flagged step is
    named with the syntax that flagged it, so a rule that silently matched
    nothing cannot pass as a clean result.
    """
    workflow_files = sorted(p.name for p in WORKFLOW_DIR.glob("*.yml"))
    assert workflow_files, f"no workflow files found under {WORKFLOW_DIR}"

    container_jobs: list[tuple[str, str, dict[str, Any]]] = []
    for name in workflow_files:
        jobs = (_load_workflow(name).get("jobs") or {}).items()
        container_jobs.extend((name, job_id, job) for job_id, job in jobs if job.get("container"))

    assert container_jobs, (
        "no job in any workflow declares a `container:`. If that is now true, "
        "this rule guards nothing and should be deleted rather than left "
        "passing vacuously; if it is not true, the glob above has drifted."
    )

    offenders: list[str] = []
    checked: list[str] = []
    for workflow, job_id, job in container_jobs:
        job_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell")
        for step in job.get("steps") or []:
            body = step.get("run")
            if not body:
                continue
            step_name = step.get("name", "(unnamed)")
            checked.append(f"{workflow}:{job_id}:{step_name}")
            shell = step.get("shell") or job_shell
            if shell and shell.split()[0] == "bash":
                continue
            for token, why in _BASH_ONLY_SYNTAX:
                if token in body:
                    offenders.append(
                        f"{workflow}:{job_id} step {step_name!r} uses {token!r} "
                        f"({why}) but resolves to shell {shell or 'sh (the container default)'!r}"
                    )
                    break

    assert checked, (
        "every container job was found but none of them has a `run:` step, so "
        "this rule inspected nothing"
    )
    assert not offenders, (
        "a container job runs bash-only syntax under the container default "
        "shell, which is dash. Add `shell: bash` to the job's `defaults.run` "
        "or to the step:\n  " + "\n  ".join(offenders)
    )
