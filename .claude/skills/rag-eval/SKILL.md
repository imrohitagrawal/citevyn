---
name: rag-eval
description: CiteVyn's RAG evaluation harness — the golden-set retrieval hit-rate + LLM-as-judge grader and its CI gate. Use whenever you add or edit a golden eval case, run or debug `make eval` or `backend/tests/test_eval_harness.py`, read an eval report, tune gate thresholds, record or update the eval baseline, or need to prove a retrieval/answer-quality change moved the number. Reach for it even when the user doesn't say "eval" explicitly — e.g. "did my retrieval change help?", "why is the golden test failing?", "add a test question", "what's our answer quality?".
---

# RAG Eval Harness (CiteVyn)

Concise operator guide for the Phase-0 eval harness (issue #96). Deep detail lives in
`tests/eval/README.md`; baseline + provider strategy in `docs/RAG_QUALITY_PLAN.md` §8a/§11a.
This is CiteVyn-specific (project-scoped skill).

## What it measures

- **Retrieval hit-rate** (hermetic, no network/LLM): does any top-k chunk come from the
  expected source? Runs the *live* `HybridRetriever` + `classify_domain`/`classify_intent`
  over the **conftest** seed corpus.
- **LLM-as-judge** (opt-in): answer quality 1–5 vs the case's `expected_gist`. Needs a real
  provider key; returns "unavailable" under the stub — **never a faked score**.

## Run it

- Full local run: `make eval` → writes `artifacts/eval_report.json`. Includes the judge only
  when a working provider key is configured.
- CI gate (hermetic, already auto-runs in the `pytest + lint` job):
  `cd backend && env -u CITEVYN_DATABASE_URL uv run pytest tests/test_eval_harness.py -q`
- With the judge locally:
  `CITEVYN_LLM_PROVIDER=gemini CITEVYN_GEMINI_API_KEY=… CITEVYN_EVAL_LLM=1 make eval`
  (`CITEVYN_EVAL_LLM=1` also enables the opt-in judge pytest).

## Add a golden case

Append one JSON object per line to `tests/eval/golden.jsonl` (full schema in
`tests/eval/README.md`). It **must be answerable from the CONFTEST seed corpus**
(`backend/tests/conftest.py::seed_catalog`) — not `db/seed`.

- `kind`: `literal` (shares vocab → keyword arm can hit) | `paraphrase` (near-zero overlap →
  isolates the vector arm, ~0 today) | `refusal` (out-of-corpus → must retrieve nothing).
- `expected_source` = a seed `source_name` (`null` for refusals); `expected_gist` = the
  correct-answer summary the judge grades against; `expect_no_answer` = `true` for refusals.
- The loader rejects duplicate ids and inconsistent rows; coverage tests require all 5 product
  areas + a paraphrase per area + refusals. Verify new cases: run the CI-gate command above.

## Interpret the result / the gate

`backend/tests/eval/runner.py::gate_failures` fails the build on: literal hit-rate < 1.0,
overall answerable < 0.60, any refusal leak, a degenerate golden set (0 answerable / no
literal bucket), or — when the judge ran — score-coverage < 0.9 or mean < 3.0. Thresholds live
in `backend/tests/eval/thresholds.py`.

- Expected baseline (SQLite/CI): **literal 1.0, paraphrase 0.0** (dead vector arm, #97),
  **refusal leaks 0**.
- When Phase 1/2 revives semantic retrieval, paraphrase hit-rate rises **on real Postgres** →
  update the baseline table in `RAG_QUALITY_PLAN` §8a. Do **not** weaken
  `test_paraphrase_baseline_is_dead` — it guards the hermetic SQLite path (vector arm
  short-circuits off-Postgres, so it stays 0 in CI by design).

## Provider / cost

Gemini free tier for dev/eval — primary `gemini-flash-latest`; OpenRouter `openai/gpt-4o-mini`
is the paid fallback (#99). Keep judge runs bounded (one call per case; ~20 cases).
