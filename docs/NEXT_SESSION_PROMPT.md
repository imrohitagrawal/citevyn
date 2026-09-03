# Next-session prompt (copy everything below the line)

---

Fresh-context continuation for CiteVyn. **The owner's 2026-09-03 production-QA round is code-
complete and merged. Nothing from it is deployed.** What remains is one owner-gated deploy and
one measurement item.

Repo: `/Users/rohitagrawal/Projects/citevyn`. Before planning anything, read `AGENTS.md`,
`code_review.md`, `docs/BACKLOG.md`, the session memory notes, and run
`gh issue list --state open`. **Re-verify every fact below against the repo before acting on it —
this block is a snapshot, not a source of truth.**

## Where things stand — verified 2026-09-03

`main` is at **`22ea68f`**. All four QA items are MERGED with post-merge CI green:

| Issue | PR | On `main` |
|---|---|---|
| #300 who-are-you routing | #305 | `4b1c5e0` |
| #301 magic-link cooldown | #309 | `8127c43` |
| #302 landing re-ask scroll | #314 | `f928928` |
| #303 markdown subset + citation chips | #315 | `b805e80` |
| #306 CSP font host | #317 | `22ea68f` |

**Production (https://citevyn.stackclimb.com, Fly app `citevyn`) still runs release v9 =
`f3e64ca`. None of the above is live.**

## The one gate

**Ask the owner for the deploy "go" before anything else.** It is the only thing standing between
the merged work and production, and it is explicitly owner-only. If authorised:

- `docs/DEPLOY_FLY.md` §4.1, with **BOTH** build args. A deploy without `VITE_API_DEMO_KEY` broke
  production for an hour (#296).
- Wake the scaled-to-zero machine with `curl /health` FIRST, or the key read comes back empty.
- Afterwards verify the served bundle has **0** hits for `local-demo-key`.
- **Flush `answer_cache`.** #303 changed the system prompt, so cached answers predate the
  markdown-subset instruction and would replay unformatted for the 24 h TTL (the same deploy note
  #174 carries).
- Then check the four QA fixes on the live site, not just `/health`.

## Then: #308 — coverage tooling (small, after the deploy)

Deliberately **measurement, not a gate**: dev dependency + `make coverage` + report-not-gate in
CI, with the blocking condition stated up front (no-decrease-vs-baseline after three stable `main`
runs, never an absolute floor). See the issue for why a percentage would not have caught the
defect that motivated it.

## Open follow-ups this round created — read before starting anything else

- **#311 — the demo Playwright suite (145 tests) runs in NO CI job, and 70 of its runs already
  fail on `main`** from one CSS class collision (`AccountMenu` reuses `className="theme-toggle"`,
  colliding with `Header`'s real toggle). This is the suite that should have caught #302. Highest
  value of the three.
- **#312** — the landing hero animation re-renders the whole app ~40×/second while the CHAT
  screen is up; it is what turned #302 into a production-only bug. Measured honestly: **not** a
  CPU problem (+33% renders, identical `LayoutCount`, CPU within noise). A `useMemo` on `chatView`
  takes idle chat-screen `scrollTop` writes from 221/12 s to **0** for +17 B.
- **#313** — `isSafeHref` in `ChatView` accepts protocol-relative URLs. **The fix written in that
  issue is insufficient**: the WHATWG URL parser strips ASCII tab/LF/CR before parsing, so
  `/\t/evil.com` IS `//evil.com` and no `startsWith` patch catches it. `frontend/src/lib/safeHref.ts`
  (added in #303) is the correct parser-based implementation — #313 is now mostly "delete the
  remaining copy and point it at that", so re-read the issue before building what it says.
- **#316** — the page loads a render-blocking Fontshare stylesheet and a preconnect for Satoshi,
  **a typeface no `font-family` in the codebase uses** (both grep hits are comments; the tokens
  are Geist). Needs an owner decision: wire Satoshi up, or delete the link and take BOTH fontshare
  hosts back out of the CSP.

## Two decisions awaiting the owner

1. **The eager-bundle ceiling moved twice this round**, 63.5 → 64.0 kB (#302) → 66.0 kB (#303).
   It is now 65,329 B with 671 B headroom. **No mechanical gate enforces this number in either
   direction** — it is prose in the `docs/BACKLOG.md` #270 row. Worth deciding whether it should
   become a real CI check, since it has now been raised twice in two PRs by the same person who
   records it.
   **Measure it with `frontend/.env.local` present** (`VITE_API_LIVE=true`, which the Dockerfile
   sets): a `git archive` checkout lacks it and builds ~860 B larger, which is not what ships.
   That discrepancy already caused one reviewer to report a wrong number.
2. **How "rather than act as a second entry point" was read in #302** — as satisfied by composer
   focus + carry-across, with every landing entry point still submitting. The strict reading would
   make #302's own bug unreachable. Easy to change if the owner meant the strict one.

## How to work — the discipline that held; do not relax it

- **Verify → implement → document. No claim without a check.** Every number you report is one you
  measured this session. This round corrected several claims that came from issue text rather than
  measurement — including two of the issues' own descriptions of their symptoms.
- **Order per PR:** baselines → RED test → implementation → **mutation-test every guard**
  (sequentially in the one tree, byte-copy restore, assert the mutation applied) → docs
  (`API_SPEC` for any contract change, `UI_DESIGN.md` for any frontend change **including its
  changelog row**, README/`.env.example` for any new setting, `SECURITY_MODEL.md` for any new
  rate-limit bucket, BACKLOG row) → commit → **live browser walkthrough BEFORE review** → review →
  fixes → **a skeptic round on the fix diff** → release-readiness gate → PR → CI → plain
  squash-merge → post-merge CI green → delete the branch → next item.
- **Review sizing:** write the dimension list BEFORE launching, and mark each run or
  skipped-because-N/A: correctness · silent-failure · security · performance · architecture/taste ·
  test-adequacy · coverage · data contract · docs-contract · completeness. An unlisted dimension is
  *silently* skipped.

### Traps this round paid for — do not re-learn them

- **A "read-only" reviewer ran `git checkout` in the shared tree** and silently reverted the
  branch. A full live walkthrough then measured `main` and appeared to show the fix completely
  broken. **Assert `git rev-parse HEAD` before AND after every measurement**, tell reviewers
  explicitly not to run checkout/stash/restore, and prefer a separate `git worktree` for
  measurement runs.
- **A test can pass for the wrong reason in ways coverage never shows.** This round: a pulse-restart
  test where an *ended* animation also read as "restarted"; a `[sendTick]` effect that could be
  deleted whole with 215 tests green, because the test never scrolled away first; a `getTimerCount()
  > 0` partner that held even when the code armed no timers; and a CSP scheme check that every
  test "covered" via the origin comparison instead. **Mutate, then read why it died.**
- **`waitStreamDone`-style waits return IMMEDIATELY** when no `.typing-cursor` exists yet, so a
  question sent 60 ms later has not landed. Wait on the BOT bubble count
  (`.message.bot-msg:not(.pending-msg)`), or your conversation is malformed and every measurement
  from it is meaningless. This cost a whole misdiagnosis round.
- **`.map(parseInline)` passes the index as the second argument.** Be explicit in `.map` callbacks.
- Port 3000 is often occupied by a dev server that is not yours. Use your own port; never kill it.

## Definition of done

Merged **and** post-merge CI green on `main`, with a live walkthrough recorded in the PR body.
Finish with **Done / Verified myself / Cleanup / Pending / Next action**, separating what *you* ran
from what a subagent reported, and stating explicitly what is merged versus what is running in
production.
