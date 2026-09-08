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
    # Which ``IndexVersion`` the owning document belongs to (#352). All three
    # arms already SELECT the ``documents`` row, so this costs no extra query and
    # no extra column — it simply stops the answer path throwing the value away.
    # It is what lets :meth:`app.retrieval.hybrid.HybridRetriever._finalize` say
    # whether an answer actually drew on more than one active index, which is a
    # strictly rarer and strictly more useful signal than "the database is
    # dual-active" (already logged once per request by the orchestrator).
    #
    # OPTIONAL, and that is a measured decision rather than a default. Review
    # showed the gap it leaves: deleting ``index_version=doc.index_version`` from
    # the keyword arm, or from the vector arm, left the ENTIRE suite green, because
    # every hit silently fell back to ``None`` and the span set emptied. Making the
    # field REQUIRED turns both into a loud ``ValidationError`` — so it was tried,
    # and measured: it reddens **135 tests** across the suite, because hand-built
    # hits are the idiom in far more than the seven factories a reviewer counted.
    # That is a disproportionate diff for a test gap, so the gap is closed by two
    # targeted behavioural tests (a keyword-arm and a vector-arm span) instead, and
    # this comment records the tradeoff rather than leaving it to be rediscovered.
    #
    # ``None`` means "this construction site did not know", never "this document
    # has no index" — ``documents.index_version`` is ``nullable=False``, so no real
    # row can produce it. ``_warn_if_evidence_spans_indexes`` skips it rather than
    # counting "unknown" as its own version, which would fire the WARN on a
    # perfectly healthy single-index database.
    #
    # Never projected onto the wire: ``chunk_to_citation`` below,
    # ``app.answer.generate._format_evidence``, ``app.answer.no_answer``'s
    # ``build_suggestions``, and the ``retrieved_evidence`` INSERT in
    # ``Orchestrator._persist_messages`` all name their fields explicitly, so this
    # reaches no response body, no prompt and no table.
    index_version: str | None = None


class EvidenceHit(RetrievedChunk):
    retrieval_type: RetrievalType = RetrievalType.hybrid
    rank: int = 0


class VectorDegrade(enum.StrEnum):
    """Why (if at all) this retrieval result must not be frozen in the answer cache.

    The name says "vector" for its history — every original member described the
    vector arm degrading to no hits. What the value has always actually driven is
    :attr:`RetrievalResult.vector_degrade`, i.e. the cache-write gate below, and
    :attr:`ambiguous_index` is a reason that has nothing to do with the vector arm
    at all. The type is deliberately NOT renamed here; this docstring is the honest
    statement of what the enum means. Renaming it — to something like
    ``CacheBlockReason``, moving ``RetrievalResult.vector_degrade`` with it — is
    where the type wants to end up, and is left to a follow-up: it touches over 150
    references across 14 files for zero behaviour change, and #352 is scoped to the
    gate, so landing it here would bury two real defects in rename noise. The
    misnomer is not dangerous while it lasts, because nothing dispatches on the
    word "vector" and the three set-equality guards over this enum
    (``_DEGRADE_PRECEDENCE``, ``_CACHE_SKIP_EVENTS``, ``_EVAL_FATAL_DEGRADES``)
    stop a future member being added silently.

    Reported at the degrade site so the answer-cache gate and its skip-WARN read
    the *actual* reason, never a config re-prediction:

    * ``none`` — nothing degraded and the active index resolved to exactly one row
      (or the arms were pinned to a named ``index_version``), so the evidence
      cannot span indexes. The vector arm either ran normally (even to a genuine
      empty result) or was never consulted (the ``exact_lookup`` short-circuit,
      #72). Cacheable.
    * ``mismatch`` — the active index was stamped by a different embedder than the
      one configured to embed queries (Tier-3, #57): the arm was disabled.
    * ``unavailable`` — the embedding provider was transiently down (Tier-1,
      ``EmbedderUnavailable``, #70).
    * ``ambiguous_index`` — MORE THAN ONE ``IndexVersion`` row claimed
      ``status=active`` while the arms were unscoped (#58/#264), so every arm
      filtered on document status alone and the evidence may union documents from
      several indexes (#352). The answer is still SERVED — that is the deliberate
      product decision at ``orchestrator._retrieve_active_index`` — but it is not
      frozen, because it would outlive the ``promote_version`` that converges the
      database. Reported instead of ``mismatch`` because the two need different
      operator actions: ``mismatch`` points at the embedder config (ADR-0003),
      this points at ``index_versions``.
    """

    none = "none"
    mismatch = "mismatch"
    unavailable = "unavailable"
    ambiguous_index = "ambiguous_index"


# ``RetrievalResult`` is a stdlib dataclass rather than a pydantic ``BaseModel``
# like its ``hits`` elements: it is an internal return DTO that never crosses a
# validation or serialization boundary, so pydantic's overhead buys nothing here.
# ``frozen=True`` marks it read-only; ``eq=False`` avoids generating an ``__eq__``
# /``__hash__`` pair that would choke on the mutable ``hits`` list if ever hashed.
@dataclass(frozen=True, eq=False)
class RetrievalResult:
    """What a retriever hands back to the orchestrator: the ranked ``hits`` plus
    the runtime :class:`VectorDegrade` reason. The orchestrator skips the
    answer-cache write whenever ``vector_degrade`` is not :attr:`VectorDegrade.none`
    and labels the skip WARN from the reason itself (#70/#72), rather than
    predicting the degrade from config, which mis-gates in both directions."""

    hits: list[EvidenceHit]
    vector_degrade: VectorDegrade


def chunk_to_citation(chunk: RetrievedChunk) -> dict[str, Any]:
    """Project a chunk into the response ``Citation`` shape."""
    return {
        "source_name": chunk.source_name,
        "title": chunk.document_title,
        "url": chunk.source_url,
        "chunk_id": str(chunk.chunk_id),
    }
