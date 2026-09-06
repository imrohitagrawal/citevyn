"""The single source of truth for "which :class:`IndexVersion` is active?" (#264).

Three call sites need to name a SINGLE active row, and before this module each
computed it itself:

* :meth:`app.retrieval.hybrid.HybridRetriever._active_index_stamp` — the Tier-3
  provenance gate (#57/#226),
* :func:`app.answer.orchestrator._retrieve_active_index` — the document scope and
  the answer cache key,
* the ``GET /health/index`` route in :mod:`app.api.routes.search` — the operator
  signal.

Two of the three enforced the single-active-row invariant (#58) and sorted
deterministically; the third did neither, so on a **dual-active** database it
named an arbitrary row and reported the vector arm ``healthy`` at the exact
moment the read path had failed closed (#264, measured at the HTTP boundary).
The divergence was documented as a known hazard inside
:func:`app.embeddings.is_index_embedder_mismatch` rather than removed. This
module removes it: one query, one ordering, one definition of "ambiguous", and
the three callers differ only in what they project out of the answer.

A fourth site reads the same column for a different question:
:func:`app.services.exact_lookup.exact_lookup` filters documents on
``index_version IN (SELECT ... WHERE status == active)``. That is set membership,
not a single-row pick, so it cannot produce the #264 coin flip and this resolver
would be the wrong tool for it — but it does mean "which index is active?" still
has a second, unrelated answer in the codebase. Tracked separately.

Deliberately **not** in this module:

* **Logging.** Each caller emits its own event name
  (``retrieval_multiple_active_indexes`` on ``citevyn.retrieval`` /
  ``orchestrator_multiple_active_indexes`` on ``citevyn.answer``). Moving the
  RETRIEVAL one in here would turn its test red — ``test_retrieval.py``'s
  ``_capture_retrieval_logs`` attaches a handler to the concrete
  ``citevyn.retrieval`` logger, so it pins the event name AND the logger. The
  orchestrator's would NOT: that test reads ``caplog.records``, which collects
  from root, so a resolver-level logger keeping the same event name leaves it
  green. Measured. So one of the two is genuinely enforced and the other rests on
  the argument that two named events on two loggers tell an operator more than
  one shared event would.
* **A WARN on the health route.** ``.github/workflows/uptime.yml`` polls it every
  30 minutes and ``infra/docker/scripts/deploy_verify.sh`` on every deploy;
  logging per probe would add noise for a condition the read path already records
  once per request. (The Fly load balancer polls ``/health``, not this route —
  see ``infra/fly/fly.toml`` — so this is a scheduled probe, not a hot path.)
* **The read-path *policy* for zero active rows.** What the arms should do when
  nothing is active is #265, deliberately owner-gated. This module reports the
  state (:attr:`ActiveIndexState.none`); it does not decide what anyone does
  about it. Resolution living in one place is precisely what gives #265 a single
  seam to change later.

**Columns, not ORM entities, on purpose.** The two read-path callers projected
three and two columns respectively; switching them to ``select(IndexVersion)``
would not merely cost more columns. The sessionmaker sets ``autoflush=False``
(``app/core/db.py``), so an entity query returns the *identity-map* instance and
would expose pending, unflushed attribute values —
:func:`app.services.index_versions.promote_version` mutates ``status`` and
``promoted_at`` in-session — and ``IndexVersion.documents`` /
``evaluation_run`` are ``lazy="raise"``, which turns a later attribute touch into
an exception where a projection simply has no such attribute. A column
projection has neither hazard, so the refactor cannot change what the read path
sees.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IndexStatus, IndexVersion

# Every column any of the three callers projects, in one place. Nothing here is
# a relationship, so this can never trip ``lazy="raise"``.
_INDEX_COLUMNS = (
    IndexVersion.index_version,
    IndexVersion.source_version_hash,
    IndexVersion.created_at,
    IndexVersion.promoted_at,
    IndexVersion.evaluation_run_id,
    IndexVersion.embedding_provider,
    IndexVersion.embedding_model,
    IndexVersion.embedding_dim,
)

# ``status`` rides along so the partition can be done in Python, but it is not a
# ``ResolvedIndex`` field — it is the thing being partitioned ON, not a property
# of the resolved row.
_STATUS_KEY = "status"

# Safety bound on the fetch. ``index_versions`` grows by one per ingest and is
# admin-controlled (production has ONE row), so this is a guard against a
# pathological table rather than a working limit — but an unbounded ``SELECT``
# is a thing ``code_review.md`` blocks on, so it is stated rather than assumed.
#
# What the cap can and cannot distort: :data:`_ACTIVE_FIRST` sorts every
# ``active`` row ahead of every ``previous_good`` row, so truncation drops the
# OLDEST previous-good rows first and can never hide an active row. The
# ambiguity decision and ``active_count`` are therefore exact for any table with
# 64 or fewer active rows, whatever else it holds.
#
# The reverse is NOT guaranteed and is worth saying plainly: with 64 or more
# ACTIVE rows the cap fills with actives and every ``previous_good`` row is
# truncated, so ``GET /health/index`` would report ``previous_good_index: null``
# — "no rollback target" — on the field #351 is about. That state is already
# ``ambiguous`` and needs a promote to converge before a rollback target means
# anything, and 64 simultaneously-active rows is far outside what any writer
# produces, so it is documented rather than defended against.
#
# That guarantee is structural on purpose. Arguing it from ``promoted_at``
# instead — "a demoted row always carries an earlier stamp than the active row
# that displaced it", which is true of both writers of ``status = active``
# (``promote_version`` and the seed's ``_promote`` each stamp it on the very next
# line) — would have made it conditional on a row whose stamp is NULL never being
# active. Nothing in ``backend/app`` or ``db/`` produces that row, but tests
# construct it (``test_promote_version_recovers_from_dual_active_state`` marks a
# candidate active without stamping), and under ``NULLS LAST`` such a row sorts
# LAST — so with enough previous-good rows the cap could have truncated away the
# very row that makes the database ambiguous. One extra sort key is cheaper than
# a footnote about when the bound holds.
_MAX_INDEX_ROWS = 64

# Actives before previous-goods, so :data:`_MAX_INDEX_ROWS` can only ever
# truncate previous-good rows. It does NOT decide anything within a status —
# ``_ORDER_BY`` still does — so the row each caller gets is unchanged.
_ACTIVE_FIRST = case((IndexVersion.status == IndexStatus.active, 0), else_=1).asc()

# ONE ordering, shared by every status and every caller. Two call sites sorting
# differently is the divergence #264 is about.
_ORDER_BY = (
    IndexVersion.promoted_at.desc().nulls_last(),
    IndexVersion.index_version.desc(),
)


@dataclass(frozen=True)
class ResolvedIndex:
    """One index version, projected. Attribute-compatible with :class:`IndexVersion`.

    The attribute names match the ORM model's so the same helpers
    (:func:`app.services.index_health.active_index_vector_health`, the route's
    ``_index_payload``) accept either without a shim.
    """

    index_version: str
    source_version_hash: str
    created_at: datetime | None
    promoted_at: datetime | None
    evaluation_run_id: uuid.UUID | None
    embedding_provider: str | None
    embedding_model: str | None
    embedding_dim: int | None


class ActiveIndexState(StrEnum):
    """How many rows claim to be the active index.

    ``ambiguous`` is a first-class state, not an error: nothing in the schema
    enforces the single-active-row invariant, and databases really do drift into
    two active rows (seeding plus repeated local ingests will do it — see
    ``test_promote_version_recovers_from_dual_active_state``). What matters is
    that every caller sees the same three-way answer rather than one of them
    silently resolving a coin flip.
    """

    none = "none"
    one = "one"
    ambiguous = "ambiguous"


@dataclass(frozen=True)
class ActiveIndexResolution:
    """The resolved active index, plus how many rows competed for the title.

    ``row`` is populated only in the ``one`` state. It is ``None`` under
    ``ambiguous`` even though the query below would produce a deterministic
    winner: handing a caller a row it has just been told is ambiguous invites it
    to treat a coin flip as an answer, which is the whole of #264.
    """

    state: ActiveIndexState
    active_count: int
    # No default: ``state=one`` with a forgotten row is a representable illegal
    # state, so every construction has to say what the row is, even when it is
    # ``None``.
    row: ResolvedIndex | None


@dataclass(frozen=True)
class IndexPartition:
    """Both answers ``GET /health/index`` needs, taken from ONE set of rows.

    Returning them together is the point: resolving them separately meant two
    statements, and under READ COMMITTED each statement takes its own snapshot,
    so a promote committing between them could make the same index appear as
    both the active index and the rollback target (#351 — reproduced on real
    Postgres with two concurrent connections).
    """

    active: ActiveIndexResolution
    previous_good: ResolvedIndex | None


def _row_to_resolved(row: Mapping[Any, Any]) -> ResolvedIndex:
    """Build a :class:`ResolvedIndex` from a projected row, keyed by NAME.

    Everything but ``status`` is passed through as a keyword, so a column added
    to, removed from, or renamed in ``_INDEX_COLUMNS`` raises ``TypeError`` here
    rather than silently mis-populating a field. (A REORDER is a no-op under
    keyword construction; ``test_resolved_index_fields_match_the_projection``
    is what pins that case, at import time.)
    """
    return ResolvedIndex(**{k: v for k, v in row.items() if k != _STATUS_KEY})


def partition_index_rows(rows: Sequence[Mapping[Any, Any]]) -> IndexPartition:
    """THE shared rule, as a pure function over already-fetched rows (#264/#351).

    This is the single guard the three call sites share. It used to be welded to
    the database access — a ``COUNT`` statement plus an ordered ``LIMIT 1`` — and
    that welding is what made the health route need three statements, and hence
    three snapshots, to answer two questions. Splitting the *rule* from the
    *fetch* lets every caller run exactly one query and still share one
    definition of "more than one active row means ambiguous".

    ``rows`` MUST already be ordered by :data:`_ORDER_BY`; the caller's single
    ``SELECT`` does that, so "first row of a status" is "newest row of that
    status" and this function does no sorting of its own. Pure and total: no
    session, no I/O, no clock.

    Deleting the ``> 1`` comparison below must turn the health-route, retrieval
    AND orchestrator tests red together —
    ``test_partition_rule_is_shared_by_all_three_call_sites`` re-proves that,
    because it is the property #264 exists to establish.
    """
    active = [r for r in rows if r[_STATUS_KEY] is IndexStatus.active]
    previous = next(
        (_row_to_resolved(r) for r in rows if r[_STATUS_KEY] is IndexStatus.previous_good),
        None,
    )

    if len(active) > 1:
        resolution = ActiveIndexResolution(
            state=ActiveIndexState.ambiguous, active_count=len(active), row=None
        )
    elif not active:
        resolution = ActiveIndexResolution(state=ActiveIndexState.none, active_count=0, row=None)
    else:
        resolution = ActiveIndexResolution(
            state=ActiveIndexState.one, active_count=1, row=_row_to_resolved(active[0])
        )
    return IndexPartition(active=resolution, previous_good=previous)


def _ordered_rows_query(*statuses: IndexStatus):
    """ONE deterministically-ordered query over the given statuses.

    One statement means one snapshot, which is what closes #351 — and it also
    closes the COUNT-then-SELECT window the pre-#264 read-path resolvers carried,
    because the count is now taken from the same rows the winner is picked from
    rather than from a separate earlier statement.

    ``promoted_at DESC NULLS LAST`` then ``index_version DESC`` — the ordering
    the retrieval gate and the orchestrator have always used, lifted here
    verbatim so the three callers cannot drift apart again. The secondary key is
    a tiebreaker, not the rule.

    Where this ordering is actually load-bearing: **``previous_good``**.
    :func:`app.services.index_versions.promote_version` demotes the outgoing
    ``active`` row and never clears the ``previous_good`` rows already there —
    it is the only writer of that status in ``backend/app`` and ``db/`` — so
    after N promotions N-1 of them coexist and only ``promoted_at`` (which a
    demoted row keeps from when it was active) says which one is the current
    rollback target.

    On the ``active`` path the ordering only decides anything when exactly one
    active row survives the partition, so it is defensive there. It is kept
    because it is the SAME ordering constant for both statuses — the divergence
    #264 is about was two call sites sorting differently.

    ``NULLS LAST`` is load-bearing on Postgres, which orders NULLs *first* under
    ``DESC`` by default: a never-promoted row must not outrank a real one. SQLite
    already sorts NULLs last under ``DESC``, so the clause is a no-op there and
    the hermetic suite cannot see it — ``test_pg_integration`` carries the
    Postgres-marked test that can.

    The ``index_version`` tiebreak is a **string** sort, so ``v2`` outranks
    ``v10``. It never decides anything today: a tie needs two rows with identical
    ``promoted_at``, and both writers of ``status = active``
    (``promote_version`` and the seed's ``_promote``) stamp ``promoted_at`` in
    the same statement, each from its own ``datetime.now(UTC)``.
    """
    return (
        select(*_INDEX_COLUMNS, IndexVersion.status)
        .where(IndexVersion.status.in_(statuses))
        .order_by(_ACTIVE_FIRST, *_ORDER_BY)
        .limit(_MAX_INDEX_ROWS)
    )


async def _fetch(session: AsyncSession, *statuses: IndexStatus) -> Sequence[Mapping[Any, Any]]:
    """Run the single ordered query and hand the rows to the pure partitioner."""
    return list((await session.execute(_ordered_rows_query(*statuses))).mappings().all())


async def resolve_active_index(session: AsyncSession) -> ActiveIndexResolution:
    """Resolve the active index into exactly one of three states.

    **ONE statement**, so one snapshot. This used to be a ``COUNT`` followed by
    an ordered ``LIMIT 1``, which is the shape both read-path resolvers had
    before #264 — and that pair is not atomic: under READ COMMITTED each
    statement takes its own snapshot, so a promote committing in between could
    return ``state=one`` for a database that already had two active rows. That
    window was carried over unchanged by the #264 refactor and is now closed,
    because the count and the winner come from the same rows.

    The cost of closing it is that ``active_count`` is now ``len(rows)`` rather
    than a ``COUNT(*)``, i.e. capped at :data:`_MAX_INDEX_ROWS`. Both WARN
    payloads still carry a real number, and above the cap it would be a floor on
    a database that is already ``ambiguous``.
    """
    return partition_index_rows(await _fetch(session, IndexStatus.active)).active


async def resolve_index_partition(session: AsyncSession) -> IndexPartition:
    """Both rows ``GET /health/index`` reports, from ONE statement (#351).

    The route asks two questions — which index is active, and which is the
    rollback target — and answering them with separate statements let a promote
    land in between and put the same index in both answers. One ordered
    ``status IN (active, previous_good)`` query plus the pure partitioner
    answers both from a single snapshot.
    """
    return partition_index_rows(
        await _fetch(session, IndexStatus.active, IndexStatus.previous_good)
    )


__all__ = [
    "ActiveIndexResolution",
    "ActiveIndexState",
    "IndexPartition",
    "ResolvedIndex",
    "partition_index_rows",
    "resolve_active_index",
    "resolve_index_partition",
]
