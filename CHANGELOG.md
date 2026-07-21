# Changelog

All notable changes to CiteVyn are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **The browser UI is served from the API at `/`.** `infra/docker/Dockerfile.api`
  gained a Node stage that builds `frontend/dist` (the bundle is gitignored, so a
  host-built copy would be absent or stale in a remote `fly deploy`), and
  `app/main.py` mounts it with `StaticFiles(html=True)` **after** every router.
  One origin serves both the UI and the API: no CORS, and the deployment stays one
  subdomain deep, which matters because Cloudflare's free Universal SSL covers only
  one level. A missing bundle is a no-op, so tests and local runs are unaffected.
  `VITE_API_LIVE=true` is passed explicitly at build time — anything else leaves the
  chat answering from its canned in-bundle `knowledgeBase`, a demo that looks
  perfect and never reaches the backend.

### Changed
- **`fly.toml` machine memory 256mb -> 512mb**, on the owner's explicit instruction
  for the first real deploy. The measured serving RSS is ~103 MiB; the peak is
  ingestion, not serving. `test_fly_config.py` pins the new value so the size (and
  the bill) cannot move silently.

### Fixed
- **`docs/DEPLOY_FLY.md` had four errors that only surface when you actually run
  it**, all found during the first live deploy:
  - `fly apps create` does **not** allocate IPs (only `fly launch` does). Without
    `fly ips allocate-v4 --shared` + `allocate-v6` the deploy succeeds, health
    checks pass, and every request fails `Could not resolve host` — which reads as
    a DNS or TLS fault and is neither. Added as §1b.
  - §2.2 said to use the `rediss://` TLS URL. Wrong for a Fly-provisioned Upstash
    database, which lives on Fly's private 6PN network (`fdaa:…`, unreachable from
    the internet) and has no public TLS endpoint. The section now documents
    `fly redis create` (no separate Upstash account), its TTY requirement, the
    $200/mo ProdPack prompt to decline, and records that Lua `EVAL` is **verified
    working** against a live instance with the real limiter script.
  - §4.4 said "ask a question through the UI" without saying where the UI was
    hosted — it was not hosted anywhere. Now points at `/` and documents the two
    request shapes that cost a debugging round-trip (`Authorization: Bearer`, not
    `X-Demo-API-Key`; body field `message`, not `content`), plus the fact that a
    refusal is often correct rather than a regression.
  - §5.1 recommended a `CNAME`; `fly certs add` asks for `A` + `AAAA`.

### Added
- **Index promotion is now gated on evaluation quality (#210).**
  `CITEVYN_INDEX_PROMOTION_MIN_PASS_RATE` (default `0.95`) had been declared
  since Slice 8 and read by nothing, while `RELEASE_PLAN` §7 and the deploy
  runbook described promotion gates as if they were enforced. `promote_version`
  now resolves the candidate's newest **completed** `EvaluationRun` (a `running`
  run is not evidence), reads `metrics["pass_rate"]` — falling back to
  `cases_passed / cases_total` — and refuses unless the measured rate is at
  least the threshold. A rate exactly equal to the threshold promotes. A
  candidate with no completed run, or with metrics that cannot be read, is
  refused as well: "unevaluated" is not "passing". The gate lives in the
  service, not the route, so every caller is gated.

  Refusals surface as a new error code **`promotion_blocked` → HTTP 409**
  (`docs/API_SPEC.md` §15), whose body names both the measured rate and the
  required threshold. `PromoteIndexResponse` gains `forced` and
  `measured_pass_rate`.

  > **Operator note.** Nothing in the deployed application writes
  > `EvaluationRun` rows — the evaluation service is read-only and the golden
  > suite runs on a laptop and in CI. So the promote step in
  > `docs/DEPLOY_FLY.md` §4.3 now returns **409** on a real stack, and so do the
  > corpus-correction promote (`RUNBOOK` §3.7) and the emergency index rollback
  > (`RELEASE_PLAN` §8). Use `POST .../promote?force=true`, which promotes
  > anyway and records `force`, `measured_pass_rate`, `threshold` and
  > `evaluation_run_id` in the `promote_index` audit row — those fields are
  > recorded on the non-forced path too, so a clean promote is evidenced as
  > loudly as an override. Run `make golden` yourself before forcing. Only gate
  > 1 of `RELEASE_PLAN` §7 is machine-enforced; gates 2-5 remain
  > operator-verified. Re-promoting the already-active index is still a no-op
  > and is never blocked — but note that this no-op is *not* the dual-active
  > repair. Converging a dual-active database means promoting a **different**
  > version, which runs the demotion loop below the gate; that repair is
  > therefore gated too, and needs `?force=true` on a stack with no evaluation
  > runs.

