# Phase 2 (retrieval quality) — design notes + why the autonomous run stopped here

> Written by the unattended RAG-completion run after Phase 1 merged (PR #103). Phase 2 was
> **investigated and design-reviewed but deliberately NOT implemented/merged** — the adversarial
> design review found the proposed refusal-safety mechanism unsound and the sound redesign
> changes a user-facing refusal contract that warrants your sign-off. This doc captures
> everything needed to resume.

## The goal (unchanged, legitimate)
Two golden paraphrases miss because CiteVyn's domain router is **literal keyword matching**
(`app/guardrails/domain.py`): a question with no product token → `Domain.unsupported`.
- `claude_code_par_toolgate` ("restrict which tools the **coding assistant** may run") — never says "Claude Code".
- `citevyn_par_membership` ("premium paid tier I can subscribe to") — never says "CiteVyn".
Both route to `unsupported`; the orchestrator then **refuses before retrieval**
(`app/answer/orchestrator.py:371-372, 388-396`), so soft-scoping the retriever alone changes
nothing for the live product. "Soft scoping" therefore intrinsically means **relaxing the
orchestrator's early-refuse gate** so unnamed-but-in-corpus questions get answered — a
user-facing refusal-contract change.

## The proposed design (what the review attacked)
Global vector recall (`product_area=None`) + a routed-area score **boost** in `hybrid._merge`;
keep exact (already soft) + keyword **scoped** (global keyword ILIKE leaks refusals via generic
tokens like "to"/"i"); an **absolute cosine floor (~0.42)** on the vector arm as the refusal net;
relax the orchestrator early-refuse gate; reconcile the eval path to match.

## Why it was NOT auto-merged — adversarial design review (4 dimensions, all found blockers)
**8 blockers + 10 majors**; `simpler-alt` returned **stop-and-report**. The load-bearing problems:

1. **The absolute floor is mathematically unsound on this corpus (BLOCKER).** Measured cosine
   sims: `refusal_openai` = **0.373** > valid `citevyn_par` = **0.341**. No floor value both
   keeps the valid answer and blocks the refusal. 0.42 "works" only by silently dropping
   `citevyn_par`. On a real corpus, competitor-API questions ("rate limit on OpenAI's API?" vs
   the claude_api rate-limit chunk) invert the ordering and the floor can't separate the classes
   at all. **Refusal safety cannot be an absolute cosine floor.**
2. **Zero hermetic CI coverage of the floor (BLOCKER).** The vector arm returns `[]` on SQLite,
   and the CI gate (`MAX_REFUSAL_LEAKS==0`) runs only on SQLite — so it passes **floor-blind**
   whether the floor is 0.42, 0.0, or absent. An unsupervised auto-merge would ship an
   **unverifiable** safety claim. (The plan mandates failing-test-first for deterministic algos;
   this had none.)
3. **Overfit to a 5-chunk corpus (MAJOR).** 0.42 sits in a 0.116-wide gap defined by 15 points.
   As the real index grows, nearest-neighbor cosine to ANY query rises → leaks **increase** at a
   fixed floor. `refusal_openai` is already only 0.047 under 0.42.
4. **Eval leak-metric vs production refusal diverge (MAJOR).** The hermetic metric counts "any
   chunk retrieved" as a leak; production refusal actually rests on the **LLM grounding-refusal**
   (`orchestrator.py:523 _is_no_answer_refusal`), which the hermetic path never exercises. Green
   CI would validate neither the floor nor the LLM backstop.
5. **Refusal-contract blast radius (MAJOR).** Relaxing the early-refuse gate changes the
   `unsupported`→`_respond_unsupported` contract (tests, the `unsupported` response field, cache
   key `product_area=domain.value`, frontend refusal copy). And it removes a **free** refusal —
   every off-domain query now costs an embedding + pgvector query + possibly an LLM call
   (DoS/budget surface).

## The sound redesign (recommended — needs your product call on the contract)
- **Refusal net = the LLM grounding-refusal that already exists** (retrieve globally, let the LLM
  decline when the chunk doesn't actually answer), NOT an absolute floor. Keep a floor only as a
  loose **cost guard** (~0.25, well below any plausible valid answer).
- **If a retrieval-time gate is wanted, make it relative/adaptive** (the plan's own "adaptive
  floor" language, §8/§9): top hit must beat the best other-area hit by a margin, or exceed
  `mean + k·std` of the retrieved similarity distribution — adapts to query + corpus, no
  per-embedder re-tuning.
- **Extract the gate as a pure function + hermetic unit test** (feed `RetrievedChunk`s with known
  scores through it) so CI actually anchors the safety behavior without pgvector.
- **Reconcile the eval to the orchestrator**: drive the full orchestrator (incl. LLM refusal) for
  refusal cases in the opt-in **judged** Postgres mode and assert `unsupported`/decline — the
  retrieval-only hermetic leak count must not be misread as end-to-end refusal safety.
- **Keep a cheap pre-filter + rate-limit** for the newly-expensive `unsupported` path.
- **Ordering:** the plan sequences BM25/tsvector (2.1) → RRF+adaptive floor (2.2) → soft scoping
  (2.3). Consider landing the **adaptive floor / margin gate (2.2)** as the refusal net *before*
  the soft-scoping gate change, so the safety mechanism exists first.

## The blocker that also gates the proof
The corrected design's refusal safety can only be proven end-to-end via the **judged** Postgres
run (LLM in the loop). During this run **Gemini free-tier *generation* was rate-limited**
(`llm_primary_unavailable_falling_back`) and the judged run was additionally interrupted by a
local Docker Desktop crash. So even the corrected design can't be eval-proven for refusal safety
right now without either a generation-quota reset or the paid OpenRouter LLM for the judge.

## Product decision for you (the reason a human should sign off)
**Should CiteVyn answer in-corpus questions that don't name the product (changing "unsupported →
immediate refusal" to "retrieve globally, answer if grounded, else decline")?** This trades a
crisp, cheap, always-safe refusal for broader recall at the cost of a per-query embedding+LLM
spend and a softer refusal contract. That's a product/UX call, not purely an engineering one.
