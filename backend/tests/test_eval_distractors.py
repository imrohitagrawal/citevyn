"""Hermetic tests for the distractor-corpus context precision/recall metric (#125, PR B).

The seed + live retrieval are Postgres-only (opt-in; the vector arm is dead on SQLite) and are
proven by the ``python -m tests.eval.distractors`` run recorded in RAG_QUALITY_PLAN §8a-8.
These tests cover the parts that DON'T need Postgres: the metric math, the gate, and the
golden↔seed key consistency (a typo'd gold key would silently score recall 0).
"""

from __future__ import annotations

from tests.eval.cases import load_cases
from tests.eval.distractors import (
    DISTRACTOR_AREA,
    DistractorOutcome,
    DistractorReport,
    distractor_gate_failures,
    seeded_chunk_keys,
)
from tests.eval.paths import DISTRACTOR_GOLDEN_PATH


def _outcome(gold: tuple[str, ...], retrieved: tuple[str, ...]) -> DistractorOutcome:
    return DistractorOutcome(case_id="c", gold_chunks=gold, retrieved_chunk_keys=retrieved)


def test_recall_at_k_counts_all_gold_anywhere_in_topk() -> None:
    both = _outcome(
        ("eval_grafana#0", "eval_grafana#1"), ("eval_grafana#0", "d1#0", "eval_grafana#1")
    )
    assert both.recall_at_k == 1.0
    # only one of two gold retrieved → recall 0.5
    one = _outcome(("eval_grafana#0", "eval_grafana#1"), ("eval_grafana#0", "d1#0", "d2#0"))
    assert one.recall_at_k == 0.5
    # neither gold retrieved → 0.0
    none = _outcome(("eval_grafana#0", "eval_grafana#1"), ("d1#0", "d2#0"))
    assert none.recall_at_k == 0.0


def test_precision_at_gold_is_rank_strict() -> None:
    # both gold occupy the top-2 → precision@2 = 1.0
    perfect = _outcome(
        ("eval_grafana#0", "eval_grafana#1"), ("eval_grafana#0", "eval_grafana#1", "d1#0")
    )
    assert perfect.precision_at_gold == 1.0
    # a distractor breaks into the top-2 (gold pushed to rank 3) → precision@2 = 0.5, even though
    # recall@k is still 1.0. This is the ranking regression recall@k alone would miss.
    outranked = _outcome(
        ("eval_grafana#0", "eval_grafana#1"), ("eval_grafana#0", "d1#0", "eval_grafana#1")
    )
    assert outranked.precision_at_gold == 0.5
    assert outranked.recall_at_k == 1.0
    # single-gold at rank 1 → precision@1 = 1.0; at rank 2 → 0.0
    assert _outcome(("eval_grafana#0",), ("eval_grafana#0", "d1#0")).precision_at_gold == 1.0
    assert _outcome(("eval_grafana#0",), ("d1#0", "eval_grafana#0")).precision_at_gold == 0.0


def test_gold_margin_reports_headroom_over_distractors() -> None:
    """gold_margin = min retrieved-gold score − max retrieved-distractor score (None if the
    top-k has no gold or no distractor)."""
    o = DistractorOutcome(
        case_id="m",
        gold_chunks=("eval_grafana#0", "eval_grafana#1"),
        retrieved_chunk_keys=("eval_grafana#0", "eval_grafana#1", "d1#0"),
        retrieved_scores=(0.80, 0.70, 0.55),
    )
    assert abs(o.gold_margin - (0.70 - 0.55)) < 1e-9  # min gold 0.70 − best distractor 0.55
    # no distractor in the top-k → margin undefined
    all_gold = DistractorOutcome(
        case_id="g",
        gold_chunks=("eval_grafana#0",),
        retrieved_chunk_keys=("eval_grafana#0",),
        retrieved_scores=(0.9,),
    )
    assert all_gold.gold_margin is None
    # scores absent (metric-math unit outcomes) → margin undefined, never crashes
    assert _outcome(("eval_grafana#0",), ("eval_grafana#0", "d1#0")).gold_margin is None


def test_report_aggregates() -> None:
    report = DistractorReport(
        outcomes=(
            _outcome(("eval_grafana#0",), ("eval_grafana#0", "d1#0")),  # recall 1, prec 1
            _outcome(("eval_grafana#1",), ("d1#0", "eval_grafana#1")),  # recall 1, prec 0
        )
    )
    d = report.as_dict()
    assert d["cases"] == 2
    assert d["mean_recall_at_k"] == 1.0
    assert d["min_recall_at_k"] == 1.0
    assert d["mean_precision_at_gold"] == 0.5


def test_gate_flags_low_recall_and_low_precision_and_empty() -> None:
    # healthy → no failures
    healthy = DistractorReport(
        outcomes=(
            _outcome(("eval_grafana#0", "eval_grafana#1"), ("eval_grafana#0", "eval_grafana#1")),
        )
    )
    assert distractor_gate_failures(healthy) == []

    # a gold chunk missing from top-k → recall failure
    low_recall = DistractorReport(
        outcomes=(_outcome(("eval_grafana#0", "eval_grafana#1"), ("eval_grafana#0", "d1#0")),)
    )
    assert any("recall@k" in f for f in distractor_gate_failures(low_recall))

    # a distractor outranks a gold (recall still 1.0) → precision failure only
    low_prec = DistractorReport(
        outcomes=(
            _outcome(
                ("eval_grafana#0", "eval_grafana#1"), ("eval_grafana#0", "d1#0", "eval_grafana#1")
            ),
        )
    )
    prec_fails = distractor_gate_failures(low_prec)
    assert any("precision@|gold|" in f for f in prec_fails)
    assert not any("recall@k" in f for f in prec_fails)

    # an empty golden set must FAIL, not pass vacuously
    assert distractor_gate_failures(DistractorReport(outcomes=())) == [
        "distractor golden set is empty (zero cases)"
    ]


def test_distractor_golden_is_consistent_with_the_seed() -> None:
    """Every distractor golden case is postgres_only, scoped to the distractor area, and labels
    gold_chunks that the seed actually produces (a typo would silently score recall 0)."""
    seeded = seeded_chunk_keys()
    # sanity: the 2-chunk gold source + 16 single-chunk distractors (incl. 2 lexical hard
    # negatives) = 18 keys, all distinct.
    assert len(seeded) == 18
    assert {"eval_grafana#0", "eval_grafana#1"} <= seeded
    # the two lexical hard negatives must be present (they give precision@|gold| its teeth).
    assert {"eval_grafana_panel_library#0", "eval_grafana_silences#0"} <= seeded
    cases = load_cases(DISTRACTOR_GOLDEN_PATH)
    assert cases, "distractor golden must not be empty"
    for case in cases:
        assert case.postgres_only, f"{case.id} must be postgres_only (vector arm is dead on SQLite)"
        assert case.area == DISTRACTOR_AREA, f"{case.id} must be scoped to {DISTRACTOR_AREA}"
        assert case.gold_chunks, f"{case.id} must label gold_chunks"
        for key in case.gold_chunks:
            assert key in seeded, f"{case.id} labels gold {key!r} the seed does not produce"
