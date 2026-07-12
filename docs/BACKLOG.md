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
| [#70](https://github.com/imrohitagrawal/citevyn/issues/70) | Cache: an answer degraded by a transient EmbedderUnavailable (Tier-1) is still cached to TTL | cache / observability | Low (bounded by TTL, self-heals; not predictable pre-retrieval) | #65 / PR #69, ADR-0003 |
| [#71](https://github.com/imrohitagrawal/citevyn/issues/71) | Refactor: share the Tier-3 mismatch predicate between `_vector_arm_enabled` and the orchestrator | retrieval / cache | Low (maintainability; correct today, guarded by a parity test) | #65 / PR #69 |
| [#72](https://github.com/imrohitagrawal/citevyn/issues/72) | Cache: exact-lookup answers are needlessly skipped from the cache under an embedder mismatch | cache | Low (correctness-preserving; missed cache write only; shares a fix with #70) | #65 / PR #69 |
| [#61](https://github.com/imrohitagrawal/citevyn/issues/61) | Frontend: real SSE streaming for chat answers (replace client-side reveal) | frontend / API | Low (V1 UX polish; needs new backend `text/event-stream` endpoint) | PR #45, RELEASE_PLAN §11 |
| [#62](https://github.com/imrohitagrawal/citevyn/issues/62) | Frontend: gate the composer while a live answer is in flight (concurrent-send interleave) | frontend | Low (V1 hardening; cosmetic, never wrong answer/citation) | PR #45 review, RELEASE_PLAN §11 |

## Operator / non-code follow-ups (not GitHub issues)

- **Live semantic e2e for #51:** set `CITEVYN_EMBEDDING_PROVIDER=gemini` + `CITEVYN_GEMINI_API_KEY`,
  re-ingest, and confirm a landing-page question returns a substantive, correctly-cited
  answer. The plumbing is verified end-to-end; only real-key semantic quality remains.
  See RUNBOOK §3.4a.

## Design references

- `docs/ADR/0003-embeddings-provider.md` — embedding provider decision, rejected
  alternatives, and the full "Deferred / Future Work" list these issues are drawn from.
