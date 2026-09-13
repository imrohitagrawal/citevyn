"""Keyword retrieval.

Placeholder for Postgres full-text search (Phase 2 ingestion) using
SQL ``LIKE`` on ``chunks.chunk_text``. Good enough for the Slice 3/4
hermetic test suite and the fixture catalog; will be replaced by a
GIN-indexed ``tsvector`` query in the next iteration.

Returns at most ``limit`` chunks ordered by ``(chunk_order, document_id,
chunk_id)`` — a TOTAL order, because ``chunk_order`` restarts at 0 in every
document and the row cap must not cut a tie the planner broke differently on
each run. Score is a flat ``0.5`` — the retriever's job is recall, not
precision; the hybrid combiner and reranker do the scoring work.

The ``limit`` is a cap on the rows RETURNED, never on the evidence the
relevance floor is judged from: see ``retrieve`` (#370).
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, DocumentStatus
from app.retrieval.types import RetrievedChunk

_STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "is", "are", "do", "does", "how", "what", "which"}
)


class KeywordRetriever:
    def __init__(
        self,
        session: AsyncSession,
        *,
        active_index_version: str | None = None,
    ) -> None:
        self._session = session
        self._active_index_version = active_index_version

    async def retrieve(
        self,
        question: str,
        *,
        product_area: str | None = None,
        limit: int = 10,
    ) -> list[RetrievedChunk]:
        # Normalize (strip trailing punctuation) BEFORE the stopword/alnum
        # filter — otherwise a punctuated stopword ("what?") escapes the
        # stopword set, then strips back to a bare stopword ("what"), which
        # would inject a broad ``ILIKE '%what%'`` clause and count toward the
        # relevance floor below.
        tokens = [
            stripped
            for stripped in (tok.rstrip("?!.,;:") for tok in question.lower().split())
            if stripped and stripped not in _STOPWORDS and any(c.isalnum() for c in stripped)
        ]
        if not tokens:
            return []

        # ONE definition of "which chunks this search may see", shared by the
        # row query and the uncapped floor check below — so the floor can never
        # be measured over a wider corpus than the rows are drawn from.
        scope: list[ColumnElement[bool]] = [Document.status == DocumentStatus.active]
        if self._active_index_version is not None:
            scope.append(Document.index_version == self._active_index_version)
        if product_area is not None:
            scope.append(Chunk.product_area == product_area)

        like_clauses = [Chunk.chunk_text.ilike(f"%{tok}%") for tok in tokens]
        stmt = (
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.document_id)
            .where(*scope)
            .where(or_(*like_clauses))
            # ``chunk_order`` restarts at 0 in every document, so a product area
            # spanning several documents has a tie at every rank and the row cap's
            # cut point was left to the planner — the same query against the same
            # corpus could return different rows run to run. ``document_id`` then
            # ``chunk_id`` makes the sort total, so the cap is deterministic. No
            # new index is needed: the sort already happens.
            .order_by(Chunk.chunk_order.asc(), Chunk.document_id.asc(), Chunk.chunk_id.asc())
            .limit(limit)
        )

        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return []

        # Require the relevant number of *distinct* query tokens to match:
        # single-token queries ("model", "gemini") must match at least
        # 1 token; multi-token queries need at least 2. Dedupe first so a
        # repeated content word ("api api") cannot satisfy the >=2 floor on
        # a single distinct token. This prevents a single noisy token (e.g.,
        # "gemini") from dominating a 4-token question about API keys, while
        # keeping single-token search useful.
        distinct_tokens = set(tokens)
        required_matches = max(1, min(2, len(distinct_tokens)))
        matched_token_count = sum(
            1
            for tok in distinct_tokens
            if any(tok in (chunk.chunk_text or "").lower() for chunk, _ in rows)
        )
        if matched_token_count < required_matches and len(rows) >= limit:
            # #370: ``rows`` is a WINDOW, not the match set. The cap is applied in
            # SQL, so when it binds the floor above asks "how many query tokens
            # appear in the rows that SURVIVED the cap" instead of "…that matched
            # the query". A discriminating token sitting in a chunk that sorts past
            # the cap made the floor see one token, demand two, and discard every
            # row the query had returned — a STEP to zero hits, not a ramp. That
            # also removed the keyword arm as the designed fallback for a degraded
            # vector arm (see ``test_hybrid_retrieve_answers_from_keyword_on_mismatch``),
            # turning recall loss into total no-evidence during an embedder
            # mismatch or a provider outage.
            #
            # Re-measure over the FULL scoped corpus — but as an AGGREGATE, so the
            # cost the cap was buying is kept: the answer comes back as one row of
            # per-token 0/1 flags instead of an unbounded stream of chunk bodies.
            # The database-side work is not new either — ``chunk_text`` carries no
            # index a leading-wildcard ILIKE could use, so the row query already
            # scans exactly these rows. And this runs ONLY when the cap actually
            # bound AND the windowed floor already failed, i.e. only on the path
            # that was about to return nothing.
            matched_token_count = await self._tokens_present_in_scope(distinct_tokens, scope)

        if matched_token_count < required_matches:
            return []

        return [
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
                score=0.5,
                # The document's owning index (#352). ``doc`` is already selected,
                # so this is free; it lets the hybrid layer see whether the merged
                # evidence spans more than one active index.
                index_version=doc.index_version,
            )
            for chunk, doc in rows
        ]

    async def _tokens_present_in_scope(
        self,
        tokens: set[str],
        scope: list[ColumnElement[bool]],
    ) -> int:
        """How many of ``tokens`` occur in ANY chunk the caller's ``scope`` admits.

        One query, one row back: a ``max(case(...))`` presence flag per token. It
        takes the caller's ``scope`` list verbatim rather than rebuilding the
        filters, so the rescue can never be measured over documents the row query
        is not allowed to return (another product area, a non-active index, a
        deprecated document).
        """
        ordered = sorted(tokens)
        stmt = (
            select(
                *[
                    func.max(case((Chunk.chunk_text.ilike(f"%{tok}%"), 1), else_=0))
                    for tok in ordered
                ]
            )
            .select_from(Chunk)
            .join(Document, Chunk.document_id == Document.document_id)
            .where(*scope)
        )
        # An aggregate with no GROUP BY always yields exactly one row — all-NULL
        # when the scope admits nothing.
        row = (await self._session.execute(stmt)).one()
        return sum(1 for present in row if present)
