# Next-session prompt (copy everything below the line)

---

Fresh-context continuation for CiteVyn's ADR-0004 login sequence. Implement **PR 13 (account
linking)** in this session, as its own branch, its own full build+review+merge cycle. **Do not
start PR 14 (magic-link + Resend) in this same session** — that's explicitly the session after
this one, once PR 13 is merged and verified live.

Repo: `/Users/rohitagrawal/Projects/citevyn`. Before planning anything, read `AGENTS.md`,
`code_review.md`, `docs/BACKLOG.md`, `docs/ADR/0004-user-accounts.md`, and the full plan at
`~/.claude/plans/can-you-please-do-abundant-wilkinson.md` (its **first** major section is PR 13;
its second is PR 14 — read both for context, but build only the first this session). Re-verify
every fact below against the repo yourself before acting on it — this block is a snapshot, not a
source of truth.

## Where things stand — verified 2026-09-02, main should include PR 12 once merged

- **PR 12 (GitHub + Google OAuth login) is open as PR #287**, branch
  `feat/oauth-login-adr0004-pr12`, commits `cb81411`/`6de26c4`/`5eede2e`/`09e36f1`. It went
  through 3 rounds of adversarial review (an ultracode multi-agent workflow + independent Codex
  review) that found and fixed 2 real bugs, was live-verified against real GitHub and Google OAuth
  apps with direct DB checks, and passed a `release-readiness-review` SHIP verdict with no
  blockers. **First action this session: confirm PR #287 is merged.** If not yet merged, merge it
  (it's already reviewed and approved-in-substance — don't re-review from scratch, just confirm CI
  is green and merge), then pull `main` before branching for PR 13.
