# Autonomous RAG-completion run — status

> Append-only progress log for the unattended Phase 1→4 run. Newest STATUS block on top.
> Purpose: survive context compaction so the run can resume.

# ============================================================================
# FINAL STATUS — autonomous run ended 2026-07-16 (morning handoff)
# ============================================================================

**One line:** Phase 1 is COMPLETE, adversarially reviewed, eval-proven, and **MERGED to main**.
Phase 2 was investigated + design-reviewed and **deliberately stopped before implementation**
because the adversarial design review found the proposed refusal-safety mechanism unsound and the
sound redesign changes a user-facing refusal contract — a call that should be yours, not an
unsupervised auto-merge. Phases 3–4 not started (they sit behind Phase 2 in the plan sequence).

## What merged
| PR | What | Eval (before → after) | State |
|---|---|---|---|
| **#103** (main `d3795f6`) | **Phase 1 PR1.1** — revive the vector arm (#97): OpenRouter/OpenAI `text-embedding-3-small` embedder behind the seam, embedding-aware seeders + db/seed backfill, opt-in Postgres eval mode | paraphrase **0/5 → 3/5**, overall **10/15 → 13/15 (0.867)**, literal 1.0, refusal leaks 0/5 (real pgvector, zero residue). Semantic-discrimination proof: real **5/5** vs stub ≤2/5 | **MERGED**, all 6 CI checks green, no `--admin` |

Full loop honored on #103: adversarial plan-review → TDD → gates (608 hermetic + 8 postgres pytest,
ruff, pyright) → **PG eval proof** → fan-out PR review (10 findings → 8 refuted → 2 confirmed majors
fixed) → release-readiness SHIP → auto-squash-merge on green CI. §8a-1 records the numbers; ADR-0003
has the OpenRouter addendum.

## What's left (in priority order)
1. **Phase 2 — retrieval quality (STOPPED, needs your product decision).** See
   [`docs/RAG_PHASE2_DESIGN_NOTES.md`](RAG_PHASE2_DESIGN_NOTES.md). The next eval win
   (paraphrase 3/5 → 4/5) requires relaxing the orchestrator's "unsupported → immediate refusal"
   contract so in-corpus questions that don't NAME the product get answered. The design review
   (8 blockers across 4 dimensions) showed an absolute similarity floor is the wrong refusal net
   (it can't separate `refusal_openai` 0.373 from valid `citevyn_par` 0.341, and has zero
   hermetic CI coverage). Recommended sound path: lean on the existing LLM grounding-refusal +
   a relative/margin gate, and reconcile the eval to drive the full orchestrator for refusals.
   **Product call for you:** should CiteVyn answer unnamed-but-in-corpus questions (broader
   recall, softer refusal contract, per-query embed+LLM cost) vs keep the crisp free refusal?
2. **#92 — real prod ingestion** (Phase-1 remainder): HTTP fetcher + shipped sources so the
   worker can ingest in the prod image. No eval delta (eval uses the conftest corpus); it's
   deploy plumbing.
3. **Phases 3 (query rewrite + conversation memory) and 4 (fallback UX, 429 UX, degrade signal)**
   — not started.

## Blockers encountered (for context)
- **Gemini free-tier EMBEDDING quota exhausted** (1000/day) → pivoted to OpenRouter/OpenAI
  embeddings (you authorized this mid-run). This is why Phase 1 uses `openrouter`, not `gemini`.
- **Gemini free-tier GENERATION rate-limited** → the LLM-judge baseline (§8a) is deferred; the
  retrieval gate (the real Phase-1 gate) is unaffected and fully proven.
- **Docker Desktop crashed** mid-run (recovered after ~4 min). It never affected merged work
  (CI runs its own Postgres) but interrupted the judged eval run.

## How to test Phase 1 live in the morning
Prereqs: Docker Desktop running; the OpenRouter key is already in `infra/docker/.env`.
```bash
cd ~/Projects/citevyn
git checkout main && git pull                      # d3795f6 or later (has #103)
export PGPW=$(grep '^POSTGRES_PASSWORD=' infra/docker/.env | cut -d= -f2-)
export DB_URL="postgresql+psycopg://citevyn:$PGPW@localhost:5432/citevyn"
make db-up
CITEVYN_DATABASE_URL=$DB_URL uv run --project backend alembic -c db/alembic.ini upgrade head
# Prove the semantic jump on REAL pgvector (needs an EMPTY catalog):
cd backend && CITEVYN_DATABASE_URL=$DB_URL \
  CITEVYN_EMBEDDING_PROVIDER=openrouter CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small \
  CITEVYN_ENVIRONMENT=local uv run python -m tests.eval.runner --postgres --no-judge
#   → overall 0.867, paraphrase 0.600, refusal leaks 0/5
# Semantic-quality proof (real embedder 5/5 vs stub at chance):
CITEVYN_EVAL_PG=1 CITEVYN_EMBEDDING_PROVIDER=openrouter \
  CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small \
  uv run python -m pytest tests/test_eval_semantic_discrimination.py -q
```
For a live chat demo, seed the demo index WITH embeddings (revives the vector arm on the demo
catalog), promote, and run uvicorn — see the "How to test live" section further down + the
phase-one runbook.

## PR / branch state
- `feat/rag-phase1-embeddings` — merged via #103, remote branch deleted.
- `feat/rag-phase2-soft-scoping` — carries only doc updates (BACKLOG sync + these status/design
  notes). No Phase-2 code. Pushed for durability; safe to delete after reading.

---
_(Older live-progress log below is retained for history.)_

## STATUS (superseded) — 2026-07-16

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
