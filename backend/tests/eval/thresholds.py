"""Eval regression thresholds — the single source of truth for the gate.

Imported by both the CI gate (:mod:`tests.test_eval_harness`) and the CLI
runner (:mod:`tests.eval.runner`) so a threshold is never defined twice.

The numbers are pinned to the **Phase 0 baseline** measured on the conftest
seed corpus (see ``docs/RAG_QUALITY_PLAN.md``): literal cases hit
deterministically (keyword/exact arm), paraphrases miss (dead vector arm,
#97), refusals retrieve nothing.

* ``MIN_LITERAL_HIT_RATE = 1.0`` — every literal case MUST hit. This is the
  strong, non-flaky regression gate: any drop means a retrieval regression on
  vocabulary the keyword arm already handles.
* ``MIN_OVERALL_HIT_RATE = 0.60`` — a floor on answerable (literal +
  paraphrase) hit-rate. Baseline is 10/15 ≈ 0.667; the floor sits just below
  so it cannot flake, and later phases only push it up.
* ``MAX_REFUSAL_LEAKS = 0`` — an out-of-corpus question must never retrieve a
  chunk.
* ``MIN_MEAN_JUDGE = 3.0`` — mean LLM-judge score floor, enforced only when the
  judge actually ran (opt-in, real provider key). Informational at baseline.
* ``MIN_JUDGE_COVERAGE = 0.9`` — when the judge is available, at least this
  fraction of attempted cases must produce a usable score. Guards against an
  inflated mean over a biased survivor subset: a run where most cases errored
  and a lucky few scored high must not pass on that mean alone.
"""

from __future__ import annotations

MIN_LITERAL_HIT_RATE = 1.0
MIN_OVERALL_HIT_RATE = 0.60
MAX_REFUSAL_LEAKS = 0
MIN_MEAN_JUDGE = 3.0
MIN_JUDGE_COVERAGE = 0.9
