# Demo Readiness Checklist — CiteVyn

This checklist is the **single source of truth** for what the demo
must demonstrate and the gate the team uses to decide the build is
"demo-ready". Every box must be ticked before recording the demo
video or opening the invite-only waitlist link.

Last verified against: `main` @ Slice 10 (50-case golden suite green).

---

## 1. Pre-flight (run on the day, ~5 min)

- [ ] `git status` — clean tree, branch is `main`, latest commit is the
      tagged release (`vX.Y.Z`).
- [ ] `git fetch --tags && git tag --list 'v*' --sort=-version:refname`
      confirms `vX.Y.Z` is the HEAD.
- [ ] `make demo` succeeds end-to-end on a clean laptop (no cached
      Docker layers).
- [ ] `curl http://localhost:8000/health` returns `{"status":"ok"}`.
      (There is no `/healthz` route — the endpoints are `/health`,
      `/health/dependencies`, and `/health/index`.)
- [ ] Browser open at `http://localhost:5173`, the demo banner shows
      "demo build" and not "preview".

## 2. Functional acceptance (Slice 1–8 must be green)

- [ ] **Slice 1** — `GET /healthz` returns 200.
- [ ] **Slice 2** — `GET /v1/products` returns the 4 product areas.
- [ ] **Slice 3** — `POST /v1/sessions` creates a session; `GET
      /v1/sessions/:id/messages` returns the seed history.
- [ ] **Slice 4** — `POST /v1/search/exact` finds
      `CLAUDE_API_RATE_LIMIT` and `--model`.
- [ ] **Slice 5** — `POST /v1/sessions/:id/messages` returns a
      grounded answer with at least one `[1]` citation.
- [ ] **Slice 6** — `POST /v1/admin/...` requires the admin key.
- [ ] ~~**Slice 7** — `POST /v1/sessions/:id/messages/stream` streams
      SSE chunks and a `final` event with a `request_id`.~~
      **N/A as of v0.10.0** — there is no streaming route on `main`
      (`messages.py` exposes only POST/GET; no `StreamingResponse` /
      `text/event-stream`). The chat UI uses a client-side reveal.
      Real SSE streaming is tracked as [#61](https://github.com/imrohitagrawal/citevyn/issues/61)
      in the V1 milestone; re-enable this check when it lands.
- [ ] **Slice 8** — the 31st demo-user request returns 429 within the
      same hour. Redis-backed limiter is the active impl.

## 3. Quality gates

- [ ] `make lint` is green (ruff + format).
- [ ] `make typecheck` is green (pyright strict on `backend/app`).
- [ ] `make test` is green (unit + route tests against in-memory SQLite).
- [ ] **`make golden` is green (50/50 cases pass).** Report is committed
      under `backend/artifacts/golden_report.json`. This is the *demo
      canary* — if any case flips red, treat as a release blocker.
- [ ] `make smoke` is green (curl-based happy path against uvicorn).
- [ ] `make e2e` is green (Playwright happy-path: chat page renders,
      a question returns an answer, the citation badge appears).

## 4. Observability + safety

- [ ] Every request carries a `request_id` header that is present in
      the response body and the structured log line.
- [ ] Production-mode env var guard: starting uvicorn with
      `CITEVYN_ENVIRONMENT=production CITEVYN_LLM_PROVIDER=stub` exits
      with the `not allowed in production` error (verify once).
- [ ] Production `CITEVYN_LLM_PROVIDER=""` (router placeholder) also
      rejects at startup.
- [ ] Default admin key `local-admin-key` is rejected in production.
- [ ] Redis is reachable from the API container; rate-limit state
      survives a worker restart (verify with two consecutive
      `curl` floods and a `docker compose restart api`).

## 5. Docs + repo hygiene

- [ ] `README.md` §13 "Demo Build Status" shows `🟢 green` (not amber).
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
      suite. The full script lives in `docs/DEMO_SCRIPT.md` and is
      frozen 24 h before the recording.
- [ ] No "live debugging" steps in the script — every transition is
      scripted and rehearsed.
- [ ] The 5-question canned demo: rate limit, exact term, multi-turn,
      off-topic, permission.

## 8. Post-demo follow-up

- [ ] If the demo surfaces a regression, open a `release-blocker` issue
      *before* the next business day.
- [ ] Bump `version.txt` and `pyproject.toml` to the next pre-release
      (`v0.11.0-rc.1`) and re-run this checklist.
- [ ] Schedule a 30-min retro within 48 h to capture what landed and
      what surprised us.

---

### Why this checklist exists

The demo is the *only* artefact most stakeholders will see. Every
ticket that ships before the demo must answer "does this advance a box
on this checklist?" If it doesn't, defer it to the post-demo sprint —
the demo build is frozen.

### When to deviate

Don't. If a box cannot be ticked, push the demo date. The 50-case
golden suite exists precisely so that we have a hard, falsifiable gate
between "looks fine" and "is fine".
