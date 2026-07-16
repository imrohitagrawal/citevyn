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
  **✅ DONE** (issue #96) — see §8a for the harness + measured baseline.
- PR0.2 ADRs for embeddings-population, tsvector, ingestion.

## 8a. Phase 0 status + measured baseline (issue #96)

**Harness** (`make eval` / `python -m tests.eval.runner`; CI gate =
`backend/tests/test_eval_harness.py`, hermetic, in the standard `pytest + lint` job):

- Golden set: `tests/eval/golden.jsonl` — 20 cases, all 5 product areas × {literal,
  paraphrase} + 5 out-of-corpus refusals. Anchored to the **conftest** seed corpus.
- Metric 1 — **retrieval hit-rate**: does any top-k hit come from the expected source?
  Measured on the live routing + `HybridRetriever` path (fully hermetic).
- Metric 2 — **LLM-as-judge** (1–5 vs expected gist): opt-in; uses the configured LLM
  (Gemini free). Under the stub it reports "unavailable" — never a faked score.
- Gate thresholds (`tests/eval/thresholds.py`): literal hit-rate == 1.0, overall answerable
  ≥ 0.60, refusal leaks == 0. Fail on **regression**, not on the known-dead state.

**Retrieval baseline (measured 2026-07-16, conftest seed corpus, SQLite / vector arm dead):**

| Metric | Value |
|---|---|
| Overall answerable hit-rate | **10/15 = 0.667** |
| — literal | **1.000** (10/10) |
| — paraphrase | **0.000** (0/5) |
| Refusal leaks | **0/5** (all correctly retrieve nothing) |

The paraphrase 0.000 is the point: the vector arm is dead (all embeddings NULL, #97). The
five paraphrases split into two buckets that pre-attribute later-phase deltas:

- **Phase-1-sensitive** (route to the correct domain but keyword-miss; embeddings alone
  should fix): `claude_api_par_throttle`, `codex_par_engine`, `gemini_api_par_auth`.
- **Phase-2-sensitive** (route to `unsupported` under hard domain scoping; also need soft
  scoping): `claude_code_par_toolgate`, `citevyn_par_membership`.

**Judge baseline: NOT YET MEASURED** at Phase-0 authoring time. The then-configured Gemini
model (`gemini-2.5-flash`) returned **404** (retired for new projects) and the free tier then
**429**s, so no real answer/judge scores could be produced. **Resolved by #99:** the primary
is now `gemini-flash-latest` (free) with an `openai/gpt-4o-mini` paid fallback, both
live-verified — re-run `CITEVYN_LLM_PROVIDER=gemini … make eval` to fill this row. The harness
handled the outage correctly — it recorded loud per-case errors and reported "no scores"
rather than fabricating one (no-silent-stub
principle). This is a **pre-existing LLM-provider config issue** (stale model/endpoint),
orthogonal to the harness; tracked as a follow-up. Re-run `CITEVYN_LLM_PROVIDER=gemini
CITEVYN_GEMINI_API_KEY=… make eval` once the model id is fixed to fill in the judge row.

## 8a-1. Phase 1 measured results (PR1.1, #97 — vector arm revived)

**Provider deviation (see ADR-0003 addendum):** Gemini free-tier embeddings hit their
**1000/day** cap during this run, so the embedder for Phase 1 is **OpenRouter
`openai/text-embedding-3-small` @ 1536** (native dim = the pgvector column; no migration),
used for BOTH corpus and query (one vector space). Added behind the seam alongside `gemini`.

**Retrieval (measured on REAL Postgres+pgvector, `python -m tests.eval.runner --postgres`):**

| Metric | Phase-0 baseline (SQLite, arm dead) | Phase-1 (Postgres, arm live) |
|---|---|---|
| Overall answerable hit-rate | 10/15 = **0.667** | 13/15 = **0.867** |
| — literal | 1.000 | **1.000** |
| — paraphrase | 0.000 | **0.600 (3/5)** |
| Refusal leaks | 0/5 | **0/5** |

The **+3 paraphrases** are exactly the Phase-1-sensitive ones (`claude_api_par_throttle`,
`codex_par_engine`, `gemini_api_par_auth`) that route to their correct domain. The two
remaining (`claude_code_par_toolgate`, `citevyn_par_membership`) route to `unsupported`
under hard domain scoping — **Phase 2** (soft scoping) territory, out of PR1.1 scope.

**Semantic-quality proof (not the golden number).** With one chunk per area under hard
scoping, the golden paraphrase metric proves the vector *plumbing* is alive on Postgres —
a *stub* embedder yields the same 3/5. What proves the embeddings are **semantic** is the
opt-in `test_eval_semantic_discrimination` control: on the SAME corpus + golden paraphrases,
GLOBALLY (no scoping), the real embedder routes **5/5** to the correct area while the
hash-bucket stub scores at chance (≤2/5). Both numbers together = "semantic search works."

**Isolation/safety of the Postgres eval:** opt-in `--postgres`; refuses a stub embedder, a
production target, a non-Postgres URL, or a non-empty catalog; seeds with `commit=False`
under a unique per-run index_version and rolls back on every exit path → **verified zero
residue** (chunks/documents/index_versions all 0 after a run). The hermetic SQLite gate
(`test_paraphrase_baseline_is_dead`) is unchanged and still guards the SQLite path.

**Judge baseline (Phase 1): deferred.** The LLM-judge drives the full orchestrator (real
generation per case). During this run Gemini free-tier **generation** was ALSO rate-limited
(`llm_primary_unavailable_falling_back` → paid `openai/gpt-4o-mini` fallback), and the long
judged transaction was interrupted when the local Docker Desktop restarted. The retrieval
gate above is fully proven and is the hard eval gate for PR1.1; the judge row is orthogonal
(answer wording, not retrieval) and is refilled by re-running, once Gemini generation quota
resets, `CITEVYN_EMBEDDING_PROVIDER=openrouter CITEVYN_LLM_PROVIDER=gemini python -m
tests.eval.runner --postgres` against a migrated-but-empty Postgres. Tracked as a follow-up.

## 8a-2. Phase 2 measured results (answer when grounded)

Questions that don't NAME a product routed to `unsupported` and were refused before any
retrieval. Phase 2 retrieves GLOBALLY for those and answers when grounded, with a confidence
gate (loose floor + relative margin — a cheap cost pre-filter) and the **LLM grounding-refusal
as the authoritative net**. Why the LLM, not the gate: empirically **no fixed retrieval
threshold separates refusals from valid answers across corpus densities** (conftest 5-chunk
needs margin ~0.15 — `refusal_openai` floor 0.373 > valid `citevyn_par` 0.341; the 33-chunk
worker corpus needs ~0.04). So the eval's refusal metric was redesigned: when the LLM ran, a
refusal "leaks" only if the **orchestrator answered it**, not if retrieval merely surfaced a
chunk the LLM then declines.

**Measured (real Postgres+pgvector; openrouter embeddings + LLM; `--postgres` judged run):**

| Metric | Phase-1 (13/15) | Phase-2 |
|---|---|---|
| Overall answerable hit-rate | 0.867 | **15/15 = 1.000** |
| — paraphrase | 0.600 (3/5) | **1.000 (5/5)** |
| — literal | 1.000 | 1.000 |
| Refusal leaks — retrieval (informational) | 0/5 | 1/5 (`refusal_openai` retrieves a chunk) |
| **Refusal leaks — judged (orchestrator declined)** | — | **0/5** (all decline, incl. openai) |
| **Judge mean (answer quality, 1–5)** | _(deferred)_ | **5.00** over 20 scored, 0 errors |

Zero DB residue (rollback verified). **Hermetic CI gate unchanged** (SQLite vector arm dead →
overall 0.667, paraphrase 0.0, 0 leaks); the Phase-2 gain is Postgres-only and
`test_paraphrase_baseline_is_dead` stays valid. The judged run also fills the §8a Phase-1
**judge baseline** row (mean 5.00). Follow-ups: grow the golden set + validate the gate on a
larger real corpus (#59); the retrieval gate needs a real LLM as the net — a stub-LLM deploy
under `answer_when_grounded` leans entirely on the loose gate (production requires a real LLM).

## 8a-3. Phase 3 measured results (multi-hop query decomposition)

Cross-product questions ("compare the Claude API and Gemini rate limits") named ≥2 products but
`classify_domain` returned only the first, so retrieval scoped to one area and the other product
was never retrieved — the answer half-covered it or declined. `classify_domains` now returns every
named product area (non-overlapping matches; CiteVyn short-circuits to preserve #49), and the
orchestrator retrieves each and round-robin-merges (`retrieve_multi`), so the answer covers all.

**Measured (real Postgres+pgvector; openrouter embeddings + LLM; `--postgres` judged run):**

| Metric | Value |
|---|---|
| Multi-hop hit-rate (EVERY named area retrieved) | **3/3 = 1.000** |
| Core overall (literal + paraphrase) | 15/15 = 1.000 (unchanged) |
| Refusal leaks — judged | 0/5 (unchanged) |
| Judge mean (now over 23 cases incl. multihop) | **4.91**, 0 errors |

Zero residue. **Hermetic CI gate unchanged** (multihop is its own bucket, EXCLUDED from the gated
overall — it needs the live vector arm to hit both areas, so it is Postgres-only-provable and gated
only on the `--postgres` run). The eval's `_retrieve_sources` mirrors the orchestrator's multi-hop
routing so a passing eval implies a working product. Follow-up: **conversation memory** (Phase 3's
other half) needs multi-turn eval infrastructure — a separate PR.

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

## 11a. LLM & embedding provider strategy (testing tiers)

Keys present in `.env`: **Gemini ✓, OpenRouter ✓** (OpenRouter wired as *fallback* only).

- **Embeddings:** Gemini `gemini-embedding-001`@1536 (`CITEVYN_EMBEDDING_PROVIDER=gemini`).
  Free tier is **rate-limit-bound for bulk corpus embedding** (embedding a full docs set = thousands
  of calls). Mitigate: **bounded corpus in dev** (hundreds of chunks, not 13k), batching, throttling,
  and caching embeddings; scale the corpus only once the pipeline is proven. The golden set is tiny (20).
- **Generation — dev / eval loop:** **Gemini (free).** `CITEVYN_LLM_PROVIDER=gemini` (current). OpenRouter
  stays fallback-only, so dev spend ≈ 0.
- **Generation — final exact testing:** **OpenRouter (paid, on demand).** Switch via
  `CITEVYN_LLM_PROVIDER=router` and pin the model with `CITEVYN_OPENROUTER_MODEL`
  (knob exists; default `openai/gpt-4o-mini` since #99). Use only for the final gated eval run;
  specify the exact model at that point. Do **not** leave the provider on OpenRouter during iterative testing.
- **Cost control:** the free Gemini path carries all iterative work; OpenRouter is reserved for the final
  quality run (and as resilience fallback). If zero OpenRouter spend is required during dev, keep provider
  = gemini (fallback won't fire unless Gemini errors).

## 11b. Budget (5x-token plan) & ultracode

The 5x-token plan makes **fan-out affordable**: run reviews, research, design panels, and test-authoring
as parallel ultracode workflows. Keep the build **critical path sequential** and **merges serial**
regardless of budget. Bound each ultracode run and prefer `pipeline()` streaming over big barriers.

## 11c. Estimate & first-demo milestone

**First meaningful live demo = end of Phase 0 + Phase 1** — a real, embedded corpus with working
semantic search, eval-proven (not cherry-picked). Effort estimate (me implementing, ultracode fan-out
for review/exploration, gated by the review-until-clean loop and your merge cadence):

| Milestone | Effort | Notes |
|---|---|---|
| Phase 0 — eval harness + golden set + baseline | ~1 session | mostly independent; parallelizable |
| Phase 1 — embeddings + bounded real ingestion + promote | ~1–2 sessions | **embedding rate limits are the main variable** |
| **→ First live demo (semantic answers on real corpus)** | **~2–3 sessions total** | the walking-skeleton milestone |
| Phase 2 — retrieval quality (BM25/RRF/scoping) | ~1–2 sessions | |
| Phase 3 + 4 — advanced + UX/ops | ~2–3 sessions | |
| **Full plan** | **~6–9 focused sessions** | wall-clock depends on review iteration, merge cadence, embedding limits |

These are **effort** estimates, not calendar promises; each PR runs the fan-out-review-until-clean loop.

## 12. Tracking

- New: **RAG eval harness** (Phase 0), **Populate chunk embeddings + provenance** (Phase 1).
- Existing folded in: **#92** (real ingestion), **#84** (golden-in-CI overlap), **#87**
  (legit no_answer / routing), **#51** (real embedder), **#59** (embedding providers/scale).
