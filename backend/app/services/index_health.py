"""Vector-arm health for the active index (Phase 4c — operator signal).

Surfaces the exact failure mode this RAG effort exists to prevent (#97): a promoted
index whose chunks have **NULL embeddings**, so the semantic/vector arm silently
returns nothing and the system quietly under-answers. It also surfaces the Tier-3
embedder **mismatch** (the configured query embedder disagrees with the index's stamp,
so cosine distance is meaningless and the read path degrades the vector arm, #57).

The signal is read-only and cheap: two COUNT queries over the active index's chunks
plus the stamp/config comparison already used by the retriever's degrade gate
(:func:`app.embeddings.is_index_embedder_mismatch`). It is projected into
``GET /health/index`` so an operator (or the load balancer / dashboard) can see, at a
glance, whether the vector arm is actually live — instead of discovering it only from
a flat eval score.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.embeddings import (
    EmbedderIdentity,
    IndexStampStatus,
    configured_embedder_identity,
    is_index_embedder_mismatch,
)
from app.models import Chunk, Document

# Status values, most-severe first. ``dead`` and ``mismatch`` mean the vector arm is
# effectively OFF (no semantic recall); ``partial`` means an ingest is incomplete;
# ``healthy`` means every chunk is embedded in the query-compatible space.
STATUS_DEAD = "dead"
STATUS_MISMATCH = "mismatch"
STATUS_PARTIAL = "partial"
STATUS_HEALTHY = "healthy"
STATUS_EMPTY = "empty"
STATUS_AMBIGUOUS = "ambiguous"


def derive_vector_arm_status(
    *, chunks_total: int, chunks_embedded: int, mismatch: bool, ambiguous: bool = False
) -> str:
    """Classify the vector arm from the chunk counts + the embedder-identity match.

    Pure and total (fully unit-testable). Precedence — the operator needs the most
    actionable label:

    * ``ambiguous`` — MORE THAN ONE row is ``active`` (#58/#264), so there is no
      "the active index" to report on. Checked FIRST because every other input is
      unknowable in this state: the chunk counts belong to whichever row was
      picked, and the stamp comparison is against whichever row was picked. The
      read path already fails closed here (``is_index_embedder_mismatch`` returns
      ``True`` for :attr:`~app.embeddings.IndexStampStatus.ambiguous`, #226), so a
      ``healthy`` verdict from this function would contradict live behaviour —
      which is exactly the lie #264 was filed for.
    * ``empty`` — the active index has no chunks yet (nothing to embed).
    * ``dead`` — chunks exist but NONE are embedded (the #97 failure: the arm returns
      nothing). Checked before ``mismatch`` because a dead arm is dead regardless of
      whose stamp it carries.
    * ``mismatch`` — chunks are embedded, but the configured query embedder disagrees
      with the index stamp, so the read path degrades the arm to a Tier-3 mismatch (#57).
    * ``partial`` — some but not all chunks are embedded (an ingest in progress / a
      backfill that stopped short).
    * ``healthy`` — every chunk is embedded in the query-compatible space.
    """
    if ambiguous:
        return STATUS_AMBIGUOUS
    if chunks_total == 0:
        return STATUS_EMPTY
    if chunks_embedded == 0:
        return STATUS_DEAD
    if mismatch:
        return STATUS_MISMATCH
    if chunks_embedded < chunks_total:
        return STATUS_PARTIAL
    return STATUS_HEALTHY


class StampedIndex(Protocol):
    """The four fields this module needs off an index row.

    Structural, so both the ORM :class:`~app.models.IndexVersion` and the
    :class:`~app.services.index_resolution.ResolvedIndex` column projection the
    shared resolver returns (#264) satisfy it without a conversion shim. Nothing
    here is a relationship, so a projection can serve as well as an entity.
    """

    @property
    def index_version(self) -> str: ...

    @property
    def embedding_provider(self) -> str | None: ...

    @property
    def embedding_model(self) -> str | None: ...

    @property
    def embedding_dim(self) -> int | None: ...


def _identity_payload(identity: EmbedderIdentity | None) -> dict[str, Any] | None:
    """Project an embedder identity to a JSON dict (provider/model/dim — never a key)."""
    if identity is None:
        return None
    return {"provider": identity.provider, "model": identity.model, "dim": identity.dim}


def ambiguous_vector_health(settings: Settings, *, active_count: int) -> dict[str, Any]:
    """The ``vector_arm`` block when the active index cannot be identified (#58/#264).

    Not routed through :func:`active_index_vector_health`, because every number
    that function returns is a MEASUREMENT of one index and here there is no one
    index to measure: counting chunks would mean first picking one of the
    ``active_count`` rows, which is the defect. The counts are therefore ``None``
    — "not measured" — rather than ``0``, which would read as ``empty``/``dead``
    and claim something nobody checked.

    ``embedder_match`` is not hard-coded: it is
    :func:`app.embeddings.is_index_embedder_mismatch` applied to the very
    sentinel the read path resolves to in this state, so this block reports the
    same verdict the vector arm acts on (#71 — a second implementation of that
    comparison is a bug). It comes out ``False``, i.e. the arm is OFF, which is
    exactly what ``HybridRetriever._vector_arm_enabled`` does here (#226).

    ``configured_query_embedder`` survives because it is read from ``Settings``,
    not from the database, so it is the one half of the comparison that stays
    knowable.
    """
    configured = configured_embedder_identity(settings)
    mismatch = is_index_embedder_mismatch(configured, IndexStampStatus.ambiguous)
    # Through the pure classifier, NOT a literal ``STATUS_AMBIGUOUS``: the
    # precedence table there is the single place that decides what a vector arm
    # is called, and a literal here would leave its ``ambiguous`` branch
    # unreachable from production — the branch's own unit tests would then pass
    # while proving nothing about what this route emits.
    status = derive_vector_arm_status(
        chunks_total=0, chunks_embedded=0, mismatch=mismatch, ambiguous=True
    )
    return {
        "status": status,
        "healthy": status == STATUS_HEALTHY,
        "chunks_total": None,
        "chunks_embedded": None,
        "embedded_ratio": None,
        "embedder_match": not mismatch,
        "index_embedder": None,
        "configured_query_embedder": _identity_payload(configured),
        "active_index_count": active_count,
    }


async def active_index_vector_health(
    db: AsyncSession,
    active_index: StampedIndex,
    settings: Settings,
    *,
    active_count: int = 1,
) -> dict[str, Any]:
    """Compute the vector-arm health block for ``active_index``.

    Counts the active index's chunks (joined via ``Document.index_version``) and how
    many carry a non-NULL embedding, compares the index's stamped embedder identity to
    the configured query embedder, and derives a status. Returns a JSON-friendly dict;
    exposes only ``provider/model/dim`` and counts — no secret, no vector data.

    ``active_count`` is reported verbatim as ``active_index_count`` so the field
    is present in every ``vector_arm`` block rather than only the ambiguous one,
    and a consumer never has to infer "how many active rows?" from the absence
    of a key. It defaults to ``1`` because this function is only reachable once
    :func:`app.services.index_resolution.resolve_active_index` has resolved to
    exactly one row — anything else routes to
    :func:`ambiguous_vector_health` instead.
    """
    index_stamp = EmbedderIdentity(
        provider=active_index.embedding_provider,
        model=active_index.embedding_model,
        dim=active_index.embedding_dim,
    )
    configured = configured_embedder_identity(settings)
    mismatch = is_index_embedder_mismatch(configured, index_stamp)

    base = (
        select(func.count())
        .select_from(Chunk)
        .join(Document, Chunk.document_id == Document.document_id)
        .where(Document.index_version == active_index.index_version)
    )
    chunks_total = int((await db.execute(base)).scalar_one())
    chunks_embedded = int((await db.execute(base.where(Chunk.embedding.is_not(None)))).scalar_one())

    status = derive_vector_arm_status(
        chunks_total=chunks_total, chunks_embedded=chunks_embedded, mismatch=mismatch
    )
    return {
        "status": status,
        "healthy": status == STATUS_HEALTHY,
        "chunks_total": chunks_total,
        "chunks_embedded": chunks_embedded,
        "embedded_ratio": (chunks_embedded / chunks_total) if chunks_total else 0.0,
        "embedder_match": not mismatch,
        "index_embedder": _identity_payload(
            index_stamp if index_stamp.provider is not None else None
        ),
        "configured_query_embedder": _identity_payload(configured),
        "active_index_count": active_count,
    }


__all__ = [
    "STATUS_AMBIGUOUS",
    "STATUS_DEAD",
    "STATUS_EMPTY",
    "STATUS_HEALTHY",
    "STATUS_MISMATCH",
    "STATUS_PARTIAL",
    "active_index_vector_health",
    "ambiguous_vector_health",
    "derive_vector_arm_status",
]
