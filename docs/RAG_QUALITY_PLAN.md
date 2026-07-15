# RAG Quality — Implementation Plan

Status: **draft for review** (no code yet). Owner: TBD. Source: end-to-end diagnosis of the
live stack + review of the `rag_intro.ipynb` reference notebook.

---

## 0. Why this plan exists (retrospective)

The live system refuses most realistic questions while a ~200-line starter notebook answers
them well. Root cause is **not** the architecture (ours is more advanced) — it is that the
**definition of "done" was component-level, never outcome-level**:

1. **Scaffolding-first, data-last.** We built hybrid retrieval, exact-lookup, domain/intent
   routing, embedder provenance, cache invalidation, a contextual chunker, migrations, auth,
   rate limits, and prod deploy — but never wired and verified the core vertical slice
   (*real question → real embedded corpus → good answer*) as a working whole.
2. **No eval harness.** "540 tests green" measured component correctness, not product
   functionality. Nothing measured retrieval hit-rate or answer quality, so
   "semantic search returns 0%" went unnoticed.
3. **Silent stubs.** A stub embedder + a 5-sentence seed silently stood in for the real
   thing; nothing flagged that a core capability was inert (all chunk embeddings NULL).
4. **Vertical slices never closed.** Work advanced by layer; "user gets a good answer" was
   nobody's acceptance criterion, so each layer could be "done" while the whole never worked.
5. **Demo-driven verification bias.** Verification used corpus-matched questions that
   happened to pass, hiding the gap.

**Prevention (baked into this plan):** eval-first, walking-skeleton vertical slice,
outcome-level Definition of Done, no silent stubs for core capabilities, realistic/external
test questions.

---

## 1. Methodology — Eval-first, Spec-anchored TDD

- **Eval-first:** build the measurement harness (retrieval hit-rate + LLM-as-judge on a
  golden set) **before** any fix. Every subsequent PR must prove it moved the number, in CI.
- **Spec-anchored:** anything touching a contract/schema/public behavior gets a 1-page ADR
  in `docs/ADR/` first (embeddings column, BM25 index, retrieval result shape, cache key,
  ingestion).
- **TDD per component:** deterministic algorithms (BM25, RRF, adaptive floor, chunk prefix,
  embedding population, cache invalidation, provenance) get a failing test first.

Per-component rhythm: **Spec (if contract) → failing test → implement → green → eval delta.**

---

## 2. Current-state root causes (diagnosed live)

1. Vector arm dead — 5/5 chunks have NULL embeddings; index embedder stamp empty. *(dominant)*
2. Corpus = 5 seed sentences; real ingestion can't run in prod (#92).
3. Keyword = `ILIKE '%tok%'`, not BM25/`tsvector`.
4. F2 keyword ≥2-token floor over-refuses on a tiny corpus with no semantic fallback.
5. Hard domain scoping to a single product-area narrows recall.
6. Ops: 30/hr demo rate limit renders as content failures; no spell tolerance.

---

## 3. Reference-notebook mapping (what we have vs missing)

