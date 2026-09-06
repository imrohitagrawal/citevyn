"""Tests for the shared active-index resolver (``app/services/index_resolution.py``, #264).

The route-level behaviour lives in ``test_search_routes.py``; this file pins the
things the HTTP tests cannot see:

* the column projection and the dataclass that receives it cannot drift apart,
* the ``index_version`` tiebreaker really is the tiebreaker,
* the recovery instruction the ambiguous response ships is actually actionable.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import IndexStatus
from app.models.index_versions import IndexVersion
from app.services.index_resolution import (
    _INDEX_COLUMNS,
    ActiveIndexState,
    ResolvedIndex,
    resolve_active_index,
    resolve_previous_good_index,
)
from tests.conftest import seed_catalog


def test_resolved_index_fields_match_the_projection() -> None:
    """``ResolvedIndex`` is built from ``_INDEX_COLUMNS``; the two must not drift.

    This test exists for the REORDER case specifically, which is the one the
    runtime cannot catch. Keying the construction by name means an added,
    removed or renamed column raises ``TypeError`` on the first request — but a
    reorder then lands every value in its right field and raises nothing, so
    only an order assertion can see it.

    Why the reorder matters: ``created_at`` and ``promoted_at`` are adjacent and
    both ``datetime | None``. Under the positional construction this replaced,
    swapping them was measured to change nothing observable — same suite result
    (``6 failed, 1833 passed`` either way, the 6 being the known ``backend/.env``
    set), pyright green (a column ``select`` erases to ``Any``), ruff green. No
    test asserts ``created_at`` on the ``/health/index`` payload, and the seed
    sets it equal to ``promoted_at``. An operator reading the promotion time
    during a rollback would have been handed the creation time.

    Turns RED if a column is added to, removed from, or reordered in
    ``_INDEX_COLUMNS`` without the same change to ``ResolvedIndex``.
    """
    projected = [c.key for c in _INDEX_COLUMNS]
    fields = [f.name for f in dataclasses.fields(ResolvedIndex)]
    assert projected == fields
    # Partner: prove the list is non-trivial, so the equality above cannot pass
    # by both sides being empty.
    assert len(fields) == 8
    assert "promoted_at" in fields and "created_at" in fields


@pytest.mark.asyncio
async def test_previous_good_tiebreak_is_index_version_descending(
    session: AsyncSession,
) -> None:
    """With equal ``promoted_at``, ``index_version DESC`` decides — and is a STRING sort.

    The sibling test in ``test_search_routes.py`` pins the ``promoted_at`` axis
    by making the two disagree. This pins the axis that only matters once
    ``promoted_at`` cannot separate the rows, which no other test reaches.

    Turns RED if ``index_version.desc()`` becomes ``.asc()`` or is dropped: both
    give ``v10``.
    """
    await seed_catalog(session)
    same_instant = datetime.now(UTC) - timedelta(hours=1)
    for version in ("v10", "v2"):  # inserted low-then-high so insertion order says v10
        session.add(
            IndexVersion(
                index_version=version,
                status=IndexStatus.previous_good,
                source_version_hash=f"sha256:{version}",
                created_at=datetime.now(UTC),
                promoted_at=same_instant,
            )
        )
    await session.commit()

    winner = await resolve_previous_good_index(session)

    assert winner is not None
    # ``"v2" > "v10"`` lexically — this documents the sort as string, not
    # numeric. Nothing in the repo can reach this state (every writer of
    # ``status = active`` stamps ``promoted_at`` in the same statement), so it
    # pins the tiebreaker's behaviour rather than endorsing it as meaningful.
    assert winner.index_version == "v2"
    # Partner: the loser really is present, so the pick above is an ordering
    # result and not "the other row was never there".
    other = await session.get(IndexVersion, "v10")
    assert other is not None and other.status is IndexStatus.previous_good


@pytest.mark.asyncio
async def test_promoting_an_already_active_version_does_not_resolve_ambiguity(
    session: AsyncSession,
) -> None:
    """The recovery instruction has to name a NON-active version (#264).

    ``/health/index`` ships this guidance in its ``message`` for a state that
    only happens mid-incident, and ``promote_version`` returns early and writes
    nothing when the target is already ``active``. So the obvious reading —
    "two are active, promote one of them" — leaves the database exactly as
    dual-active as it was, and every probe keeps failing.

    ``test_promote_version_recovers_from_dual_active_state`` covers the case that
    DOES converge (promoting a third, non-active candidate). This is its
    partner: without it, nothing shows that the distinction matters, and the
    shipped message could quietly say the wrong thing.

    Turns RED if ``promote_version`` ever starts demoting the other active rows
    on an already-active target — at which point the message should change too.
    """
    from app.services import index_versions as index_version_service

    await seed_catalog(session)  # v1 active
    second = IndexVersion(
        index_version="v2",
        status=IndexStatus.active,
        source_version_hash="sha256:v2",
        created_at=datetime.now(UTC),
        promoted_at=datetime.now(UTC),
    )
    session.add(second)
    await session.commit()

    before = await resolve_active_index(session)
    assert before.state is ActiveIndexState.ambiguous
    assert before.active_count == 2

    # Promote one of the two rows that are ALREADY active — the naive recovery.
    await index_version_service.promote_version(
        session,
        index_version="v1",
        admin_user_id="admin",
        request_id="req-naive-recovery",
    )
    await session.commit()

    after = await resolve_active_index(session)
    assert after.state is ActiveIndexState.ambiguous, (
        "promoting an already-active version converged the database — if this is "
        "now true, /health/index's recovery message is wrong"
    )
    assert after.active_count == 2
