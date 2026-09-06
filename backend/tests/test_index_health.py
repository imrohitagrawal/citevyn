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


@pytest.mark.asyncio
async def test_vector_health_cannot_report_healthy_while_announcing_many_active(
    session,
) -> None:
    """``active_index_vector_health`` holds the #264 invariant itself, not just its caller.

    An earlier revision took ``active_count`` only to echo it into the payload
    and never fed it to the classifier, so a review produced
    ``{"status": "healthy", "healthy": true, "active_index_count": 7}`` from this
    function directly — #264 restated inside a single response, with the only
    thing preventing it in production being an ``if`` in the route.

    Both directions, so neither half is vacuous: the SAME index and the SAME
    chunks read ``healthy`` at ``active_count=1`` and ``ambiguous`` above it.

    Turns RED if the ``ambiguous=active_count > 1`` argument is dropped from the
    ``derive_vector_arm_status`` call.
    """
    from app.core.config import Settings
    from app.embeddings import configured_embedder_identity
    from app.embeddings.stub import StubEmbedder
    from app.models import IndexStatus, IndexVersion
    from app.services.index_health import active_index_vector_health
    from tests.conftest import seed_catalog

    settings = Settings(_env_file=None)
    identity = configured_embedder_identity(settings)
    await seed_catalog(session, embedder=StubEmbedder(dim=identity.dim), embedder_identity=identity)
    active = await session.get(IndexVersion, "v1")
    assert active is not None and active.status is IndexStatus.active

    single = await active_index_vector_health(session, active, settings, active_count=1)
    many = await active_index_vector_health(session, active, settings, active_count=7)

    # The control: this index genuinely IS healthy, so the flip below is caused
    # by the count and not by a broken seed.
    assert single["status"] == STATUS_HEALTHY
    assert single["healthy"] is True
    assert single["chunks_total"] > 0

    assert many["status"] == STATUS_AMBIGUOUS
    assert many["healthy"] is False
    assert many["active_index_count"] == 7
