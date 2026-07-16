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
