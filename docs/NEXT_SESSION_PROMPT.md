# Next-session prompt (copy everything below the line)

---

Fresh-context continuation for CiteVyn. Work the owner's production QA findings from 2026-09-03,
**one issue per PR, in this order, each fully built + reviewed + merged before the next starts:**
#300 (self-referential questions), #301 (magic-link send cooldown), #302 (scroll to an existing
answer from the landing sections), #303 (answer rendering: markdown subset + clickable citations).
**Do not start anything beyond these four.** PRs 14 and 15 of ADR-0004 are merged AND deployed
(Fly release v9); do not re-open them.

Repo: `/Users/rohitagrawal/Projects/citevyn`. Before planning anything, read `AGENTS.md`,
`code_review.md`, `docs/BACKLOG.md`, the four issues (`gh issue view 300 301 302 303` — each carries
the owner-agreed fix shape; build THAT, do not redesign), `docs/API_SPEC.md` §4a/§4c/§6, the
session memory notes, and run `gh issue list --state open`. Re-verify every fact below against the
repo before acting on it — this block is a snapshot, not a source of truth.

## Where things stand — verified 2026-09-03

- `main` is at `756b45a` (docs) on top of `f3e64ca` (ADR-0004 PR 15, PR #298). Production
  (https://citevyn.stackclimb.com, Fly app `citevyn`) runs **release v9 = `f3e64ca`**, migration head
  `0013` on Neon, all 9 secrets deployed, Resend sending from `login@mail.stackclimb.com`. The owner
  verified magic-link sign-in live. **First action:** `git rev-parse main origin/main` must agree
  before branching; `git pull --ff-only`.
- **Baselines to re-measure yourself before changing anything** (run them in the background while you
  read): backend `cd backend && uv run pytest -q` was 1630 passed / 20 skipped / **6 failed — the 6
  are pre-existing and unrelated** (a local `backend/.env` holds real keys that pydantic-settings feeds
  into `Settings()` when a test `delenv`s a var; in your own tests override with
  `monkeypatch.setenv(..., "")`, never `delenv`). Frontend `npm test` was 186/186; `npm run build`
  main chunk was **198.35 kB / 63,495 B gzip against a 63,500 B line** (measure the exact bytes with
  node `zlib.gzipSync` on `dist/assets/index-*.js`; Vite's display rounds). The line is prose, not CI
  (#296). #303 will need bytes: if the ceiling must move, raise it deliberately in that PR and record
  the new number in BACKLOG #270 — do not squeeze by renaming files. Every other PR must not grow the
  eager chunk; new UI rides the lazy chunks (`AuthModal`, `Nudge`, the drawers).
- **Open follow-ups to respect, not fix:** #294 (auth advisories), #296 (mechanical bundle/deploy
  checks), #286/#288/#289/#290 (older). Cross-check BACKLOG.md before planning.
- **What the last two PRs left that these touch:** `useLandingState.send` already has the
  duplicate-question guard (`flashExisting`) — #302 is a mount-timing bug in the screen switch, not a
  missing feature. `ChatView` renders answer text as plain text; `citations[].marker` is on the wire
  and `lib/citations.ts` numbers cards by it — #303 builds on that. The greeting short-circuit
  (`is_greeting`, PR #90) is the pattern for #300; the CiteVyn topic is keyword-detected in
  `backend/app/guardrails/domain.py`. The magic-link request bucket trio in
  `backend/app/core/rate_limit.py` (`_MAGIC_LINK_ROLE`, `magic_link_rate_key`,
  `enforce_magic_link_rate_limit`, threaded through BOTH limiters and `_settings_match`) is the exact
  template for #301's 60-second bucket; the request route's **statement-count parity** white-box test
  (`test_request_runs_the_same_statement_count_whether_or_not_the_email_exists`) must stay green.
- **Local live-verification facts:** the docker DB `citevyn-db` is at migration head `0013`;
  `alembic` runs as `CITEVYN_DATABASE_URL="$(grep '^CITEVYN_DATABASE_URL=' backend/.env | cut -d= -f2-)"
  uv run --project backend alembic -c db/alembic.ini upgrade head` from the repo root. A stale `uvicorn`
  may hold `:8000` — `lsof -nP -iTCP:8000 -sTCP:LISTEN` first. The backend serves the built frontend
  only if `backend/frontend_dist` exists: `cd frontend && npm run build`, `ln -s ../frontend/dist
  backend/frontend_dist` (git-ignored; delete at cleanup), then from `backend/`
  `CITEVYN_EMAIL_OUTBOX_DIR=<scratchpad>/outbox uv run uvicorn app.main:app --port 8000` and drive
  `http://localhost:8000/` (NOT the Vite dev server). Magic-link emails land as files in that outbox.
  The Claude-in-Chrome extension was NOT connected last session; the fallback that worked is
  Playwright's Chromium from `frontend/node_modules` (`import { chromium } from
  "/Users/rohitagrawal/Projects/citevyn/frontend/node_modules/@playwright/test/index.mjs"` in a
  scratchpad `.mjs`), driving the real site with direct `docker exec citevyn-db psql` checks between
  steps. Register throwaway accounts on localhost only; never the owner's credentials; never click an
  OAuth Authorize button. Delete throwaway rows at cleanup.
- **Shell gotchas that cost time:** the Bash tool's cwd persists across calls — always `cd
  /Users/rohitagrawal/Projects/citevyn && …` or use absolute paths; zsh treats a leading `=` in an
  argument (`echo ====X====`) as an expansion and fails; `uv run` must run from `backend/` or with
  `--project backend`. `vitest` cases near the 5 s timeout fail under machine load (a parallel `npm ci`
  or headless Chrome from another session) — rerun the suite alone on an idle machine before calling
  one a regression; the `LandingPage.*` files are the sensitive ones.

## Your authority

**May do without asking:** branch from `main`, implement, run the full local battery, open the PR,
merge once verified — same as PRs 0–15. **Must not do without the owner:** `fly deploy` (ask for a
"go" per PR, or batch the four and ask once at the end — say which you recommend); push a version
tag; touch Resend/DNS or any real API key; any paid API call (stay on `provider=stub`; for #300's
golden cases the judged eval self-skips on stub, do NOT set a real key); force-push (append fix
commits); `git clean -fdx`; `git stash -u`; delete any branch you did not create this session (five
older local `feat/adr0004-pr*` branches exist — leave them). If a deploy is authorised: the runbook
command in `docs/DEPLOY_FLY.md` §4.1 with BOTH build args, wake the scaled-to-zero machine with
`curl /health` first (or the key read is empty), and verify the served bundle has 0 hits for
`local-demo-key` afterwards — a deploy without `VITE_API_DEMO_KEY` broke production for an hour.

## The four items — the issues carry the detail, this is the shape

- **#300** (backend, small, T2): closed list of self-referential phrasings → the About CiteVyn
  retrieval path, same short-circuit family as `is_greeting`; anchored regex, a negative for
  "who are the Codex maintainers"; golden eval cases via the `rag-eval` skill (hit-rate only on stub).
- **#301** (backend + frontend, small, T3 because it is auth-adjacent): a 1-per-60 s per-address
  bucket applied on both branches; 60 s countdown on the modal button driven by a mockable timer;
  inline 429 copy.
- **#302** (frontend, small, T2): `ChatView` owns the scroll on mount for a pending highlight; the
  landing entry points hand over to the chat once a conversation exists (owner decision, recorded in
  the issue). Must be verified live — jsdom cannot see mount timing.
- **#303** (frontend + one prompt line in the backend, medium, T2): markdown-subset renderer with an
  escape-everything-else test, citation chips linked to sources, cards collapsed per document, a
  once-per-session legend, plain `[n]` preserved in persisted/copied text. Measure the bundle.

## How to work — the discipline PRs 0–15 actually followed, do not relax it

- **Verify → implement → document. No claim without a check.** Every number you report is one you
  measured this session.
- **Order per PR:** baselines → RED test → implementation → mutation-test every guard (sequentially in
  the one tree, byte-copy restore, assert the mutation applied — ruff-format can silently un-apply one)
  → docs (`docs/API_SPEC.md` for any contract change incl. every new audit `metadata.event`; README §8
  endpoint table or `tests/test_readme_endpoints.py` fails; `.env.example` + README §5 for any new
  setting; BACKLOG row) → commit → **live browser walkthrough BEFORE review** (both PR 14's and PR 15's
  only real bugs were found live, not by the hermetic tests) → review → fixes → second skeptic round on
  the fix diff → release-readiness gate → PR → CI → plain squash-merge as a sole command → post-merge CI
  on `main` green → delete the branch local + remote → next item.
- **Review sizing:** T2 items get the Workflow tool with 4–5 read-only lenses (correctness, tests,
  frontend/a11y/bundle, docs-contract, + security for #301) → 3 perspective-diverse skeptics per
  finding (reproduce / impact / precedent, default-refute). Pass sub-agents the absolute repo path and
  make them read-only. **Do not edit the tree while a review runs.** Classify per `AGENTS.md`: only
  reproduced CRITICAL_BLOCKER / REQUIRED_CONTRACT block; reproduce before fixing; every fix commit gets
  its own skeptic pass (3 agents was enough); cap at two routine rounds; surviving advisories you do not
  fix become a GitHub issue comment (or a new issue) AND a `docs/BACKLOG.md` row in the same PR;
  refuted findings are recorded in the PR body as considered.
- **Git:** one PR per item; commit messages end with the `Claude-Session:` trailer and carry no
  "Generated with" footer; finish each item with local `main` equal to `origin/main` and a clean tree.
- **$0.** `provider=stub` for anything LLM-shaped. No real email key exists locally (the outbox is the
  delivery path).

## Definition of done

Each item: merged **and** post-merge CI green on `main`, with a live walkthrough recorded in the PR
body. After #303, **stop**. Finish with **Done / Verified myself / Cleanup / Pending / Next action**,
separating what *you* ran (with output) from what a subagent reported, stating explicitly what is
merged versus what is running in production (nothing you merge is deployed until the owner says "go"),
and ending with "nothing pending — safe to close this session" in those words if that is true.
