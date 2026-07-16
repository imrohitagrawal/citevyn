# Autonomous RAG-completion run — status

> Append-only progress log for the unattended Phase 1→4 run. Newest STATUS block on top.
> Purpose: survive context compaction so the run can resume.

# ============================================================================
# STATUS — 2026-07-17 · CI FLAKE #85 + EVAL PRECISION/RECALL #125 (autonomous run)
# ============================================================================

**One line:** Fixed the intermittent CI merge-gate flake (#85) and landed the bulk of the
deferred eval-hardening #125 — chunk-level retrieval identity + a rank metric, a distractor
corpus with context precision/recall, and golden growth 31→50 — as four small PRs, each
through the FULL loop (fan-out adversarial plan review → TDD → gates → real-Postgres eval
proof → fan-out PR review w/ per-finding verification → release-readiness → green CI →
squash-merge). No `--admin`, no bypass. Only human-labeled judge-calibration remains on #125.

## What merged this run
| PR | Item | What | Result |
|---|---|---|---|
| **#131** | 0 — CI flake #85 | `make db-verify` retries both `psql` calls (10×2s) through the pgvector:pg18 first-boot restart race; cap still hard-fails a genuinely broken boot (no false green) | Verified fresh-volume `make ci-smoke` ×3 self-heals; #85 CLOSED |
| **#132** | 1 (PR A) — chunk identity + rank metric | Stable chunk key `"{source_name}#{chunk_order}"` (over uuid4 / collision-prone content_checksum); eval-layer chunk_id→key map (raise-on-miss); `EvalCase.gold_chunks`; **MRR + precision@1** for single-relevant cases, `--postgres`-gated | Baseline (stable ×5 runs): MRR 1.000, precision@1 1.000 (n=18); locked judged run unchanged |
| **#133** | 1 (PR B) — distractor corpus | Dedicated `seed_eval_distractors` (own throwaway area, own active index, full postgres_session rails, zero residue) + **context recall@k + precision@\|gold\|** via VECTOR-ONLY retrieval; 2 lexical HARD NEGATIVES so the metric detects a subtle regression, not just a dead arm; per-case `gold_margin` instrument | All cases recall/precision 1.000, gold beats hard negatives by min 0.092 cosine (stable ×5); fully isolated from locked runs |
| **#134** | 1 (PR C) — golden growth | Main golden **31→50**: +10 refusal robustness (6 off-domain + 4 in-domain near-miss `postgres_only`), +4 verified paraphrases, +2 multihop — every one proven on the judged run | core 23/23, multihop 5/5, refusal leaks judged 0/19, groundedness 1.000/18, gate PASSED |

## Measured baselines + margin gates (real Postgres, openai/text-embedding-3-small, 2026-07-17)
- **Rank metric (#132):** `MIN_PRECISION_AT_1 = 1.0` (pinned — the 2 GLOBAL-routed paraphrases
  rank their gold over all other areas, precision@1 strictly stronger than hit-rate there;
  deterministic run-to-run, verified stable ×5), `MIN_MRR = 0.95` (tolerant companion). n=18.
- **Distractor context (#133):** `MIN_DISTRACTOR_RECALL_AT_K = 1.0`,
  `MIN_DISTRACTOR_PRECISION_AT_GOLD = 1.0` — EARNED against lexical hard negatives with a
  measured 0.092 min cosine margin (0.092–0.158), recorded per-case as `gold_margin` for
  early warning. Distractor-mode-only (opt-in `python -m tests.eval.distractors`; NOT wired
  into CI to avoid same-DB concurrency with the judged pass — run serially).
- **Golden (#134):** 50 cases; all locked RATES preserved (counts grew: core 19→23, multihop
  3→5, refusal leaks 0/6→0/19 judged, all still perfect).
- Distractor corpus size: **18 chunks** (2 gold + 16 within-area distractors incl. 2 hard neg).
- Golden set final count: **50** main + **3** distractor-golden cases.

## Design shaped by fan-out adversarial review (honored, not overridden)
- PR A: 3 plan skeptics (ranking-triviality → the metric already bites on the 2 global
  paraphrases, not pure plumbing; chunk-key stability → composite key, raise-on-miss; baseline
  non-regression → `()` defaults + `.get()`-guarded postgres-only gate) + 3 PR reviewers.
- PR B: 2 plan skeptics (keyword-ILIKE confound → VECTOR-ONLY retrieval; seed isolation → 5
  guardrails incl. build_embedder-not-get_embedder, empty-catalog refusal, exactly-1-active-
  index) + 2 PR reviewers (adversarial-metric → the metric was too easy, so added lexical hard
  negatives + the gold-margin instrument).
- PR C: judged-run verification is the gate — a 5th paraphrase (`citevyn_par_cost`) was DROPPED
  rather than lower a gate when the global confidence gate suppressed it.

## Remaining on #125 (the ONLY open piece — DEFERRED by design this run)
- **Human-labeled judge-calibration subset** (Item 2d): measure judge-vs-human agreement on a
  small hand-scored slice. Needs a human in the loop (out of scope for an unattended run).
  #125 stays OPEN scoped to this alone; BACKLOG row updated.

## How to run the new pieces
```bash
# Distractor context precision/recall (opt-in, Postgres-only; run serially vs the judged pass):
export PGPW=$(grep '^POSTGRES_PASSWORD=' infra/docker/.env|cut -d= -f2-)
export DB_URL="postgresql+psycopg://citevyn:$PGPW@localhost:5432/citevyn"
export OR_KEY=$(grep '^CITEVYN_OPENROUTER_API_KEY=' infra/docker/.env|cut -d= -f2-)
make db-up && CITEVYN_DATABASE_URL=$DB_URL uv run --project backend alembic -c db/alembic.ini upgrade head
docker exec -e PGPASSWORD="$PGPW" citevyn-db psql -U citevyn -d citevyn -c \
  "TRUNCATE exact_terms, chunks, documents, index_versions CASCADE;"
cd backend && CITEVYN_DATABASE_URL=$DB_URL CITEVYN_EMBEDDING_PROVIDER=openrouter \
  CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small CITEVYN_OPENROUTER_API_KEY=$OR_KEY \
  uv run python -m tests.eval.distractors           # → recall/precision 1.000, min margin 0.092
# The rank metric + golden 50 ride the existing judged run: uv run python -m tests.eval.runner --postgres
```

# ============================================================================
# STATUS — 2026-07-16 · EVAL HARDENING + ENFORCEMENT (autonomous run)
# ============================================================================

**One line:** The RAG eval's answer-quality signal is now **trustworthy** (panel + adversarial
veto + deterministic groundedness + injection resistance) and the gate is **enforced** both in
CI (owner must add one secret) and locally (pre-commit hook). Four items, each through the full
loop (adversarial plan review → TDD → gates → real-Postgres judged eval proof → fan-out PR review
w/ per-finding verification → release-readiness → green CI → squash-merge). No `--admin`, no bypass.

## What merged this run
| PR | Item | What | Result |
|---|---|---|---|
| **#124** | 1 — Judge robustness | Prompt-ensemble panel (N rubric framings, temp-0, median) + adversarial **veto** (`min(median, adversarial)`, a floor not a member) + deterministic **groundedness** (word-boundary hard-fact match, per-case on `--postgres`) | judge 4.88 → panel min-vetoed ~4.7; groundedness 1.000/11; core 15/15; zero residue |
| **#126** | 2 (slice) — Adversarial hardening | `postgres_only` bucket + **prompt-injection resistance** (`must_not_contain`, leak = sentinel present AND facts ungrounded) + adversarial golden cases (2 injection, 2 misspelling, 1 in-domain near-miss refusal) + one-chunk-per-area guard | injection 0 leaks/2; misspell recovery 2/2; in-domain refusal declines; core 19/19; zero residue |
| **#127** | 3 — CI enforcement | `answer-quality-eval` CI job: pgvector service → migrate → judged `--postgres` eval enforcing MIN_MEAN_JUDGE + groundedness + injection. **Guarded to skip when the secret is absent** (green, no merge block); `panel=1` bounds cost | reviewed SOUND (no secret leak); skips cleanly today |
| **#128** | 4 — Local enforcement | git **pre-commit hook** (ruff format+check on staged backend py) + `make install-hooks` + CONTRIBUTING docs | blocks bad format/lint, skips docs-only — verified |

## Judge robustness: before → after
- **Before:** a SINGLE LLM-judge call (temp 0), able to over-score a plausible-but-wrong answer; no groundedness or injection axis.
- **After:** median over N reproducible rubric framings (noise) + adversarial veto (over-scoring) + judge-INDEPENDENT groundedness (a wrong hard fact fails regardless of the judge; `"50 requests per minute"` not credited by `"150…"`/`"0.50…"`) + injection resistance. Robustness proven by hermetic tests (median smooths a noisy outlier; adversarial vetoes a fooled `[5,5]→2`; numeric superstrings score 0) + opt-in real-key tests. Design shaped by adversarial plan reviews (Item 1: 2 blockers; Item 2: 8 blockers + 13 majors) + fan-out PR reviews (all confirmed findings fixed).

## Enforcement: live vs pending
- **CI answer-quality gate — WIRED, PENDING one owner action.** The `answer-quality-eval` job runs the judged eval + gates, but the judged run needs the OpenRouter key as a repo Actions secret. **ACTION REQUIRED (owner only):** add `CITEVYN_OPENROUTER_API_KEY` under *Settings → Secrets and variables → Actions*. Until then the job skips (green). The exact job config (`--postgres`, `openai/text-embedding-3-small`, `router`, `CITEVYN_EVAL_JUDGE_PANEL=1`) was proven locally this run, so it flips on cleanly. Recommend running it once on a PR before making it a *required* check (LLM-judged gate is non-deterministic; MIN_MEAN_JUDGE=3.0 has wide headroom).
- **Local pre-commit — LIVE.** `make install-hooks` installs the ruff format+check gate.

## Deferred (tracked)
- **#125** — context precision/recall + distractor corpus + golden-set growth to 50–100 + human judge-calibration. The naive design is unsound on the current harness (source-level identity; `uuid4` chunk ids; `top_k ≥ corpus size`); needs chunk-level relevance identity on a separate distractor index. Precise spec in the issue.

## How to run it
```bash
# Hermetic gate (no key/Docker): 748 pass
cd backend && env -u CITEVYN_DATABASE_URL uv run pytest -m "not postgres" -q
# Judged proof (Docker + key in infra/docker/.env):
export PGPW=$(grep '^POSTGRES_PASSWORD=' infra/docker/.env|cut -d= -f2-)
export DB_URL="postgresql+psycopg://citevyn:$PGPW@localhost:5432/citevyn"
export OR_KEY=$(grep '^CITEVYN_OPENROUTER_API_KEY=' infra/docker/.env|cut -d= -f2-)
make db-up && CITEVYN_DATABASE_URL=$DB_URL uv run --project backend alembic -c db/alembic.ini upgrade head
docker exec -e PGPASSWORD="$PGPW" citevyn-db psql -U citevyn -d citevyn -c \
  "TRUNCATE exact_terms, chunks, documents, index_versions CASCADE;"
cd backend && CITEVYN_DATABASE_URL=$DB_URL CITEVYN_EMBEDDING_PROVIDER=openrouter \
  CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small CITEVYN_LLM_PROVIDER=router \
  CITEVYN_OPENROUTER_API_KEY=$OR_KEY uv run python -m tests.eval.runner --postgres
#  → core 19/19, multihop 3/3, followup 3/3, refusal leaks judged 0/6, injection 0/2,
#    groundedness 1.000/15, judge ~4.6, gate PASSED, zero residue
make install-hooks   # local pre-commit gate
```

# ============================================================================
# FINAL STATUS — 2026-07-16 · RAG_QUALITY_PLAN COMPLETE (all phases 0→4 MERGED)
# ============================================================================

**One line:** **Every phase of `docs/RAG_QUALITY_PLAN.md` is now implemented, eval-proven,
reviewed, and MERGED to `main` (`968de50`).** This run completed the two remaining phases —
**3B conversation memory** and **4 UX & ops** — through the full loop (adversarial plan +
fan-out PR review → TDD → gates → real-Postgres eval proof → release-readiness SHIP → green
CI → auto-squash-merge; no `--admin`, no bypass).

## What merged THIS run
| PR | Phase | What | Result |
|---|---|---|---|
| **#111** (`c20a1b6`) | 3B.1 | Multi-turn eval infra: `followup` case kind + `history` + gap control (prerequisite) | followup **0/3** (gap demonstrated), baselines unchanged |
| **#113** (`e75395f`) | 3B.2 | Conversation memory: `build_contextual_query` + `recent_user_questions` → orchestrator rewrites an anaphoric follow-up against recent turns (routing + retrieval + generation + cache key); kill-switch | follow-up **0/3 → 3/3**, core 15/15, multihop 3/3, refusal leaks judged 0/5, judge 4.85, zero residue |
| **#115** (`75624ee`) | 4c | `GET /health/index` `vector_arm` block (dead/mismatch/partial/healthy) — operator sees the #97 failure | live-verified PG: dead 0/5 → healthy 5/5 |
| **#116** (`d9d8334`) | 4b | Distinct 429 UI — amber `warning` toast (transient) vs red `error` alert | 35 frontend tests + tsc |
| **#117** (`968de50`) | 4a | Graceful fallback — nearest-doc `suggestions` on an in-domain no_answer; off-corpus stays suggestion-free | 714 backend + 35 frontend |
| #114 | docs | 3B closeout | — |

## Eval numbers per phase (before → after, real Postgres+pgvector, judged `--postgres`)
| Metric | Phase-3a (start of run) | **Phase-3B/4 (now)** |
|---|---|---|
| Core overall (literal + paraphrase) | 15/15 | **15/15** |
| Multi-hop | 3/3 | **3/3** |
| **Follow-up (conversation memory)** | — (no infra) | **3/3** (memory ON) · 0/3 (memory OFF control) |
| Refusal leaks — judged | 0/5 | **0/5** |
| Judge mean | 4.91 (23 cases) | **4.85–4.88** (26 cases incl. 3 judged followups) |
| DB residue | zero | **zero** |

## What's left (tracked follow-ups — none block the plan)
- **#112** (filed this run) — conversation memory: entity-aware rewrite for off-corpus
  topic-pivot follow-ups (an adjacent pivot gets a disclaimed prior-topic answer — honest
  relevance miss; the LLM net refuses clear pivots). Low priority.
- Pre-existing backlog unchanged: #59 (embedding providers/scale), #61 (SSE streaming),
  #62 (composer gating), #82/#85 (CI), #84 (CiteVyn-meta), #87 (legit no_answer), #93 (seed
  log redaction). Rate-limit *segmentation* (the other half of PR4.2) remains a small ops
  follow-up. See `docs/BACKLOG.md`.

