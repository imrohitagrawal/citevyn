"""Exact-term lookup service.

Backs the public :http:post:`/v1/search/exact` route. The intent is
"did the user paste a known flag, command, config key, or model
name?" — if so, return the canonical chunk(s) that contain it,
without paying for a vector roundtrip. The service is a thin
SQLAlchemy query helper; the policy (rate limiting, audit, response
shape) lives in the route.

Design choices
--------------

- **Product area is mandatory** — the same flag name in
  ``claude-code`` vs ``anthropic-sdk-python`` is a different answer.
  We refuse the request rather than silently fall back to a
  global search, because global would mix products.
- **Index-version filtering is optional but recommended** — the
  caller can pin to ``active`` (default) or pass a specific
  ``index_version`` for replay / debugging.
- **Result cap is server-controlled** — the service clamps to
  :data:`MAX_RESULTS` regardless of what the caller asks for,
  to bound the response size.
- **No fuzzy match** — exact-lookup is binary: term is in
  ``exact_terms`` or it is not. Spelling variants belong in the
  hybrid-search path (slice 8 step 4+) or a future
  ``/v1/search/suggest`` route.

The module is dependency-injectable: :func:`exact_lookup` takes an
:class:`~sqlalchemy.ext.asyncio.AsyncSession` and returns a list
of plain dataclasses that the route layer can serialise.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.middleware import get_current_request_id
from app.models.documents import Document
from app.models.enums import IndexStatus, TermType
from app.models.exact_terms import ExactTerm
from app.models.index_versions import IndexVersion

_logger = logging.getLogger("citevyn.retrieval")

#: Hard cap on the number of hits returned in one response.
#: Bigger requests are clamped to this value; the route layer
#: also enforces it before the service is called.
MAX_RESULTS = 25


@dataclass(frozen=True)
class ExactLookupHit:
    """One match returned by :func:`exact_lookup`.

    Plain dataclass (not a Pydantic model) because the service
    is a transport-agnostic helper — the route layer wraps it
    in a response model.
    """

    term_id: str
    term_text: str
    term_type: TermType
    product_area: str
    document_id: str
    chunk_id: str
    index_version: str
    score: float  # always 1.0 for exact lookups; kept for shape parity with hybrid search


async def exact_lookup(
    session: AsyncSession,
    *,
    term: str,
    product_area: str,
    term_type: TermType | None = None,
    index_version: str = "active",
    limit: int = MAX_RESULTS,
) -> list[ExactLookupHit]:
    """Return exact-term matches for ``term`` in ``product_area``.

    Parameters
    ----------
    session
        Open async session. The function does not commit — the
        caller controls the transaction boundary.
    term
        The exact string the user pasted. The comparison is
        case-sensitive (canonical flag / config names are
        case-sensitive by convention).
    product_area
        Filter on the ``exact_terms.product_area`` column.
        Required because the same term name can mean different
        things in different products.
    term_type
        Optional filter on the term type (e.g. only ``flag``
        hits). When ``None`` all term types are returned.
    index_version
        ``"active"`` (default) restricts to the currently
        promoted index; any other string is treated as a
        specific :class:`IndexVersion` key.
    limit
        Maximum number of hits to return. Clamped to
        :data:`MAX_RESULTS`.

    Returns
    -------
    list[ExactLookupHit]
        Empty list when no match exists. Never ``None``.
    """
    if not term:
        # Defensive guard — the route layer already validates,
        # but a refactor could forget. Return empty rather than
        # raise so the caller doesn't have to wrap.
        return []
    if not product_area:
        # Same: product area is mandatory; empty means "global"
        # which we don't support.
        return []

    capped_limit = max(1, min(limit, MAX_RESULTS))

    # Build the base query. We join to documents so we can filter
    # on the index version attached to the source document; the
    # join is on the FK the model already declares.
    # ``Document.index_version`` rides along in the projection so the union below
    # is observable (#352). Both branches join ``documents`` on the same condition
    # and always did, so the join is hoisted out of the branch rather than written
    # twice — the emitted SQL is unchanged apart from the one extra column, and
    # selecting the column is what makes the hoist load-bearing rather than
    # cosmetic. The relationship is ``lazy="raise"``, so the join has to be
    # explicit; explicit also keeps the SQL predictable.
    # ``.label(...)`` so the WARN below reads ``row.index_version`` by NAME. The
    # hits loop beneath uses positional ``row[1]``/``row[2]``, which is this file's
    # existing idiom, but a positional read of a column ADDED to the end is the
    # kind that silently retargets when someone inserts a column ahead of it, with
    # no test failing.
    stmt = (
        select(
            ExactTerm,
            ExactTerm.document_id,
            ExactTerm.chunk_id,
            Document.index_version.label("index_version"),
        )
        .join(Document, ExactTerm.document_id == Document.document_id)
        .where(ExactTerm.term_text == term)
        .where(ExactTerm.product_area == product_area)
    )

    if term_type is not None:
        stmt = stmt.where(ExactTerm.term_type == term_type)

    # Filter on the active index, unless the caller pinned a
    # specific one (e.g. for replay). The "active" string is
    # the sentinel — a real index_version is just a string PK.
    if index_version == "active":
        stmt = stmt.where(
            Document.index_version.in_(
                select(IndexVersion.index_version).where(IndexVersion.status == IndexStatus.active)
            )
        )
    else:
        stmt = stmt.where(Document.index_version == index_version)

    stmt = stmt.limit(capped_limit)

    rows = (await session.execute(stmt)).all()

    # ``index_version="active"`` is SET MEMBERSHIP, not a single-row pick: on a
    # database where more than one ``IndexVersion`` claims ``active`` this route
    # silently unions their documents, at the exact moment the answer pipeline's
    # provenance gate has decided it cannot tell which index owns the vectors
    # (#352). It is left unioning on purpose — this route has no answer cache to
    # poison, and changing what it returns is the same CLASS of owner decision as
    # #265 (which is scoped to ZERO active rows, a different state) — but
    # it no longer does so silently.
    #
    # STATED LIMIT: this sees only the rows that survived ``limit(capped_limit)``,
    # which carries no ``ORDER BY``, so a truncated result can hide the second
    # index and under-report. It can therefore prove a union happened and can
    # never prove one did not.
    _warn_if_hits_span_indexes(rows)

    hits: list[ExactLookupHit] = []
    for row in rows:
        # ``row`` is a Row; first element is the ExactTerm ORM
        # instance, the rest are the selected columns.
        term_row: ExactTerm = row[0]
        hits.append(
            ExactLookupHit(
                term_id=str(term_row.term_id),
                term_text=term_row.term_text,
                term_type=term_row.term_type,
                product_area=term_row.product_area,
                document_id=str(row[1]),
                chunk_id=str(row[2]),
                index_version=index_version,
                score=1.0,
            )
        )
    return hits


def _warn_if_hits_span_indexes(rows: Sequence[Any]) -> None:
    """WARN when this lookup's rows came from more than one index version (#352).

    Pure over rows already fetched — no extra query.

    Read by NAME (``row.index_version``) rather than by position, so inserting a
    column ahead of it cannot silently retarget this WARN at the wrong value. The
    protection is the **by-name read**, not the ``.label(...)``: a skeptic mutated
    the label away and the whole suite stayed green, because SQLAlchemy already
    keys that column ``index_version`` either way. The label is kept as an explicit
    statement of the name this line depends on, and is honestly inert.

    **Stated, because this file is now half-converted:** the hits loop below still
    reads ``row[1]`` / ``row[2]`` positionally for ``document_id`` and ``chunk_id``
    — the values actually returned to API callers. The column-insertion hazard is
    closed for the log line and still live on those two.

    Note that ``ExactLookupHit.index_version`` echoes back what the CALLER asked
    for (the literal ``"active"`` on the default path), so before this the
    response could not tell an operator which index any hit belonged to, and
    nothing else recorded it. Changing that field to the per-hit truth is a
    response-contract change and is deliberately not done here.

    Payload shaped for ``app.core.logging``'s ``extra=`` allowlist:
    ``index_version_count`` is an ``int`` (printed verbatim under any key) and
    ``index_versions`` is a joined ``str`` under a key on both
    ``EMITTED_TEXT_KEYS`` and ``OPAQUE_ID_KEYS``.
    """
    versions = sorted({r.index_version for r in rows if r.index_version is not None})
    if len(versions) <= 1:
        return
    _logger.warning(
        "exact_lookup_spans_multiple_indexes",
        extra={
            "request_id": get_current_request_id(),
            "index_versions": ",".join(versions),
            "index_version_count": len(versions),
        },
    )


__all__ = ["ExactLookupHit", "exact_lookup", "MAX_RESULTS"]
