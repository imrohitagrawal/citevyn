# Autonomous RAG-completion run — status

> Append-only progress log for the unattended Phase 1→4 run. Newest STATUS block on top.
> Purpose: survive context compaction so the run can resume.

## STATUS (live) — 2026-07-16

**Overall:** **Phase 1 PR1.1 MERGED** (PR #103, main `d3795f6`, #97 closed — all 6 required CI
checks green, auto-squash-merged, no admin bypass). The walking-skeleton milestone is done:
semantic search works on real pgvector, eval-proven (paraphrase 0→3/5, overall 13/15). Proceeding
to **Phase 2** (retrieval quality — where the eval number moves next) as the higher-value step;
**#92** (real prod-ingestion plumbing — no eval delta, uses the conftest corpus) is the tracked
Phase-1 remainder to pick up if time permits.

### Environment decisions (locked)
- **Embedding provider for this run = OpenRouter `openai/text-embedding-3-small` @ 1536 dims.**
  - Reason: Gemini free-tier embeddings hit the **daily** quota
    (`embed_content_free_tier_requests`, limit **1000/day**, `RESOURCE_EXHAUSTED`,
    still 429 after a 70s wait → confirmed daily cap, not per-minute burst). Free Gemini
    embeddings are unavailable tonight.
  - User (awake, mid-run) explicitly authorized the OpenRouter fallback; key present in
    `infra/docker/.env` (`CITEVYN_OPENROUTER_API_KEY`, 73 chars). Verified working:
    `openai/text-embedding-3-small` returns native **1536-dim** vectors (matches the
    pgvector column exactly → **no migration needed**), supports batch + a `dimensions` param.
  - **Vector-space rule honored:** corpus AND query embedded by the SAME model (no per-call
    Gemini→OpenRouter mixing — that is ADR-0003's rejected anti-pattern). This is the clean
    "new provider behind the Embedder seam" path (issue #59).
  - Cost: ~$0.02/1M tokens; a few-hundred-chunk corpus + eval queries ≈ well under a cent.
    Embeddings will be **disk-cached** so re-runs cost ~0.
- **Postgres+pgvector:** `pgvector/pgvector:pg18` via `make db-up`; Docker 29.6.1 present.

### Phase status
- Phase 0 (eval harness): DONE previously (PR #98, main `43972a3`).
- Phase 1 (#97 embeddings + #92 ingestion): **IN PROGRESS** on branch
  `feat/rag-phase1-embeddings`. Stack up (pg18+pgvector, alembic 0004). Premise validated:
  OpenAI embeddings route 5/5 paraphrases to correct area; refusals → `unsupported` (0 leaks).
  Empirical Phase-1 target: paraphrase 0/5→3/5, overall 10/15→13/15 (the 2 remaining route to
  `unsupported` = Phase 2). Adversarial plan-review done (5 skeptics; 1 blocker + majors folded).
- Phase 2/3/4: not started.

### Phase-1 plan-review synthesis (folded into implementation)
- **PR1.1 = "Revive the vector arm" (closes #97):** OpenRouterEmbedder (OpenAI-compat, done) +
  config/factory wiring + coherence & prod-key guards + embedding-aware seeders
  (`commit`/`embedder` params, backward-compat) + db/seed backfill + **Postgres eval mode**
  (opt-in `--postgres`/`CITEVYN_EVAL_PG=1`, non-stub-gated, rolled-back, zero-residue) +
  **semantic-discrimination proof + stub control** (the honest "semantic works" evidence, since
  1-chunk-per-area makes the golden number plumbing-level) + ADR-0003 addendum.
- **Gameable-metric fix:** golden 3/5 claim narrowed to "vector arm alive on PG"; the
  discrimination test (real 5/5 vs stub ≈chance, global no-scoping) is what proves semantic quality.
- **Isolation:** seed_catalog commits → add `commit=False`; PG eval single-session + finally-rollback
  + unique index_version + refuse in production + residue assertion.
- **PR1.2 = #92** real HTTP fetcher + shipped sources (next).

### Phase 1 PR1.1 — code COMPLETE + committed (branch feat/rag-phase1-embeddings, `d743cbc`)
- **All gates passed while Docker was up:** 607 hermetic pytest + 8 postgres + ruff format/check
  + pyright(app) 0 errors. PG eval PROVEN + captured (`/tmp/cv_eval_pg.json`):
  paraphrase 0/5→3/5, overall 13/15 (0.867), literal 1.0, refusal leaks 0/5, **zero residue**.
  Semantic-discrimination proof: real 5/5 vs stub ≤2/5.
- **Fan-out PR review running** (8 dimensions × adversarial per-finding verify).
- Judge baseline deferred (Gemini generation also rate-limited; long paid-fallback txn was cut
  by the Docker crash — orthogonal to the retrieval gate).
- **⚠️ Docker Desktop CRASHED mid-run** ("unable to start"). Does NOT block PR/CI (CI runs its
  own Postgres; hermetic gates need no Docker; PG proof already captured). Restart attempted.
  Only affects local re-runs + the morning live demo (restart Docker first).

### How to test live in the morning (after Phase 1 merges)
1. Ensure Docker Desktop is running (it crashed during the autonomous run — reopen it).
2. `make db-up` then migrate with the real password:
   `export PGPW=$(grep '^POSTGRES_PASSWORD=' infra/docker/.env|cut -d= -f2-)`
   `export DB_URL="postgresql+psycopg://citevyn:$PGPW@localhost:5432/citevyn"` ; `make migrate`
3. Seed WITH real embeddings (revives the vector arm on the demo index):
   `CITEVYN_DATABASE_URL=$DB_URL CITEVYN_EMBEDDING_PROVIDER=openrouter
    CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small
    uv run --project backend python -m db.seed.seed_users`  (then seed_catalog)
4. Prove the eval jump on real pgvector:
   `cd backend && CITEVYN_DATABASE_URL=$DB_URL CITEVYN_EMBEDDING_PROVIDER=openrouter
    CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small
    uv run python -m tests.eval.runner --postgres --no-judge`  → overall 0.867, paraphrase 0.6.
   (Requires an EMPTY catalog; truncate first if you seeded in step 3.)
