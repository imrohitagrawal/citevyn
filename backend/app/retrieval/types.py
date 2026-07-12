"""Shared types for the retrieval layer.

``RetrievedChunk`` is the value object that flows from any retriever
into the answer engine. ``EvidenceHit`` adds the metadata (rank,
retrieval type) the engine needs to persist the trace.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.models.enums import RetrievalType


class RetrievedChunk(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    product_area: str
    source_name: str
    document_title: str
    section_path: str
    heading: str
    parent_heading: str | None = None
    chunk_text: str
    context_summary: str
    source_url: str
    score: float = 0.0


class EvidenceHit(RetrievedChunk):
    retrieval_type: RetrievalType = RetrievalType.hybrid
    rank: int = 0


class VectorDegrade(enum.Enum):
    """Why (if at all) the vector retrieval arm degraded to no hits at runtime.

    Reported at the degrade site so the answer-cache gate and its skip-WARN read
    the *actual* reason, never a config re-prediction:

    * ``none`` — the vector arm ran normally (even to a genuine empty result), OR
      it was never consulted (the ``exact_lookup`` short-circuit, #72). Cacheable.
    * ``mismatch`` — the active index was stamped by a different embedder than the
      one configured to embed queries (Tier-3, #57): the arm was disabled.
    * ``unavailable`` — the embedding provider was transiently down (Tier-1,
      ``EmbedderUnavailable``, #70).
    """

    none = "none"
    mismatch = "mismatch"
    unavailable = "unavailable"


# ``RetrievalResult`` is a stdlib dataclass rather than a pydantic ``BaseModel``
# like its ``hits`` elements: it is an internal return DTO that never crosses a
# validation or serialization boundary, so pydantic's overhead buys nothing here.
# ``frozen=True`` marks it read-only; ``eq=False`` avoids generating an ``__eq__``
# /``__hash__`` pair that would choke on the mutable ``hits`` list if ever hashed.
@dataclass(frozen=True, eq=False)
class RetrievalResult:
    """What a retriever hands back to the orchestrator: the ranked ``hits`` plus
    the runtime :class:`VectorDegrade` reason. The orchestrator gates the
    answer-cache write on ``vector_degraded`` (any non-``none`` reason) and labels
    the skip WARN from the reason itself (#70/#72), rather than predicting the
    degrade from config, which mis-gates in both directions."""

    hits: list[EvidenceHit]
    vector_degrade: VectorDegrade

    @property
    def vector_degraded(self) -> bool:
        return self.vector_degrade is not VectorDegrade.none


def chunk_to_citation(chunk: RetrievedChunk) -> dict[str, Any]:
    """Project a chunk into the response ``Citation`` shape."""
    return {
        "source_name": chunk.source_name,
        "title": chunk.document_title,
        "url": chunk.source_url,
        "chunk_id": str(chunk.chunk_id),
    }
