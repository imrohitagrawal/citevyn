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

Blank lines and `#`-prefixed lines are skipped, so the file stays annotatable.

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