## How to test it all live
Prereqs: Docker running; the OpenRouter key is already in `infra/docker/.env`.
```bash
cd ~/Projects/citevyn && git checkout main && git pull        # 968de50 or later
export PGPW=$(grep '^POSTGRES_PASSWORD=' infra/docker/.env | cut -d= -f2-)
export DB_URL="postgresql+psycopg://citevyn:$PGPW@localhost:5432/citevyn"
export OR_KEY=$(grep '^CITEVYN_OPENROUTER_API_KEY=' infra/docker/.env | cut -d= -f2-)
make db-up
CITEVYN_DATABASE_URL=$DB_URL uv run --project backend alembic -c db/alembic.ini upgrade head

# 1) Full eval proof (truncate first → the runner seeds+rolls back, zero residue):
docker exec -e PGPASSWORD="$PGPW" citevyn-db psql -U citevyn -d citevyn \
  -c "TRUNCATE exact_terms, chunks, documents, index_versions CASCADE;"
cd backend && CITEVYN_DATABASE_URL=$DB_URL \
  CITEVYN_EMBEDDING_PROVIDER=openrouter CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small \
  CITEVYN_LLM_PROVIDER=router CITEVYN_OPENROUTER_API_KEY=$OR_KEY \
  uv run python -m tests.eval.runner --postgres
#   → core 15/15, followup 1.000 (3/3), multihop 1.000, refusal leaks judged 0/5, judge ~4.85, gate PASSED

# 2) Vector-arm health signal (4c): seed WITH embeddings, then GET /health/index
#    → vector_arm.status "healthy" (5/5); seed with embedder=None → "dead" (0/5).
#    (See scratchpad verify_health.py pattern from the run, or hit the live uvicorn.)

# 3) Hermetic gates (no Docker/keys needed):
cd backend && env -u CITEVYN_DATABASE_URL uv run pytest -m "not postgres" -q   # 714 passed
cd ../frontend && npm run type-check && npx vitest run                          # 35 passed
```
For a live chat demo (conversation memory + fallback UX + 429 toast): seed a demo index WITH
embeddings, promote, run uvicorn + the live frontend (`VITE_API_LIVE=true`), then ask a product
question followed by an anaphoric follow-up ("How can I raise it?") — the follow-up now answers
(memory). See the phase-one runbook for the seed→promote→serve chain.

