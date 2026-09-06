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
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import db as db_module
from app.core.config import get_settings
from app.embeddings import IndexStampStatus, configured_embedder_identity
from app.embeddings.stub import StubEmbedder
from app.main import create_app
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


@pytest.fixture
def app_with_seeded_session_for_resolution(session: AsyncSession):
    """A FastAPI app whose ``get_session`` yields this test's session."""
    app = create_app()

    async def _override():
        yield session

    app.dependency_overrides[db_module.get_session] = _override
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


def _add_index(session, *, version: str, status: IndexStatus, promoted_at) -> None:
    import asyncio

    session.add(
        IndexVersion(
            index_version=version,
            status=status,
            source_version_hash=f"sha256:{version}",
            created_at=datetime.now(UTC),
            promoted_at=promoted_at,
        )
    )
    asyncio.get_event_loop().run_until_complete(session.commit())


def test_resolved_index_fields_match_the_projection() -> None:
    """``ResolvedIndex`` is built from ``_INDEX_COLUMNS``; the two must not drift.

    This test exists for the REORDER case specifically, which is the one the
    runtime cannot catch. Keying the construction by name means an added,
    removed or renamed column raises ``TypeError`` on the first request — but a
    reorder then lands every value in its right field and raises nothing, so
    only an order assertion can see it.

    Why the reorder matters: ``created_at`` and ``promoted_at`` are adjacent and
    both ``datetime | None``. Under the positional construction this replaced,
    swapping them was measured to change nothing observable — the suite result
    was byte-identical with and without the swap, pyright stayed green (a column
    ``select`` erases to ``Any``) and so did ruff. No test asserts ``created_at``
    on the ``/health/index`` payload, and the seed sets it equal to
    ``promoted_at``. An operator reading the promotion time during a rollback
    would have been handed the creation time.

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


# ---------------------------------------------------------------------------
# The pure partitioner (#264 rule, #351 single-snapshot fetch)
# ---------------------------------------------------------------------------


def _row(version: str, status: IndexStatus, promoted_at=None) -> dict:
    """A projected row shaped like the one the shared query returns."""
    return {
        "index_version": version,
        "source_version_hash": f"sha256:{version}",
        "created_at": None,
        "promoted_at": promoted_at,
        "evaluation_run_id": None,
        "embedding_provider": None,
        "embedding_model": None,
        "embedding_dim": None,
        "status": status,
    }


def test_partition_is_pure_and_takes_the_first_row_of_each_status() -> None:
    """The rule is a pure function over PRE-ORDERED rows — no session, no sorting.

    That purity is what let the fetch collapse to one statement (#351): the
    ">1 active means ambiguous" rule no longer has to be welded to a ``COUNT``
    query, so both of the health route's questions can be answered from a single
    snapshot.

    The input deliberately lists a LATER row first for each status, because the
    caller's ``ORDER BY`` has already run — this function must not re-sort, or
    the shared ordering constant would be silently bypassed.
    """
    from app.services.index_resolution import partition_index_rows

    rows = [
        _row("a-newest", IndexStatus.active),
        _row("pg-newest", IndexStatus.previous_good),
        _row("pg-older", IndexStatus.previous_good),
    ]
    part = partition_index_rows(rows)

    assert part.active.state is ActiveIndexState.one
    assert part.active.active_count == 1
    assert part.active.row is not None and part.active.row.index_version == "a-newest"
    assert part.previous_good is not None
    assert part.previous_good.index_version == "pg-newest"


@pytest.mark.parametrize(
    ("statuses", "expected_state", "expected_count"),
    [
        ([], ActiveIndexState.none, 0),
        ([IndexStatus.previous_good], ActiveIndexState.none, 0),
        ([IndexStatus.active], ActiveIndexState.one, 1),
        ([IndexStatus.active, IndexStatus.active], ActiveIndexState.ambiguous, 2),
        ([IndexStatus.active] * 5, ActiveIndexState.ambiguous, 5),
    ],
)
def test_partition_counts_actives_from_the_rows_it_is_given(
    statuses: list[IndexStatus], expected_state: ActiveIndexState, expected_count: int
) -> None:
    """The count comes from the SAME rows the winner is picked from (#351).

    It used to come from a separate ``COUNT`` statement, which is what made the
    resolver non-atomic: two statements, two snapshots, so a promote landing
    between them could report ``one`` for a database that already had two.

    Turns RED if the ``len(active) > 1`` comparison is weakened or the count
    stops being derived from the row list.
    """
    from app.services.index_resolution import partition_index_rows

    part = partition_index_rows([_row(f"v{i}", s) for i, s in enumerate(statuses)])
    assert part.active.state is expected_state
    assert part.active.active_count == expected_count
    # Ambiguity never hands back a row to treat as the answer.
    if expected_state is ActiveIndexState.ambiguous:
        assert part.active.row is None


def test_partition_rule_is_shared_by_all_three_call_sites(
    app_with_seeded_session_for_resolution, session, monkeypatch
) -> None:
    """All three callers must go through the ONE pure rule (#264).

    This is the property the whole package exists to establish, re-proved after
    #351 moved the rule out of the query layer: replacing
    ``partition_index_rows`` with a stub that always says ``ambiguous`` must
    change what the health route, the retrieval provenance gate AND the
    orchestrator all do — on a database with exactly ONE active row, where the
    real rule would say ``one``.

    A mutation test shows the same thing by deleting the guard; this asserts it
    executably, so the property cannot quietly decay between review rounds.

    Turns RED if any call site stops routing through the shared function.
    """
    import asyncio

    from app.answer.orchestrator import _retrieve_active_index
    from app.retrieval.hybrid import HybridRetriever
    from app.services import index_resolution

    identity = configured_embedder_identity(get_settings())
    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        seed_catalog(session, embedder=StubEmbedder(dim=identity.dim), embedder_identity=identity)
    )

    # Control: with the real rule, one active row resolves to ``one`` everywhere.
    assert loop.run_until_complete(index_resolution.resolve_active_index(session)).state is (
        ActiveIndexState.one
    )

    def _always_ambiguous(rows):
        return index_resolution.IndexPartition(
            active=index_resolution.ActiveIndexResolution(
                state=ActiveIndexState.ambiguous, active_count=99, row=None
            ),
            previous_good=None,
        )

    monkeypatch.setattr(index_resolution, "partition_index_rows", _always_ambiguous)

    with TestClient(app_with_seeded_session_for_resolution) as client:
        body = client.get("/health/index").json()
    assert body["vector_arm"]["status"] == "ambiguous", "the health route bypassed the shared rule"
    assert body["vector_arm"]["active_index_count"] == 99

    stamp = loop.run_until_complete(
        HybridRetriever(
            session, active_index_version=None, embedder_identity=identity
        )._active_index_stamp()
    )
    assert stamp is IndexStampStatus.ambiguous, "the retrieval gate bypassed the shared rule"

    assert loop.run_until_complete(_retrieve_active_index(session)) == ("", ""), (
        "the orchestrator bypassed the shared rule"
    )


def test_health_index_reads_the_index_table_in_exactly_one_statement(
    app_with_seeded_session_for_resolution, session
) -> None:
    """#351: two questions, ONE snapshot.

    The route asks which index is active and which is the rollback target.
    Answering them in separate statements let a promote commit in between and
    put the same index in both answers — reproduced on real Postgres with two
    concurrent connections. One ordered query over both statuses closes it.

    Counting statements is a structural proxy that SQLite can check; the
    Postgres-marked test in ``test_pg_integration.py`` proves the actual
    concurrent behaviour, which this cannot see.

    Turns RED if either row is fetched with its own query again.
    """
    import asyncio

    from sqlalchemy import event

    identity = configured_embedder_identity(get_settings())
    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        seed_catalog(session, embedder=StubEmbedder(dim=identity.dim), embedder_identity=identity)
    )
    _add_index(
        session,
        version="pg1",
        status=IndexStatus.previous_good,
        promoted_at=datetime.now(UTC) - timedelta(hours=1),
    )

    seen: list[str] = []

    engine = session.get_bind()

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if "index_versions" in statement:
            seen.append(statement)

    try:
        with TestClient(app_with_seeded_session_for_resolution) as client:
            body = client.get("/health/index").json()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert len(seen) == 1, f"expected ONE index_versions query, got {len(seen)}:\n" + "\n".join(
        seen
    )
    # Partner: the single query really did answer BOTH questions, so the count
    # above is not 1 because the route stopped reporting something.
    assert body["active_index"]["index_version"] == "v1"
    assert body["previous_good_index"]["index_version"] == "pg1"
