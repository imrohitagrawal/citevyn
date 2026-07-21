# Next-session prompt (copy everything below the line)

---

ultracode — full authority, fully autonomous. Repo: `/Users/rohitagrawal/Projects/citevyn`,
branch `main`, clean and synced with origin.

Read `AGENTS.md`, `code_review.md`, `docs/BACKLOG.md` and the session memory FIRST.

**Three rules govern this whole session. They are not advice — each has a checkable output, and
your final report must show it.**

1. **PLAN BEFORE CODE.** No implementation until the plan has survived an adversarial fan-out and
   reached a fixpoint. The goal is that PR review finds almost nothing, because the design was
   already attacked and the code was already proven working.

2. **REVIEWERS AND BUILDERS MUST EXECUTE, NOT JUST READ.** A review that only reads the diff is
   half a review. Every reviewer runs the thing. Every builder proves their own work runs BEFORE
   calling it done.

3. **NEVER CONCLUDE ABSENCE FROM A FAILED LOCAL REPRO — AND NEVER TRUST A SUCCESS MESSAGE.**
   Both halves cost the last session real errors, and both are cheap to prevent:

   - *Before* calling ANY live-observed bug transient, not-reproducible, or fixed, **query
     production's audit trail first** and say so in your report. `audit_events.metadata` records
     the exact orchestrator exit reason (`no_answer` / `uncited_answer` /
     `citation_validation_failed` / `weak_evidence`). Last session #215 was declared "not
     reproducible" from a local repro that could not reproduce it; ONE read-only query for $0
     overturned that and found the real cause. A local repro that cannot reproduce is **not**
     evidence of absence.
   - *After* any command that reports success, **verify the resulting STATE, not the message.**
     That rule, applied immediately after the production evaluation printed `passed`, is what
     found **#229** — the run had genuinely succeeded, and the health endpoint still contradicted
     it. The same discipline applies to sub-agent reports: one agent's #208 fix passed 7 of its own
     tests and 4 mutations and still failed live.

   Applied to this session's own contents: **do not take any root cause below on faith** — each
   carries its evidence so you can re-derive it cheaply. Verify, then fix.

═══════════════════════════════════════════════════════════════
COST DISCIPLINE — READ FIRST
═══════════════════════════════════════════════════════════════
OpenRouter has ~**$1.10** remaining. It is a balance, not an allowance.

- **Build everything on `stub`.** `backend/.env` is on stub; leave it there.
- **Almost nothing here needs an LLM.** The #215 fix is a pure function. Formatting, chips and the
  timeout are frontend/config. Retrieval work is measured on the hermetic eval harness. If you
  reach for a provider mid-build, stop and ask what you are actually trying to learn.
- **ONE scripted live pass at the very end** — a fixed list of ~8–10 questions, not exploratory
  clicking. The public demo spends real money per question.
- **Prefer Gemini free tier** for that pass. Check `gemini-flash-latest` health FIRST — it was
  returning 503, which is the only reason the demo was burning OpenRouter credit at all.
- **Hard ceiling for the session: $0.10.** Realistically it should be $0.00. If you project going
  over, stop and report.
- Before ANY paid call, state in one line why the free path cannot answer the question.

═══════════════════════════════════════════════════════════════
PHASE 0 — CLEAN SLATE
═══════════════════════════════════════════════════════════════
- `git fetch --prune`; confirm `main == origin/main`, tree clean, no stray branches.
- Baseline FROM REPO ROOT and record exact counts:
  `env -u CITEVYN_DATABASE_URL uv run --project backend pytest backend/tests -q` → **1357 passed,
  16 skipped**; `cd frontend && npm test` → **89 passed**.
  *(Run pytest from the repo ROOT. From `backend/` a gitignored `.env` bleeds `CITEVYN_*` and you
  will chase three phantom failures.)*
- Measure and record the **current frontend bundle size** (`npm run build`) — it is a gate later.
  Baseline: `index.js` **189.86 kB / 60.40 kB gzip**.
- `gh issue list --state open`; reconcile against `docs/BACKLOG.md`.