_All work this run went through: adversarial plan/PR review (fan-out) → TDD → full gates
(ruff format+check, pyright, 714 hermetic + 8 postgres pytest, 35 vitest, stylelint) →
real-Postgres judged eval proof → release-readiness SHIP → 6/6 CI green → auto-squash-merge._

# ============================================================================
# STATUS — 2026-07-16 (Phase 3B MERGED; conversation memory; followup 3/3)
# ============================================================================

**One line:** **Phase 3B (conversation memory) is COMPLETE, eval-proven, and MERGED.**
Anaphoric follow-ups ("How can I raise it?") that previously refused now resolve against
the session's recent turns: **follow-up hit-rate 0/3 → 3/3**, core **15/15**, multi-hop
**3/3**, judged refusal leaks **0/5**, judge **4.85** over 26 (incl. 3 judged followups),
zero residue — all on real Postgres+pgvector.

| PR | What | Result | State |
|---|---|---|---|
| **#111** (`c20a1b6`) | **Phase 3b.1** — multi-turn eval infra: `followup` case kind + `history` + gap control (prerequisite) | followup 0/3 (gap demonstrated), baselines unchanged | **MERGED** |
| **#113** (`e75395f`) | **Phase 3b.2** — conversation memory: `build_contextual_query` + `recent_user_questions` → orchestrator rewrites an anaphoric follow-up against recent turns (routing + retrieval + generation + cache key); kill-switch `conversation_memory` | **followup 3/3**, core 15/15, multihop 3/3, refusal leaks judged 0/5, judge 4.85, zero residue | **MERGED** |