| Notebook step | CiteVyn | Note |
|---|---|---|
| Load | ✅+ | worker fetchers/parser; live path uses unshipped fixtures (#92) |
| Chunk | ✅ ahead | contextual `chunker.py` — but bypassed by flat seed data |
| Embed & store | ⚠️ arch only | pgvector/HNSW/Gemini wired; **embeddings NULL** |
| Retrieve | ✅ ahead (degraded) | hybrid vs vector-only; but vector dead + keyword `ILIKE` |
| Generate | ✅ equal | grounded prompt, citations, IDK contract |
| Eval | ❌ missing | **adopt the notebook's hit-rate + LLM-judge** |
| UI | ✅ ahead | React vs Gradio |

Four "advanced" notebook limitations: **Hybrid** (arch present, degraded), **Contextual
chunking** (implemented, bypassed), **Query rewriting/decomposition** (missing),
**Conversation memory** (missing).

---

## 4. Pre-flight — locked decisions

- **Methodology:** Eval-first, Spec-anchored TDD.
- **Corpus (D1):** fetch official docs live (prod HTTP fetcher over `llms-full.txt`-style
  sources) via the existing contextual chunker.
- **Embedder (D2):** Gemini `gemini-embedding-001` @ 1536 (#51) — needs a live key.
- **Chunking (D3):** contextual `chunker.py`, ~512/50.
- **Keyword (D4):** Postgres `tsvector` + GIN (no new dep).
- **Reranker (D5):** RRF fusion first; cross-encoder deferred.
- **Eval set (D6):** 20 golden Q&A to start → 50–100.
- **Branching (D7):** small single-workstream PRs straight to `main`, each eval-gated.

Prereqs: branch off clean `main` (after #94 resolves); Gemini embeddings key present;
docs sources fetchable.

---

## 5. Per-PR execution loop (points 3–6)

```
Spec (if contract) → failing test(s) → implement → tests green
   → run eval harness, record delta → open PR
   → FAN-OUT REVIEW (blast-radius-sized, §6)
   → resolve ALL blocking findings → RE-RUN fan-out
   → repeat until a full pass yields zero CONFIRMED blocking findings ("branch clean")
   → release-readiness ship gate → merge
```

**Merge rule:** merge only when (a) fan-out returns zero confirmed blocking findings after
the resolve-loop, (b) tests green, (c) **eval score ≥ baseline**, (d) release-readiness = SHIP.

---

## 6. Multi-agent fan-out review (blast-radius-sized)

| PR touches | Fan-out (parallel), then synthesize |
|---|---|
| Algorithm/logic | `code-review` + `pr-test-analyzer` + adversarial verify per finding |
| Schema/migration | above + `security-review` + `type-design-analyzer` + migration-reversibility (T3) |
| Ingestion/worker/infra | above + `verify` (end-to-end) + `silent-failure-hunter` |
| Retrieval/answer contract | above + **eval harness as a hard gate** |
| Any → ship | close with `release-readiness-review` |

Large/cross-cutting phases escalate to an **ultracode Workflow** (multi-agent, adversarial
verify, loop-until-clean).

---

## 7. Ultracode parallelization strategy

Concurrency cap ~`min(16, cores−2)` per workflow; `parallel()` = barrier, `pipeline()` =
no-barrier streaming; parallel file-mutating agents use **git worktree isolation**.

**Fan-out (parallel) — high value:**
- **Review** (per PR): review dimensions + per-finding verifiers in parallel; loop until clean.
- **Research/mapping**: parallel readers over retrieval/ingestion/cache/routing.
- **Design panels**: N independent approaches to a hard design, scored in parallel, synthesized.
- **Test authoring**: exact/keyword/semantic/adversarial suites concurrently.
- **Golden-set drafting**: one agent per product area, then dedupe.
- **Disjoint implementation**: non-overlapping components in parallel worktrees, then integrate.

**Critical path (sequential):** eval harness → embeddings → ingestion → retrieval tuning →
advanced. Same-file edits and merges are serialized.

**Per-phase parallelism:**
- **Phase 0:** eval-harness build ∥ ADR authoring ∥ golden-set drafting (disjoint) → integrate.
- **Phase 1:** embedder wiring ∥ ingestion fetcher (mostly disjoint; worktrees) → integrate → eval.
- **Phase 2:** BM25 index ∥ RRF fusion (different files) parallel; keyword-floor sequenced (shared file).
- **Every PR:** review fan-out fully parallel.

Trade-offs: high token cost; parallel code-gen needs a synthesis/integration step so designs
don't diverge; keep merges serial.

---

## 8. Phased implementation

**Phase 0 — Measurement & specs (first)**
- PR0.1 Eval harness: golden set + hit-rate + LLM-judge, local + CI (extends #84). Baseline now.
- PR0.2 ADRs for embeddings-population, tsvector, ingestion.

**Phase 1 — Foundation (walking skeleton)**
- PR1.1 Populate embeddings at seed + ingest; stamp index provenance. (TDD + eval jump.)
- PR1.2 Real ingestion (#92): prod HTTP fetcher → contextual chunker → embed → candidate → promote.
- Exit: semantic search works on a real corpus; hit-rate ≥ target (e.g. 80%).

**Phase 2 — Retrieval quality**
- PR2.1 Keyword `ILIKE` → `tsvector`/BM25 + GIN.
- PR2.2 RRF fusion + adaptive keyword floor.
- PR2.3 Soft domain scoping (boost, not hard filter).
- PR2.4 (optional) cross-encoder reranker.

**Phase 3 — Advanced**
- PR3.1 Query rewriting/decomposition (multi-hop).
- PR3.2 Conversation memory (prior turns → retrieval + prompt; use `sessions.summary`).

**Phase 4 — UX & ops**
- PR4.1 Graceful fallback UX (nearest-doc suggestions).
- PR4.2 Rate-limit segmentation + distinct 429 UI.
- PR4.3 VectorDegrade / dead-embedding health signal.

---

## 9. Thorough test plan (standing regression suite)

- **Retrieval modes:** exact-lookup (env vars, flags, error strings); keyword/BM25 (ranking,
  multi-term, stopword-only); **semantic/paraphrase (zero literal overlap)**; hybrid fusion;
  reranking reorders.
- **Data/ingestion integrity:** every chunk embedded at correct dim; index provenance stamped
  and matching; contextual prefix present; chunk size/overlap bounds; idempotent re-ingest;
  candidate-vs-active isolation (F1 guard).
- **Routing:** every domain + unsupported; ambiguous/mixed; intents
  (exact_lookup/how_to/faq/clarify/unsupported); greeting variants vs greeting-prefixed asks.
- **Synthesis & grounding:** citations resolve to real chunks; IDK only when truly absent;
  no-hallucination (judge + citation overlap); answer-style variants.
- **Corner/adversarial:** misspellings ("waht", "moidels"); empty/whitespace/very-long;
  punctuation/case/unicode; code/symbols; prompt injection in docs; citation gaming; refusal
  bypass; out-of-corpus vs in-corpus; multi-hop.
- **Conversation (post 3.2):** pronoun follow-ups, topic switch, context carry + reset.
- **Non-functional:** rate-limit (429 after N, window reset, distinct UI); concurrency
  (independent streams — regression guard for c792681); degrade/failover (vector/LLM/embedder);
  cache correctness (hit/miss, staleness invalidation #65, no caching degraded #70/#72); latency.
- **Eval gate (CI):** golden hit-rate ≥ threshold and mean judge score ≥ threshold fail the
  build on regression.

---

## 10. Definition of Done & risks

**DoD (per PR):** spec/ADR (if contract) · tests (happy/failure/edge) · eval delta ≥ 0 ·
fan-out review clean · release-readiness SHIP · BACKLOG/issues synced.

**Risks:** embeddings API cost/limits (batch + cache); ingestion network/allowlist in prod
(#92 fetcher + fixtures fallback); dual-active index during promote (F1 guard + tests);
eval-set bias (grow to 50–100 + adversarial cases).

---

## 11. Tracking

- New: **RAG eval harness** (Phase 0), **Populate chunk embeddings + provenance** (Phase 1).
- Existing folded in: **#92** (real ingestion), **#84** (golden-in-CI overlap), **#87**
  (legit no_answer / routing), **#51** (real embedder), **#59** (embedding providers/scale).
