"""Vector retrieval (pgvector).

On Postgres, performs a cosine-distance query against the ``embedding`` pgvector
column (migration ``0004``) using the ``<=>`` operator and the HNSW index. On
SQLite (the hermetic test engine), pgvector does not exist, so the retriever
returns ``[]`` — the rest of the pipeline (exact + keyword) still runs.

The :class:`Embedder` seam, :class:`StubEmbedder`, and :func:`build_embedder` now
live in :mod:`app.embeddings` (one provider seam shared by the write and read
paths). They are re-exported here for backward compatibility.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import Embedder, StubEmbedder, build_embedder
from app.models import Chunk, Document, DocumentStatus
from app.retrieval.confidence import is_confident_global_result
from app.retrieval.types import RetrievedChunk

__all__ = ["Embedder", "StubEmbedder", "VectorRetriever", "build_embedder"]


class VectorRetriever:
    def __init__(
        self,
        session: AsyncSession,
        *,
        active_index_version: str | None = None,
        embedder: Embedder | None = None,
        global_confidence: tuple[float, float] | None = None,
    ) -> None:
        self._session = session
        self._active_index_version = active_index_version
        self._embedder = embedder
        # ``(min_top_score, min_margin)`` for the GLOBAL confidence gate (Phase 2).
        # Applied only when ``product_area is None`` (a global "answer when grounded"
        # search): an off-corpus query's nearest chunks are dropped so the arm
        # contributes nothing rather than a spurious hit. ``None`` = no gate (the
        # in-domain path and legacy callers are unaffected).
        self._global_confidence = global_confidence

    async def retrieve(
        self,
        question: str,
        *,
        product_area: str | None = None,
        limit: int = 10,
    ) -> list[RetrievedChunk]:
        # Vector search is only meaningful when an embedder is wired AND the
        # database supports pgvector. The hermetic test engine is SQLite (no
        # pgvector); the production engine is Postgres. Detect the dialect at
        # runtime and bail out cleanly on SQLite so the rest of the pipeline can
        # still run. The dialect guard runs BEFORE the ``<=>`` query is built, so
        # the pgvector operator is never emitted against a non-pgvector backend.
        if self._embedder is None:
            return []
        if not self._session.bind or self._session.bind.dialect.name != "postgresql":
            return []

        embedding = await self._embedder.embed(question)
        # Compute the cosine distance in the database and select it as a column
        # so results carry a real score and are ordered by the HNSW index.
        distance = Chunk.embedding.cosine_distance(embedding).label("distance")  # type: ignore[attr-defined]
        stmt = (
            select(Chunk, Document, distance)
            .join(Document, Chunk.document_id == Document.document_id)
            .where(Document.status == DocumentStatus.active)
            .where(Chunk.embedding.is_not(None))  # type: ignore[attr-defined]
            .order_by(distance)
            .limit(limit)
        )
        if self._active_index_version is not None:
            stmt = stmt.where(Document.index_version == self._active_index_version)
        if product_area is not None:
            stmt = stmt.where(Chunk.product_area == product_area)

        rows = (await self._session.execute(stmt)).all()

        # Global "answer when grounded" confidence gate (Phase 2): on an unscoped
        # search (``product_area is None``), trust the vector result only when its
        # best hit clearly stands out — otherwise an off-corpus question would
        # surface a spurious nearest chunk. Rows are ordered by distance, so the
        # scores are already descending. Skipped entirely for in-domain retrieval.
        if product_area is None and self._global_confidence is not None:
            scores = [max(0.0, 1.0 - float(dist)) for _c, _d, dist in rows]
            min_top_score, min_margin = self._global_confidence
            if not is_confident_global_result(
                scores, min_top_score=min_top_score, min_margin=min_margin
            ):
                return []

        results: list[RetrievedChunk] = []
        for chunk, doc, dist in rows:
            # Cosine distance is in [0, 2]; convert to a [0, 1]-ish similarity.
            score = max(0.0, 1.0 - float(dist))
            results.append(
                RetrievedChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    product_area=chunk.product_area,
                    source_name=doc.source_name,
                    document_title=doc.title,
                    section_path=chunk.section_path,
                    heading=chunk.heading,
                    parent_heading=chunk.parent_heading,
                    chunk_text=chunk.chunk_text,
                    context_summary=chunk.context_summary,
                    source_url=doc.source_url,
                    score=score,
                )
            )
        return results
