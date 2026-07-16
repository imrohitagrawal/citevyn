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

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.embeddings import (
    EmbedderIdentity,
    configured_embedder_identity,
    is_index_embedder_mismatch,
)
from app.models import Chunk, Document, IndexVersion

# Status values, most-severe first. ``dead`` and ``mismatch`` mean the vector arm is
# effectively OFF (no semantic recall); ``partial`` means an ingest is incomplete;
# ``healthy`` means every chunk is embedded in the query-compatible space.
STATUS_DEAD = "dead"
STATUS_MISMATCH = "mismatch"
STATUS_PARTIAL = "partial"
STATUS_HEALTHY = "healthy"
STATUS_EMPTY = "empty"


def derive_vector_arm_status(*, chunks_total: int, chunks_embedded: int, mismatch: bool) -> str:
    """Classify the vector arm from the chunk counts + the embedder-identity match.

    Pure and total (fully unit-testable). Precedence — the operator needs the most
    actionable label:

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
    if chunks_total == 0:
        return STATUS_EMPTY
    if chunks_embedded == 0:
        return STATUS_DEAD
    if mismatch:
        return STATUS_MISMATCH
    if chunks_embedded < chunks_total:
        return STATUS_PARTIAL
    return STATUS_HEALTHY


def _identity_payload(identity: EmbedderIdentity | None) -> dict[str, Any] | None:
    """Project an embedder identity to a JSON dict (provider/model/dim — never a key)."""
    if identity is None:
        return None
    return {"provider": identity.provider, "model": identity.model, "dim": identity.dim}


async def active_index_vector_health(
    db: AsyncSession, active_index: IndexVersion, settings: Settings
) -> dict[str, Any]:
    """Compute the vector-arm health block for ``active_index``.

    Counts the active index's chunks (joined via ``Document.index_version``) and how
    many carry a non-NULL embedding, compares the index's stamped embedder identity to
    the configured query embedder, and derives a status. Returns a JSON-friendly dict;
    exposes only ``provider/model/dim`` and counts — no secret, no vector data.
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
    }


__all__ = [
    "STATUS_DEAD",
    "STATUS_EMPTY",
    "STATUS_HEALTHY",
    "STATUS_MISMATCH",
    "STATUS_PARTIAL",
    "active_index_vector_health",
    "derive_vector_arm_status",
]
