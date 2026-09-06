"""The single source of truth for "which :class:`IndexVersion` is active?" (#264).

Three call sites need this answer and, before this module, each computed it
itself:

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

Deliberately **not** in this module:

* **Logging.** Each caller emits its own event name
  (``retrieval_multiple_active_indexes`` on ``citevyn.retrieval`` /
  ``orchestrator_multiple_active_indexes`` on ``citevyn.answer``) and both names
  AND both logger names are asserted by tests. Worse, two of those assertions are
  negative (``assert not any(...)``), so a WARN moved in here would make them
  pass *vacuously* — green for the wrong reason — while only the positive ones
  went red. The resolver stays silent; the callers log.
* **A WARN on the health route.** That route is polled by the load balancer, by
  ``.github/workflows/uptime.yml`` on a schedule and by
  ``infra/docker/scripts/deploy_verify.sh``. Logging per probe would flood; the
  read path already records the condition once per request.
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
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import func, select
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
    row: ResolvedIndex | None = None


def _newest_first(status: IndexStatus):
    """``status``-filtered query for the single newest row, deterministically ordered.

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

    On the ``active`` path the ordering is defensive and **unreachable by
    construction**: :func:`resolve_active_index` returns ``ambiguous`` before it
    runs whenever more than one row qualifies, so there is never more than one
    row left to order. It is kept because deleting it would leave the three
    callers' SQL non-identical again, which is the divergence #264 is about — not
    because any test can observe it.

    ``NULLS LAST`` is load-bearing on Postgres, which orders NULLs *first* under
    ``DESC`` by default: a never-promoted row must not outrank a real one.
    """
    return (
        select(*_INDEX_COLUMNS)
        .where(IndexVersion.status == status)
        .order_by(
            IndexVersion.promoted_at.desc().nulls_last(),
            IndexVersion.index_version.desc(),
        )
        .limit(1)
    )


async def _newest(session: AsyncSession, status: IndexStatus) -> ResolvedIndex | None:
    row = (await session.execute(_newest_first(status))).first()
    if row is None:
        return None
    return ResolvedIndex(*row)


async def count_active_indexes(session: AsyncSession) -> int:
    """How many rows currently carry ``status = active``."""
    stmt = select(func.count(IndexVersion.index_version)).where(
        IndexVersion.status == IndexStatus.active
    )
    return int((await session.execute(stmt)).scalar_one())


async def resolve_active_index(session: AsyncSession) -> ActiveIndexResolution:
    """Resolve the active index into exactly one of three states.

    Two queries, matching what the retrieval gate and the orchestrator already
    ran: a ``COUNT`` (whose exact value both of their WARN payloads carry) and a
    deterministically-ordered ``LIMIT 1``. Collapsing them into a single
    ``LIMIT 2`` would save a roundtrip but reduce ``active_count`` to "1 or more
    than 1", losing the number those payloads report.
    """
    active_count = await count_active_indexes(session)
    if active_count > 1:
        return ActiveIndexResolution(
            state=ActiveIndexState.ambiguous, active_count=active_count, row=None
        )
    row = await _newest(session, IndexStatus.active)
    if row is None:
        return ActiveIndexResolution(state=ActiveIndexState.none, active_count=0, row=None)
    return ActiveIndexResolution(state=ActiveIndexState.one, active_count=1, row=row)


async def resolve_previous_good_index(session: AsyncSession) -> ResolvedIndex | None:
    """The current rollback target: the most recently demoted ``previous_good`` row.

    No count check, unlike the active path: more than one ``previous_good`` row
    is *normal* (see :func:`_newest_first`), not drift, so ">1" needs a
    deterministic winner rather than an ``ambiguous`` state a caller would have
    to handle. ``docs/DEPLOY_FLY.md`` §4.4 makes ``GET /health/index`` the
    post-deploy check and §6 item 4 makes the previous-good index the rollback
    target, so naming an arbitrary one of those rows points an incident at the
    wrong index.
    """
    return await _newest(session, IndexStatus.previous_good)


__all__ = [
    "ActiveIndexResolution",
    "ActiveIndexState",
    "ResolvedIndex",
    "count_active_indexes",
    "resolve_active_index",
    "resolve_previous_good_index",
]
