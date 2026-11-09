"""The precision@1 floor, lowered to production's measured baseline (#596).

Lowering a check is proved both ways: the measured value now passes, and the
regression seen before it still fails. Each test names the change that turns it red.
"""

from __future__ import annotations

from typing import Any

from tests.eval import runner as runner_mod
from tests.eval.thresholds import MIN_PRECISION_AT_1


def _summary(precision_at_1: float) -> dict[str, Any]:
    return {
        "retrieval": {
            "answerable_total": 5,
            "hit_rate_by_kind": {"literal": 1.0},
            "overall_hit_rate": 1.0,
            "refusal_leaks": 0,
            "ranked_total": 32,
            "precision_at_1": precision_at_1,
            "mrr": 0.969,
        },
        "embedder": {"mode": "postgres"},
        "judge": {"available": False},
    }


def _p1_failures(value: float) -> list[str]:
    return [f for f in runner_mod.gate_failures(_summary(value)) if f.startswith("precision@1")]


def test_productions_measured_precision_passes() -> None:
    """Red when: the floor is raised above production's measured 0.969 (31/32)
    without retrieval improving to match."""
    assert _p1_failures(31 / 32) == []


def test_exactly_the_floor_passes() -> None:
    """Red when: the gate compares with ``<=`` instead of ``<``, failing a run
    that sits exactly on the decided floor."""
    assert _p1_failures(0.96) == []


def test_the_earlier_regression_still_fails() -> None:
    """Red when: the floor is lowered so far that the 0.938 measured on the
    OpenRouter route (30/32) passes."""
    assert _p1_failures(30 / 32)


def test_the_floor_is_the_decided_value() -> None:
    """Red when: the floor moves without a new owner decision in docs/DECISIONS.md."""
    assert MIN_PRECISION_AT_1 == 0.96
