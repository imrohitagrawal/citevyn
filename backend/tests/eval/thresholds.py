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
# Judge-robustness panel (Item 1). The judged metric is now the mean over per-case
# ``min(standard_median, adversarial)`` scores (see ``judge.py``); ``MIN_MEAN_JUDGE``
# gates that mean. ``CONTESTED_SPREAD`` is the standard-member max-min at or above
# which a case is flagged ``contested`` (same-rubric disagreement worth review) —
# informational, not a gate.
CONTESTED_SPREAD = 2
# Deterministic groundedness (Item 1c) is gated PER CASE on the ``--postgres`` run only
# (the mode where every fact-bearing answerable case can actually retrieve — the
# hermetic dead-vector-arm path would structurally zero the paraphrase fact-cases and
# is excluded, mirroring the multihop gate). On that run every fact-bearing case must be
# FULLY grounded (coverage 1.0): a single wrong/absent hard fact fails, which an
# aggregate mean over binary single-fact cases would leak. Facts are chosen phrasing-
# stable (identifiers + number-with-alternatives) so a correct answer reaches 1.0
# (empirically it does); a legitimate miss is fixed by adding an alternative, never by
# weakening the gate. ``grounded_fact_rate`` is still reported for visibility.
# Multi-hop (Phase 3): a cross-product case must retrieve EVERY named area. Provable
# only with the live vector arm, so it is gated ONLY on the --postgres run (excluded
# from the hermetic overall gate). Every multi-hop case must hit there.
MIN_MULTIHOP_HIT_RATE = 1.0
# Conversation memory (Phase 3b): an anaphoric follow-up must resolve against its
# prior turns and retrieve its expected area. Unlike multihop, the memory rewrite
# resolves DETERMINISTICALLY (domain routing + keyword), so this is gated on the
# HERMETIC run too (a strong, non-flaky gate — a broken rewrite fails CI). Every
# followup case must hit.
MIN_FOLLOWUP_HIT_RATE = 1.0
# Chunk-level rank-sensitive metric (#125). MRR + precision@1 over single-relevant answerable
# cases (exactly one gold chunk): the 15 core literal+paraphrase + 3 followup = 18 cases (the
# adversarial injection/misspelling cases opt OUT — rank-precision on a malformed query is a
# category error). Gated ONLY on the --postgres run: hermetically the vector arm is dead, so
# paraphrase cases structurally score 0 and a literal-only MRR would just restate the literal
# hit-rate.
#
# What actually MOVES the number: 16 of the 18 route SCOPED (retrieve(product_area=area) → the
# gold is the only candidate in its one-chunk area → structurally rank-1, a keyword-arm
# tautology on today's corpus). Only 2 route GLOBAL — claude_code_par_toolgate,
# citevyn_par_membership (product_area=None → the unscoped vector arm ranks the gold against
# ALL other areas' chunks) — so precision@1 there is a strictly stronger signal than hit-rate,
# and a fully-dead vector arm drops it below 1.0 (the globals return []). PR B's within-area
# distractors make the scoped cases informative too.
#
# Baseline MEASURED 2026-07-17 on real Postgres + openai/text-embedding-3-small, verified
# STABLE across 5 consecutive runs (byte-identical rank order every run): MRR 1.000,
# precision@1 1.000 over n=18 (recorded in docs/RAG_QUALITY_PLAN.md §8a-7). precision@1 is
# pinned EXACT (1.0): embeddings are effectively deterministic run-to-run here, so a wrong-area
# chunk outranking a gold on either global case is a real regression, not jitter. MRR keeps a
# small margin as a tolerant companion. The 1.0 is an ASSERTION about retrieval quality ("a
# good retriever keeps every gold #1"), NOT an immovable floor: when PR B adds a deliberately-
# hard within-area distractor that legitimately outranks a gold, lowering precision@1 to the
# new measured baseline WITH justification is the correct move.
MIN_MRR = 0.95
MIN_PRECISION_AT_1 = 1.0
# Distractor-corpus context precision/recall (#125, PR B). Measured by the OPT-IN, Postgres-
# only distractor eval (tests.eval.distractors) over 18 competing chunks (2 gold + 16
# distractors, incl. 2 LEXICAL HARD NEGATIVES that share the gold vocabulary) with VECTOR-ONLY
# scoped retrieval — never by the locked hermetic/judged run. ``RECALL_AT_K`` = every gold
# chunk is in the top-k; ``PRECISION_AT_GOLD`` = the top-|gold| retrieved keys are all gold
# (precision@2 for the 2-gold case) — the rank-strict axis a distractor breaking into the top
# would fail. (At the 1.0 pin precision@|gold| subsumes recall@k per case; recall is kept as a
# cheap independent safety net that bites if the precision floor is ever lowered.)
#
# Pinned 1.0/1.0 is EARNED, not a knife-edge: measured 2026-07-17 on real Postgres +
# openai/text-embedding-3-small, STABLE across 5 runs, the true gold outranks even the lexical
# hard negatives (panel_library, silences) by a min cosine margin of 0.092 (0.092–0.158;
# recorded per-case in the report's ``gold_margin`` so a shrinking margin warns before a flip).
# A regression that let a hard negative outrank a gold would have to erode ~0.09 of margin — a
# real ranking regression, not provider jitter. Lower ONLY with a re-measured justification
# (e.g. a future confuser that legitimately outranks a gold). See RAG_QUALITY_PLAN §8a-8.
MIN_DISTRACTOR_RECALL_AT_K = 1.0
MIN_DISTRACTOR_PRECISION_AT_GOLD = 1.0
