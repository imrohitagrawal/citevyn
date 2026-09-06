"""Search HTTP routes (Slice 8 step 3).

* ``POST /v1/search/exact`` — exact-term lookup. The intent is
  "did the user paste a known flag, command, config key, model
  name, etc.?" and short-circuit the answer pipeline. Backed
  by :func:`app.services.exact_lookup.exact_lookup`.
* ``GET /health/index`` — moved here from the placeholder
  health module so it lives next to its sibling search route
  and so it can read the real :class:`IndexVersion` rows.

Both endpoints sit behind :func:`require_demo_api_key` for the
search route; the index-health route is unauthenticated so a
load balancer can probe it.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.rate_limit import rate_limited_demo
from app.models.enums import TermType
from app.services.exact_lookup import (
    MAX_RESULTS,
    ExactLookupHit,
    exact_lookup,
)
from app.services.index_health import active_index_vector_health, ambiguous_vector_health
from app.services.index_resolution import (
    ActiveIndexState,
    ResolvedIndex,
    resolve_active_index,
    resolve_previous_good_index,
)

router = APIRouter(tags=["search"])


def _request_id(request: Request) -> str:
    """Return the request id stamped on :class:`Request` by the middleware."""
    return str(request.state.request_id)


# ---------------------------------------------------------------------------
# /v1/search/exact
# ---------------------------------------------------------------------------


class ExactSearchRequest(BaseModel):
    """Body for ``POST /v1/search/exact``.

    ``term`` is the verbatim string the user pasted (e.g.
    ``"--max-tokens"``). ``product_area`` is required so we
    never run an unscoped global lookup — the same flag name
    in two products can mean different things.
    """

    term: str = Field(min_length=1, max_length=512)
    product_area: str = Field(min_length=1, max_length=64)
    term_type: TermType | None = None
    index_version: str = Field(default="active", max_length=64)
    limit: int = Field(default=10, ge=1, le=MAX_RESULTS)


class ExactSearchHit(BaseModel):
    """One hit in the response list."""

    term_id: uuid.UUID
    term_text: str
    term_type: TermType
    product_area: str
    document_id: uuid.UUID
    chunk_id: uuid.UUID
    index_version: str
    score: float


class ExactSearchResponse(BaseModel):
    """Response envelope for :http:post:`/v1/search/exact`."""

    request_id: str
    query: str
    product_area: str
    index_version: str
    total: int
    hits: list[ExactSearchHit]


@router.post("/v1/search/exact", response_model=ExactSearchResponse)
async def search_exact(
    request: Request,
    body: Annotated[ExactSearchRequest, Body()],
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _user_id: Annotated[str, Depends(rate_limited_demo)],
) -> ExactSearchResponse:
    """Return exact-term matches for ``body.term`` in ``body.product_area``.

    The demo path runs as a single :data:`DEMO_USER_ID`; the
    per-user limiter still applies so a flood of exact searches
    doesn't starve the answer endpoint. The ``rate_limited_demo``
    dependency chains :func:`require_demo_api_key` with
    :func:`enforce_rate_limit` so every authenticated route
    shares one enforcement path.
    """
    request_id = _request_id(request)

    hits: list[ExactLookupHit] = await exact_lookup(
        db,
        term=body.term,
        product_area=body.product_area,
        term_type=body.term_type,
        index_version=body.index_version,
        limit=body.limit,
    )

    return ExactSearchResponse(
        request_id=request_id,
        query=body.term,
        product_area=body.product_area,
        index_version=body.index_version,
        total=len(hits),
        hits=[
            ExactSearchHit(
                term_id=uuid.UUID(hit.term_id),
                term_text=hit.term_text,
                term_type=hit.term_type,
                product_area=hit.product_area,
                document_id=uuid.UUID(hit.document_id),
                chunk_id=uuid.UUID(hit.chunk_id),
                index_version=hit.index_version,
                score=hit.score,
            )
            for hit in hits
        ],
    )


# ---------------------------------------------------------------------------
# /health/index
# ---------------------------------------------------------------------------


@router.get("/health/index")
async def health_index(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Report the active and previous-good index versions + vector-arm health.

    Reads the :class:`IndexVersion` table; returns a 200 with
    ``status="pre_index"`` and ``active_index=None`` when no
    index has been promoted yet. The shape matches the
    Slice 1 placeholder so the load balancer / dashboard
    doesn't need a code change.

    The active index also carries a ``vector_arm`` block (Phase 4c): whether its chunks
    are actually embedded and query-compatible, so an operator can SEE a dead/mismatched
    vector arm (the #97 failure) instead of inferring it from a flat eval score. This is
    an ADDITIVE field — the top-level ``status`` keeps its existing "is there an active
    index" meaning (``ready``/``degraded``/``pre_index``) so a dead-embedding index does
    NOT flip the health probe to a draining state (that would risk pulling a serving pod
    over a signal the operator, not the load balancer, should act on). Read
    ``vector_arm.status`` for the vector-arm verdict.

    Both rows are resolved through
    :mod:`app.services.index_resolution`, the same resolver the retrieval
    provenance gate and the orchestrator use, so this route can no longer name a
    different index than the one actually serving (#264). That also gives it a
    sixth ``vector_arm.status``, ``ambiguous``: more than one row is ``active``,
    the read path has failed closed, and there is no single index to report on.
    """
    request_id = _request_id(request)

    # Resolve both rows through the SHARED resolver (#264) so this route names
    # the same active index the retrieval gate and the orchestrator do. It used
    # to run one unordered ``status IN (active, previous_good)`` query and take
    # whichever row came back first, which meant no ordering and — worse — no
    # way to notice a second active row at all.
    resolution = await resolve_active_index(db)
    previous = await resolve_previous_good_index(db)

    # Dual-active (#58/#264). The read path fails closed here — the provenance
    # gate resolves to ``IndexStampStatus.ambiguous`` and the vector arm is
    # switched OFF — so reporting a clean verdict on one arbitrarily-chosen row
    # is the specific lie this route existed to prevent. ``active_index`` is
    # ``null`` rather than the newest row: naming one of the N would restate the
    # coin flip at a second key, where a dashboard would read it as the answer.
    if resolution.state is ActiveIndexState.ambiguous:
        return {
            "request_id": request_id,
            # Still "ready", NOT a draining state: the API does keep answering
            # (retrieval falls back to the status-only filter and the lexical
            # arms still serve), and the docstring's additive contract exists so
            # an operator-fixable data problem cannot pull a serving pod out of
            # rotation. Read ``vector_arm`` for the verdict.
            "status": "ready",
            "active_index": None,
            "previous_good_index": _index_payload(previous) if previous else None,
            "vector_arm": ambiguous_vector_health(settings, active_count=resolution.active_count),
            "message": (
                f"{resolution.active_count} index versions are marked active; "
                "promote one to converge."
            ),
        }

    active = resolution.row

    if active is None and previous is None:
        return {
            "request_id": request_id,
            "status": "pre_index",
            "active_index": None,
            "previous_good_index": None,
            "vector_arm": None,
            "message": "No active index exists yet.",
        }

    vector_arm = (
        await active_index_vector_health(db, active, settings, active_count=resolution.active_count)
        if active is not None
        else None
    )
    return {
        "request_id": request_id,
        "status": "ready" if active is not None else "degraded",
        "active_index": _index_payload(active) if active else None,
        "previous_good_index": _index_payload(previous) if previous else None,
        "vector_arm": vector_arm,
        "message": None,
    }


def _index_payload(row: ResolvedIndex) -> dict[str, Any]:
    """Project a resolved index row into the response shape."""
    return {
        "index_version": row.index_version,
        "source_version_hash": row.source_version_hash,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "promoted_at": row.promoted_at.isoformat() if row.promoted_at else None,
        "evaluation_run_id": str(row.evaluation_run_id) if row.evaluation_run_id else None,
    }


__all__ = ["router", "search_exact", "health_index"]
