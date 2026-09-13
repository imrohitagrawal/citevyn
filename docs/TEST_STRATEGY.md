# CiteVyn Test Strategy

## 1. Purpose

This document defines the MVP testing strategy for CiteVyn.

The system is not considered ready because the API works. It is ready only when retrieval, citations, no-answer behavior, and guardrails pass measurable gates.

## 2. Testing Principles

1. Tests must protect product quality, not implementation convenience.
2. Tests must not be weakened to make current code pass.
3. Factual answers require citations.
4. Unsupported questions must be refused.
5. Retrieval correctness must be measured separately from answer wording.
6. Exact lookup must be tested separately from semantic retrieval.
7. Follow-up context must not contaminate product areas.

## 3. Test Layers

### 3.1 Unit Tests

Cover:

1. Query normalization.
2. Domain classification.
3. Intent classification.
4. Cache key generation.
5. Exact term extraction.
6. Chunk metadata generation.
7. Citation formatting.
8. No-answer response generation.

### 3.2 Integration Tests

Cover:

1. Ingestion pipeline.
2. Database schema.
3. Vector search.
4. Keyword search.
5. Exact lookup.
6. Cache reads/writes.
7. API endpoints.
8. Admin endpoints.

### 3.3 Retrieval Tests

Each retrieval test should define:

1. Question.
2. Expected product area.
3. Expected document or chunk.
4. Forbidden chunks.
5. Expected retrieval strategy.

### 3.4 Answer Quality Tests

Evaluate:

1. Faithfulness.
2. Citation correctness.
3. Completeness.
4. Refusal correctness.
5. Step-by-step behavior.
6. No unsupported claims.

### 3.5 Security Tests

Cover:

1. Anonymous access blocked.
2. Admin endpoints protected.
3. Rate limiting.
4. Prompt injection attempts.
5. Secret redaction.
6. Source allowlist enforcement.

### 3.6 Deployment Tests

Cover:

1. Health endpoints.
2. Active index availability.
3. Last-known-good fallback.
4. Environment variable validation.
5. Cost-limit behavior.

## 4. MVP Release Gates

| Metric | Required Score |
|---|---:|
| Domain guardrail accuracy | 100% on critical unsupported cases |
| Retrieval hit rate | >=95% |
| Citation correctness | >=95% |
| Answer faithfulness | >=95% |
| No-answer correctness | >=95% |
| Exact lookup accuracy | >=95% |
| Follow-up context correctness | >=90% |
| Cache correctness | >=95% |
| End-to-end golden pass rate | >=95% |
| Backend line coverage | **no increase in uncovered lines** — never a percentage — see below |

### 4.1 Why coverage is the one row that is not a score

Every other row is a *required score*. Coverage deliberately is not, and this is
the one gate we argue AGAINST having in that shape.

Coverage shows which lines **ran**, never which are **checked**. The repo has the
scar: a line in `app/worker/promotion_eval.py` measured **97% covered** and was
executed by the suite, yet deleting it left all 42 promotion tests green. A
percentage would have said nothing about that defect, and a required score invites
tests written to move the number rather than to catch a defect.

What IS enforced, since #321, is a count: the number of lines no test executes
(`lines-valid` - `lines-covered`, read from `artifacts/coverage.xml`) must not
INCREASE against the baseline recorded in `backend/coverage-baseline.json`. Equal
passes; lower passes and says the baseline can be lowered. That is not a target
anyone can chase — you cannot raise it by writing a test that executes code
without checking it, and it does not move when `app/` grows with covered code,
which a percentage does (`lines-valid` drifted 5562 -> 5567 -> 5585 across
successive `main` runs with no change in test quality at all). The comparison is
`backend/scripts/coverage_baseline.py`; it runs inside the already-required
`pytest + lint` check, and `make coverage-gate` runs the same comparison locally.
A missing, malformed or empty report FAILS it rather than comparing nothing.

`make coverage` on its own stays the plain report. Read the figure precisely.
It covers the hermetic suite
(`-m "not postgres"`),
because the `postgres`-marked tests run in their own job without coverage. It is
not a whole-repo number.

