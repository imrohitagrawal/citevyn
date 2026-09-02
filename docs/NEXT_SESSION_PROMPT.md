# Next-session prompt (copy everything below the line)

---

Fresh-context continuation for CiteVyn's ADR-0004 login sequence. Implement **PR 14 (magic-link
login + Resend email + authenticated set/change password)** in this session, as its own branch, its
own full build+review+merge cycle. **Do not start anything beyond PR 14.** PR 13 (account linking)
is merged; do not re-open it.

Repo: `/Users/rohitagrawal/Projects/citevyn`. Before planning anything, read `AGENTS.md`,
`code_review.md`, `docs/BACKLOG.md`, `docs/ADR/0004-user-accounts.md`, `docs/API_SPEC.md` §4a/§4b,
run `gh issue list --state open`, and read the full plan at
`~/.claude/plans/can-you-please-do-abundant-wilkinson.md` — its **second** major section
("Magic-Link Login + Resend Email Integration — ADR-0004 PR 14") is the spec; the first section
(PR 13) is done and is useful only as the worked example of how a PR in this sequence gets built.
The plan's "Review record" already went through 2 adversarial rounds — read it for the *why*, do
not re-litigate it: no separate email password-reset flow; `GET` confirm renders an interstitial
and only a real `POST` consumes the token (no auto-submit JS, no meta-refresh); the timing-oracle
fix is equal-cost work on BOTH branches (a dummy write, not "return before the background task");
a dedicated magic-link rate bucket, never `enforce_auth_login_rate_limit`; whether
`current_password` is required is decided ONLY from the server-loaded `user.password_hash`, never
from body-field presence; any password set/change revokes the user's other sessions; no
session-binding on the magic-link token (cross-device by design). Re-verify every fact below
against the repo yourself before acting on it — this block is a snapshot, not a source of truth.

## Where things stand — verified 2026-09-02

- **PR 13 is MERGED** as PR #291, main `88c6cf3`, branch deleted, all four post-merge workflows on
  main green. **Nothing from PRs 5–13 is deployed** (`fly deploy` is owner-only) — merged, not
  running in production. **First action this session:** confirm `main` is at or past `88c6cf3`,
  `git pull --ff-only`, and check `git rev-parse main origin/main` agree BEFORE branching (a prior
  PR once picked up sibling commits because local main was ahead of origin).
- **Baselines to re-measure yourself before changing anything** (run them in the background while
  you read): backend `cd backend && uv run pytest -q` was 1553 passed / 18 skipped / 6 failed — the
  6 are pre-existing and unrelated (3 come from a local `backend/.env` holding real OAuth keys that
  pydantic-settings feeds into `Settings()` when a test `delenv`s a var; in your own tests override
  with `monkeypatch.setenv(..., "")` instead of `delenv`). Frontend `npm test` was 158/158;
  `npm run build` main chunk was **63.40 kB gzip against a 63.5 kB hard line** — every new UI in PR
  14 must ride the already-lazy `AuthModal` chunk (the plan says so); measure after, and report the
  number.
- **Open follow-ups to respect, not fix** (all in `docs/BACKLOG.md`): #286 SQLite never enforces FK
  constraints in this test suite, so a migration's FK/CASCADE behaviour is only proven on real
  Postgres; #288 `/me`'s `anonymous` flag is derived from `email is None` (PR 14 adds
  `has_password` to that same payload — do not reintroduce an email-based derivation, and do not
  fix #288 here unless it blocks); #289 OAuth env vars are documented in no env example — PR 14 adds
  `CITEVYN_RESEND_API_KEY`-class settings, so put YOUR new vars in `.env.example` and the README env
  table even though the OAuth ones aren't there yet; #290 `HistoryDrawer` focus gap (mirror the
  focus-on-open pattern from `ConnectedAccountsDrawer.tsx` in any new dialog surface).
