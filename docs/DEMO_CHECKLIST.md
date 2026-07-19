# Demo Readiness Checklist — CiteVyn

This checklist is the **single source of truth** for what the demo
must demonstrate and the gate the team uses to decide the build is
"demo-ready". Every box must be ticked before recording the demo
video or opening the invite-only waitlist link.

Last verified against: `main` @ v0.10.0 + #168 (52-case golden suite green).

Every command, URL and route below was re-verified against the code on
2026-07-20 (#168). If you find one that is wrong, fix it in the same PR —
a checklist that fails on its own first step trains people to skip it.

---

## 1. Pre-flight (run on the day, ~5 min)

- [ ] `git status` — clean tree, branch is `main`, latest commit is the
      tagged release (`vX.Y.Z`).
- [ ] `git fetch --tags && git tag --list 'v*' --sort=-version:refname`
      confirms `vX.Y.Z` is the HEAD.
- [ ] `make demo` succeeds end-to-end on a clean laptop. It is
      `db-up + migrate + seed`: it starts **Postgres + Redis only**
      (`up -d db redis`, no app containers, so there are no app image
      layers to bust), runs `alembic upgrade head`, and seeds demo
      users + the catalog. It does **not** start the API or the
      frontend — the next two boxes each need their own process.
      To build the app images instead, that is `make image-smoke`.
- [ ] API up: `cd backend && uv run uvicorn app.main:app --reload` in
      its own shell, then `curl http://localhost:8000/health` returns
      `{"status":"ok"}`. (There is no `/healthz` route — the endpoints
      are `/health`, `/health/dependencies`, and `/health/index`.)
- [ ] Frontend up: `cd frontend && npm run dev`, browser open at
      `http://localhost:3000` (the Vite dev server binds **3000**, see
      `frontend/vite.config.ts`; it is also the only origin in the
      backend's default CORS allow-list). `npm run preview` — the
      built-bundle server, not the demo path — binds 4173 instead.
      Confirm the demo banner shows "demo build" and not "preview".

## 2. Functional acceptance (Slice 1–8 must be green)

Route names below are the live FastAPI paths; cross-check against
`http://localhost:8000/openapi.json` if one ever looks wrong.

- [ ] **Slice 1** — `GET /health` returns 200 (there is no `/healthz`).
- [ ] **Slice 2** — the seeded catalog covers the 4 product areas plus
      the `citevyn` meta source. **No route exposes the catalog** — there
      is no `GET /v1/products` — so assert it at the DB (dev
      credentials, same as the Makefile's `DB_URL`):

      ```bash
      psql postgresql://citevyn:citevyn@localhost:5432/citevyn \
        -c "select distinct product_area from documents order by 1"
      ```

      returns `citevyn, claude_api, claude_code, codex, gemini_api`.
- [ ] **Slice 3** — `POST /v1/sessions` creates a session; `GET
      /v1/sessions/:id` returns the session metadata *plus* the ordered
      message list. (There is no *collection* `GET` under
      `/v1/sessions/:id/messages` — that path is `POST`-only; the only
      messages `GET` is the single-message citation hydrator
      `GET /v1/sessions/:id/messages/:message_id`.)
- [ ] **Slice 4** — `POST /v1/search/exact` finds
      `CLAUDE_API_RATE_LIMIT` and `--model`.
- [ ] **Slice 5** — `POST /v1/sessions/:id/messages` returns a
      grounded answer with at least one `[1]` citation.
- [ ] **Slice 6** — `POST /v1/admin/index_versions/:index_version/promote`
      (the only admin `POST`) requires the admin key; without it, 401.
- [ ] ~~**Slice 7** — `POST /v1/sessions/:id/messages/stream` streams
      SSE chunks and a `final` event with a `request_id`.~~
      **N/A as of v0.10.0** — there is no streaming route on `main`
      (`messages.py` exposes only POST/GET; no `StreamingResponse` /
      `text/event-stream`). The chat UI uses a client-side reveal.
      Real SSE streaming is tracked as [#61](https://github.com/imrohitagrawal/citevyn/issues/61)
      in the V1 milestone; re-enable this check when it lands.
- [ ] **Slice 8** — the 31st demo-user request returns 429 within the
      same hour (`rate_limit_demo_user_per_hour`, default 30; admin is
      100). Redis-backed limiter is the active impl — see §4.

## 3. Quality gates

- [ ] `make lint` is green (ruff + format).
- [ ] `make typecheck` is green (pyright strict on `backend/app`).
- [ ] `make test` is green (unit + route tests against in-memory SQLite).
- [ ] **`make golden` is green (52/52 cases pass).** Cases live in
      `tests/golden/cases/` at the **repo root** (not under `backend/`);
      the report is written to `backend/artifacts/golden_report.json`,
      which is **gitignored** (`artifacts/` in `.gitignore`) — attach it
      to the release, don't expect it in the tree. This is the *demo
      canary* — if any case flips red, treat as a release blocker.
- [ ] `make smoke` is green (curl-based happy path against uvicorn,
      `scripts/smoke.sh`).
- [ ] `make e2e` is green. **Caveat:** `make e2e` currently just re-runs
      `scripts/smoke.sh` — it is *not* the Playwright suite. (The
      target's banner points at `docs/adr/0004-frontend-ci.md`, which
      does not exist; `docs/ADR/` stops at 0003.) Run the real browser
      suite separately: `cd frontend && npm run test:e2e`
      (config: `frontend/playwright.config.ts`).

## 4. Observability + safety

- [ ] Every request carries an `X-Request-ID` response header
      (`Settings.request_id_header`) whose value also appears as
      `request_id` in the response body and in the structured log line.
- [ ] Production-mode env var guard: starting uvicorn with
      `CITEVYN_ENVIRONMENT=production CITEVYN_LLM_PROVIDER=stub` exits
      with the `not allowed in production` error (verify once).
- [ ] Production `CITEVYN_LLM_PROVIDER=""` (router placeholder) also
      rejects at startup.
- [ ] Default admin key `local-admin-key` is rejected in production.
- [ ] Redis is reachable from the `api` container and
      `CITEVYN_REDIS_URL` is set, so `get_limiter()` selects
      `RedisRateLimiter` and not the in-process fallback. Rate-limit
      state then survives a restart — verify with two consecutive
      `curl` floods either side of `docker compose restart api`.
      **If `CITEVYN_REDIS_URL` is unset the limiter is the in-process
      dict and the count resets on every restart**, which reads as
      "the limiter is broken" during a demo.

## 5. Docs + repo hygiene

- [ ] `README.md` §13 "Demo Build Status" shows `🟢 green` (not amber),
      **and** its golden-case count matches what `make golden` actually
      printed. (At the time of #168 the README still said 50/50 while
      the suite ran 52/52 — the two drift independently.)
- [ ] `CHANGELOG.md` top entry is the demo cut (`v0.10.0`).
- [ ] `docs/DEMO_CHECKLIST.md` (this file) is up to date.
- [ ] `docs/DEPENDABOT_TRIAGE.md` is up to date.
- [ ] No `release-blocker` labeled dependabot PR is open. If one is,
      the demo cannot ship until it's either merged or explicitly
      waived in writing.

## 6. Live gate — deploy-verify + rollback drill (one command)

Run this **on the deploy host** against the real stack. It is the gate that
satisfies `RELEASE_PLAN` §10 blocker 9 ("rollback is not tested"):

```bash
VERSION=v0.10.0 PREV_VERSION=v0.9.0 make deploy-verify
```

It backs up, deploys the target, functionally verifies the *deployed* system
(cited answer, refusal, exact lookup, admin protected), rolls back to the
previous tag and re-verifies, then rolls forward and re-verifies. It prints a
PASS/FAIL summary and exits non-zero on any failure.

- [ ] `make deploy-verify` exits 0 with `RESULT: ✓ GATE PASSED`.
- [ ] `git tag` records the previous green tag.
- [ ] `make deploy` is documented to a fresh VM.
- [ ] The drill was executed within the past 14 days.

Preview the plan without touching anything:
`./infra/docker/scripts/deploy_verify.sh --dry-run`.
Standalone incident rollback: `make rollback TAG=v0.9.0` (or `TAG=--previous`).

## 7. Demo script alignment

- [ ] The recorded demo uses **only** queries that pass the golden
      suite, and the script is frozen 24 h before the recording.
      ~~The full script lives in `docs/DEMO_SCRIPT.md`.~~
      **NOT YET WRITTEN** — `docs/DEMO_SCRIPT.md` does not exist on
      `main`. Until it does, the frozen script is whatever the recording
      owner circulates; write the file before the next demo cut so this
      box has something to point at.
- [ ] No "live debugging" steps in the script — every transition is
      scripted and rehearsed.
- [ ] The 5-question canned demo: rate limit, exact term, multi-turn,
      off-topic, permission.

## 8. Post-demo follow-up

- [ ] If the demo surfaces a regression, open a `release-blocker` issue
      *before* the next business day.
- [ ] Bump the version to the next pre-release (`0.11.0rc1`) and re-run
      this checklist. The version lives in **`backend/pyproject.toml`**
      (`version = "0.10.0"`) — there is no `version.txt` in this repo.
      `frontend/package.json` carries its own, independent `0.1.0` and
      is deliberately not kept in lockstep.
- [ ] Schedule a 30-min retro within 48 h to capture what landed and
      what surprised us.

---

### Why this checklist exists

The demo is the *only* artefact most stakeholders will see. Every
ticket that ships before the demo must answer "does this advance a box
on this checklist?" If it doesn't, defer it to the post-demo sprint —
the demo build is frozen.

### When to deviate

Don't. If a box cannot be ticked, push the demo date. The 52-case
golden suite exists precisely so that we have a hard, falsifiable gate
between "looks fine" and "is fine".
