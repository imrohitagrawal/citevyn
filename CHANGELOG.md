# Changelog

All notable changes to CiteVyn are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Anaphoric follow-ups returned the previous answer verbatim (#169).** Conversation
  memory resolved a follow-up by CONCATENATION (`"What is Codex CLI? who built it?"`),
  and the leading clause is a complete self-contained question — so the LLM answered
  that and ignored the follow-up. Routing still uses the concatenation (it is what
  pulls a bare anaphor onto the right product domain); retrieval, generation and the
  cache key now get a condensed standalone question. Every multi-turn eval case now
  asserts its answer is not byte-identical to the previous turn's.

### Changed
- **`answer_policy_version` `v1` → `v2`** — invalidates every cached answer, which is
  how the answers poisoned by #169 are evicted. They sit under valid keys with a
  correct `source_version_hash` and `embedder_identity`, so nothing else would clear
  them.

  > **Rollback caveat.** Reverting past this release restores `v1` and brings those
  > poisoned rows back into key scope for the remainder of the cache TTL (default
  > 24h). When rolling back across this bump, set
  > `CITEVYN_ANSWER_POLICY_VERSION=v3` rather than accepting the reverted default —
  > a cold cache costs a refill, re-serving a known-bad answer is silent. See
  > RUNBOOK §5.3a; `infra/docker/scripts/rollback.sh` does the naive revert.

## [0.10.0] — 2026-07-19

MVP demo release. Closes the RAG quality plan (Phases 0–4), turns the vector
arm back on with real embeddings and real prod ingestion, hardens the
answer-quality eval into a CI gate, ships the live chat UI end-to-end, and
clears the Phase-5 release blockers (#81, #82, #87, #93).

### Added
- **RAG quality plan complete (`docs/RAG_QUALITY_PLAN.md`, Phases 0–4).**
  - Phase 0 — RAG eval harness (#96): golden set + retrieval hit-rate +
    opt-in LLM-as-judge, CI-gated (`make eval`).
  - Phase 1 — revived the dead vector arm (#97) with an OpenRouter/OpenAI
    `text-embedding-3-small` embedder behind the seam, embedding-aware
    seeders, and read-time index provenance; real prod ingestion ships
    source docs as worker package data (#92).
  - Phase 2 — answer-when-grounded (#107): global retrieval + confidence
    gate + an LLM grounding-refusal net, with a judged eval metric.
  - Phase 3 — multi-hop query decomposition by product domain (#109) and
    conversation memory that resolves anaphoric and content-noun follow-ups
    (#112, #113).
  - Phase 4 — graceful degradation: nearest-doc suggestions on in-domain
    no_answer (#117, 4a), a distinct 429 rate-limit toast (#116, 4b), and
    vector-arm health on `/health/index` (#115, 4c).
- **Real LLM + embeddings providers** (#47, #51/#56): Gemini primary +
  OpenRouter fallback for chat, real Gemini/pgvector embeddings, exact-lookup
  fallback, and CiteVyn-meta answers.
- **Live chat UI wired to the backend** behind a live/demo toggle (#45), plus
  the full React landing page and polished Q&A chat surface.
- **Answer-quality eval hardening**: a trustworthy judge (panel + adversarial
  veto + groundedness + prompt-injection resistance, #124/#126), enforced in
  CI behind the OpenRouter secret (#127); chunk-level retrieval identity with
  MRR/precision@1 (#132), a distractor corpus with context precision/recall
  (#133), and golden-set growth to 50 cases (#134).
- **CI image build+boot smoke gate** (#82): `make image-smoke` builds and
  *boots* the api (GET /health=200) and worker (`python -m app.worker.cli`)
  images as a PR gate and a release-publish gate, so a non-booting container
  can no longer ship green.
- RAG eval harness (Phase 0 of `docs/RAG_QUALITY_PLAN.md`, #96): a JSONL
  golden set at `tests/eval/golden.jsonl` (20 cases across all 5 product
  areas × {literal, paraphrase} plus out-of-corpus refusals) scored by
  two outcome metrics — retrieval hit-rate (hermetic, via the live
  `HybridRetriever` path) and an opt-in LLM-as-judge (1–5 vs an expected
  gist; reports "unavailable" under the stub, never a fabricated score).
  The CI gate (`backend/tests/test_eval_harness.py`) runs in the existing
  hermetic `pytest + lint` job and fails on retrieval regression, refusal
  leaks, degenerate golden sets, or a total/partial judge outage. Exposed
  via `make eval`. Baseline recorded in `RAG_QUALITY_PLAN` §8a (literal
  hit-rate 1.0, paraphrase 0.0 — the dead vector arm, #97).
- 50-case golden evaluation suite under `tests/golden/cases/` plus a
  runner module that boots the FastAPI app against the in-memory
  SQLite seed and exercises the full public surface. Exposed via
  `make golden` (full run) and `make golden-smoke` (3-case sanity).
- Production guard: `Settings._reject_stub_llm_in_production` now
  rejects the Slice 9b router placeholder (`CITEVYN_LLM_PROVIDER=""`)
  in addition to `"stub"`. Two new unit tests
  (`test_settings_constructor_rejects_empty_llm_provider_in_production`,
  `test_settings_constructor_accepts_empty_llm_provider_in_development`)
  pin the contract.
- `docs/DEMO_CHECKLIST.md` — single source of truth for what the demo
  must demonstrate and the gate the team uses to declare the build
  "demo-ready".
- `scripts/refresh_sources.sh` — operator script that runs
  `make refresh` and pipes the new docs index through the same path
  the prod worker uses. The script is idempotent and refuses to run
  with an unset `CITEVYN_REDIS_URL` so a partial refresh cannot leave
  the index in a half-built state.
- `docs/DEPENDABOT_TRIAGE.md` and the `release-blocker` repo label so
  dependabot PRs touching rate-limit, security, or DB-migration code
  cannot be auto-merged.
- Frontend CI (`.github/workflows/frontend-ci.yml`) that builds the
  Vite bundle, runs ESLint, type-checks, and uploads the build output
  as a workflow artifact.

### Changed
- `Makefile` now lists `golden`, `golden-smoke`, and `eval` (the RAG eval
  harness) in the developer workflow header. The `make demo` target
  resolves `demo-frontend` so the chat UI comes up alongside the API.
- `README.md` §13 ("Demo Build Status") flips from amber to green once
  the golden suite is green on the cut commit. The badge link now
  points at the latest nightly run.

### Fixed
- LLM model retirement (#99): the configured chat model `gemini-2.5-flash`
  is retired for new Google API projects (404 "no longer available to new
  users"), which broke — or silently forced onto the paid fallback —
  grounded-answer generation. The primary default is now
  `gemini-flash-latest` (free tier; alias auto-tracks the current Flash GA)
  and the OpenRouter fallback is `openai/gpt-4o-mini` — a *different*
  provider family, so one Google-side retirement can no longer take out
  both arms at once. Ordering is cost-driven (free primary, paid backstop).
  Live-verified end-to-end. Surfaced by the Phase 0 eval baseline run.
- `runner.py` (golden): the in-memory cache and the rate limiter were
  leaking state between cases. The runner now builds a fresh
  `TestClient` per case (configurable via
  `fresh_client_per_case=False`) and pins
  `CITEVYN_RATE_LIMIT_ENABLED=false` for the run.
- The `runner.py` CLI was wired to `--report-path` but the argparse
  flag is `--report`. Make targets corrected.
- `make demo` on a fresh clone: `${CITEVYN_ACME_EMAIL:?…}` aborted
  compose parsing on the caddy service even when caddy was behind
  the `prod` profile, and every service's `env_file: - .env`
  required the gitignored file to exist. The ACME interpolation now
  falls back to a dev default; `make db-up` bootstraps
  `infra/docker/.env` from `prod.env.example` with clearly-marked
  stub secrets; and `infra/docker/scripts/_env_guard.sh` is sourced
  by `deploy.sh`/`refresh.sh`/`backup.sh`/`make restore` to refuse
  any prod entry point while the stubs are still in place.
- Frontend live-mode e2e test was permanently skipped under the demo
  Playwright config because `state.pending` only goes true on the
  `sendLive` path. Added `frontend/vite.liveStub.ts` (in-process
  Vite plugin that stubs `/v1/sessions` and `/v1/sessions/*/messages`
  when `VITE_LIVE_STUB=1`), `frontend/playwright.live.config.ts`
  (companion config with `grep: /live only/i` plus `VITE_API_LIVE=true`
  so the previously-skipped loading-indicator test now runs and
  asserts), and `.github/workflows/frontend-live-e2e.yml` to wire it
  into CI on PRs that touch `frontend/**`. The demo config still
  skips the test (intended — the demo path is instant by design) and
  continues to gate merges via the 57-case demo suite.

## [0.9.1] — 2026-05-12

### Fixed
- Slice 9.1 follow-up: the `x-anthropic-billing-header` env var name
  was case-sensitive in code but lower-cased on Linux containers
  (imrohitagrawal/citevyn#11 follow-up, commit `4a01850`).

## [0.9.0] — 2026-04-30

### Added
- Slice 8: Redis-backed sliding-window rate limiter with a Lua
  `EVAL` script that does `ZREMRANGEBYSCORE` + a conditional
  `ZADD` + `EXPIRE` in a single round trip. Replaces the
  in-process limiter when `CITEVYN_REDIS_URL` is set.
- Slice 7: Server-Sent Events streaming on
  `POST /v1/sessions/:id/messages/stream`.

## [0.8.0] — 2026-04-02

### Added
- Slice 6: admin key + admin routes (`/v1/admin/products`, `/v1/admin/
  sources`, `/v1/admin/sources/refresh`).
- Slice 5: orchestrator + grounded answer shape with `request_id`,
  `domain`, `intent`, `confidence`, `retrieval_strategy`, and
  `citations[]`.

---

### Release procedure

1. Cut a release branch: `git switch -c release/vX.Y.Z`.
2. Update `version.txt` and `pyproject.toml`.
3. Add a fresh `[X.Y.Z]` section to this file, dated today.
4. Run `make lint && make typecheck && make test && make golden` and
   attach the `golden_report.json` to the PR.
5. Open the PR with the `release-blocker` label removed (re-add it
   after merge if a hot-fix is required).
6. On merge, tag: `git tag -a vX.Y.Z -m "vX.Y.Z — <one-liner>"`.
7. Push: `git push origin main --follow-tags`.
