# Next-session prompt (copy everything below the line)

---

Fresh-context continuation for CiteVyn. Repo: `/Users/rohitagrawal/Projects/citevyn`.

Before planning anything, read `AGENTS.md`, `code_review.md`, `docs/BACKLOG.md`, the session
memory notes, and run `gh issue list --state open`. **Re-verify every fact below against the
repo before acting on it — this block is a snapshot, not a source of truth.**

## Where things stand — verified 2026-09-04

**Do not trust any SHA or issue list in this file — regenerate them.** A handover that states
its own commit SHA is wrong the moment it is committed; that exact defect shipped twice here.
Run these first and treat the output, not this file, as truth:

```
git rev-parse --short HEAD && git status --porcelain && gh pr list --state open
flyctl releases --app citevyn | head -3 && curl -s https://citevyn.stackclimb.com/health
gh api repos/imrohitagrawal/citevyn/branches/main/protection --jq '.required_status_checks.contexts'
gh issue list --state open --limit 60
```

At the time of writing production was release **v13** and every merged item below was live.

Closed this round, each merged with post-merge CI green and deployed:

| Issue | PR | What it was |
|---|---|---|
| #311 | #324 | Demo Playwright suite ran in no CI job; 39 specs failing on a CSS class collision |
| #322 | #327 | CSP guards were last-wins while browsers are first-wins |
| #312 | #328 | Landing hero re-rendered the app ~40x/s behind the chat screen |
| #290 | #330 | HistoryDrawer never took focus, and its focus test guarded nothing |
| #289 | #333 | OAuth env vars documented nowhere an operator looks |
| #288 | #334 | A real OAuth account was reported anonymous and locked out of its own menu |
| #316 | #336, #338 | Geist was never delivered; four other families loaded but rendered nothing |
| #326 | #339 | Frontend CI could not block a merge |

## The one thing that changed about HOW you work here

**`main` now requires seven status checks, not five.** `type-check + unit tests + build`
(which carries the 341 unit tests AND the bundle budget gate) and `Demo-mode Playwright (no
visual snapshots)` (124 tests) are now REQUIRED. `frontend.yml` deliberately has NO `paths:`
filter — a path-filtered workflow that does not trigger reports nothing, so a required context
would hang forever on a docs-only PR. Do not add path filters back to that workflow.

Consequence: a flaky frontend test now blocks merges. The job asserts `flaky == 0` on purpose.
Two flakes were fixed this round (`behavior.spec.ts:553` scroll, `behavior.spec.ts:125`
ticker); if a third appears, fix the flake rather than relaxing the guard.

## Open follow-ups, roughly by value

Regenerated from GitHub at the time of writing — **re-run `gh issue list --state open`**,
and read `docs/BACKLOG.md` for the write-up on each:

```
  #59   Embeddings: additional providers behind the seam + scale tuning (Voyage/OpenAI, HNSW recall, corpus refresh)
  #61   Frontend: real SSE streaming for chat answers (replace client-side reveal)
  #62   Frontend: gate the composer while a live answer is in flight (concurrent-send interleave)
  #84   CiteVyn-meta answers: maturation follow-ups (from #49 review)
  #119  Conversation memory: scale to long conversations (rolling summary + LLM rewrite + token budget + index)
  #125  Eval harness: chunk-level context precision/recall + distractor corpus + golden-set growth
  #154  V1: Feedback capture wired into the eval loop (not model retraining)
  #155  V1: Evaluation + live-ops dashboard
  #156  V1: Better re-ranking of retrieved chunks
  #157  V2: ChatGPT (OpenAI) official docs as a 5th product domain
  #158  V2: Voice output (TTS) for answers
  #264  /health/index is not dual-active aware: it reports the vector arm healthy while retrieval fails closed
  #265  Zero active index rows: the arms scan by document status while the provenance gate has no row to check, so the vector arm can run on foreign vectors
  #270  Real user accounts (login): ADR-0004 implementation, PRs 0-12
  #273  Move /health/index and /health/dependencies behind admin auth (deferred from ADR-0004 PR 2)
  #286  SQLite test suite has FK enforcement off — masks real Postgres-only bugs
  #294  ADR-0004 PR 14 advisory follow-ups (magic-link login + password) recorded from review
  #296  Deploy runbook omits --build-arg VITE_API_DEMO_KEY: a deploy that follows docs/DEPLOY_FLY.md ships a frontend that cannot call the API
  #321  Coverage: record a baseline across three main runs, then decide whether to make it blocking
  #323  Bundle budget gate passes silently if the budget key is mistyped, and measures only one chunk of the eager graph
  #325  Visual snapshot suite runs in no CI job (darwin-only baselines), and how-it-works has a ±1px unstable height
  #329  demoTimer is not gated on screen: a landing demo answer keeps streaming while the chat screen is up
  #331  Drawers set aria-modal="true" but do not trap Tab: 3 presses reach the page behind the backdrop
  #332  29 of 84 Settings fields are documented in no env example (the OAuth gap was one instance)
  #335  Password nudge now reaches email-less OAuth accounts, where a password can never be used to log in
```