═══════════════════════════════════════════════════════════════
PHASE 1 — PLAN, THEN ATTACK THE PLAN (no implementation code)
═══════════════════════════════════════════════════════════════
1. Write the plan. For every inventory item: problem, root cause **with evidence**, proposed fix,
   blast radius, rollback, how it will be proven RED→GREEN, and what would make the fix wrong.

2. **Fan out sub-agents to attack the PLAN**, in parallel, read-only. Give each an explicit lens
   and the absolute path `/Users/rohitagrawal/Projects/citevyn`. **Each must RUN things** — build
   the frontend, execute the harness, query the DB — not merely read. Minimum lenses:
   - **Correctness** — independently re-derive each stated root cause. Is it actually the cause?
   - **Security** — markdown puts model output in the DOM. Treat the model as hostile. Use the
     `security-review` skill.
   - **Blast radius** — which changes touch the shared request path and therefore need the owner's
     decision rather than a merge?
   - **Test adequacy** — for each proposed test, construct a mutation it would MISS.
   - **Completeness** — what does the plan silently drop?
   - **Taste / simplicity** — `taste-check`. Is anything over-built?
   - **Sequencing** — do any two fixes interact? (One such interaction is already known and
     documented below; assume there are more.)
3. Iterate to a **fixpoint**: re-run the fan-out until a fresh pass finds nothing new.
4. **Traceability gate:** produce a table mapping every inventory item to a planned task. Anything
   unmapped is scheduled or explicitly declined with a reason. **No item may silently vanish.**

Skills: `taste-check`, `security-review`, `rag-eval`, `webapp-testing`, `e2e-testing-patterns`,
`release-readiness-review`.

═══════════════════════════════════════════════════════════════
PHASE 2 — DECOMPOSE
═══════════════════════════════════════════════════════════════
Small, independently-reviewable, independently-mergeable PRs. One concern each. State the order and
why. Do NOT batch unrelated fixes.

**Known-correct order (derived last session — change it only with a stated reason):**

1. **A** — restore `white-space: pre-wrap` + a regression test. Trivial, zero-risk, ships first.
2. **#215** — validator fix **AND** citation numbering, **in ONE PR** (see A2 below for why
   splitting them ships a wrong-citation bug).
3. **B** — markdown rendering.
4. **Chips** — clickable `[n]` citation markers; re-scope `pre-wrap` in the same PR.
5. **C** — timeout / cold start.
6. **E** — smaller findings + record hygiene.
7. **D** — retrieval quality. **STOP-AND-REPORT, do not merge.**