- **Backend test baseline** (re-verify yourself, don't trust this number): 1531 passed / 18
  skipped / 6 failed on the OAuth branch — all 6 are known pre-existing, unrelated to OAuth (3
  pre-existing before PR 12 even started, 3 caused only by a local `backend/.env` file with real
  OAuth test credentials polluting `Settings()` construction in `test_oauth_config.py` — not a
  real failure if your `backend/.env` doesn't have OAuth keys in it, or if you temporarily move it
  aside to check).
- **A real, pre-existing production bug was found and fixed inside the PR 12 branch** (not
  introduced by it — verified against a clean `main` checkout in a throwaway worktree):
  `_mint_principal()` didn't reliably order its combined `User`+`AuthSession` flush on Postgres,
  causing `ForeignKeyViolation` for every first-time anonymous visitor. Fixed with an explicit
  `db.flush()` between the two `db.add()` calls, commit `09e36f1`. Nothing further needed here —
  just be aware this fix exists and don't reintroduce the bug in PR 13's own session-handling code.
- **GitHub issue #286** tracks a known, deliberately-deferred testing-infrastructure gap: SQLite
  (the hermetic test dialect) never enforces FK constraints anywhere in this codebase, which is why
  the bug above was invisible to 1,500+ passing tests. Quantified at 140 additional test failures
  if FK enforcement is turned on globally — mostly (not confirmed entirely) unrealistic test
  fixtures rather than confirmed additional bugs. **Owner decision: shipped OAuth first, tracked
  the rest — do not attempt to fix #286 in this session**, it's out of scope for PR 13.
- **The reusability guidance from PR 13's planning phase should already be in this repo's
  `AGENTS.md`** (a new "Building a feature likely needed across future projects" section) and in
  `imrohitagrawal/repo-template`'s `AGENTS.md`. Confirm both landed; if either didn't, that's a
  loose end from the PR 12 session to close before or alongside PR 13, not a PR 13 blocker.

## Your authority

**May do without asking:** branch from `main`, implement, run the full local battery, open the PR
for PR 13, merge once verified — same authority as prior PRs in this sequence.

**Must not do without the owner:** `fly deploy`; push a version tag; any paid API call or real
provider key beyond the two OAuth apps already configured; force-push; `git clean -fdx`;
`git stash -u`; delete any branch you did not create this session; touch `imrohitagrawal/repo-template`
again unless the check above finds it genuinely missing the AGENTS.md section.

## What PR 13 actually is — read the plan file for the real detail, this is just the shape

Lets a signed-in user with a password account **connect** a GitHub/Google identity to their
*existing* account (distinct from PR 12, which only supports OAuth as a way to log in or create a
brand-new account). This is the ADR's own promised password-recovery mitigation, made to actually
work. The plan (already through 2 rounds of adversarial review — read the "Review record" section
for the reasoning, don't re-litigate it) covers:

- Two start routes (`oauth_start` unchanged, new `connect/start`), a session-freshness gate
  (**owner-confirmed required**: reject `connect` unless the caller's session was created within
  the last ~20 minutes — this bounds a stolen-cookie's exploitable window from up to 180 days down
  to the first 20 minutes after a real login).
- `oauth_callback` dispatches to `_handle_login_intent` (pure extraction, zero behavior change)
  or `_handle_connect_intent` (new) based on the nonce's `return_intent`.
- `_link_identity` with an explicit `LinkResult` enum (`LINKED` / `ALREADY_LINKED_SAME` /
  `LINKED_ELSEWHERE`) — **never** silently reassigns an identity already linked to a different
  account, including under a concurrent-insert race (this was round 1's most significant security
  finding — read it before touching the race-handling code).
- `GET /v1/auth/me` gains `providers: list[str]`.
- Frontend: a new lazy `ConnectedAccountsDrawer.tsx` (mirroring `HistoryDrawer.tsx`), reached via
  a third `AccountMenu.tsx` menuitem — **not** inline buttons in the existing dropdown (the
  original plan draft had this backwards; `AccountMenu.tsx` is eagerly bundled, a lazy drawer costs
  less against the ~62.5 kB soft bundle budget).
- Fixes the pre-existing `LandingPage.tsx` `replaceState` bug (strips the whole query string, not
  just this app's own params) while this code is already being touched.
- 14 backend test scenarios listed in the plan, effort estimate ~19.5 hours.

## How to work — same discipline as PRs 0–12, do not relax it

- **Verify → implement → document. No claim without a check.**
- **Run pytest from `backend/`:** `uv run pytest -q`. Lint: `ruff check . && ruff format --check . && pyright`. Frontend: `npm test`, `npx tsc --noEmit`.
- **Blast radius: T3** (same tier as PR 12 — auth/security-relevant). Full T2 fan-out +
  `security-review` + adversarial verify, closing with `release-readiness-review` before merge.
  Use the Workflow tool for the review phase, same as PR 12.
- **Cap routine review at two rounds.** Only reproduced CRITICAL_BLOCKER/REQUIRED_CONTRACT
  findings block. The planning phase already found and resolved the two most severe issues
  (concurrent-race false-success, stolen-cookie permanent-backdoor) — expect the *implementation*
  review to still find something real in the actual code, the way PR 12's did twice; don't treat
  the planning review as a substitute.
- **Mutation-test every guard you add** — revert the fix, confirm the test fails for the right
  reason, restore. Every test ships with one line naming the exact change that turns it red.
- **Manual verification against real GitHub/Google OAuth apps** (already configured from PR 12),
  confirmed via direct DB queries (`user_identities`, not just the UI) — not just mocked tests.
- **Update `docs/API_SPEC.md`'s `GET /v1/auth/me` response shape** — every prior PR in this
  sequence updated it; this one should too (this also closes the non-blocking doc gap flagged in
  PR 12's release-readiness-review).
- **$0.** Stay on `provider=stub` for anything LLM-related; OAuth calls against the real
  GitHub/Google apps are free.

## Definition of done

Merged **and** verified. Green on a branch is not done. Update `docs/BACKLOG.md` in the same
change that merges. Once PR 13 is merged and verified, **stop** — do not start PR 14
(magic-link + Resend) in this session; that's a separate fresh-context session, per the owner's
explicit request to keep these as sequential, isolated builds.

Finish with: **Done / Verified myself / Cleanup / Pending / Next action** — separating what *you*
ran (with output) from what a subagent reported, and stating explicitly what is merged versus what
is actually running in production.
