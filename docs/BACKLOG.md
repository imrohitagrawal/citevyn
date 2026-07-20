# Backlog / open follow-ups

**Read this at the start of every work session, together with the live GitHub issue
list (`gh issue list --state open`).** This file is the durable, in-repo index of
tracked follow-up work so a session never re-implements or overlooks something that is
already filed. When you open, close, or supersede an issue, update the matching row here
in the same change.

> This file mirrors GitHub issues; GitHub is the source of truth for status. If a row
> here and the issue disagree, trust the issue and fix the row.

## Roadmap milestones

Post-MVP work is organized under two GitHub milestones (see `RELEASE_PLAN.md` §11–12):

- **[V1](https://github.com/imrohitagrawal/citevyn/milestone/1)** — depth/polish for a
  portfolio-grade demo (no new content domains or heavy surfaces).
- **[V2](https://github.com/imrohitagrawal/citevyn/milestone/2)** — breadth + heavier
  surfaces, deferred until V1 depth is proven.

### V1 milestone

| Issue | Title | Area | Notes |
|---|---|---|---|
| [#153](https://github.com/imrohitagrawal/citevyn/issues/153) | Live hosted public demo + cost guardrails | infra / ops | Highest V1 ROI; also completes the Phase-5 live deploy-verify + rollback gate; §9 cost limits are a hard prerequisite before a public URL. **Layer 0 (provider cap) + Layer 6 (CI spend) DONE** — see `docs/COST_CONTROLS.md`; CI now runs the judged eval on main / `full-eval`-labelled PRs only, at full coverage (case sampling was measured and rejected: 42/58 golden cases carry a zero-tolerance oracle). **Layers 1, 2, 3, 5 DONE** (PRs #184/#188/#189/#196 — metering incl. the embedder seam, admission control, §9 daily budget, `GET /v1/admin/budget` + `make budget`). **Layer 4 (persisted per-user limiter) CLOSED as won't-build — see #197 below.** All six layers are therefore resolved |
| [#61](https://github.com/imrohitagrawal/citevyn/issues/61) | Real SSE streaming for chat answers | frontend / API | Verified: **no streaming route exists on `main`** — a real backend build, not a rewire |
| [#154](https://github.com/imrohitagrawal/citevyn/issues/154) | Feedback capture wired into the eval loop | backend / frontend | Value is the eval flywheel, **not** model retraining; most invasive V1 item (DB + API) |
| [#155](https://github.com/imrohitagrawal/citevyn/issues/155) | Evaluation + live-ops dashboard | frontend / observability | Surfaces existing eval metrics + live cost/latency/refusal; pairs with #154 |
| [#156](https://github.com/imrohitagrawal/citevyn/issues/156) | Better re-ranking of retrieved chunks | backend / RAG | Feature-flagged, cost-aware, proven on golden + distractor eval sets |
| [#62](https://github.com/imrohitagrawal/citevyn/issues/62) | Composer gating while a live answer is in flight | frontend | Small hardening; do alongside #61 |

### V2 milestone

| Issue | Title | Area | Notes |
|---|---|---|---|
| [#157](https://github.com/imrohitagrawal/citevyn/issues/157) | ChatGPT (OpenAI) official docs — 5th domain | backend / corpus | Deferred: breadth-not-depth **and** licensing-gated (ADR-0003). Not deferred for UI risk |
| [#158](https://github.com/imrohitagrawal/citevyn/issues/158) | Voice output (TTS) for answers | frontend / API | Large surface, off-core; explicit MVP non-goal |

## Open follow-ups (unmilestoned)

| Issue | Title | Area | Priority | Origin |
|---|---|---|---|---|
| [#59](https://github.com/imrohitagrawal/citevyn/issues/59) | Embeddings: additional providers behind the seam + scale tuning (Voyage/OpenAI, HNSW recall, corpus refresh) | embeddings | Low (at scale / if Gemini insufficient) | #51 / PR #56, ADR-0003 |
| [#84](https://github.com/imrohitagrawal/citevyn/issues/84) | CiteVyn-meta maturation: ~~name recognition~~ (**item 1 done, PR #172** — single-token speech-to-text aliases (`sitewin`, `citevin`, …) route + canonicalize. The two-word `"site win"` form is a tested MISS: three adversarial rounds showed surrounding-token rules cannot separate it from ordinary English (`may the best site win!`), so it needs intent detection over the whole utterance — see the issue thread before retrying a regex), real-embedder no_answer golden, golden-in-CI, ~~offline-copy convergence~~ (**done** — `frontend/src/lib/citevynAliases.ts` mirrors the guardrail pattern; a pytest drift guard parses the TS list), ~~refusal-copy nudge~~ (**done** — `DEFAULT_UNSUPPORTED_REFUSAL` now names CiteVyn itself), `/about` deploy | backend / frontend | Low (the alias instance hits the owner's own demo flow) | #49 / PR #83 review; #169 live verification |
| [#119](https://github.com/imrohitagrawal/citevyn/issues/119) | Conversation memory: scale to long conversations (rolling summary via `sessions.summary` + LLM standalone-question rewrite + token-budgeted generator context + `(session_id, created_at)` index) | backend / RAG | Low (current design is constant-cost per turn; this adds depth) | live-test review |
| [#125](https://github.com/imrohitagrawal/citevyn/issues/125) | Eval harness: **most landed** (PR #132 chunk-level identity + MRR/precision@1; PR #133 distractor corpus + context precision/recall; PR #134 golden growth 31→50). **Remaining:** human-labeled judge-calibration subset (judge-vs-human agreement) | eval / RAG | Low (remaining piece is calibration, not gating) | Item 2 eval-hardening plan review (deferred) |
| [#174](https://github.com/imrohitagrawal/citevyn/issues/174) | Answer path: an uncited, non-refusal answer was returned with ALL retrieved chunks attached at `confidence=high` — citations strongest where grounding was weakest. **Fixed in PR #176.** Deploy note: flush `answer_cache`, since pre-deploy ungrounded answers replay from cache for the 24h TTL | backend / answer | — (fixed, PR open) | #175 adversarial review |
| [#148](https://github.com/imrohitagrawal/citevyn/issues/148) / [#150](https://github.com/imrohitagrawal/citevyn/issues/150) / [#151](https://github.com/imrohitagrawal/citevyn/issues/151) | Dependabot: fastapi runtime bump + two `actions/*` MAJOR bumps (v4→v7). Left unmerged **by policy** — `DEPENDABOT_TRIAGE.md` requires a named Backend-tech-lead / Ops reviewer for these tiers, unlike dev-only bumps. All CI-green and rebased | deps | Owner-gated | DEPENDABOT_TRIAGE.md |
| — | `DEPENDABOT_TRIAGE.md` describes a `release-blocker` label and a nightly demo-readiness gate that checks for it, but **no automation applies the label** (all four open dependabot PRs carry only `dependencies`), so that gate can never fire | ops / ci | Low (policy-vs-reality drift) | this session |
| [#170](https://github.com/imrohitagrawal/citevyn/issues/170) | ~~Corpus: `claude_code.md` has no installation content~~ **(fixed). The two paths it did NOT reach are fixed too, structurally, in #178: `db/seed/seed_catalog.py` now ingests the shipped corpus instead of carrying a copy, and the frontend offline KB has a Claude Code install branch.** Original: `claude_code.md` had no installation content, so "How do I install Claude Code?" refuses (identical single-turn and as a follow-up — a corpus gap, not retrieval) | corpus / worker | — (fixed) | #169 live verification |
| [#178](https://github.com/imrohitagrawal/citevyn/issues/178) | ~~Corpus content lived in FOUR places (worker sources / conftest fixture / `db/seed` / frontend KB) and drifted~~ **Fixed: `db/seed/seed_catalog.py` now runs the real ingestion pipeline over `app/worker/sources/*.md` (copy deleted — `make demo` needs no separate ingest step because seeding IS the ingest); the two copies that cannot be derived are covered by drift guards (`backend/tests/test_corpus_single_source.py`, `frontend/src/data/knowledgeBase.corpus.test.ts`) that fail the build when a corpus edit invalidates them; the frontend Claude Code install-routing bug is fixed; `npm test` now runs in CI and the frontend workflow triggers on corpus edits** | corpus / db / frontend / ci | — (fixed) | #170 review |
| [#163](https://github.com/imrohitagrawal/citevyn/issues/163) | ~~Worker: `Document.content_checksum` is a misnomer (hashes name+title, not content) + `IngestionRunner` still defaults to the retired `sha256:mvp-snapshot-2` placeholder with a now-backwards docstring~~ **(fixed, PR pending — column renamed to `identity_checksum` via migration 0005 with a reversible downgrade; `source_version_hash` is now a required kwarg).** Deploy note: 0005 is a rename, so the API and worker must be deployed together with the migration | backend / worker | Low (latent clarity/correctness; real content fingerprint now lives in `cli.content_version_hash`) | PR #162 adversarial review (F5 / P3) |

## Recently closed

- **[#163](https://github.com/imrohitagrawal/citevyn/issues/163)** — `Document.content_checksum`
  renamed to `identity_checksum` (migration **0006**, reversible); `IngestionRunner` now REQUIRES
  `source_version_hash` instead of defaulting to a retired placeholder. PR #187. NB the branch was
  cut before #184 and both migrations claimed `revision="0005"` — alembic would have seen two heads
  and `upgrade head` would have failed. **Any branch cut before #184 has this hazard.**

- **[#167](https://github.com/imrohitagrawal/citevyn/issues/167)** — a Redis outage no longer
  reports `index_unavailable`. PR #190. The bigger find: `error_response` returns
  `HTTPException(detail=envelope)` and nothing flattened it, so the wire body was
  `{"detail": {...}}` — the frontend read `body.error`, got `undefined`, and the new branch could
  **never fire in production**. Its three frontend tests passed *vacuously*. Now flattened for
  EVERY error code, which also fixes an `API_SPEC` §4 violation.

- **[#168](https://github.com/imrohitagrawal/citevyn/issues/168)** — DEMO_CHECKLIST routes/port
  corrected + a guard test. PR #191. The guard's first version exempted **13 of 16 routes**; the
  reviewer reintroduced #168's original defect verbatim and the suite stayed green. Now scoped to
  the disclaimed token and verb-aware.

- **[#178](https://github.com/imrohitagrawal/citevyn/issues/178)** — corpus is single-source:
  `db/seed/seed_catalog.py` runs the real ingestion pipeline over `app/worker/sources/*.md`
  instead of carrying a copy. PR #192. Took three rounds: the bootstrap originally wrote **stub
  vectors** that survived into a live index (vector arm enabled, ranking by SHA-256 hash distance,
  `/health/index` reporting healthy). Final fix is a `NullEmbedder` seam so they are never written.

- **[#153](https://github.com/imrohitagrawal/citevyn/issues/153) Layers 2, 3, 5** — PRs #188/#189.
  §9 daily budget (soft $5 warn / hard $10 stop, transient not refusal, SQL-summed since midnight
  UTC so restart-proof, fail-closed), concurrency cap, `GET /v1/admin/budget`, and `make budget`
  wired into deploy-verify. **Layer 1 completed for the embedder** in PR #196.

## Open follow-ups filed this session

| Issue | Why it matters |
|---|---|
| [#195](https://github.com/imrohitagrawal/citevyn/issues/195) | **Release blocker 9 is worse than "untested": rollback to `v0.9.0` is IMPOSSIBLE.** Its image cannot boot (uvicorn shebang points at a build-stage path), and `rollback.sh` rebuilds from the target tree. `v0.9.0` is the only tag, so there is no reachable rollback target. Fix: tag a bootable `v0.9.1` from any commit after #34. |
| [#197](https://github.com/imrohitagrawal/citevyn/issues/197) | **CLOSED — won't build _as specified_.** #153 Layer 4 asked to "persist the per-user rate limiter". (1) **The persistence half already shipped**: `infra/docker/prod.env.example:86` sets `CITEVYN_REDIS_URL`, so `_build_limiter` (`backend/app/core/rate_limit.py:338`) selects `RedisRateLimiter`; the prod bucket is a Redis `ZSET` that already outlives an api restart. There is no in-process limiter in prod to persist. (2) **The "per-user" half does not exist and cannot be delivered by persisting anything**: `require_demo_api_key` returns the *constant* `DEMO_USER_ID` (`backend/app/core/security.py:29,59`) and `_bucket_key` keys on it (`rate_limit.py:256`), so all demo traffic shares ONE bucket, `citevyn:rl:demo_user`. ⚠️ **This means the global-lockout hazard #197 warned about is already LIVE, not averted** — 30 successful queries from one visitor deny the demo to everyone for an hour, and the Redis path means an api restart no longer clears it. Successor filed as [#203](https://github.com/imrohitagrawal/citevyn/issues/203) (per-visitor identity) — that, not persistence, is the remaining work, and it blocks a public URL. Layer 3 (§9 daily budget, PR #188) remains the real spend control. Branch `feat/153e-persist-rate-limiter` retained. |
| [#183](https://github.com/imrohitagrawal/citevyn/issues/183) | ~~`postgres-migrations` never runs on push to `main` (PR-only `if:`, null payload on push).~~ **CLOSED — PR #201.** Also adds a test that parses every workflow and fails on an unintended `pull_request`-only condition, so the class of silent skip cannot recur. |
| [#203](https://github.com/imrohitagrawal/citevyn/issues/203) | **The demo rate-limit bucket is GLOBAL and now restart-proof.** Surfaced while closing #197: `DEMO_USER_ID` is a constant and the Redis limiter is already live, so 30 successful queries from ONE visitor deny the demo to everyone for a rolling hour, and an api restart no longer clears it. Needs per-visitor identity (session and/or IP). **Blocks publishing a public URL** (the demo half of #153); does not block cutting the tag. |

- **[#161](https://github.com/imrohitagrawal/citevyn/issues/161)** — CLOSED, no demonstrable
  behavioural impact. The `${v:1:-1}` observation was linguistically correct (bash 4.2+; bash 3.2
  raises "substring expression < 0"), but no failing invocation was ever produced. The code was
  hardened anyway — #179 repaired four silently-failing `test_env_guard.sh` cases and #181 gave
  `tests/shell/` a runner matrixed over ubuntu-latest **and macos-latest** (bash 3.2). With that
  lane green on both, the guard is proven on 3.2. Reopen with a concrete reproducer.

- **[PR #39](https://github.com/imrohitagrawal/citevyn/pull/39)** — CLOSED as superseded. The
  landing page shipped by another route: `frontend/src/components/LandingPage.tsx` (+
  `landing-sections.tsx`, `Hero.tsx`, `styles/landing.css`), wired via `App.tsx` and covered by
  `LandingPage.test.tsx`. Salvage was checked first: `frontend/playwright.config.ts` is already on
  `main`; `.agents/skills/**` is superseded by `SKILL.md` + `skills-lock.json`; `.vite/deps/*` were
  accidental build artifacts. Branch retained. (Aside, still true: `make e2e` runs a curl smoke and
  its help text still points at "Slice 11" for the Playwright upgrade.)

- **[#153](https://github.com/imrohitagrawal/citevyn/issues/153) Layers 0 + 6** — PR #182
  (main `112c3ff`). Judged eval now runs on `main` / `full-eval`-labelled PRs only, at FULL
  coverage. Case sampling was measured and REJECTED (42/58 cases carry a zero-tolerance oracle that
  sampling switches off, not averages down; ceiling was ~28%). Also fixed: the job's PR-only `if:`
  meant it never ran on `main` at all. New `docs/COST_CONTROLS.md`.

- **[#153](https://github.com/imrohitagrawal/citevyn/issues/153) Layer 1** — PR #184
  (main `3464aa3`). Per-call cost metering: `provider_calls` + migration 0005, priced by
  provider+model. **Layers 2-5 remain open** — see `docs/AUTORUN_HANDOFF_2026-07-20.md`.

- **[#82](https://github.com/imrohitagrawal/citevyn/issues/82)** — No CI job built/booted the
  api+worker images, so a container-runtime break (interpreter/CMD, which `docker build` does
  NOT catch) shipped green (the class that let the 3.14 bump merge non-booting). Fixed via
  `fix/82-ci-image-boot-smoke`: `infra/docker/scripts/image_smoke.sh` builds+BOOTS the images
  (api boots under `CITEVYN_ENVIRONMENT=local` → GET /health=200; worker execs
  `python -m app.worker.cli list-sources` exit 0), wired as `make image-smoke`, a CI PR-gate
  job (`image-smoke`), and a release.yml load→boot→push gate so a non-booting image fails the
  release BEFORE `:latest` publishes. dependabot groups the uv-builder + slim-runtime `FROM`
  refs so a minor bump can't drift the interpreters apart. Verified locally: smoke passes on
  the real images AND fails on a deliberately non-serving image (proven gate, not a rubber stamp).

- **[#87](https://github.com/imrohitagrawal/citevyn/issues/87)** — Retrieval returned
  no_answer for legitimate source-named questions ("How do I install the Codex CLI?"). Root
  cause was NOT domain misrouting (routing is correct: domain=codex): on the LIVE Postgres
  path (real embeddings) the repro already returns evidence, but it survived HERMETICALLY
  (SQLite, vector arm off) because the thin conftest codex/gemini fixtures lacked content the
  real shipped corpus has, so scoped keyword retrieval found nothing. Fixed via
  `fix/87-source-named-retrieval-regression`: enrich conftest.seed_catalog codex (install +
  OPENAI_API_KEY) and gemini (streaming) chunks to mirror the real worker sources; mirror
  install into db/seed; add regression guards — a hermetic retriever test (asserts the RIGHT
  content is retrieved), golden case codex_011, and CI-gated `--postgres` eval case
  codex_lit_install. golden 49/51→52/52; judged eval improved (overall 10/15→11/16, judge
  4.16→4.29, groundedness 0.818→0.833, paraphrase held 0.0, refusal leaks 0). No orchestrator
  code change.

- **[#93](https://github.com/imrohitagrawal/citevyn/issues/93)** — Seed modules logged the
  full `CITEVYN_DATABASE_URL` (password included) to stdout; `deploy.sh`/CI ran them, so the
  credential landed in deploy/CI logs. Fixed via `fix/93-redact-seed-db-password`: a shared
  `db.seed.redact_database_url` helper (SQLAlchemy `hide_password=True`; bails to a placeholder
  on an unparseable URL or a raw-`@`-in-password that `make_url` would mis-split) routes both
  success prints. Package-relative import so it resolves under BOTH the deploy image layout
  (`python -m seed.*`, `PYTHONPATH=/db`) and repo-root/CI (`python -m db.seed.*`). TDD + a
  deploy-layout import regression test (the review caught the absolute import breaking prod).
  Verified: live seed run prints `citevyn:***@…`, 20/20 unit tests green, lint+typecheck clean.

- **[#112](https://github.com/imrohitagrawal/citevyn/issues/112)** — Conversation memory:
  entity-aware CONTENT-NOUN follow-up rewrite. A follow-up naming no product + no bare anaphora
  ("is there a credentials file option?") used to refuse; `condense_question_llm` now resolves
  it via an LLM standalone-question rewrite, wired as a PURE recall-improver on the answer-when-
  grounded path (can't hijack routing). Deterministic regex kept for the hermetic followup gate;
  a new `judge_only` eval flag validates the case on the judged run only. Answered + gate green
  (stable ×3), locked numbers unchanged. See RAG_QUALITY_PLAN §8a-10.

- **[#85](https://github.com/imrohitagrawal/citevyn/issues/85)** — CI flake: `compose-db-smoke`
  `db-verify` raced the pgvector:pg18 first-boot restart (`FATAL: shutting down` / `database
  "citevyn" does not exist`). Both `docker exec psql` calls (`SELECT 1` + `CREATE EXTENSION
  vector`) now retry in a bounded loop (10×2s) that rides out the transient window; the cap still
  hard-fails a genuinely broken boot (no false green). Fixed via `fix/85-db-verify-retry`,
  Makefile-only, verified with fresh-volume `make ci-smoke` ×3.

- **[#120](https://github.com/imrohitagrawal/citevyn/issues/120)** / **[#121](https://github.com/imrohitagrawal/citevyn/issues/121)** / **[#122](https://github.com/imrohitagrawal/citevyn/issues/122)** — chat UX fixes
  (surfaced by live testing): transport errors (429/5xx/network) no longer wear the "NO SOURCE —
  REFUSED" content badge (distinct rate-limit/connection notice, #120); re-asking a failed question
  re-shows the user bubble (#121); autoscroll no longer yanks the view down when scrolling up during
  streaming (#122). Fixed via `feat/chat-ux-fixes`, frontend-only, live-verified.

- **[#92](https://github.com/imrohitagrawal/citevyn/issues/92)** — Worker prod ingestion: source
  docs now ship as package data under `backend/app/worker/sources/` (were unshipped test
  fixtures). MERGED via PR #105 (main `f199a2f`). Verified inside a built worker image + the
  worker ingested a real 33-chunk corpus on Postgres. `HttpFetcher` stays an unwired seam
  (curated license-clean local docs are the intended source, per ADR-0003). Completes Phase 1.
- **[#97](https://github.com/imrohitagrawal/citevyn/issues/97)** — Populate chunk embeddings +
  index provenance (revive the dead vector arm), Phase 1 PR1.1. MERGED via PR #103 (main
  `d3795f6`). OpenRouter/OpenAI `text-embedding-3-small` embedder behind the seam + embedding-aware
  seeders + db/seed backfill + opt-in Postgres eval mode. Proven on real pgvector: paraphrase
  0/5→3/5, overall 13/15 (0.867), zero residue; semantic-discrimination proof real 5/5 vs stub.
  See RAG_QUALITY_PLAN §8a-1. Phase-1 remainder: **#92** (real prod ingestion) still open.

- **[#96](https://github.com/imrohitagrawal/citevyn/issues/96)** — RAG eval harness (Phase 0)
  landed via PR #98 (main `43972a3`). Golden set + retrieval hit-rate + LLM-judge, CI-gated;
  baseline in `docs/RAG_QUALITY_PLAN.md` §8a.
- **[#99](https://github.com/imrohitagrawal/citevyn/issues/99)** — retired `gemini-2.5-flash`
  replaced via PR #100 (main `cc72b01`): primary `gemini-flash-latest` (free) + `openai/gpt-4o-mini`
  fallback (paid, different provider family). Live-verified. Follow-up: refill the judge baseline
  (§8a) via `make eval` during Phase 1.

## Operator / non-code follow-ups (not GitHub issues)

- **Enable the CI answer-quality gate (owner-only):** add `CITEVYN_OPENROUTER_API_KEY` as a
  repo Actions secret (*Settings → Secrets and variables → Actions*). The `answer-quality-eval`
  CI job (PR #127) skips until it is present; adding it flips the judged MIN_MEAN_JUDGE +
  groundedness + injection gate on. Config proven locally (`--postgres`,
  `openai/text-embedding-3-small`, `router`, `CITEVYN_EVAL_JUDGE_PANEL=1`). Recommend one
  trial PR run before making it a *required* check. See `docs/AUTORUN_STATUS.md` (top block).
- **Live semantic e2e for #51:** set `CITEVYN_EMBEDDING_PROVIDER=gemini` + `CITEVYN_GEMINI_API_KEY`,
  re-ingest, and confirm a landing-page question returns a substantive, correctly-cited
  answer. The plumbing is verified end-to-end; only real-key semantic quality remains.
  See RUNBOOK §3.4a.

## Design references

- `docs/ADR/0003-embeddings-provider.md` — embedding provider decision, rejected
  alternatives, and the full "Deferred / Future Work" list these issues are drawn from.