═══════════════════════════════════════════════════════════════
PHASE 3 — IMPLEMENT (parallel where safe; prove it runs)
═══════════════════════════════════════════════════════════════
**Parallelism rules — sub-agents SHARE one working tree.** Last session an implementation agent
wrote into the checked-out branch and blocked a commit.
- Parallelise REVIEW freely.
- Parallelise BUILDING only across **disjoint files** using `isolation: "worktree"`.
- Realistic split here: **backend** (#215 validator, D) and **frontend** (A, B, chips) can run in
  parallel worktrees. Within the frontend, A→B→chips are sequential by dependency — do not fake
  parallelism there.
- #215 spans both (validator + wire shape + frontend adapter). Keep it ONE builder.

**Every builder must self-verify BEFORE declaring done** — this is the point of the whole session:
- Run the full suite from repo root; report the exact count.
- `ruff check`, `ruff format --check`, and `pyright` **run from `backend/`** (repo-root pyright
  picks up a different config and reports hundreds of unrelated errors).
- For frontend work: `npm test`, `npm run build`, **and actually render it** — use `webapp-testing`
  / Playwright and LOOK at the output. Screenshot it. "The test passes" is not "the user sees the
  right thing".
- Prove every guard RED before GREEN and put the real output in the PR.
- Mutation-test anything you call a guard, and **grep to confirm the mutation applied** —
  ruff-format can silently un-apply one and hand you a false survival.

**Hygiene:** explicit pathspecs on every commit, never a bare `-m`. No Claude attribution footer.
Never `git checkout` to undo a mutation on uncommitted work — restore from a `cp` backup. Do NOT
modify `backend/.env` or `infra/docker/.env`. Keep `docs/BACKLOG.md` in sync in the SAME commit.

═══════════════════════════════════════════════════════════════
PHASE 4 — REVIEW (executing, all angles), GATE, MERGE
═══════════════════════════════════════════════════════════════
Size the review to blast radius per `AGENTS.md`. **Reviewers must execute**, and must cover every
angle below that applies. A reviewer that only reads the diff has not reviewed it.

| Angle | What it means here |
|---|---|
| **Correctness** | Re-derive the root cause independently; try to construct a false pass |
| **User-visible UI** | Actually render it. Screenshot. Does a real answer look right end to end? |
| **Exact matching** | `/v1/search/exact` + `exact_lookup` intent still behave — the validator change touches the shared answer path |
| **Streaming** | The reveal is progressive. Does partial markdown flicker or break mid-stream? |
| **Accessibility** | Chips keyboard-reachable, focus visible, accessible names; markdown emits real semantic elements |
| **Dark mode** | Both themes — the repo ships light AND dark tokens |
| **Responsive / mobile** | Narrow viewport; long code spans must not overflow the bubble |
| **Security** | Hostile model output: `javascript:` links, embedded HTML, injection via a poisoned corpus. Never `dangerouslySetInnerHTML` |
| **Performance** | Bundle-size delta (gated, see B), render cost on long answers, no added network round-trips |
| **Regression** | Locked golden/eval numbers must NOT move; full suite count reported |
| **Observability** | Does the change alter what lands in `audit_events`? More answers will now survive — confirm the trail still distinguishes outcomes |
| **Rollback** | Stated per PR |

Close each PR with `release-readiness-review` as the ship/no-ship gate. Merge yourself once CI is
green — verified by the JOB running, not by an unchanged `/health` 200. **Never close an issue
without evidence in the closing comment.** End state: `main` green, tree clean, branches pruned,
BACKLOG accurate.

═══════════════════════════════════════════════════════════════
THE INVENTORY
═══════════════════════════════════════════════════════════════

## A1 — #215 is a CITATION VALIDATOR bug, not retrieval

**Root cause is established from the production audit trail. Verify it; do not re-litigate it.**

`backend/app/llm/validation.py:104-111` hard-fails when cited indices have a gap:

```python
expected = set(range(1, max(cited_indices, default=0) + 1))
missing = sorted(expected - set(cited_indices))
if missing:
    return CitationValidationResult(valid=False, ...)   # whole answer discarded
```

Production evidence (`audit_events`, 26 messages → **3 hits, ~12%**):

```
10:33:03 -> citation_validation_failed: citation indices must be contiguous from 1; missing [2]
11:34:33 -> citation_validation_failed: citation indices must be contiguous from 1; missing [2]
15:52:34 -> citation_validation_failed: citation indices must be contiguous from 1; missing [1, 4, 5]
```

The user sees "I couldn't find a grounded answer" — indistinguishable from a retrieval failure,
which is why it was misfiled as one.

The rule contradicts the product's own cite-once design: the orchestrator says "the response
surface only shows the citations the model actually referenced", and the very next branch treats
*uncited* bullets as a **warning only**. Citing `[1]` alone is fine; citing `[1]` and `[3]` discards
the answer. Genuine hallucination is already caught by the `out_of_range` check above it.

**Verification is FREE and needs no LLM** — the validator is pure. Construct the exact production
failure (answer citing `[1]` and `[3]`, six evidence bullets) and assert.

**Already verified, do NOT re-investigate:** retrieval is innocent — on a production-faithful stack
the "failing" phrasing ranks the right chunk **#1 at 0.8246**, margin 0.074, inside the documented
answerable band. `confidence` is citation density (`len(cited)/len(evidence)`), NOT a retrieval
score, so the original issue's "corpus retrieves at the floor" premise is wrong.

## A2 — THE BLOCKER: citation numbering is broken, and A1's bug is hiding it

**Do not ship A1 alone.** The wire `Citation` shape carries **no index**:

```python
{"source_name": ..., "title": ..., "url": ..., "chunk_id": ...}
```

and the frontend re-numbers by array position (`frontend/src/lib/citations.ts`:
`const n = String(index + 1)`).

So once non-contiguous citations are allowed through, a model citing `[1]` and `[3]` produces two
citations, rendered as cards **1** and **2**, while the text still says **`[1]` … `[3]`** — the
marker points at a card that does not exist.

**The contiguity rule is load-bearing for the frontend's numbering.** Removing it without fixing
numbering trades a false refusal for a WRONG CITATION — worse, for a product whose whole promise is
trustworthy sources.

**Decision (made; implement it):** add the marker index to the wire — the original 1-based evidence
index the model cited — and have the frontend use it instead of array position.
- Rejected: rewriting `[3]`→`[2]` in the answer text (string surgery on model output; also breaks a
  literal `[3]` inside a code span).
- Rejected: sending all evidence (that is exactly what #174 fixed).
- Requires: `docs/API_SPEC.md` §5 update, the frontend adapter, and the **offline canned
  knowledgeBase path kept consistent**.

## B1 — `white-space: pre-wrap` REGRESSION (ship first, trivial)

Absent from `frontend/src/`. It existed: added in `7e9bc92` and `ad062bb`, **deleted by `2503dd4`
"commit green CiteVyn landing baseline"** — the design port overwrote the chat CSS and dropped it.
Nothing caught it: 94 UI tests, none asserts a multi-line answer renders as multiple lines.

**Must ship with a test that asserts RENDERED OUTPUT.** jsdom does not compute CSS, so a `pre-wrap`
assertion needs Playwright (`frontend/tests/` already has one, incl. visual snapshots).

## B2 — Markdown renders literally

`frontend/src/components/ChatView.tsx:171` renders `{m.text}` as a raw React text node. No markdown
library has ever been in `frontend/package.json`. Live symptom: bulleted answers arrive as one
run-on blob with visible `*` and `**`.

**The system prompt does NOT constrain output format** (`backend/app/llm/prompts.py` only constrains
*content*), so the long tail is real: tables, code fences, nested lists, numbered steps.

**Decision (made; implement it):** `react-markdown`, **without `rehype-raw`**, with an element
allowlist (`p, ul, ol, li, strong, em, code, pre, a, blockquote`). Never `dangerouslySetInnerHTML`.
Reuse the existing `isSafeHref` guard for links; force `rel="noopener noreferrer"`.
**Gate:** if the measured bundle delta exceeds **~40 kB gzip** over the 60.40 kB baseline, fall back
to a hand-rolled zero-dependency renderer (bullets/bold/inline-code only) **plus** one line in the
system prompt constraining output to short paragraphs and simple bullets. Measure, do not assume.
Justify the dependency in the PR per `AGENTS.md`.

**Open design call the plan must make:** the reveal is progressive (`m.streaming`), so markdown will
be parsed on INCOMPLETE text (`**bo` → literal asterisks that later snap to bold). Decide: buffer
markdown until the reveal completes, or accept partial parse. Justify it and test it.

## B3 — Clickable citation chips (IN SCOPE, after B2)

Make the inline `[n]` an interactive element. **Only correct once A2 lands** — chips are also the
best proof the numbering is right, because a wrong number becomes instantly visible.
- Click **scrolls to and highlights the source card in-page** (do not navigate away; the card
  already carries the outbound link).
- Real `<button>`, keyboard-focusable, accessible name e.g. *"Source 1: Claude API Reference"*.
- A literal `[1]` inside a code span must **NOT** become a chip.
- Re-scope `pre-wrap` in this PR: keep it for plain-text paths (refusal copy, error notices), drop
  it from the markdown-rendered container, or block margins plus preserved newlines will
  double-space.

## C — Timeout / cold start

`frontend/src/lib/api.ts` sets `DEFAULT_TIMEOUT_MS = 20_000`; `fly.toml` sets
`min_machines_running = 0`. Cold start + embedding + LLM (worse when Gemini 503s and fails over)
exceeds 20s → "⚠ TEMPORARILY UNAVAILABLE — Request timed out". Observed live. Consider a longer
timeout and/or `min_machines_running = 1`, respecting the fly.toml comments on the free allowance.
**Do not change machine memory** (`test_fly_config.py` pins it).

## D — Retrieval quality — LAST, and STOP BEFORE MERGING

1. **`KeywordRetriever` contributes zero ranking signal.** Every hit scores a flat `0.5`, ordered by
   `chunk_order`. Every chunk of a routed doc matches (the chunker prefixes the H1, so
   `claude`/`api` always hit), so scoped ranking is decided entirely by the vector arm — and when
   that degrades, ranking collapses to **document order**, with `top_k=6` vs 7 chunks dropping one
   by position, not relevance. Proven: all four #215 phrasings produced an identical hermetic
   ordering. Belongs to **#156**.
2. **#226** — `HybridRetriever._active_index_stamp` resolves provenance by `status == active`,
   ignoring `active_index_version`, so a caller retrieving a NON-active index gets the vector arm
   enabled against a mismatched index. Measured. Interacts with the #58 dual-active guard (which
   today ENABLES the arm on unknown provenance) and breaks
   `test_active_index_stamp_none_on_dual_active`.

**Both are corpus-wide ranking changes. Measure on the eval harness (`rag-eval`) — NOT the live
demo. If either moves the golden numbers, STOP, write up before/after, and report. Owner's call.**

## E — Smaller findings and record hygiene

1. **Promotion suite measures the wrong granularity** — asserts the right *document* appeared, not
   rank. A corpus reduced to one H1-only chunk per doc still passes **10/15**; only the 0.95
   aggregate saves the gate. `tests/eval/retrieval.py` already computes MRR/precision@1.
2. **#162 double-ingest is invisible to the promotion gate** — re-ingest duplicates chunks (7→14),
   suite still scores 1.0 and promotes.
3. **`scripts/smoke.sh` never asks a question** — asserts `/health` and stops. The README is honest
   about it now, but it would not have caught a single bug found last session.
4. **`evaluate_index` commits a caller-owned session** — safe today (only the CLI calls it);
   consider taking an `async_sessionmaker` like `drive()` does.
5. **Embedder `aclose` leak** in `cli._cmd_evaluate` — pre-existing, shared with `_cmd_run`. LOW.
6. **Asset-guard regex gaps** in `backend/tests/test_frontend_assets.py` — misses `srcset`, CSS
   `url()`, unquoted attributes. Documented, not closed.
7. **Stale records:** `docs/BACKLOG.md` still lists dependabot #148/#150/#151 as open; they are not.
   Only **PR #227** is open (node 22→**26**). `DEPENDABOT_TRIAGE.md` now has an explicit
   **build-only base image** row: no human reviewer required, CI `image-smoke` is the gate, and it
   is green. **But do NOT merge #227 as-is — see E9.**
8. **[#229](https://github.com/imrohitagrawal/citevyn/issues/229) — `/health/index` and the admin
   API report `evaluation_run_id: null` even when the index HAS passing evidence.**
   `index_versions.evaluation_run_id` is declared in the model, the `0001` migration and the admin
   schema, but **nothing has ever assigned it**; the gate resolves evidence the other way, via
   `evaluation_runs.index_version`. So the health display contradicts the gate, and an operator who
   trusts it reaches for `?force=true` — the exact habit #216 removed. Fix `evaluate_index` to set
   it; decide deliberately whether it points at the newest run or only at passing ones, and keep
   `_latest_completed_run` authoritative for the gate.
9. **[#231](https://github.com/imrohitagrawal/citevyn/issues/231) — CI tests the frontend on Node
   20; production ships a bundle built on Node 22. Nothing reconciles them.**
   `frontend.yml:36` and `frontend-live-e2e.yml:28` pin `setup-node` to **20**;
   `Dockerfile.api:55` builds the shipped bundle on **22**; there is no `.nvmrc` and no `engines`
   field. **CI has never validated the artifact production serves.** PR #227 would widen that from
   two majors to six while CI stays on 20 and reports green either way.

   **This supersedes the earlier "merge #227 after the frontend work" advice, which was WRONG**:
   `setup-node` and the Dockerfile are independent, so Playwright never exercises the Dockerfile's
   Node no matter when #227 merges. Correct order:
   1. Add `frontend/.nvmrc` as the single source of truth; point both workflows at it with
      `node-version-file:`; align `Dockerfile.api` to the same major — **reconcile on the current
      known-good 22 first**, so the reconciliation itself is not also a version bump.
   2. Add a guard test asserting the Dockerfile's `FROM node:<major>` matches the pin. The repo
      already has this pattern (`test_fly_config.py`, `test_readme_endpoints.py`). Without it this
      silently re-drifts on the next bump.
   3. **Then** treat 20/22 → 26 as ONE deliberate bump moving CI and the Dockerfile together, so
      the tests gating it run on the version being shipped. Merge #227 as part of that, or close it
      in favour of the combined change.

**File GitHub issues for anything without one and index it in `BACKLOG.md`.** Currently unfiled:
A2, B1, B2, B3, C, and E1–E6.

═══════════════════════════════════════════════════════════════
GOTCHAS — do not rediscover these
═══════════════════════════════════════════════════════════════
- **A local repro that cannot reproduce is NOT evidence of absence.** Last session's biggest error.
  `audit_events` records the exact orchestrator exit reason (`no_answer` / `uncited_answer` /
  `citation_validation_failed` / `weak_evidence`) and settled #215 in ONE read-only query for $0.
  Read the audit trail BEFORE concluding anything about a live bug. This works:
  `fly ssh console -a citevyn -C "python -c \"import os,psycopg; c=psycopg.connect(os.environ['CITEVYN_DATABASE_URL'].replace('+psycopg','')); print(list(c.execute('SELECT ...')))\""`
  A base64-`exec` form of the same command is blocked by the safety classifier, reasonably — write
  it plainly.
- **Verify sub-agent self-reports against the running system.** One agent's #208 fix passed 7 of its
  own tests and 4 mutations and still failed live.
- **GREEN != COVERED.** Mutation-test guards; grep to confirm the mutation applied.
- Check the test **COUNT**, not just exit status.
- Run pyright from `backend/`, pytest from the repo ROOT.
- `fly deploy` is blocked by the safety classifier — the owner runs it. `fly ssh console -C` with a
  plain command is NOT blocked.
- A **refusal is often correct** — the corpus is six documents. Before calling one a regression,
  `grep -ri "<term>" backend/app/worker/sources/`.
- A question with no product noun routes to `domain: unsupported` BY DESIGN.

═══════════════════════════════════════════════════════════════
LIVE-OPS FACTS
═══════════════════════════════════════════════════════════════
- **https://citevyn.stackclimb.com** — public, live. Fly app `citevyn`, org `personal`, region
  `iad`, ONE `shared-cpu-1x`, scale-to-zero. Neon Postgres + pgvector, Upstash Redis via Fly.
- Active index `v1`, 42 chunks, vector arm healthy, `gemini/gemini-embedding-001@1536`.
- **The promotion gate now HAS evidence.** `citevyn-worker evaluate --index-version v1` was run
  against production and passed (`pass_rate=1.0 cases=15`); the gate's own resolver confirms
  `promotable WITHOUT force: True`. Re-run it after any re-ingest, since the evidence attests to the
  corpus as it was measured:
  `fly ssh console -a citevyn -C "python -m app.worker.cli evaluate --index-version v1"`
  (The machine is scale-to-zero — `curl .../health` first or `fly ssh` fails with "no started VMs".)
- **BUT `/health/index` still reports `evaluation_run_id: null`** — see **#229**. Do not read that
  field as "no evidence"; it is a different, never-populated column. Fixing it is in scope (E8).
- `VITE_API_LIVE=true` is the Dockerfile default — no build-arg needed.
- Uptime probe every 30 min; do not tighten it (each probe wakes the machine).

═══════════════════════════════════════════════════════════════
OWNER-GATED — remind, do not attempt
═══════════════════════════════════════════════════════════════
- `fly deploy --build-arg VERSION=$(git describe --tags --always)` (classifier-blocked).
- PR #227 (node 22→26) needs **no** reviewer — `DEPENDABOT_TRIAGE.md`'s build-only base-image row
  makes CI `image-smoke` the gate. It is held for a technical reason, not a policy one: see E9/#231.
- Cloudflare MCP token is READ-ONLY; DNS automation needs `Zone:DNS:Edit`.
- Never paste a credential into chat.

REPORT: ship/no-ship per item with REAL command output, screenshots for anything user-visible, the
bundle-size delta, the final suite counts, anything you could not verify, anything you chose not to
do and why, and the exact spend.