## Deploy

`docs/DEPLOY_FLY.md` §4.1, with **BOTH** build args. Two things that runbook does not say
loudly enough, both learned the hard way:

- **Fly's REMOTE builder stalled three times** on the build-context upload (548 KB at ~0.5
  KB/s, then `deadline_exceeded`). `flyctl deploy --local-only` with Docker Desktop worked
  first try. A failed build creates NO release — check `flyctl releases` rather than assuming
  a partial deploy.
- Wake the scaled-to-zero machine with `curl /health` FIRST or the demo-key read returns
  empty, and an empty `VITE_API_DEMO_KEY` ships the public default and 401s every browser call
  (#296). Guard for it; the deploy script did.

Afterwards verify the served bundle has **0** hits for `local-demo-key`, and check the actual
UI, not just `/health`.

## How to work — the discipline that held

- **Verify → implement → document. No claim without a check.** Every number you report is one
  you measured this session.
- **Order per PR:** baselines → RED test → implementation → **mutation-test every guard**
  (sequentially, byte-copy restore, assert the mutation applied) → docs (`API_SPEC` for a
  contract change, `UI_DESIGN.md` + its changelog row for any frontend change, BACKLOG row) →
  commit → **live browser walkthrough BEFORE review** → review → fixes → **a skeptic round on
  the fix diff** → PR → CI → plain squash-merge → post-merge CI green → delete branch → next.
- **Review sizing:** write the dimension list BEFORE launching and mark each run or
  skipped-because-N/A. An unlisted dimension is *silently* skipped.

### Traps this round paid for — do not re-learn them

- **The guard you write is the guard to mutate.** Three separate guards written this round were
  themselves the bug they guarded against: a CI step that asserted tests were SELECTED (`--list`
  counts skipped tests, and Playwright exits 0 when all skip); a CSP check that pinned the
  CONSTANT while the emitted header went unchecked; and an env-var docs check that matched the
  name ANYWHERE in the file, so deleting the assignment left it green because the name appeared
  in a comment above.
- **Assert what the consumer receives**, not the value you happen to be looking at.
- **A hidden Chrome tab throttles timers ~32x**, which makes a streaming answer look stalled
  mid-sentence and reads exactly like a production bug. Check `document.visibilityState` before
  concluding anything about streaming.
- **`playwright.config.ts`'s `reuseExistingServer: false` is load-bearing.** Set it to `true`
  and Playwright adopts a hand-started dev server carrying `.env.local`'s `VITE_API_LIVE=true`,
  and 20 chat specs fail against a backend that is not running. The config's own comment warns
  about this; I did it anyway and lost a round.
- **`setsid` does not exist on macOS**, and a Monitor whose filter matches only success patterns
  will sit silent through a failure for an hour. Verify a background process actually started.
- **A reviewer subagent can move HEAD.** Assert `git rev-parse HEAD` before and after any
  measurement, and tell reviewers not to run checkout/stash/restore.

## Definition of done

Merged **and** post-merge CI green on `main`, with a live walkthrough recorded in the PR body.
Finish with **Done / Verified myself / Cleanup / Pending / Next action**, separating what *you*
ran from what a subagent reported, and stating explicitly what is merged versus what is running
in production.