### Fixed
- **`docs/API_SPEC.md` §13 documented a promote path that does not exist.**
  It said `POST /internal/v1/indexes/{index_version}/promote`, which 404s;
  `docs/DEPLOY_FLY.md` had already recorded the discrepancy. Corrected to the
  implemented route, `POST /v1/admin/index_versions/{index_version}/promote`.

## [0.11.0] — 2026-07-21

> **Operator summary for this release.** Four items need action, in this order:
> rotate the database password if you hold deploy/CI logs from before #165;
> deploy the API and worker **together** with migration `0006`; **re-seed** to
> retire stale `docs.test` rows; and **flush `answer_cache`** (the
> `answer_policy_version` bump below does this for you). Any client parsing the
> error envelope must also move to the flat shape — see *Changed*.

### Security
- **`make migrate` / `make seed` echoed the database password (#165).** The
  recipes were not `@`-prefixed, so make printed the full command line —
  including `CITEVYN_DATABASE_URL` with credentials — to stdout. `deploy.sh` and
  CI both run these targets, so the password landed in deploy and CI logs, one
  line before the output #93 had already redacted. Both recipes are now
  `@`-prefixed with a redacted progress line (`citevyn:***@host:5432`).

  > **Operator note.** Rotate the database password if you have deploy or CI
  > logs from before this release.

- **Production entry points now refuse weak `CITEVYN_DEMO_API_KEY` and
  `CITEVYN_ADMIN_API_KEY` (#200).** `_env_guard.sh` applies the same test the app
  applies at boot (non-empty, not the published default, ≥16 chars), so a
  half-filled `.env` fails in a second instead of crash-looping the api after a
  60-second health poll. The admin key — which promotes an index and reads the
  budget — was previously only compared against the Makefile bootstrap stub, so
  empty, absent, and the published `local-admin-key` default all passed.

  > **Operator note.** A `.env` that used to pass may now be rejected if either
  > key is under 16 characters. That is intended; the app would refuse to boot on
  > it anyway. `prod.env.example` no longer ships `CITEVYN_LLM_PROVIDER=stub`,
  > which the guard rejects — a literal `cp prod.env.example .env` could not pass
  > its own guard even when fully filled in.

### Added
- **`deploy-verify` proves the rollback it claims (#195).** Two drills: **A**, a
  data-recovery rollback (`backup.sh` → stop writers → `restore.sh` → api healthy
  → full functional re-verify), which always runs; and **B**, the code rollback to
  `PREV_VERSION` plus roll-forward, which runs only when that tag is the same
  migration generation. When it is not, the gate asserts the refusal is fast and
  FAILS unless narrowed with `--data-rollback-only`. The summary reports which
  path a run actually proved and reports production's real state
  unconditionally — it no longer prints "rollback proven" for a path it did not
  run. Both drills are now proven end to end: 42 passed / 0 failed against a real
  prod stack, with the full probe suite (cited answer, refusal, exact lookup,
  admin 401) re-run against the **rolled-back** stack. See RELEASE_PLAN §10.

- **Cost controls, §9 (#153).** Per-call metering into `provider_calls` priced by
  provider **and** model (#184, migration `0005`); a daily budget summed in SQL
  since midnight UTC — soft $5 warn, hard $10 stop, fail-closed — plus a
  concurrency cap (#188); `GET /v1/admin/budget` and `make budget` (#189); and
  embedding spend metered on the `Embedder` seam at both production construction
  sites (#196). An unknown model records `priced=false` with cost 0 rather than a
  guess; `unpriced_calls` is the under-counting alarm.

  > **Deploy note.** A full corpus re-ingest now counts against the §9 budget and
  > can trip the $10 hard limit, which stops paid calls on the answer path too.
  > Check `make budget` before re-ingesting. `make budget` is also wired into
  > `deploy-verify` preflight and its exit codes are load-bearing: `0` headroom,
  > `1` LOW (blocks the deploy), `2` could-not-check (warns). A **missing**
  > provider-side cap exits `1`, not `0`.

- **One-command deploy-verify and rollback** (`make deploy-verify`,
  `make rollback TAG=…`, #160), plus `infra/docker/scripts/rollback.sh` — the
  incident tool DEMO_CHECKLIST §6 referenced but which did not exist.
- **CI builds and BOOTS the api and worker images (#82).** A container-runtime
  break (interpreter/CMD) is invisible to `docker build`, which is how a
  non-booting image once shipped green. `image_smoke.sh` runs as a PR gate and as
  a release gate before `:latest` publishes.
- **A runner for `tests/shell/`, matrixed on bash 3.2 and 5.x (#181)**, so the
  operator scripts are verified against both the macOS system bash and modern
  Linux bash. The suite had no runner and was not executed in CI at all.
- **Dictated "site win" is recovered as CiteVyn (#84, #175).** Speech-to-text
  mangles the product name and the two-word forms were proven un-disambiguable by
  regex. A deterministic prefilter keeps ordinary traffic free of cost, and the
  LLM is asked only when the question would otherwise refuse as `unsupported`.

### Changed
- **Error responses are now flat on the wire (#167).** `error_response` put the
  envelope inside `HTTPException.detail`, and FastAPI serialized it as
  `{"detail": {...}}` — so `body.error.code` was `undefined` for every client, in
  violation of `docs/API_SPEC.md` §4, and the frontend branches that read it were
  dead code. A dedicated handler now emits the envelope flat and wraps
  framework-raised 404/405 in the same shape.

  > **Deploy note.** This is a wire-format change. Any client parsing
  > `{"detail": {"error": …}}` must move to the flat `{"error": {"code": …}}`.

- **`answer_policy_version` `v1` → `v2`** — invalidates every cached answer, which
  is how the answers poisoned by #169 are evicted. They sit under valid keys with
  a correct `source_version_hash` and `embedder_identity`, so nothing else would
  clear them.

  > **Rollback caveat.** Reverting past this release restores `v1` and brings
  > those poisoned rows back into key scope for the remainder of the cache TTL
  > (default 24h). When rolling back across this bump, set
  > `CITEVYN_ANSWER_POLICY_VERSION=v3` rather than accepting the reverted default
  > — a cold cache costs a refill, re-serving a known-bad answer is silent. See
  > RUNBOOK §5.3a.

- **The shipped source docs are the single source of truth for the corpus
  (#178).** `db/seed/seed_catalog.py` no longer carries a hand-written copy; it
  runs the real ingestion pipeline over `app/worker/sources/*.md`, so seeding *is*
  the ingest. The two copies that cannot be derived (the conftest fixture and the
  frontend offline KB) are held by drift guards that fail the build when a corpus
  edit invalidates them. `npm test` now runs in CI.

  > **Deploy note.** A re-seed **retires** `v1` documents no allowlisted source
  > owns. A database seeded before this change keeps stale `source_name="docs.test"`
  > rows (11 docs / 47 chunks) serving old text with fabricated citation URLs —
  > re-seed to clear them. The seed fails loud and leaves `v1` unpromoted if any
  > source fails.

- **`documents.content_checksum` renamed to `identity_checksum` (#163).** The
  column hashed name+title, not content, and `IngestionRunner` defaulted to a
  retired placeholder; `source_version_hash` is now a required kwarg.

  > **Deploy note.** Migration **`0006`** is a rename, so the API and worker must
  > be deployed **together** with it. Migration `0005` (`provider_calls`, #184) is
  > additive and safe to apply ahead of the app.

- **The judged answer-quality eval is frequency-bounded, not sampled (#153,
  #182).** It runs on a push to `main` or on a PR labelled `full-eval`, at full
  coverage. Sampling was implemented, measured and rejected: 42 of 58 golden cases
  carry zero-tolerance, judge-independent oracles that sampling switches *off*
  rather than degrading, for a ceiling of ~28% saving. New `docs/COST_CONTROLS.md`.

### Fixed
- **Rolling back across a migration boundary died inside a container (#195).**
  `rollback.sh` warned that the target predated applied migrations and then
  proceeded anyway; the live DB stays stamped at the newest revision, which the
  older tree does not contain, so `alembic upgrade head` failed with
  `Can't locate revision identified by '0006'` mid-deploy. It now REFUSES before
  the checkout, names the backup-restore path (RUNBOOK §4.2), and offers
  `--allow-migration-mismatch`. It also takes `--base-ref`, because the check
  compares against the *deployed* tree and a previous rollback leaves you on a
  detached HEAD — where the boundary would be invisible. `make restore` had never
  been executed and was missing `PGPASSWORD` — the defect that made `make backup`
  unusable — so it is now a real script, `infra/docker/scripts/restore.sh`, and it
  restores in a **single transaction** so a failure cannot leave the live database
  half-dropped.

- **The rollback drill could leave production stopped (#195).** The data-recovery
  drill stopped `api`/`worker` and, on any downstream failure, returned without
  restarting them — and the summary only mentioned production state on a path that
  never ran. The drill now raises its stopped-flag *before* the stop (`stop` is not
  atomic) and restores the writers from an `EXIT`/`INT`/`TERM` trap, so every exit
  path — including Ctrl-C — brings production back.

- **Anaphoric follow-ups returned the previous answer verbatim (#169).**
  Conversation memory resolved a follow-up by CONCATENATION
  (`"What is Codex CLI? who built it?"`), and the leading clause is a complete
  self-contained question — so the LLM answered that and ignored the follow-up.
  Routing still uses the concatenation (it is what pulls a bare anaphor onto the
  right product domain); retrieval, generation and the cache key now get a
  condensed standalone question. Every multi-turn eval case now asserts its answer
  is not byte-identical to the previous turn's.

- **An uncited answer was returned with every retrieved chunk attached at
  `confidence=high` (#174)** — citations strongest exactly where grounding was
  weakest. An answer with no cited indices is now a no-answer and is never cached.

  > **Deploy note.** Flush `answer_cache`; pre-deploy ungrounded answers otherwise
  > replay for the 24h TTL. The `answer_policy_version` bump above does this.

- **Index promotion returned 500 on a dual-active database (#166).**
  `promote_version()` used `scalar_one_or_none()` to find the row to demote, so
  two active rows raised `MultipleResultsFound`. Promotion is the only API that
  can repair index state, so a drifted database was unrecoverable through the API
  — the one call that fixes it was the one that crashed. It now demotes every
  active row.

- **A Redis outage reported the search index as down (#167).** The limiter failed
  closed with `index_unavailable`, so the code contradicted its own message. New
  `rate_limiter_unavailable` (503) is returned instead, with its own frontend copy
  — a transport notice, never the content-refusal badge.

- **The Postgres migration gate never ran on `main` (#183).**
  `github.event.pull_request` is null on a push, so the fork guard evaluated false
  post-merge and the job that applies every migration against real Postgres was
  silently skipped — leaving `main` with no schema-drift signal. A new test parses
  every workflow and fails any job gated on the pull_request payload without a
  push escape, so this class of silent skip cannot recur.

- **"How do I install Claude Code?" refused (#170, #177).** `claude_code.md`
  carried no installation content at all, while `codex.md` always had a CLI
  install section. Surfaced once #169 stopped concatenating follow-ups, which had
  been masking it.

  > **Deploy note.** Corpus content only reaches the live index via re-ingest +
  > admin promote (RUNBOOK §3.7).

- **Questions about CiteVyn refused when the name was mangled (#84, #172, #193).**
  `citevyn.md` was already shipped and ingested — routing never reached it, because
  the guardrail matched a bare `\bcitevyn\b`. The guardrail and the frontend
  offline matcher now share an alias list, pinned by a cross-language drift guard.

- **`tests/shell/test_env_guard.sh` had four silently-failing cases (#161, #179)**,
  and the suite read the caller's exported environment, so a case that
  deliberately omitted a key tested the operator's value instead of empty (#200).

- **`docs/DEMO_CHECKLIST.md` referenced routes and ports that do not exist
  (#168)**, now corrected and pinned by a guard that is scoped to the disclaimed
  token and is verb-aware.

- **CI flake: `db-verify` raced the pgvector:pg18 first-boot restart (#85).** Both
  `docker exec psql` calls now retry in a bounded loop that rides out the transient
  window; the cap still hard-fails a genuinely broken boot.

### Known issues
- **The demo rate-limit bucket is GLOBAL (#203).** `require_demo_api_key` returns a
  constant user id, so every demo visitor shares one bucket. Thirty successful
  queries from one visitor deny the demo to everyone for a rolling hour, and
  because the bucket lives in Redis an api restart no longer clears it. Needs
  per-visitor identity (session and/or IP). **Blocks publishing a public URL**; it
  does not block cutting this tag. `deploy_verify.sh` now names this explicitly
  when it rate-limits itself, since one gate run spends ~16 of the 30/hour cap.
- **#153 Layer 4 (persist the per-user limiter) was closed as won't-build (#197).**
  The persistence half already shipped; the missing half is identity, not
  durability — see #203.

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
2. Update the version in `backend/pyproject.toml`. (This step used to say
   "update `version.txt` and `pyproject.toml`" — there is no `version.txt` in
   this repo and there never was, so the procedure could not be followed as
   written.)
3. Add a fresh `[X.Y.Z]` section to this file, dated today.
4. Run `make lint && make typecheck && make test && make golden` and
   attach the `golden_report.json` to the PR.
5. Open the PR with the `release-blocker` label removed (re-add it
   after merge if a hot-fix is required).
6. On merge, tag: `git tag -a vX.Y.Z -m "vX.Y.Z — <one-liner>"`.
7. Push: `git push origin main --follow-tags`.
