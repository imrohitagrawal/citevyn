# Next-session prompt (copy everything below the line)

---

ultracode — autonomous continuation for CiteVyn. Work through PRs 5–12 of the ADR-0004
login plan to completion, in order, without checking in between individual PRs — but
follow the pause points named explicitly below.

Repo: `/Users/rohitagrawal/Projects/citevyn`. Before planning anything, read `AGENTS.md`,
`code_review.md`, `docs/BACKLOG.md`, `docs/ADR/0004-user-accounts.md`, the full owner-approved
plan at `~/.claude/plans/i-also-want-you-hashed-marble.md`, and `gh issue view 270` for the
live PR checklist. Re-verify every fact below against the repo yourself before acting on it —
this block is a snapshot, not a source of truth.

## Where things stand — verified 2026-09-01, main = 262c95c

- **PRs 0–4 of the ADR-0004 login sequence are merged**: ADR + doc amendments (#271),
  session/message ownership + expiry IDOR fix (#272), security-response-headers + prod
  docs-disable (#274), persistent per-visitor cookie identity + migration `0007` (#275),
  Argon2id password-hashing module with no routes yet (#276). Every one of those PRs was
  mutation-tested and independently security-reviewed by a subagent before merge; #274 and
  #276 each had at least one real subagent-caught issue fixed before merge — expect the same
  discipline to keep finding things, not to be a formality.
- **Backend baseline on `main`: 1471 passed / 17 skipped**, run from the repo root on
  `provider=stub`. Re-run it yourself before trusting this number.
- **Nothing is deployed.** `v0.12.0` is still untagged (`docs/RELEASE_CANDIDATE_v0.12.0.md`,
  itself still owner-gated) — the live Fly deployment is whatever predates even the Wave 0–2
  work in this doc. That matters for sequencing below: there is no live production account
  data at risk *today*, but the one-way-door caution in the ADR is about what happens once
  this ships and real accounts exist, not about the current stale deploy. Confirm deploy
  status yourself (`fly status`, or ask the owner) rather than assuming either way.
- **10 open issues remain**, none are bugs: #270 (this umbrella tracker), #273 (deferred
  health-route-behind-admin, needs coordinated secret plumbing — do not fold this into PR 5–12
  work, it is out of scope for the login sequence), plus the pre-existing V1/V2 feature
  backlog. Re-run `gh issue list --state open` — do not trust this count.
- **Pre-existing worktrees/branches** under `.claude/worktrees/` and a few local branches with
  `[origin/...: gone]` are leftovers from *prior* sessions, not this one. Leave them — only
  clean up branches/worktrees your own session created and merged.

## Your authority

**May do without asking:** branch, open PRs, run the full local battery, merge your own PRs to
`main` once verified — for PRs 5, 6, 7, 8, 9, 10, and 11.

**Pause and report back before starting PR 12** (GitHub OAuth): it adds a third-party
credential flow (`state`/PKCE, redirect-URI validation, a `user_identities` table) that is
worth a design sanity-check with the owner before implementation, not just before merge.

**Must not do without the owner, same as every prior wave:** `fly deploy` (classifier-blocked
by design); push a version tag; any paid API call or real provider key (stay on
`provider=stub`); force-push; `git clean -fdx`; `git stash -u`; delete any branch you did not
create this session.

## PR-by-PR notes (see the ADR and plan for full detail — this is sequencing guidance, not a spec)

- **PR 5** (migration `0008`, `users` identity columns, `sessions.user_id` FK
  RESTRICT→CASCADE): additive/reversible on its own — nullable columns, no data yet. Prove the
  round trip the same way `0007`'s test does (`test_migrations.py`), against both SQLite and
  the real-Postgres CI job. The danger this migration sets up is not in PR 5 itself; it is that
  **after PR 6 populates real accounts, `downgrade 0008` destroys them** — record that caution
  in the PR body so it is visible to whoever reads it later, not just in the ADR.
- **PR 6** (`/v1/auth/{register,login,logout,me}`, claim-on-login, auth rate limiters,
  `AuditAction.login` emission): the first PR that can create a REAL second principal. This is
  where the PR 1 ownership predicate and the PR 3 cookie resolver earn their keep — write the
  actual two-real-account IDOR test the plan calls for (account B cannot read account A's
  session), not just the anonymous-principal version PR 3 already has. Use `verify_password_or_dummy`
  from PR 4 on the login path, not `verify_password` directly, or the timing-oracle work from PR 4
  was wasted. This PR is large enough to warrant the Workflow tool for its review phase (T3 on the
  AGENTS.md blast-radius table: security-review + adversarial verify-per-finding, closing with
  release-readiness-review) — you have Workflow available and should use it here rather than a
  single sequential pass.
- **PR 7** (frontend honest copy): ships value alone even if the rest slips — low risk, can go
  out ahead of or independent of 8–11 if that's convenient.
- **PR 8–9** (frontend authStore/useAuth/AuthModal, session claim wired client-side): needs a
  real browser check via the `webapp-testing` or `claude-in-chrome` tooling, not just Playwright
  assertions — the plan calls for focus-trap/Escape/focus-restoration behavior that is easy to
  get structurally right and behaviorally wrong.
- **PR 10** (`GET /v1/me/sessions` + history drawer): needs citation hydration fixed first
  (`_message_payload` returns no citations despite its docstring, per the plan) — check whether
  that's still true before assuming it's done.
- **PR 11** (per-user rate tiers): small, high value-to-effort — the plan estimates ~10 lines.

## How to work — same discipline as PRs 0–4, do not relax it

- **Verify → implement → document. No claim without a check.**
- **Run pytest from the repo root:** `env -u CITEVYN_DATABASE_URL uv run --project backend pytest backend/tests -q`. Lint from `backend/`: `ruff check . && ruff format --check . && pyright`.
- **Size review by blast radius** (AGENTS.md table). PR 6 and PR 12 are T3; PR 7/11 are more like T1–T2.
- **Cap routine review at two rounds.** Only reproduced CRITICAL_BLOCKER/REQUIRED_CONTRACT findings block.
- **Mutation-test every guard you add**, and `grep` to confirm the mutation actually landed before trusting a red result — `ruff format` silently un-applies some hand-edited mutations.
- **Every test ships with one line naming the exact change that turns it red.**
- **The merge gate:** before merging anything touching the answer pipeline, ask in the PR body whether it changes what gets cached — this sequence mostly doesn't touch the answer pipeline, but PR 10 (citation hydration) might.
- **Run an independent subagent security review on every PR that touches auth, cookies, passwords, or migrations** — that's most of PRs 5, 6, 8, 9, 12. Do not skip it because the last four came back clean; PR 2's review found a real gap, PR 6 is bigger than any of PRs 1–4.
- **Serialize merges.** Every PR edits `docs/BACKLOG.md`'s #270 row — parallel merges conflict by construction.
- **$0.** Stay on `provider=stub`. The judged eval self-skips without a key — that is correct, do not "fix" it.

## Definition of done

Merged **and** verified. Green on a branch is not done. Never close #270 or its checklist items
whose fix sits on an unmerged branch. Keep `docs/BACKLOG.md` and `gh issue view 270`'s checklist
in sync with reality in the same change that merges each PR.

Finish with: **Done / Verified myself / Cleanup / Pending / Next action** — separating what
*you* ran (with output) from what a subagent reported, and stating explicitly what is merged
versus what is actually running in production (still nothing, until the owner deploys).
