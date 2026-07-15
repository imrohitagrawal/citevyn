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
| [#85](https://github.com/imrohitagrawal/citevyn/issues/85) | CI flaky: `compose-db-smoke` `db-verify` races the pg18 first-boot restart (`FATAL: shutting down`); retry the `SELECT 1` / `CREATE EXTENSION` | ci | Medium (flakes the merge gate) | #83 CI |
| [#92](https://github.com/imrohitagrawal/citevyn/issues/92) | Worker: prod ingestion needs a real fetcher + shipped sources (default LocalFetcher reads unshipped `tests/fixtures/*.md`, so `run` fails in the prod image) | worker / deploy | Medium (worker model fixed in #81, but ingestion can't succeed until this lands) | #81 verification |
| [#93](https://github.com/imrohitagrawal/citevyn/issues/93) | Seed modules log the database URL including the password to stdout (`seed_users`/`seed_catalog`); redact before merge into deploy/CI logs | security / db | Medium (secret in deploy logs) | #81 verification |
| [#96](https://github.com/imrohitagrawal/citevyn/issues/96) | RAG eval harness: golden set + retrieval hit-rate + LLM-as-judge, CI-gated (Phase 0 of RAG_QUALITY_PLAN; supersedes #84 golden-in-CI) | eval / ci | High (measurement foundation for all RAG work) | RAG_QUALITY_PLAN |
| [#97](https://github.com/imrohitagrawal/citevyn/issues/97) | Populate chunk embeddings + index provenance — revive the dead vector arm (all chunk embeddings NULL) (Phase 1) | backend / retrieval | High (dominant cause of poor answers) | RAG_QUALITY_PLAN |

## Operator / non-code follow-ups (not GitHub issues)

- **Live semantic e2e for #51:** set `CITEVYN_EMBEDDING_PROVIDER=gemini` + `CITEVYN_GEMINI_API_KEY`,
  re-ingest, and confirm a landing-page question returns a substantive, correctly-cited
  answer. The plumbing is verified end-to-end; only real-key semantic quality remains.
  See RUNBOOK §3.4a.

## Design references

- `docs/ADR/0003-embeddings-provider.md` — embedding provider decision, rejected
  alternatives, and the full "Deferred / Future Work" list these issues are drawn from.
