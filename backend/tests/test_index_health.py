"""Tests for the vector-arm health signal (Phase 4c, app/services/index_health.py).

Two layers:

* ``derive_vector_arm_status`` — the pure classifier (all five states, exhaustively).
* ``active_index_vector_health`` + the ``/health/index`` route — the DB counting +
  embedder-identity comparison, exercised against the hermetic seeded catalog (whose
  chunks are unembedded → the ``dead`` state, which is exactly the #97 failure the
  signal exists to surface).
"""

from __future__ import annotations

import pytest

from app.services.index_health import (
    STATUS_AMBIGUOUS,
    STATUS_DEAD,
    STATUS_EMPTY,
    STATUS_HEALTHY,
    STATUS_MISMATCH,
    STATUS_PARTIAL,
    derive_vector_arm_status,
)


@pytest.mark.parametrize(
    ("total", "embedded", "mismatch", "expected"),
    [
        (0, 0, False, STATUS_EMPTY),  # no chunks yet
        (5, 0, False, STATUS_DEAD),  # chunks exist, none embedded (#97)
        (5, 0, True, STATUS_DEAD),  # dead wins over mismatch
        (5, 5, True, STATUS_MISMATCH),  # embedded but wrong vector space (Tier-3)
        (5, 3, False, STATUS_PARTIAL),  # ingest in progress
        (5, 5, False, STATUS_HEALTHY),  # every chunk embedded, query-compatible
    ],
)
def test_derive_vector_arm_status(total: int, embedded: int, mismatch: bool, expected: str) -> None:
    assert (
        derive_vector_arm_status(chunks_total=total, chunks_embedded=embedded, mismatch=mismatch)
        == expected
    )


def test_derive_vector_arm_status_defaults_to_not_ambiguous() -> None:
    """The ``ambiguous`` keyword is optional and defaults to "one active row" (#264).

    Partner to the cases below: without this, they could pass because the
    parameter is somehow always truthy. This pins that the six cases above —
    which do not pass ``ambiguous`` at all — are genuinely the non-ambiguous
    branch.
    """
    assert (
        derive_vector_arm_status(chunks_total=5, chunks_embedded=5, mismatch=False)
        == STATUS_HEALTHY
    )


@pytest.mark.parametrize(
    ("total", "embedded", "mismatch"),
    [
        (0, 0, False),  # would be ``empty``
        (5, 0, False),  # would be ``dead`` — the most severe existing state
        (5, 5, True),  # would be ``mismatch``
        (5, 3, False),  # would be ``partial``
        (5, 5, False),  # would be ``healthy`` — the #264 lie
    ],
)
def test_ambiguous_outranks_every_other_vector_arm_state(
    total: int, embedded: int, mismatch: bool
) -> None:
    """#264: with >1 active row, no other verdict is knowable, so ``ambiguous`` wins.

    Each row above is one of the five states the classifier can otherwise
    return (the parametrize table in ``test_derive_vector_arm_status`` proves
    each of these inputs really does produce that other state), so this pins the
    precedence against every one of them rather than against a single sample.

    Turns RED if the ``ambiguous`` branch is moved below any other check, or
    removed.
    """
    assert (
        derive_vector_arm_status(
            chunks_total=total, chunks_embedded=embedded, mismatch=mismatch, ambiguous=True
        )
        == STATUS_AMBIGUOUS
    )
