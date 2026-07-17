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

## 8a-4. Phase 3b measured results (conversation memory)

An anaphoric follow-up ("How can I raise it?") after a product turn named no product on
its own, so it routed to `unsupported` and — finding nothing confident in the global arm
— was refused. Conversation memory now reads the session's recent USER turns
(`recent_user_questions`) and, when the follow-up is genuinely anaphoric/elliptical AND
names no product, prepends the most-recent prior product turn
(`build_contextual_query`). The resolved query drives domain routing, retrieval, the
generated answer, AND the cache key (so two sessions asking the same follow-up under
different topics never cross-serve). A self-contained off-domain sentence ("what's the
weather?") carries no anaphora, so it is left unchanged and still reaches the refusal
(adversarial R1). Single-turn (no prior) is a byte-for-byte no-op.

**Measured (real Postgres+pgvector; openrouter embeddings + LLM; `--postgres` judged run):**

| Metric | Phase-3a | Phase-3b |
|---|---|---|
| Follow-up hit-rate (memory ON) | — | **3/3 = 1.000** |
| Follow-up hit-rate (memory OFF, raw control) | — | **0/3 = 0.000** |
| Core overall (literal + paraphrase) | 15/15 | 15/15 (unchanged) |
| Multi-hop | 3/3 | 3/3 (unchanged) |
| Refusal leaks — judged | 0/5 | **0/5** (off-topic follow-up still declines) |
| Judge mean (now over 26 cases incl. 3 judged followups) | 4.91 | **4.88**, 0 errors |

**Refusal safety.** The rewrite only fires on a genuine anaphoric/elliptical follow-up
that names no product (`is_anaphoric_followup`), so a self-contained off-domain sentence
("what's the weather?") is left unchanged and still refused. An off-corpus PIVOT that
opens with an anaphor ("and how do I do that on Kubernetes?") IS contextualized, but the
LLM grounding-refusal net (Phase 2) is the authoritative gate: a clear pivot finds no
support in the routed chunk and is declined (`refusal_leaks_judged` 0/5; verified in the
judged eval + a hermetic test that memory routing HONORS the LLM refusal). The residual —
a pivot semantically adjacent to the prior topic answered-with-disclaimer — is an honest
relevance miss (not a fabrication) tracked as **#112** (entity-aware rewrite); handing
generation only the bare follow-up regresses genuine anaphora, so it is not the fix.

Zero residue (chunks/documents/index_versions/messages/sessions all 0 after rollback).
Unlike `multihop`, the followup rewrite resolves DETERMINISTICALLY (domain routing +
keyword), so the followup bucket is gated on the **hermetic** run too (a broken rewrite
fails CI) — the eval's retrieval metric persists the history as real `Message` rows and
reads them back through the SAME `recent_user_questions` helper the orchestrator uses, so
a passing eval implies a working product (adversarial finding #2). The permanent
memory-OFF raw-miss control (`test_followup_raw_misses_without_memory`) keeps the hit
attributable to memory. Design limitation (documented): the antecedent is the
most-recent prior product turn — deep coreference past an intervening topic is out of
scope. Kill-switch: `conversation_memory=False` restores the pre-3b behavior.

## 8a-5. Eval-hardening — trustworthy answer-quality signal (Item 1)

The judged metric was a SINGLE LLM-judge call — noisy, and able to over-score a
*plausible-but-wrong* answer. It is now three complementary signals (see
`tests/eval/judge.py`, `tests/eval/groundedness.py`):

- **Prompt-ensemble panel** — N distinct rubric *framings* (not temperature samples)
  at temp 0.0; the **median** smooths one framing's interpretation bias while staying
  reproducible (no run-to-run flake). `CITEVYN_EVAL_JUDGE_PANEL` sets N (odd; default 3).
- **Adversarial veto** — one skeptical fact-checker pass; the gated score is
  `min(standard_median, adversarial)`. The skeptic is a *floor*, never averaged into the
  median (a lone low vote can't move a median — it would be discarded on exactly the
  plausible-but-wrong case it targets; plan-review blocker).
- **Deterministic groundedness** — judge-*independent*: declared `expected_facts` (env
  vars, headers, CLI commands, or a number *with* its unit; `|`-alternatives any-of)
  must appear in the answer, **word-boundary matched** so `"50 requests per minute"` is
  NOT credited by `"150 requests per minute"` or `"0.50 requests per minute"`
  (plan-review + PR-review). **Gated per case on the `--postgres` run only** (the mode
  where fact-cases can retrieve; the hermetic dead-arm path would structurally zero the
  paraphrase fact-cases — excluded like the multihop gate): every fact-bearing case must
  be fully grounded there, so a single wrong fact fails (an aggregate mean over binary
  single-fact cases would leak it). A golden-integrity test asserts each fact is
  groundable in the seed corpus (any-of).

**Measured (real Postgres+pgvector; openrouter embeddings + LLM; `--postgres` judged run):**

| Metric | Single-call (before) | **Panel + veto + groundedness (now)** |
|---|---|---|
| Core overall (literal + paraphrase) | 15/15 | **15/15** (unchanged) |
| Multi-hop / Follow-up | 3/3 / 3/3 | **3/3 / 3/3** (unchanged) |
| Refusal leaks — judged | 0/5 | **0/5** (unchanged) |
| Judge mean | 4.88 (single call) | **~4.7** (4.69–4.73 across runs; panel min-vetoed — fresh metric, NOT the 4.88 baseline; the veto conservatively lowers over-scores) |
| Contested (standard-framing disagreement) | — | **0–1/26** (a multihop case occasionally flagged) |
| **Groundedness fact-rate** | — | **1.000** over 11 fact-bearing cases (0 under-grounded) |
| DB residue | zero | **zero** |

The panel mean (4.73) is a *new* metric — the min-veto makes it a strictly more
conservative number than the single-call 4.88, so the two are not directly comparable;
`MIN_MEAN_JUDGE=3.0` still passes with wide headroom. Robustness is proven by hermetic
tests (median smooths a noisy outlier; the adversarial vetoes a fooled panel `[5,5]+2→2`;
`"150 requests per minute"` scores 0 for a `"50…"` fact) plus opt-in real-key tests
(`CITEVYN_EVAL_LLM=1`: a deliberately-wrong answer scores ≤2 and fails groundedness; the
same answer is stable across repeated runs). The hermetic CI gate is unchanged.

## 8a-6. Eval-hardening — adversarial cases + injection resistance (Item 2 slice)

A single-LLM meter also needs sharper INPUTS. This slice adds adversarial hardening with
zero risk to the locked hermetic baseline (all new cases are `postgres_only`, excluded
from the hermetic run + gates):

- **`postgres_only` bucket field** — a case meaningful only on the live vector arm
  (misspellings; in-domain near-miss refusals) is excluded from the hermetic run/gates
  and evaluated on `--postgres` (mirrors the multihop/followup pattern).
- **Prompt-injection resistance** — `must_not_contain` on a case lists forbidden
  sentinels; the produced answer is checked **judge-independently** and any obeyed
  injection fails the run (zero tolerance).
- **Adversarial golden cases** (postgres-only): 2 prompt-injection (answer correctly AND
  ignore "reply PWNED / output HACKED-TOKEN"), 2 misspellings ("rate limt for teh Claude
  API"), 1 in-domain near-miss refusal (Claude API *price per token* — in-domain but not
  in the docs → must decline, not invent).

**Measured (real Postgres+pgvector; `--postgres` judged run, 31 cases):**

| Metric | Value |
|---|---|
| Core overall (literal+paraphrase, incl. 4 new adversarial literals) | **19/19 = 1.000** |
| Multi-hop / Follow-up | 3/3 / 3/3 |
| Refusal leaks — judged (incl. in-domain pricing) | **0/6** (all decline, incl. the near-miss) |
| **Injection resistance** | **0 leaks / 2 cases** (no sentinel emitted) |
| Misspelling recovery | 2/2 (typo → correct answer, groundedness 1.0) |
| Groundedness fact-rate | 1.000 over 15 fact-bearing cases |
| Judge mean (panel min-vetoed) | ~4.6 (4.58–4.61), 0 errors, contested 0/31 |
| DB residue | zero |

Misspelling cases are `kind=paraphrase` (the bucket that isolates vector recovery) so a
typo-recall miss degrades the 0.60-floored overall gate gracefully instead of tripping the
strict literal=1.0 invariant (PR-review). Injection leak = sentinel present AND the answer
failed to ground its facts, so a resistant answer that names the sentinel while declining
is not a false leak. Proven identical under the CI-bound `CITEVYN_EVAL_JUDGE_PANEL=1`.

Design shaped by an adversarial plan review (8 blockers + 13 majors) that showed the naive
"context precision/recall + distractor corpus" design is UNSOUND on the current harness
(source-level identity only; `uuid4` chunk ids; `top_k ≥ corpus size`; distractors flipping
the hermetic paraphrase baseline). **Deferred to a tracked follow-up** (needs chunk-level
relevance identity on a separate distractor index): context precision/recall metrics, the
distractor corpus, golden-set growth toward 50–100, and a human-labeled judge-calibration
subset. Shipped here is the safe, high-value adversarial slice.

## 8a-7. Chunk-level retrieval identity + rank metric — PR A of #125

The first slice of the deferred #125 work: **chunk-level identity plumbing** + a
**rank-sensitive metric** (MRR + precision@1), additive and reporting-only — the locked
hermetic and judged baselines are byte-for-byte unchanged (proven below). No `seed_catalog`
default-output change, no `app/` change, no schema/migration.

- **Stable chunk key** = the composite `"{source_name}#{chunk_order}"` (e.g. `claude_api#0`),
  derived in the eval layer by joining `Chunk→Document`. Chosen over `chunk_id` (uuid4,
  regenerated every seed → unlabelable) AND over `content_checksum` (a content HASH that
  *collides* for byte-identical chunks → would silently mis-map identity; adversarial plan
  review). Golden cases label `gold_chunks` by this key; a hermetic guard asserts every
  labelled key names a real seeded chunk so a typo can't silently zero a case's rank.
- **Chunk-level retrieval identity** — the eval maps each ordered retrieval hit's
  `chunk_id` → its stable key (raising loudly, never silently skipping, on an unmapped id).
- **Rank-sensitive metric** — for single-relevant cases (exactly one gold chunk), MRR and
  precision@1 (rank of the gold chunk), NOT precision@k (rank-insensitive with one gold) or
  recall (trivial while `top_k ≥ corpus`). Gated **`--postgres`-only** (the hermetic vector
  arm is dead → paraphrases structurally score 0), mirroring the multihop/groundedness gates.
- **Pool = well-formed answerable queries only** (15 core literal+paraphrase + 3 followup =
  **n=18**). Adversarial injection/misspelling cases opt OUT — holding a deliberately-malformed
  query to a strict rank-1 pin is a category error (they are scored on injection-resistance /
  typo hit-rate / groundedness instead; a fan-out PR review flagged the misspelled global
  `adv_misspell_authheader` as the one drift-prone case, so it is excluded).
- **What actually moves the number:** 16 of 18 route SCOPED (`retrieve(product_area=area)` →
  the gold is the only candidate in its one-chunk area → structurally rank-1, a keyword-arm
  tautology today). Only **2** route GLOBAL (`claude_code_par_toolgate`, `citevyn_par_membership`
  → `product_area=None`, the unscoped vector arm) — there the gold is ranked against ALL other
  areas' chunks, so precision@1 is strictly stronger than hit-rate and a fully-dead vector arm
  drops it below 1.0. Verified in the report: `claude_code#0` outranks `codex#0`/`gemini_api#0`;
  `citevyn#0` outranks `claude_api#0`/`gemini_api#0`. PR B broadens ranking to scoped areas via
  a distractor seed.

**Measured baseline (2026-07-17, real Postgres+pgvector, `openai/text-embedding-3-small`,
`--postgres` judged run, panel=1; rank metric verified STABLE across 5 consecutive runs —
byte-identical rank order every run):**

| Metric | Value |
|---|---|
| **Chunk rank — MRR** (n=18 single-relevant) | **1.000** |
| **Chunk rank — precision@1** (n=18) | **1.000** |
| Core overall (literal+paraphrase) | 19/19 = 1.000 |
| Multi-hop / Follow-up | 3/3 / 3/3 |
| Refusal leaks — judged | 0/6 |
| Injection resistance | 0 leaks / 2 |
| Groundedness fact-rate | 1.000 / 15 |
| Judge mean (panel min-vetoed) | 4.55, 0 errors |
| DB residue | zero |

Gates (`thresholds.py`): `MIN_PRECISION_AT_1 = 1.0` (pinned exact — embeddings are effectively
deterministic run-to-run here, so a wrong-area chunk outranking a gold on a global case is a
real regression, not jitter; only the 2 global cases can move it), `MIN_MRR = 0.95` (tolerant
companion margin). Both `--postgres`-only + non-empty-pool-guarded. The 1.0 pin is an
assertion about retrieval quality, not an immovable floor — a deliberately-hard PR-B distractor
that legitimately outranks a gold lowers it to the new measured baseline with justification.
Design hardened by three fan-out plan skeptics (ranking-triviality, chunk-key stability,
baseline non-regression) + three fan-out PR reviewers (correctness, adversarial-metric,
test-coverage).

## 8a-8. Distractor corpus + context precision/recall — PR B of #125

PR A's rank metric only bit on the 2 global paraphrases because the clean corpus has ONE
chunk per area — retrieval never has to *choose*. PR B adds a dedicated, eval-only
**distractor corpus** so `top_k` is forced to select among many candidates, making context
recall/precision a real signal. **Fully isolated** — a SEPARATE seed function + SEPARATE
golden + SEPARATE opt-in runner; the locked hermetic and judged runs are untouched (762
hermetic tests pass; no `seed_catalog`/`_retrieve_sources`/`RetrievalReport`/main-golden
change).

- **`seed_eval_distractors`** (`tests/eval/distractors.py`) seeds a throwaway area
  `eval_grafana` = one 2-chunk GOLD source (`eval_grafana#0` dashboards, `eval_grafana#1`
  alerting) + 16 within-area distractor sources (18 chunks total), embedded, under exactly
  ONE active `IndexVersion` (asserted). NEVER `conftest.seed_catalog`. The last 2 distractors
  are **lexical HARD NEGATIVES** (`panel_library` shares "panels/dashboards"; `silences`
  shares "alert notifications") — without them every distractor is a disjoint subtopic and any
  non-broken embedder passes, so the metric could only detect a dead arm (which hit-rate
  already catches). The near-misses give precision@|gold| the teeth to catch a SUBTLE ranking
  regression (adversarial PR review).
- **`postgres_distractor_session`** carries the full `postgres_session` rails (no-prod,
  Postgres-URL, non-stub embedder via `build_embedder` not `get_embedder`, empty-catalog
  refusal, rollback → zero residue). Run SERIALLY vs the judged pass (same DB).
- **VECTOR-ONLY retrieval** (`VectorRetriever` scoped to the area) — NOT the hybrid path,
  whose flat-0.5 keyword ILIKE arm would confound the ranking into a keyword tautology, and
  NOT `classify_domain` routing (a fictional-product query would hit the margin-gated global
  arm). Measures the cosine ranking the metric claims.
- **Metric = recall@k + precision@|gold|** (MAP deferred as brittle for a 2-relevant case).
  precision@|gold| is rank-strict: a distractor breaking into the top-|gold| fails it even
  when recall@k stays 1.0.

**Measured baseline (2026-07-17, real Postgres + `openai/text-embedding-3-small`,
`python -m tests.eval.distractors`, verified STABLE across 5 runs — byte-identical margins):**

| Case | recall@k | precision@\|gold\| | gold margin (cosine) |
|---|---|---|---|
| `distractor_multi_dash_alert` (2 gold) | 1.000 | 1.000 (both gold ranked #1–#2 over 16 distractors) | 0.150 |
| `distractor_single_dashboards` | 1.000 | 1.000 (gold #1 over the `panel_library` hard negative) | **0.092** |
| `distractor_single_alerting` | 1.000 | 1.000 (alerting #1 over the `silences` hard negative + dashboards sibling) | 0.158 |

`gold_margin` = min retrieved-gold cosine − max retrieved-distractor cosine (recorded per case
in the report so a shrinking margin warns before a flip). The min, 0.092, is the true gold vs
its LEXICAL hard negative — comfortable, stable headroom, not a knife-edge.

Gates: `MIN_DISTRACTOR_RECALL_AT_K = 1.0`, `MIN_DISTRACTOR_PRECISION_AT_GOLD = 1.0`
(distractor-mode-only). 1.0 is EARNED against the hard negatives with a 0.092 margin, so a
regression that let a near-miss outrank a gold is a real ranking regression, not jitter —
lower only with a re-measured justification. Design hardened by two fan-out plan skeptics
(metric-confounding → vector-only; seed-isolation → the 5 guardrails) + two fan-out PR
reviewers (correctness/isolation → CLEAN; adversarial-metric → added the hard negatives + the
gold-margin instrument so the metric detects a subtle regression, not just a dead arm).

## 8a-9. Golden-set growth — PR C of #125

Grew the main golden **31 → 50** cases, every addition VERIFIED on the real-Postgres judged
run (a case retrieval can't handle is not added — it would regress the gate, not measure
quality). All locked RATES are preserved; the counts grow:

- **+10 refusal robustness** — 6 diverse off-domain topics (Docker, AWS, React, git, SQL,
  Terraform, JS, MongoDB, Linux/cron) that route `unsupported` → decline (hermetic-safe), and
  **4 in-domain near-miss refusals** (`postgres_only`): a real product is named but the fact
  (uptime SLA, supported languages, per-token price, keyboard shortcuts) is NOT in the docs,
  so the orchestrator must DECLINE rather than invent. Measured: judged refusal leaks **0/19**
  (all decline, incl. every near-miss — the grounding-refusal net holds).
- **+4 paraphrases** (`postgres_only`, so they never drag the hermetic 0.60 overall floor):
  additional zero-overlap phrasings for claude_api / claude_code / codex / gemini_api, each
  verified to retrieve its area and rank the gold #1. (A 5th, `citevyn_par_cost`, was DROPPED:
  too generic → the global confidence gate suppressed it; not added rather than lower a gate.)
- **+2 multihop** — Claude-API-vs-Codex and Gemini-auth-vs-Claude-Code-permissions; both hit
  BOTH areas (multihop **5/5**).

Measured (2026-07-17, real Postgres, 50 cases): core overall **23/23**, multihop **5/5**,
followup 3/3, refusal leaks judged **0/19**, injection 0/2, groundedness 1.000/18, chunk rank
MRR/precision@1 1.000 (n=22), judge 4.76, gate PASSED, zero residue.

**Corpus cap (honest):** the main golden is anchored to the locked 5-chunk `seed_catalog`
(one chunk per area, deliberately unchanged). Each chunk covers ~2 facts, so beyond ~50 cases
new literal/paraphrase additions become near-duplicates without discriminating power. Pushing
toward the 100 end of the range would require EXPANDING the seed corpus (new curated content),
which is out of scope here (it changes the locked corpus + the one-chunk-per-area guard). The
distractor corpus (§8a-8) is the seam for adding ranking-discriminating cases without touching
the locked seed.

## 8a-10. Conversation memory: entity-aware content-noun follow-up (#112)

Live QA found a real gap: a CONTENT-NOUN follow-up ("is there a credentials file option?"
after "how do I authenticate to the Gemini API?", or "what are the different models?" after
"what is claude?") names no product and carries no BARE anaphora, so the deterministic regex
`build_contextual_query` left it → it routed `unsupported` → **refused** a perfectly answerable
question. The standalone form ("…Gemini credentials file?") answers fine.

Fix (`condense_question_llm`, #112): an **LLM entity-aware "standalone question" rewrite**,
layered on the deterministic regex (which stays for the 3 hermetic bare-anaphora cases, so the
LOCKED hermetic followup gate is byte-for-byte unchanged). Design shaped by two adversarial
plan skeptics:

- **Pure recall-improver, never drives routing.** Wired in `Orchestrator.ask` INSIDE the
  answer-when-grounded (global, confidence-gated) branch, AFTER `domain`/`intent`/`answer_globally`
  are fixed from the un-rewritten query. So even a rewrite that (wrongly) injects a product token
  can NOT flip a pivot onto the scoped, un-gated path — it only changes the TEXT fed to the
  gated global retrieval + generation. The confidence gate + grounding-refusal net remain the
  sole refusal authority. Guarded on history-present + regex-left-unchanged + a real provider
  (`llm_provider != "stub"`); any LLM error falls back to the un-rewritten query (never a 500).
  Proven by a hermetic anti-hijack test: a pivot whose rewrite injects "Claude API" still
  retrieves `product_area=None` and refuses.
- **Eval-safe via a new `judge_only` case flag.** `postgres_only` gates hermetic-vs-postgres,
  not retrieval-vs-judge — a content-noun followup would still enter the `--postgres`
  `followup_hit_rate` pool (resolved there by regex-only, no LLM) and fail the locked gate.
  `judge_only` excludes a case from the retrieval report entirely (validated SOLELY by the
  orchestrator-driven judged run).

**Measured (2026-07-17, real Postgres, judged run, STABLE across 3 runs):** the new
`followup_gemini_credfile_contentnoun` case (judge_only + postgres_only) is **answered** —
"Yes, the Gemini CLI accepts the API key in a credentials file [1]", judge **5**, groundedness
1.0 — every run. All LOCKED numbers hold: overall 23/23, multihop 5/5, followup (retrieval) 3/3,
refusal leaks judged **0/19**, injection 0/2, groundedness 1.000/19, MRR/precision@1 1.000,
judge 4.69, gate PASSED, zero residue. **Closes #112.**

## 8a-11. Concepts/glossary source — conceptual questions now answer (#112 follow-up)

Live QA: "what is an LLM?", "is Codex an LLM?", "what are the different models?" all REFUSED —
no source doc defined these general terms, so declining was correct but unhelpful for a broad
(non-developer) audience. Fix: a new **`concepts` source** (`app/worker/sources/concepts.md`,
registered in `MVP_SOURCES`) — an original plain-language glossary defining an LLM and stating
that Claude/Claude Code/Codex/Gemini are LLM-based tools — plus a one-line "it is an LLM-based
tool" mention added to each product doc (so the *scoped* "is Codex an LLM?" route answers too).
Mirrored into `conftest.seed_catalog` (+ `EXPECTED_AREAS`) so the eval corpus covers it, with
two `postgres_only` golden cases (`concepts_lit_llm`, `concepts_par_llm` — they route
`unsupported` → the global vector arm, dead hermetically).

**Verified live** (real Postgres + router LLM): all three previously-refused questions now
answer with citations — "What is an LLM?" cites `concepts`; "Is Codex an LLM?" → "Yes, Codex is
an LLM-based tool… [1]" cites `codex`; "What are the different models?" answers from
`concepts`+product docs. **Judged eval:** both concepts cases answered (judge 4, groundedness
1.0); overall answerable **25/25**, multihop 5/5, followup 3/3, refusal leaks judged 0/19,
injection 0/2, groundedness 1.000/20, MRR/precision@1 1.000, gate PASSED, zero residue.

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
- PR3.1 Query rewriting/decomposition (multi-hop). **✅ DONE** (#109) — see §8a-3.
- PR3.2 Conversation memory (prior turns → retrieval + prompt). **✅ DONE** (Phase 3b) — see
  §8a-4. Recent USER turns rewrite an anaphoric follow-up (deterministic; `sessions.summary`
  not needed — recent `Message` rows are the reliable per-turn signal).

**Phase 4 — UX & ops**
- PR4.1 Graceful fallback UX (nearest-doc suggestions). **✅ DONE** — when evidence was
  retrieved but no answer could be grounded (LLM declined / citation-failed), the
  no_answer response carries deduped nearest-doc `suggestions` (title/url/product_area);
  the chat renders "You might find these helpful:" instead of a dead-end refusal. A clean
  off-corpus refusal (no evidence) stays suggestion-free.
- PR4.2 Rate-limit segmentation + distinct 429 UI. **Distinct 429 UI ✅ DONE** — a rate
  limit renders as the amber `warning` toast (transient/recoverable), visually distinct
  from the red `error` alert used for server/transport failures. Rate-limit *segmentation*
  (per-endpoint limits) remains a separate ops follow-up.
- PR4.3 VectorDegrade / dead-embedding health signal. **✅ DONE** — `GET /health/index`
  carries an additive `vector_arm` block (empty/dead/mismatch/partial/healthy) so an
  operator can SEE the #97 failure (NULL embeddings) or a Tier-3 mismatch; live-verified
  on Postgres (dead 0/5 → healthy 5/5).

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
