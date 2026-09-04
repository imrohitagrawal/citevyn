# Next-session prompt (copy everything below the line)

---

Fresh-context continuation for CiteVyn. Repo: `/Users/rohitagrawal/Projects/citevyn`.

Before planning anything, read `AGENTS.md`, `code_review.md`, `docs/BACKLOG.md`, the session
memory notes, and run `gh issue list --state open`. **Re-verify every fact below against the
repo before acting on it — this block is a snapshot, not a source of truth.**

## Where things stand — verified 2026-09-04

`main` is at **`a51e209`**, and **production is release v13**, deployed and verified. Unlike
previous handovers, merged work IS live.

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

- **#337** — the shared `quality-gate` workflow fails a required check when `npm audit` hits a
  registry blip. Blocked two PRs in one day. The fix is in `imrohitagrawal/.github`, a
  different repo, so this is best done in a session focused there.
- **#323** — the bundle gate passes silently if the budget key is mistyped (`gz > undefined`
  is `false`), and measures one chunk rather than the eager graph.
- **#325** — the 22 visual snapshots are darwin-only and run in no CI job; `how-it-works` has
  a ±1px unstable height.
- **#332** — 29 of 84 `Settings` fields appear in no env example, including
  `cors_allowed_origins` and `rate_limit_key_salt`. Needs triage + an allowlist BEFORE
  widening the #289 guard.
- **#331** — both drawers set `aria-modal="true"` but do not trap Tab; three presses reach the
  page behind the backdrop. `AuthModal` already has a guarded trap worth extracting.
- **#329** — `demoTimer` is not gated on `screen` (same class as #312, bounded and harmless).
- **#335** — the password nudge reaches email-less OAuth accounts, where a password can never
  log anyone in.

Also open and unchanged: #321, #296, #294, #286, #273, #270, #265, #264, and the V1/V2 items.

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