One decimal place, deliberately. A bare integer here is ambiguous by
construction: `coverage.py`'s `TOTAL` row rounds this ratio and prints **97%**,
while this document and the README previously said **96%**, its floor — two
honest sources disagreeing about one measurement taken at the same instant, which
reads as staleness and was in fact filed as staleness (#421). The ratio is
quoted so the two can be reconciled rather than ranked. README §12 states it
identically; if you update one, update both.

Use coverage to find code no test *touches*; use mutation
testing to find code no test *defends*. Where the two disagree, mutation wins.

#308 shipped the measurement advisory and stated its own promotion condition so
it could not drift into permanence unexamined: only once a baseline is stable
across three consecutive `main` runs, and then as **no regression against that
baseline** — never an absolute floor. [#321](https://github.com/imrohitagrawal/citevyn/issues/321)
closed that: the condition was over-satisfied (six consecutive successful `main`
runs, bit-identical at `lines-covered=5403 / lines-valid=5585`), and the gate
shipped BLOCKING in exactly that shape.

## 5. Golden Dataset

Minimum 50 questions:

| Category | Count |
|---|---:|
| Codex usage | 10 |
| Claude usage | 8 |
| Claude Code usage | 10 |
| Gemini usage | 8 |
| Exact lookup | 6 |
| Multi-doc how-to | 3 |
| Follow-up questions | 2 |
| Unsupported/out-of-domain | 2 |
| No-answer/weak-evidence | 1 |

## 6. Golden Case Format

```yaml
case_id: golden_001
question: "How do I configure Claude Code permissions?"
expected_domain: "claude_code"
expected_intent: "how_to"
expected_behavior: "answer"
expected_sources:
  - "Claude Code permissions documentation"
required_answer_points:
  - "mentions permission configuration"
  - "does not invent unsupported modes"
forbidden_answer_points:
  - "claims unsupported admin feature"
```

## 7. Test Governance Rule

Tests must not be changed to make current implementation pass.

A test can change only when:

1. Official documentation changed.
2. Product scope changed.
3. Expected behavior was wrongly specified.
4. Test data became stale.
5. Product owner explicitly approves the change.

Every test change must include:

```text
old expectation
new expectation
reason
source evidence
approval note
impact on metrics
```

## 8. Required Negative Tests

1. Unsupported product question.
2. General AI opinion question.
3. Pricing question not in indexed docs.
4. Future roadmap question.
5. Prompt injection inside user query.
6. Prompt injection inside retrieved document.
7. Secret-looking input.
8. Ambiguous product reference.
9. No matching source evidence.
10. Exact flag that does not exist.

## 9. No-Answer Tests

The system must say:

> I could not find credible information in the indexed official documentation to answer this reliably.

when evidence is weak inside supported scope.

## 10. Unsupported Tests

The system must say:

> I can answer questions about Claude, Claude Code, Codex, and Gemini using indexed official documentation. I do not have credible source material in this assistant to answer that.

when the question is out of scope.

## 11. Open Questions

1. Who owns the final golden dataset?
2. Which evaluator model or rules will be used?
3. How will citation correctness be judged?
4. How often will regression tests run?
5. Will evaluation run on every candidate index?

## 12. Which CI Checks Block a Merge (#379)

### 12.1 Required status checks on `main`

Nine contexts are required. Read with
`gh api repos/imrohitagrawal/citevyn/branches/main/protection/required_status_checks`
(read 2026-09-08):

| Context | Produced by |
|---|---|
| `Analyze (python)` | `codeql.yml` |
| `CodeQL` | GitHub's code-scanning app, not a job in this repo |
| `alembic + postgres integration tests` | `ci.yml` |
| `pytest + lint` | `ci.yml` |
| `quality-gate / quality-gate` | `pr-quality.yml` |
| `type-check + unit tests + build` | `frontend.yml` |
| `Demo-mode Playwright (no visual snapshots)` | `frontend.yml` |
| `shell suites (bash 3.2 + 5.x) (ubuntu-latest)` | `ci.yml` |
| `shell suites (bash 3.2 + 5.x) (macos-latest)` | `ci.yml` |

`strict: true` (a branch must be up to date with `main` before merging) and
`enforce_admins: true` (the rules apply to admins too).

### 12.2 What is advisory

`Live-mode Playwright (stub backend)` (`frontend-live-e2e.yml`) is **advisory**:
it is not in the list above, so it can go red and the pull request still merges.

It is the only job that executes these five tests, in two spec files:

| Test | What only this job proves |
|---|---|
| `behavior.spec.ts:534` | the composer refuses a second in-flight question (#62), **and** announces the refusal — "Not sent" / "kept" at lines 629-630, which is #356's *gap 2* |
| `behavior.spec.ts:473` | the pending-bubble loading indicator |
| `behavior.spec.ts:1272` | the #302 landing re-ask scrolls to the existing answer and stays |
| `answer-format.spec.ts:52` | a multi-line answer renders as multiple lines, not one blob |
| `answer-format.spec.ts:150` | the markdown subset, citation chips, and per-document card collapsing (#303) |

**Correction (measured 2026-09-08).** An earlier version of this list credited
the live suite with "the arriving-answer announcement (#356)". It is not
live-only: `behavior.spec.ts:1453` asserts it **unskipped** in the required
`Demo-mode Playwright` job, and `ChatView.test.tsx` covers it in vitest inside
the required `type-check + unit tests + build` job. Counts re-derived 2026-09-09:
`grep -rn "Answer ready" frontend/src` → **20** hits — 5 in `ChatView.tsx` and 15
in `ChatView.test.tsx` — and `grep -rn "Answer ready" frontend/tests` → 4. (This
paragraph first said "15 hits under `frontend/src`". That 15 is the test file's
share alone; the directory total is 20.) The #356 coverage that
*is* live-only is the refused-submit announcement, which `docs/BACKLOG.md`'s
#379 row calls gap 2. The multi-line-rendering test above was missing from the
list entirely.

`judged answer-quality eval (needs CITEVYN_OPENROUTER_API_KEY secret)` is also
advisory and deliberately so: it is the paid job, gated behind a label.

### 12.3 Measured case for promoting the live check

Every figure here is **timestamped and moving**. The invariant is what carries
the argument, not the number: re-derive before quoting, and a *larger* run count
that is still 100% `success` at `run_attempt: 1` **confirms** this case rather
than contradicting it.

- **Flake record: every run has passed, on the first attempt.** Read 2026-09-08:
  `total_count: 171` — the complete history of the workflow from its first
  commit, 2026-07-14 to 2026-09-08, 56 days. All 171 are `success` and all 171
  are `run_attempt: 1`, so no re-run is hiding an attempt-1 failure. (An earlier
  reading in this file said 168; three more runs landed since.) Re-derive with:

  ```sh
  gh api --paginate \
    'repos/imrohitagrawal/citevyn/actions/workflows/frontend-live-e2e.yml/runs?per_page=100' \
    --jq '.workflow_runs[] | "\(.conclusion) attempt=\(.run_attempt)"' | sort | uniq -c
  ```

- **Cost: $0.** The repo is public (`gh repo view --json visibility` → `PUBLIC`),
  and `gh api repos/imrohitagrawal/citevyn/actions/runs/<id>/timing` returns
  `billable.UBUNTU.total_ms: 0` on all 15 most recent runs.
- **Wall clock added to a pull request: 0 minutes.** Over **each job's own last
  15 runs** (re-measured 2026-09-08) the live job took min 85 s / median 92 s /
  max 97 s, against `Demo-mode Playwright`'s min 472 s / median 529 s /
  max 563 s. Those are **not the same date range**, and an earlier version of
  this line wrongly said they were: `frontend.yml` has no path filter and runs
  far more often, so its 15-run window spanned a few hours on 2026-09-08 while
  the live job's spanned 2026-09-06 to 2026-09-08. Both windows move. What
  carries the point is unaffected: the live job is a **separate workflow**, so it
  runs in parallel and never becomes the critical path.

### 12.4 The prerequisite, now done

`frontend-live-e2e.yml` used to be path-filtered to `frontend/**`. A required
check whose workflow never triggers reports **nothing**, and the pull request
waits on it forever — see GitHub's ["Handling skipped but required
checks"](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks).
13 of the last 30 merged pull requests touched zero `frontend/**` files
(re-derived 2026-09-08; an earlier reading said 14, and it is now 13 only because
#395 merged and pushed #333 out of the 30-PR window — the window moves, so
re-derive rather than quoting this), so promoting the context with the filter in
place would have blocked all of them.
The filter has been removed; `backend/tests/test_gating_workflows.py` keeps it
off.

### 12.5 The promotion command (owner only)

This is a branch-protection change, so only the repo owner runs it.

```sh
gh api --method POST \
  repos/imrohitagrawal/citevyn/branches/main/protection/required_status_checks/contexts \
  -f 'contexts[]=Live-mode Playwright (stub backend)'
```

Use this endpoint, not the alternatives:

- It **adds** one context and touches nothing else, so `strict: true` and
  `enforce_admins: true` are preserved because they are never sent.
- `PATCH .../protection/required_status_checks` replaces the whole
  required-status-checks object: every one of the nine contexts above has to be
  re-listed, and `strict` re-sent, or they are silently dropped.
- `PUT .../branches/main/protection` (full protection) **replaces every field**.
  Omitting `enforce_admins` clears it — and it is `true` today, so that half is
  live. The half about `required_pull_request_reviews` does **not** apply here:
  this branch has no review requirement configured
  (`gh api …/branches/main/protection` returns no such key), so there is nothing
  for a `PUT` to clear. It is still the command that quietly unprotects the
  branch; the `enforce_admins` clause is the reason.

**Known trade-off in the recommended endpoint.** GitHub's docs attach a "Closing
down notice" to the `contexts` parameter and point at `checks` for finer control.
Concretely: all nine existing entries carry an `app_id` pin — `15368`
(github-actions) on eight, `57789` (github-advanced-security) on `CodeQL` — while
a context added through the `contexts` endpoint lands with `app_id: null`, which
accepts a same-named check from **any** app. Read the pins with:

```sh
gh api repos/imrohitagrawal/citevyn/branches/main/protection/required_status_checks \
  --jq '.checks[] | "\(.app_id)  \(.context)"'
```

The recommendation above still stands: the `checks` route is part of the same
whole-object replace as `PATCH`, so taking it to gain one `app_id` pin means
re-sending all nine entries and `strict` correctly, which is the larger risk. If
the pin is wanted, add the context first with the command above, then set it
deliberately as a separate step.

Verify afterwards with
`gh api repos/imrohitagrawal/citevyn/branches/main/protection/required_status_checks`
and confirm the returned `contexts` array has ten entries and `strict` is still
`true`.

### 12.6 What the guards cover, and what they do not

`backend/tests/test_gating_workflows.py` (in the required `pytest + lint` job)
asserts that:

- no gating workflow is path-filtered, and each still triggers on `pull_request`;
- **no gating job is defused** — no job-level `if:`, no `continue-on-error:` that
  is not literally `false`, and no step-level `if:` outside a byte-exact
  allowlist of four artifact-upload steps. Three words (`continue-on-error:
  true` on an assertion step) otherwise leave a required check green while it
  asserts nothing;
- the live job's `name:` matches the context string in §12.5 byte for byte —
  compared against the `contexts[]=` **argument extracted from the fenced
  command above**, not merely against the file, because the string appears in
  this document twice and only one copy gets pasted into a shell;
- the live job asserts **execution** from its run's JSON `stats` rather than a
  `--list` count, and `frontend.yml`'s rename trap walks a skip list that is
  **complete**, not merely non-empty;
- the three copies of the `live only` selection regex agree, and the five
  live-only test titles are pinned as a set — read from source with
  commented-out declarations stripped, because commenting a live-only test out
  is invisible to every CI job.

It cannot read branch protection. It keeps the live workflow eligible; it cannot
confirm the owner promoted it, or notice it being demoted. The full blind-spot
register is `_UNGUARDABLE` in that file.