- **What PR 13 left in the code that PR 14 touches:** `GET /v1/auth/me` (+register/login) returns
  `providers: list[str]` from `_auth_user_payload` in `backend/app/api/routes/auth.py` — add
  `has_password: bool` there so all three routes stay consistent, and extend
  `frontend/src/lib/types.ts` `AuthUserResponse` (non-optional). `AccountMenu.tsx` has a third
  menuitem opening the lazy `ConnectedAccountsDrawer` — the plan names that drawer as the natural
  home for a "Set a password" entry point. `app.core.auth_sessions` owns session mechanics — a
  bulk "revoke all other sessions for user" helper belongs there. `backend/app/core/oauth_http.py`
  is the model for a zero-app-import seam; `email_client.py` and `token_secrets.py` must satisfy
  the same `grep "^from app\." <file>` check (one request-id logging import is the tolerated
  exception).
- **Local live-verification facts** (they cost time to rediscover): a stale `uvicorn` from a prior
  session may still hold `:8000` — `lsof -nP -iTCP:8000 -sTCP:LISTEN` first, replace it. The
  backend serves the built frontend only if `backend/frontend_dist` exists — `npm run build`, then
  `ln -s ../frontend/dist backend/frontend_dist`, run uvicorn on `:8000`, and drive
  `http://localhost:8000/` in Chrome (NOT the Vite dev server: every server-issued redirect lands on
  `:8000`). Delete the symlink at cleanup (it is untracked residue). The docker DB is `citevyn-db`
  (`docker exec citevyn-db psql -U citevyn -d citevyn -c ...`) and is at migration head `0011`;
  `alembic` runs as `uv run --project backend alembic -c db/alembic.ini ...` from the repo root.
  `LandingPage.authToast.test.tsx` cases sit near the 5 s vitest timeout and fail under load — rerun
  on an idle machine before calling one a regression.

## Your authority

**May do without asking:** branch from `main`, implement, run the full local battery, open the PR,
merge once verified — same as PRs 0–13. The plan is owner-approved, so the schema change it
specifies (one new `magic_link_tokens` table, `user_id` FK `ondelete="CASCADE"`) is approved; any
schema change the plan does NOT specify needs the owner.

**Must not do without the owner:** `fly deploy`; push a version tag; create a Resend account, add
or change DNS on `stackclimb.com`, or use any real email-provider API key — the moment the work
needs a real key or a verified sending domain, **stop that thread and ask**, finishing everything
that doesn't depend on it; any paid API call (stay on `provider=stub`, $0); force-push (append fix
commits instead); `git clean -fdx`; `git stash -u`; delete any branch you did not create this
session; touch `imrohitagrawal/repo-template`.

## What PR 14 actually is — the plan has the detail, this is the shape

- `backend/app/core/email_client.py` (a minimal `Protocol` + `ResendEmailClient`, `httpx`, no new
  dependency) and `backend/app/core/token_secrets.py` (pure `generate/hash/verify`, stdlib only).
  **Decide and record how a magic link is obtained locally without a provider** — recommended: a
  dev-only file-outbox `EmailClient` that writes the rendered email under a local path when
  `environment != "production"`, refused by a `Settings` validator in production. **Never log the
  token** — `AGENTS.md` forbids logging tokens even in dev, and a magic-link token IS the credential.
- Migration for `magic_link_tokens` (additive, one table); issuing a new token deletes the user's
  prior live ones; 10-minute TTL.
- `POST /v1/auth/magic-link/request` (always 202, equal-cost branches, own rate bucket),
  `GET /v1/auth/magic-link/confirm` (interstitial, read-only), `POST .../confirm` (atomic
  `DELETE … RETURNING` claim, then the existing `claim_and_login`, redirect `/?auth=ok`),
  `POST /v1/auth/me/password` (server-decided current-password requirement, revokes other
  sessions), `has_password` on `/me`.
- Frontend: `AuthModal` modes `magic-link` and `set-password`, an in-modal `role="status"` notice,
  the one-time generic nudge after a passwordless login with a `localStorage` dismiss, and
  `maxLength={128}` on the password fields.
- 14 backend test scenarios are listed in the plan; PR 13 shipped 20 for its 14 — expect the same.

## How to work — the discipline PRs 0–13 actually followed, do not relax it

- **Verify → implement → document. No claim without a check.** Every number in your report is one
  you measured this session.