**Refusal safety (fan-out review):** an off-corpus PIVOT opening with an anaphor is
contextualized, but the Phase-2 LLM grounding-refusal net declines clear pivots (k8s →
no_answer, verified); generating against the bare follow-up regresses genuine anaphora
(measured), so it is rejected. Residual (adjacent-pivot answered-with-disclaim, honest
relevance miss) tracked as **#112**. See §8a-4.

**Remaining:** **Phase 4** UX/ops — 4a graceful fallback UX (nearest-doc suggestions),
4b distinct 429 rate-limit UI (#61/#62 adjacent), 4c VectorDegrade/dead-embedding health
signal. Mostly frontend/ops (prove via verify/webapp-testing + hermetic backend tests
where there's a runtime surface); not eval-measurable.

# ============================================================================
# STATUS — 2026-07-16 (Phases 1, 2, 3a MERGED; core eval 15/15 + multihop 3/3)
# ============================================================================

**One line:** **Phases 1, 2, and Phase-3 query decomposition are COMPLETE, eval-proven, and MERGED.**
The RAG system now answers single-product, unnamed-but-in-corpus, AND cross-product questions well:
core overall **10/15 → 15/15**, paraphrase **0/5 → 5/5**, multi-hop **3/3**, judged refusal leaks
**0/5**, judge mean **~4.9** — all on real Postgres+pgvector. Remaining: **Phase 3b conversation
memory** (needs multi-turn eval infra — the harness is single-turn) and **Phase 4** UX/ops.

## What merged
| PR | What | Result | State |
|---|---|---|---|
| **#103** (`d3795f6`) | **Phase 1.1** — revive the vector arm (#97): OpenRouter/OpenAI embedder + embedding-aware seeders + db/seed backfill + opt-in Postgres eval mode | paraphrase **0→3/5**, overall **10/15→13/15**, leaks 0, zero residue; discrimination real 5/5 vs stub | **MERGED** |
| **#105** (`f199a2f`) | **Phase 1.2** — ship source docs as package data (#92) | verified inside a built worker image; ingested a real 33-chunk corpus | **MERGED** |
| **#107** (`b654ddd`) | **Phase 2** — answer when grounded: global confidence-gated retrieval + LLM grounding-refusal net + judged eval refusal metric | paraphrase **3/5→5/5**, overall **→15/15**, judged refusal leaks **0/5**, judge 5.00 | **MERGED** |
| **#109** (`2f4f8a4`) | **Phase 3a** — multi-hop query decomposition: `classify_domains` + `retrieve_multi` + orchestrator routing + eval `multihop` bucket | **multihop 3/3** (each cross-product Q retrieves both areas), judge 4.91, core 15/15, zero residue | **MERGED** |
| #104/#106/#108 | docs closeouts | — | MERGED |

**Remaining (fresh-scope feature work):** Phase 3b conversation memory (`sessions.summary` → retrieval
+ prompt; needs a multi-turn eval harness); Phase 4 UX/ops (fallback UX, distinct 429 UI #61/#62,
VectorDegrade health signal). Neither moves the current single-turn golden set — both need new eval
cases/infra first.

_Every merge: full loop (design → adversarial plan/PR review → TDD → gates → real-Postgres eval proof →
release-readiness SHIP → 6/6 CI green → auto-squash-merge). No `--admin`, no bypass._

## Phase 2 plan (decision LOCKED = "answer when grounded")
The next eval win needs the refusal-contract change. The design review proved an absolute floor is
unsound; the sound design (in `docs/RAG_PHASE2_DESIGN_NOTES.md`) is: global vector recall + routed-area
boost, the **existing LLM grounding-refusal as the real refusal net**, a **relative/margin** confidence
gate (not absolute), and relax the orchestrator's `unsupported→refuse-early` gate. **Key coupling that
makes this a fresh-context task:** under global retrieval, refusal queries WILL retrieve nearest chunks,
so the hermetic `MAX_REFUSAL_LEAKS==0` retrieval metric fundamentally conflicts — the eval's refusal
metric must be **redesigned to "the orchestrator declined" (judged/LLM-in-loop)**, and the golden set
grown/realistic-ified against the #92 corpus. Suggested PR decomposition:
1. **Eval refusal-metric redesign** (judged/orchestrator-decline) + a hermetic pure-function test for
   the confidence gate — the prerequisite; no product-contract change yet.
2. **Retriever**: global vector recall + routed-area boost + a loose cost-guard + a relative/margin gate.
3. **Orchestrator**: relax `unsupported→refuse-early`; keep a cheap pre-filter + rate-limit; LLM
   grounding-refusal is the net. Eval-prove on the real corpus via the judged `--postgres` run.

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
