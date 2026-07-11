# ADR-0003: Real Embedding Provider, pgvector Retrieval, and Corpus Expansion

## Status

Accepted. Implemented for issue #51 (real embedder + expanded corpus). Tier 3
cross-provider failover is **deferred** (see "Deferred / Future Work").

## Date

2026-07-11

## Context

Even after the real Gemini/OpenRouter **LLM** landed (PR #47), answer quality was
capped by the **embedding layer**, not the LLM:

1. **Embeddings were a deterministic SHA-256 hash, not semantic.** Two independent
   `StubEmbedder` implementations existed and never shared a vector space:
   - `backend/app/worker/embedder.py` — *write* path: synchronous, hard-coded
     `dim=64`, ignored `Settings` entirely.
   - `backend/app/retrieval/vector.py` — *read* path: async, `dim=settings.embedding_dim`
     (1024), honored `embedding_provider`, but was **never wired into the
     orchestrator** (`HybridRetriever(session)` passed `embedder=None`, so the vector
     arm always returned `[]`).
3. **Storage was a pickled `bytea` blob, not pgvector.** `chunks.embedding` was a
   `PickledEmbedding` (`LargeBinary`). `retrieval/vector.py` already called the
   pgvector operator `.cosine_distance(...)` against that blob — which cannot execute
   on a `bytea` column. So the "pgvector path" was scaffolded but non-functional; on
   Postgres it would have errored, and on SQLite it short-circuited to `[]`.
4. **The corpus was 4 tiny hand-paraphrased Markdown docs** (~300–530 bytes each) for
   Claude API, Claude Code, Codex, and Gemini.

Net effect: vector retrieval was effectively dead end-to-end. This ADR records the
decisions taken to make retrieval genuinely semantic, and — importantly — records the
alternatives we **rejected** and the work we **deliberately deferred**, so future
debugging can distinguish "deliberate trade-off" from "bug."

## Decision

1. **Embedding provider: Gemini `gemini-embedding-001` at 1536 dimensions.**
2. **Convert `chunks.embedding` to a real pgvector `vector(1536)` column** (Postgres)
   via migration `0004`, with a working rollback, and add an HNSW index.
3. **Unify the two embedders behind one config seam** (`embedding_provider`) mirroring
   the LLM factory, and **wire the embedder into the retrieval orchestrator** so the
   vector arm is live.
4. **Expand the corpus** with larger, original (paraphrased, license-clean) reference
   docs for the existing 4 products, each annotated with its official source URL.
5. **Stamp `embedding_provider + model + dim` onto `IndexVersion`** (the Tier 3
   guardrail) so future cross-provider failover is safe to add, without building the
   failover machinery now.

## Key Design Choices

### 1. Why Gemini `gemini-embedding-001` (not Voyage / OpenAI)

- **Reuses the existing key.** The stack already runs Gemini as the primary LLM
  (`CITEVYN_GEMINI_API_KEY`). The *same* key unlocks the embeddings endpoint. This
  upholds the project principle already encoded in `app/llm/factory.py`: "a single key
  of either kind is enough to get real answers." No new provider, no new secret.
- **Minimal, low-risk code.** `gemini-embedding-001` uses identical auth
  (`x-goog-api-key`), the same base URL, and a sibling endpoint (`:embedContent` vs
  `:generateContent`) as the existing `GeminiLLMClient`. The new client mirrors it
  almost line-for-line — same constructor triad (eager key-raise / injectable
  `http_client` / `_owns_client`+`aclose`), same **error-body-not-leaked** branch
  (issue #50), same `httpx.MockTransport` test pattern.
- Voyage (`voyage-3`, the aspirational config default) and OpenAI
  (`text-embedding-3-small`) were rejected **for now** only because each needs a brand
  new provider client and a brand new key that nothing else in the stack uses. They
  remain viable behind the same seam (see Deferred).

### 2. Why 1536 dimensions

- **pgvector cannot index vectors over 2000 dimensions** (HNSW/IVFFlat hard limit).
  Gemini's default output is 3072 → unindexable. We must truncate.
- Gemini uses **Matryoshka Representation Learning**; its *recommended* truncation
  sizes are 768 / 1536 / 3072 (pre-normalized at those sizes). **1536 is the largest
  recommended size that fits under the 2000-dim index limit** → best retrieval quality
  we can actually index.
- The previous config default (`embedding_dim=1024`) is not a recommended MRL size and
  is retired in favor of 1536.
- **Asymmetric task types:** ingest embeds with `task_type=RETRIEVAL_DOCUMENT`, queries
  with `RETRIEVAL_QUERY` — Gemini's documented retrieval-optimized modes.

### 3. Why the pgvector migration is mandatory (not optional)

Issue #51's Done criterion is literally *"vector retrieval must return hits on
Postgres, not `[]`."* The stored column was a pickled blob that the `cosine_distance`
operator cannot run against, so there is no way to close #51 without converting the
column to `vector(dim)`. Per `code_review.md` ("migration without rollback" blocks a
ship), migration `0004` ships a tested `downgrade()` that reverts `vector` → `bytea`.

### 4. StubEmbedder stays the default / offline path

`embedding_provider="stub"` remains the default so tests stay hermetic and no key is
required locally — exactly like the LLM stub. The real provider raises eagerly on a
missing key; the stub never does. Production is prevented from silently shipping stub
embeddings by a startup validator mirroring `validate_llm_provider`.

### 5. Corpus: original, license-clean, cited

We **rejected pasting verbatim official documentation** (Anthropic/OpenAI/Google docs
are copyrighted; verbatim copies into an open-source repo are a licensing liability).
Instead we expanded the four existing docs with larger **original paraphrased** content
across more `##` sections, each `SourceSpec` annotated with the real upstream URL so
citations resolve to a true source. This keeps ingest hermetic (no network) and the
diff reviewable. We stayed on the current 4 products (depth, not breadth) because depth
is what lifts answer quality; adding product areas is orthogonal churn.

## Alternatives Considered

### Cross-provider embedding fallback (Gemini → OpenAI/Voyage at query time) — REJECTED

A `FallbackEmbedder` mirroring `FallbackLLMClient` is technically easy but **unsafe for
embeddings**, and this is the single most important thing for a future maintainer to
understand:

- **LLM fallback is safe** because every answer is independent and stateless.
- **Embedding fallback is not**, because retrieval compares the *query* vector against
  *stored document* vectors, and cosine distance is only meaningful when **both come
  from the same model / vector space.**
- If an index is built with Gemini and a query-time fallback embeds with Voyage:
  - **different dim** → pgvector throws a hard dimension error; or
  - **same dim** → *worse*: no error, but silently wrong retrieval, and the LLM then
    confidently cites the wrong sources. This is a **silent data-correctness failure**.

Therefore a naive query-time cross-provider fallback is an **anti-pattern** and is not
implemented. The correct pattern is: the embedding model is a **property of the index**
(Tier 3, below); "failover" means **re-ingesting under the secondary provider as a new
index version**, with queries always using the model that built the active index.

## Tiered resilience model (what shipped vs. deferred)

| Tier | What it is | Status |
|---|---|---|
| **Tier 1** | Same-provider retry on transient errors (429/503/timeout) — same model, same vector space, pure resilience | **Shipped** |
| **Tier 2** | Provider *choice* via config (`CITEVYN_EMBEDDING_PROVIDER`); the chosen provider builds AND queries the index → always correct. No failover | **Shipped (Gemini wired; Voyage/OpenAI can be added behind the seam)** |
| **Tier 3 guardrail** | Stamp `provider + model + dim` onto `IndexVersion` so a query embedder can be checked against the model that built the active index | **Shipped (the stamp only)** |
| **Tier 3 failover** | Automatic re-ingest under a secondary provider + query-time model matching driven off the stamp | **Deferred** |

## Deferred / Future Work

Revisit these **only if Gemini proves insufficient** in practice:

1. **Tier 3 cross-provider failover machinery** — build on the `IndexVersion` stamp:
   detect Gemini outage at ingest → build a fallback index version under Voyage/OpenAI;
   query-time selects the embedder matching the active index's stamp. Guarded so a
   provider/dim mismatch between query and index is impossible.
2. **Additional providers behind the seam** — Voyage `voyage-3` (1024) and OpenAI
   `text-embedding-3-small` (1536) clients, selectable by `CITEVYN_EMBEDDING_PROVIDER`.
3. **CI pgvector image** — CI's `postgres:16` service must become
   `pgvector/pgvector:pg16` for the postgres-marked pgvector test to run in CI
   (addressed as part of this change; noted here for traceability).
4. **Batching / rate-limit tuning** — ingest embeds per-chunk today; batch the
   `embedContent` calls if corpus size grows enough to matter.
5. **Corpus growth via `refresh_sources.sh`** — the upstream-snapshot ingestion module
   (`db/ingest/`) referenced by `scripts/refresh_sources.sh` still does not exist; real
   (licensed) corpus refresh is a separate effort.

## Consequences

- **Schema change** (`0004`): Postgres `chunks.embedding` becomes `vector(1536)`;
  SQLite stays `LargeBinary` (hermetic tests unaffected). Rollback provided.
- **New dependency**: `pgvector` (Python) for the SQLAlchemy `Vector` column type.
- **Config**: `embedding_dim` default 1024 → 1536; `embedding_provider` gains a real
  `gemini` branch; `embedding_model` default → `gemini-embedding-001`.
- **Operational**: to get real cited answers end-to-end, set `CITEVYN_EMBEDDING_PROVIDER=gemini`
  and `CITEVYN_GEMINI_API_KEY`, then re-ingest (embeddings are model-specific — an index
  built under the stub must be rebuilt under Gemini). See `docs/RUNBOOK.md`.
