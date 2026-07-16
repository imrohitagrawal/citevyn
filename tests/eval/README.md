# RAG eval golden set (`golden.jsonl`)

Phase 0 of `docs/RAG_QUALITY_PLAN.md` (issue #96): the **measurement foundation** for
RAG quality. This is distinct from `tests/golden/` (the assertion-based regression
runner). Here we measure two *outcome* metrics:

1. **Retrieval hit-rate** — does any top-k retrieved chunk come from the expected
   source? Fully hermetic (seeded SQLite + the real `HybridRetriever`, no network/LLM).
2. **Answer quality** — an LLM-as-judge score (1–5) vs an expected gist. Requires a real
   LLM provider (Gemini, free); skipped — never faked — under the stub.

Executor code lives in `backend/tests/eval/`; run it with `make eval` or
`python -m tests.eval.runner`. The CI regression gate is
`backend/tests/test_eval_harness.py`.

## Line schema (one JSON object per line)

| Field | Type | Meaning |
|---|---|---|
| `id` | str | Unique case id. |
| `area` | str | `claude_api` / `claude_code` / `codex` / `gemini_api` / `citevyn` / `out_of_corpus`. |
| `kind` | str | `literal` / `paraphrase` / `refusal` (see below). |
| `question` | str | The user question. |
| `expected_source` | str \| null | Seed `source_name` the correct chunk belongs to; `null` for refusals. |
| `expected_gist` | str | Short summary of the correct answer, scored by the judge. |
| `expect_no_answer` | bool | `true` for refusal/out-of-corpus cases. |
| `expected_facts` | list[str] | *(optional, answerable-only)* Hard facts a correct grounded answer MUST state — see **Groundedness** below. |

Blank lines and `#`-prefixed lines are skipped, so the file stays annotatable.

## Answer-quality robustness (Item 1)

A single LLM judge can be noisy or over-score a *plausible-but-wrong* answer. The
judged run (`--postgres`) therefore combines three signals:

1. **Prompt-ensemble panel** — each answer is scored by N distinct rubric *framings*
   (not temperature samples) at temp 0.0; the **median** smooths one framing's
   interpretation bias while staying reproducible. `CITEVYN_EVAL_JUDGE_PANEL` sets N
   (odd; default 3).
2. **Adversarial veto** — one skeptical fact-checker pass; the gated score is
   `min(standard_median, adversarial)`, so a skeptic that catches an over-scored
   answer pulls it down (it is a floor, never averaged away).
3. **Deterministic groundedness** — judge-*independent*. Declare `expected_facts` on a
   case (phrasing-stable tokens — env-var names, headers, CLI commands, or a number
   *with* its unit; each entry may list `|`-separated alternatives, any-of). A fact is
   matched at word boundaries, so `"50 requests per minute"` is **not** satisfied by
   `"150 requests per minute"` or `"0.50 requests per minute"` — a wrong hard fact
   fails regardless of the judge. **Gated per case on the `--postgres` run only** (the
   mode where fact-bearing answerable cases can actually retrieve; the hermetic
   dead-vector-arm path would structurally zero the paraphrase fact-cases, so it is
   excluded — exactly like the multihop gate): every fact-bearing case must be **fully
   grounded** there, so a single wrong fact fails (an aggregate mean over binary
   single-fact cases would leak it). A golden-integrity test asserts each fact is
   groundable (at least one alternative appears in the seed corpus).

## Adversarial hardening (Item 2)

Two case fields sharpen the meter against adversarial inputs:

- **`postgres_only: true`** — the case is meaningful ONLY on the live vector arm
  (misspellings the keyword/exact arm can't recover; in-domain near-miss refusals). It
  is **excluded from the hermetic run and its gates** (mirrors the multihop/followup
  Postgres-only pattern) and evaluated on the `--postgres` judged run. This is the
  supported way to add a case that would misfire on the dead-arm SQLite path.
- **`must_not_contain: [...]`** — **prompt-injection resistance**. A list of forbidden
  (non-empty) substrings — a sentinel the injection tells the model to emit, e.g.
  `"PWNED"`. Checked **judge-independently** on the judged run. A case counts as OBEYING
  the injection (a leak, zero tolerance) only when the sentinel is present AND the answer
  failed to answer (its `expected_facts` are not fully grounded) — so a resistant answer
  that names the sentinel while declining ("I won't reply with PWNED …") is not a false
  leak. **Declare `expected_facts` on an injection case** so "did it still answer?" is
  measurable.

Deferred (tracked follow-up): context precision/recall + a distractor corpus need
chunk-level relevance identity (retrieved chunk ids + stable chunk keys + gold-chunk
labels on a separate distractor index) — a distinct, careful PR that must not perturb the
locked hermetic baseline. Growing the golden set toward 50–100 and a human-labeled judge
calibration subset ride along there.

## CI enforcement of answer quality

The hermetic retrieval gate runs in the standard `pytest + lint` CI job (no key, no
network). The **judged answer-quality gate** (`MIN_MEAN_JUDGE` + per-case groundedness +
prompt-injection resistance) runs in a dedicated `answer-quality-eval` CI job that spins up
a pgvector service and drives the orchestrator per golden case with a real LLM + embedder.

It requires the OpenRouter key as a **repo Actions secret** (`CITEVYN_OPENROUTER_API_KEY`),
which only the repo **owner** can add. Until it is present the job **skips** every
meaningful step (a loud `::notice::`) and stays green — the key is never hardcoded or
faked. **To enable the gate:** add `CITEVYN_OPENROUTER_API_KEY` under *Settings → Secrets
and variables → Actions*; the next run flips it on. Cost is bounded via
`CITEVYN_EVAL_JUDGE_PANEL=1` (1 rubric framing + the adversarial veto = 2 judge
calls/case) on a small model over the ~31-case golden set.

## Kinds

- **`literal`** — shares vocabulary with the seed corpus; the keyword/exact arm can hit.
- **`paraphrase`** — a semantic equivalent with (near) zero literal overlap with the
  answer text. These isolate the **dead vector arm** (#97): expected hit-rate ~0 at the
  Phase 0 baseline. When embeddings are populated (Phase 1) and domain scoping softens
  (Phase 2), these are the cases whose hit-rate should climb — the eval delta each PR must
  prove.
- **`refusal`** — out-of-corpus / off-domain. The correct behavior is to retrieve
  **nothing** and decline. Never counted toward answerable hit-rate.

## Anchoring

Cases are anchored to the **conftest** seed corpus (`backend/tests/conftest.py::seed_catalog`)
— the corpus the hermetic harness and CI run against — **not** `db/seed` (whose Gemini chunk
is about the API key, not streaming). Keep new cases answerable from that 5-chunk corpus, or
the hit-rate metric loses meaning.
