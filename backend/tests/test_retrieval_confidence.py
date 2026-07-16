"""Hermetic tests for the global-retrieval confidence gate (Phase 2).

Pure-function tests (no DB, no network) — the review's explicit ask, since the
vector arm is dead on the hermetic SQLite engine so the gate cannot be exercised
end-to-end there. The threshold cases are the REAL similarity scores measured on
the ingested 33-chunk corpus, so this test both pins the logic and documents why
an absolute floor fails but the margin separates.
"""

from __future__ import annotations

import pytest

from app.retrieval.confidence import is_confident_global_result

# Defaults under test (mirror the Settings defaults added for Phase 2).
_FLOOR = 0.30
_MARGIN = 0.04


def _confident(scores: list[float]) -> bool:
    return is_confident_global_result(scores, min_top_score=_FLOOR, min_margin=_MARGIN)


# --- empty / degenerate ----------------------------------------------------


def test_empty_result_is_never_confident() -> None:
    assert _confident([]) is False


def test_single_hit_passes_on_floor_alone() -> None:
    # No runner-up to compare against → the floor is the only guard.
    assert _confident([0.55]) is True
    assert _confident([0.20]) is False  # below floor


# --- the floor guard (rejects barely-related nearest chunks) ---------------


@pytest.mark.parametrize(
    "top,second",
    [(0.205, 0.180), (0.112, 0.110), (0.128, 0.101), (0.194, 0.170)],
)
def test_low_floor_refusals_rejected(top: float, second: float) -> None:
    """Real refusal sims (k8s/weather/python/capital) — nearest chunk is barely
    related, so the top score is below the floor regardless of margin."""
    assert _confident([top, second]) is False


# --- the margin guard (the case the absolute floor CANNOT catch) -----------


def test_competitor_api_refusal_rejected_by_margin_not_floor() -> None:
    """`refusal_openai` measured 0.403 top — ABOVE the 0.30 floor, and above a valid
    in-corpus paraphrase (0.394). Only its tiny margin (0.021) rejects it."""
    assert _confident([0.403, 0.382]) is False  # margin 0.021 < 0.04


def test_valid_paraphrase_below_competitor_refusal_still_passes() -> None:
    """`citevyn_par` measured 0.394 top (LOWER than the openai refusal's 0.403) but a
    healthy margin (0.116) — the relative signal keeps it where a floor would drop
    it or admit the refusal."""
    assert _confident([0.394, 0.278]) is True  # margin 0.116 >= 0.04


@pytest.mark.parametrize(
    "top,second",
    [(0.502, 0.419), (0.547, 0.438), (0.588, 0.479), (0.581, 0.511), (0.832, 0.664)],
)
def test_real_answerables_pass(top: float, second: float) -> None:
    """Real answerable sims (claude_code/claude_api/codex/gemini paraphrases + a
    literal) — one clearly-best chunk, so all pass the gate."""
    assert _confident([top, second]) is True


def test_margin_just_above_and_below_threshold() -> None:
    # Use unambiguous margins (avoid the exact-0.04 float-representation boundary).
    assert _confident([0.60, 0.55]) is True  # margin 0.05 > 0.04
    assert _confident([0.60, 0.57]) is False  # margin 0.03 < 0.04


def test_separation_holds_on_the_measured_corpus() -> None:
    """End-to-end: every measured answerable is confident and every measured refusal
    is not — the property the whole gate exists to guarantee."""
    answerables = [
        [0.502, 0.419],
        [0.394, 0.278],
        [0.547, 0.438],
        [0.588, 0.479],
        [0.581, 0.511],
        [0.832, 0.664],
        [0.830, 0.598],
    ]
    refusals = [[0.205, 0.180], [0.112, 0.110], [0.403, 0.382], [0.128, 0.101], [0.194, 0.170]]
    assert all(_confident(a) for a in answerables)
    assert not any(_confident(r) for r in refusals)
