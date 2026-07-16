# Backlog / open follow-ups

**Read this at the start of every work session, together with the live GitHub issue
list (`gh issue list --state open`).** This file is the durable, in-repo index of
tracked follow-up work so a session never re-implements or overlooks something that is
already filed. When you open, close, or supersede an issue, update the matching row here
in the same change.

> This file mirrors GitHub issues; GitHub is the source of truth for status. If a row
> here and the issue disagree, trust the issue and fix the row.

## Open follow-ups

| Issue | Title | Area | Priority | Origin |
|---|---|---|---|---|
| [#59](https://github.com/imrohitagrawal/citevyn/issues/59) | Embeddings: additional providers behind the seam + scale tuning (Voyage/OpenAI, HNSW recall, corpus refresh) | embeddings | Low (at scale / if Gemini insufficient) | #51 / PR #56, ADR-0003 |
| [#61](https://github.com/imrohitagrawal/citevyn/issues/61) | Frontend: real SSE streaming for chat answers (replace client-side reveal) | frontend / API | Low (V1 UX polish; needs new backend `text/event-stream` endpoint) | PR #45, RELEASE_PLAN §11 |
| [#62](https://github.com/imrohitagrawal/citevyn/issues/62) | Frontend: gate the composer while a live answer is in flight (concurrent-send interleave) | frontend | Low (V1 hardening; cosmetic, never wrong answer/citation) | PR #45 review, RELEASE_PLAN §11 |
| [#81](https://github.com/imrohitagrawal/citevyn/issues/81) | Prod container stack not deployable: deploy.sh/refresh.sh alembic+`--sqlalchemy-url`+seed path, false-green health gate, stub-in-prod; worker CMD/service model + healthcheck | infra / deploy | **Reopened** — items 1–8 fixed in PR `fix/81-prod-deploy-path-v2` (was prematurely closed 2026-07-12 without fixing any item) | #34 review |
| [#82](https://github.com/imrohitagrawal/citevyn/issues/82) | No CI job builds/boots the api+worker images (container-runtime breaks ship green); add build+boot smoke; group the two docker `FROM` refs in dependabot | ci | Medium (systemic gate gap) | #34 review |
| [#84](https://github.com/imrohitagrawal/citevyn/issues/84) | CiteVyn-meta maturation: intent-detect token-absent phrasings, real-embedder no_answer golden, golden-in-CI, offline-copy convergence, refusal-copy nudge, `/about` deploy | backend / frontend | Low | #49 / PR #83 review |
| [#93](https://github.com/imrohitagrawal/citevyn/issues/93) | Seed modules log the database URL including the password to stdout (`seed_users`/`seed_catalog`); redact before merge into deploy/CI logs | security / db | Medium (secret in deploy logs) | #81 verification |
| [#112](https://github.com/imrohitagrawal/citevyn/issues/112) | Conversation memory: entity-aware rewrite for off-corpus topic-pivot follow-ups (honest relevance miss when a pivot is semantically adjacent to the prior topic) | backend / RAG | Low (LLM net refuses clear pivots; bounded phrasing class) | Phase 3b fan-out review |
| [#119](https://github.com/imrohitagrawal/citevyn/issues/119) | Conversation memory: scale to long conversations (rolling summary via `sessions.summary` + LLM standalone-question rewrite + token-budgeted generator context + `(session_id, created_at)` index) | backend / RAG | Low (current design is constant-cost per turn; this adds depth) | live-test review |
| [#125](https://github.com/imrohitagrawal/citevyn/issues/125) | Eval harness: chunk-level context precision/recall + distractor corpus + golden-set growth toward 50–100 + human judge-calibration subset | eval / RAG | Medium (sharpens retrieval-ranking signal; needs chunk-level identity on a separate distractor index) | Item 2 eval-hardening plan review (deferred) |

## Recently closed

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
