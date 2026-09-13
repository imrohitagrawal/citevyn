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

KNOWN RESIDUAL, stated because it is easy to mistake this arm for more than it
is. The floor is a GATE on the whole result, not a ranking. When it is rescued
by a token living past the cap, the rows returned are still the first ``limit``
by ``chunk_order`` — so the chunk that justified the rescue is, by construction,
the one NOT returned. The arm therefore answers "this query is discriminating
enough against this corpus, here is the scoped window" and not "here is the best
evidence". Promoting the discriminating chunk into the window is RANKING — D4
(BM25/tsvector) and D5 (reciprocal-rank fusion) in #370 — and is deliberately
not attempted here. What the fix restores is the arm's existence as the fallback
for a degraded vector arm; the hybrid combiner, the reranker and the LLM
grounding-refusal net remain what decide whether that evidence is good enough.
``test_the_rescued_window_excludes_the_chunk_that_justified_it`` pins this, so
the behaviour is chosen rather than merely un-noticed.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, DocumentStatus
from app.retrieval.types import RetrievedChunk

_STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "is", "are", "do", "does", "how", "what", "which"}
)

# Escape character for the relevance floor's LIKE patterns. Deliberately NOT a
# backslash: a backslash is the default LIKE escape on Postgres but not in the
# SQL standard, so naming a visible, non-backslash character makes the pattern
# mean the same thing on both dialects this suite runs against.
_LIKE_ESCAPE = "/"


def _like_literal(token: str) -> str:
    """``token`` as a LIKE pattern matching it LITERALLY, wildcards included.

    The relevance floor is asked twice — once in Python over the returned window
    (``tok in chunk_text``) and once in SQL over the whole scope — and the two
    answers must agree, or the arm's verdict would depend on which path happened
    to run, i.e. on ``limit``. Python's ``in`` is a literal substring test while
    LIKE's ``%`` and ``_`` are wildcards, so without this the SQL floor is the
    more permissive of the two and a query containing ``_`` (``claude_code`` — a
    real ``product_area`` string users type) is scored differently by each.

    NOTE the row query deliberately does NOT escape: its wildcard behaviour
    predates #370, and changing which rows a search RETURNS is out of scope
    here. The floor may therefore decline to count a token the row query matched
    only through a wildcard — the conservative direction, and exactly what the
    Python half has always done.

    ONE DIALECT EXCEPTION, stated because the equivalence above is otherwise
    unqualified and would be wrong. ``ILIKE`` on aiosqlite is rendered as
    ``lower() LIKE lower()``, and SQLite's ``lower()`` is ASCII-only — so for
    NON-ASCII text ("MODÈLE") the two halves still disagree there, in the
    SQL-stricter direction. Postgres, where ``lower()`` is locale-aware, agrees;
    production runs Postgres and the SQLite URL is set only by the test
    conftest, so this is a property of the hermetic harness, not of the product.
    Escaping the wildcards is what removes the disagreement that DID reach
    production inputs (``claude_code``, ``100%``).

    The escape character is doubled BEFORE the wildcards are prefixed. That
    order matters: prefixing first would insert escape characters that the
    doubling pass would then double, corrupting every pattern containing ``%``
    or ``_``.
    """
    out = token.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
    for wildcard in ("%", "_"):
        out = out.replace(wildcard, _LIKE_ESCAPE + wildcard)
    return f"%{out}%"


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
            # It is a SECOND scan of the same rows, not a free one: ``chunk_text``
            # carries no index a leading-wildcard ILIKE could use, so both queries
            # scan the scoped chunks. What is bounded is the transfer (one row)
            # and the frequency — this runs only when the window came back FULL
            # and the windowed floor had already failed, i.e. only on the path
            # that was about to return nothing.
            #
            # ``len(rows) >= limit`` means "the window is full", which is weaker
            # than "the cap truncated something": a match set of exactly ``limit``
            # rows trips it with nothing lost. That is deliberate and harmless —
            # it can never UNDER-trigger (SQL LIMIT returns ``min(matches, limit)``
            # rows, so a truncated set always fills the window), and when nothing
            # was truncated the aggregate re-derives the same verdict, because
            # ``_like_literal`` gives it the same semantics as the Python check
            # above and a chunk matching no token contributes 0 to every flag.
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

        The row query's ``or_(*like_clauses)`` is deliberately NOT repeated here.
        A chunk matching none of the tokens contributes 0 to every flag, so the
        filter cannot change any ``max`` — it would only couple this query to the
        row query's (unescaped, see :func:`_like_literal`) patterns, which are
        not the same patterns and must not be assumed to be.
        """
        ordered = sorted(tokens)
        stmt = (
            select(
                *[
                    func.max(
                        case(
                            (
                                Chunk.chunk_text.ilike(_like_literal(tok), escape=_LIKE_ESCAPE),
                                1,
                            ),
                            else_=0,
                        )
                    )
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