- **Order:** baselines → implement backend with tests → frontend with tests → mutation-test →
  docs → commit → **live browser walkthrough** → review → fixes → second review round → gate → PR.
  The live walkthrough comes BEFORE review, not after: PR 13's only real bug (a declined consent
  reported as "Sign-in failed") was invisible to 45 mocked tests and found in Chrome in two minutes.
  For PR 14 that means, against local Postgres with direct DB queries: request a link, open the
  interstitial with a plain GET and prove the token row is still there, `curl` the GET as a
  "scanner" and prove it consumed nothing, submit the POST and prove login + row deleted + no
  `password_hash` change, set a password and prove the other session's cookie now 401s, change it
  with `current_password` omitted and prove the 422. Use the Chrome tools; register throwaway
  accounts on localhost; **never enter the owner's real credentials and never click an OAuth
  Authorize/consent button** — those are the owner's.
- **Tests:** `cd backend && uv run pytest -q`; `ruff check . && ruff format --check . && pyright`;
  frontend `npm test`, `npx tsc --noEmit`, `npm run build`. Because this PR has a migration, ALSO
  run the postgres-marked tests locally against `citevyn-db` (`make test-pg` with
  `CITEVYN_PG_TEST_URL`) and prove `downgrade` works — SQLite cannot see an FK mistake (#286, and
  the `_mint_principal` bug PR 12 found only on Postgres). Any new route must be added to the
  README §8 endpoint table or `tests/test_readme_endpoints.py` fails; any new audit `metadata.event`
  string must be added to `docs/API_SPEC.md`'s event list.
- **Mutation-test every guard:** sequentially in the one working tree (never fan out mutating
  agents — they overwrite each other), restore from a byte copy of the file rather than
  `git checkout`, and assert the mutation actually applied before trusting a "survived" (ruff-format
  can silently un-apply a mutation and produce a false survival). Every test's docstring names the
  exact change that turns it red. Pay particular attention to the plan's tests 2, 5 and 11 — they
  guard the two CRITICAL findings the planning review caught.
- **Blast radius T3.** Review with the Workflow tool as PR 12/13 did: independent lenses
  (security/adversary, correctness, silent-failure, tests, frontend/a11y/bundle, docs-contract,
  simplicity) → 3 perspective-diverse skeptics per finding, default-refute → synthesis, then the
  `release-readiness-review` skill as the ship/no-ship gate before merge. Pass sub-agents the
  absolute repo path and make them read-only. **Do not edit the tree while a review is running** —
  skeptics read the live files. Classify per `AGENTS.md`: only reproduced CRITICAL_BLOCKER /
  REQUIRED_CONTRACT findings block; reproduce before fixing. **Every fix commit gets its own skeptic
  round** (3 agents over the fix diff was enough for PR 13). Cap at two routine rounds. Every
  surviving ADVISORY item you don't fix becomes a GitHub issue AND a `docs/BACKLOG.md` row in the
  same change; refuted findings are recorded in the PR body as considered.
- **Docs in the same change:** `docs/API_SPEC.md` (new §4c for magic-link + password, `/me` shape),
  `docs/ADR/0004-user-accounts.md` (this PR reopens the "no password reset" trade-off — record the
  new decision and its residuals in the existing style, and add PR row 14), `docs/SECURITY_MODEL.md`
  recovery wording, README endpoint table, `.env.example` for the new settings, and the `#270` row
  in `docs/BACKLOG.md`.
- **Git:** one PR; commit messages without the Claude Code attribution footer; plain squash-merge
  as a sole command once CI is green (`--auto` and compound merge lines are classifier-blocked);
  delete the merged branch local + remote; finish with local `main` equal to `origin/main` and a
  clean tree; delete only residue you created (the symlink, servers you started, scratch files).
- **$0.** `provider=stub` for anything LLM-shaped; no Resend key exists — the email path is verified
  with the dev outbox and a unit-tested `httpx.MockTransport` client.

## Definition of done

Merged **and** post-merge CI green on `main`. Green on a branch is not done. Then **stop** — do not
start the next item. Finish with **Done / Verified myself / Cleanup / Pending / Next action**,
separating what *you* ran (with output) from what a subagent reported, stating explicitly what is
merged versus what is running in production, listing what needs the owner (Resend account, DNS,
deploy), and ending with "nothing pending — safe to close this session" in those words if that is
true.
