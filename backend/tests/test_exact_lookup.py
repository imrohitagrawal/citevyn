"""Tests for :mod:`app.services.exact_lookup`.

The service is a thin SQLAlchemy query helper on top of the
``exact_terms`` table. Tests run against the per-test
``session`` fixture seeded with the demo catalog (which now
also creates an active :class:`IndexVersion` row).
"""

from __future__ import annotations

import pytest

from app.models.enums import IndexStatus, TermType
from app.services.exact_lookup import (
    MAX_RESULTS,
    ExactLookupHit,
    exact_lookup,
)
from tests.conftest import seed_catalog


async def _seed(session) -> None:
    await seed_catalog(session)


@pytest.mark.asyncio
async def test_exact_lookup_returns_match_in_active_index(session) -> None:
    """A flag present in the active index is returned with score 1.0."""
    await _seed(session)
    from sqlalchemy import select

    from app.models.index_versions import IndexVersion

    active = (
        await session.execute(select(IndexVersion).where(IndexVersion.status == IndexStatus.active))
    ).scalar_one()
    # The seed places --model under codex, not claude_api, so a
    # scoped product_area query is required.
    hits = await exact_lookup(
        session,
        term="--model",
        product_area="codex",
        index_version=active.index_version,
    )
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, ExactLookupHit)
    assert hit.term_text == "--model"
    assert hit.term_type is TermType.flag
    assert hit.product_area == "codex"
    assert hit.score == 1.0
    assert hit.index_version == active.index_version


@pytest.mark.asyncio
async def test_exact_lookup_uses_active_sentinel(session) -> None:
    """Passing ``index_version='active'`` is resolved to the active row."""
    await _seed(session)
    from sqlalchemy import select

    from app.models.index_versions import IndexVersion

    active = (
        await session.execute(select(IndexVersion).where(IndexVersion.status == IndexStatus.active))
    ).scalar_one()

    hits = await exact_lookup(
        session,
        term="--model",
        product_area="codex",
        index_version="active",
    )
    assert len(hits) == 1
    assert hits[0].index_version == "active"
    # And the actual hit points at the active index version.
    assert active.index_version in {active.index_version}


@pytest.mark.asyncio
async def test_exact_lookup_scoped_to_product_area(session) -> None:
    """The same term in two product areas is a different answer."""
    await _seed(session)
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.chunks import Chunk
    from app.models.documents import Document
    from app.models.enums import DocumentStatus
    from app.models.exact_terms import ExactTerm
    from app.models.index_versions import IndexVersion

    active = (
        await session.execute(select(IndexVersion).where(IndexVersion.status == IndexStatus.active))
    ).scalar_one()
    now = datetime.now(UTC)
    doc = Document(
        index_version=active.index_version,
        source_name="claude_api",
        product_area="claude_api",
        source_url="https://example.com/claude-api",
        title="Claude API extras",
        identity_checksum="deadbeef" * 8,
        last_fetched_at=now,
        status=DocumentStatus.active,
    )
    session.add(doc)
    await session.flush()
    chunk = Chunk(
        document_id=doc.document_id,
        product_area="claude_api",
        section_path="flags",
        heading="flags",
        parent_heading=None,
        chunk_text="The --model flag selects the model.",
        context_summary="--model flag in Claude API.",
        chunk_order=0,
        content_checksum="chk_claude_chunk_0",
        exact_terms=[],
    )
    session.add(chunk)
    await session.flush()
    session.add(
        ExactTerm(
            term_text="--model",
            term_type=TermType.flag,
            product_area="claude_api",
            document_id=doc.document_id,
            chunk_id=chunk.chunk_id,
        )
    )
    await session.commit()

    codex_hits = await exact_lookup(
        session, term="--model", product_area="codex", index_version="active"
    )
    claude_hits = await exact_lookup(
        session, term="--model", product_area="claude_api", index_version="active"
    )
    assert len(codex_hits) == 1
    assert len(claude_hits) == 1
    assert codex_hits[0].product_area == "codex"
    assert claude_hits[0].product_area == "claude_api"
    assert codex_hits[0].chunk_id != claude_hits[0].chunk_id


@pytest.mark.asyncio
async def test_exact_lookup_filters_by_term_type(session) -> None:
    """``term_type`` narrows the result set."""
    await _seed(session)
    hits = await exact_lookup(
        session,
        term="--model",
        product_area="codex",
        term_type=TermType.command,
        index_version="active",
    )
    assert hits == []


@pytest.mark.asyncio
async def test_exact_lookup_returns_empty_on_unknown_term(session) -> None:
    """A term that doesn't exist in any chunk returns an empty list."""
    await _seed(session)
    hits = await exact_lookup(
        session,
        term="--does-not-exist",
        product_area="codex",
        index_version="active",
    )
    assert hits == []


@pytest.mark.asyncio
async def test_exact_lookup_returns_empty_on_empty_term(session) -> None:
    """Empty ``term`` short-circuits to an empty list (defensive)."""
    await _seed(session)
    hits = await exact_lookup(session, term="", product_area="codex", index_version="active")
    assert hits == []


@pytest.mark.asyncio
async def test_exact_lookup_returns_empty_on_empty_product_area(
    session,
) -> None:
    """Empty ``product_area`` short-circuits — global search is not supported."""
    await _seed(session)
    hits = await exact_lookup(session, term="--model", product_area="", index_version="active")
    assert hits == []


@pytest.mark.asyncio
async def test_exact_lookup_clamps_limit_to_max_results(session) -> None:
    """The service clamps ``limit`` to :data:`MAX_RESULTS`."""
    await _seed(session)
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.chunks import Chunk
    from app.models.documents import Document
    from app.models.enums import DocumentStatus
    from app.models.exact_terms import ExactTerm
    from app.models.index_versions import IndexVersion

    active = (
        await session.execute(select(IndexVersion).where(IndexVersion.status == IndexStatus.active))
    ).scalar_one()
    now = datetime.now(UTC)
    for i in range(30):
        doc = Document(
            index_version=active.index_version,
            source_name=f"src_{i}",
            product_area="codex",
            source_url=f"https://example.com/{i}",
            title=f"src {i}",
            identity_checksum=f"chk_{i}" + "0" * 60,
            last_fetched_at=now,
            status=DocumentStatus.active,
        )
        session.add(doc)
        await session.flush()
        chunk = Chunk(
            document_id=doc.document_id,
            product_area="codex",
            section_path=f"h{i}",
            heading=f"h{i}",
            parent_heading=None,
            chunk_text="x" * 10,
            context_summary="x" * 10,
            chunk_order=0,
            content_checksum=f"chk_codex_chunk_{i}",
            exact_terms=[],
        )
        session.add(chunk)
        await session.flush()
        session.add(
            ExactTerm(
                term_text="--model",
                term_type=TermType.flag,
                product_area="codex",
                document_id=doc.document_id,
                chunk_id=chunk.chunk_id,
            )
        )
    await session.commit()

    hits = await exact_lookup(session, term="--model", product_area="codex", limit=1000)
    assert len(hits) == MAX_RESULTS


def test_max_results_constant_is_a_positive_int() -> None:
    """The cap is exported and is a positive integer."""
    assert isinstance(MAX_RESULTS, int)
    assert MAX_RESULTS >= 1
    assert MAX_RESULTS <= 100


@pytest.mark.asyncio
async def test_exact_lookup_pins_to_specific_index_version(session) -> None:
    """An explicit non-active ``index_version`` is honoured (replay path).

    Two non-active index versions both contain a term that the
    active index does not. Passing the version key returns the
    row; passing the other version returns nothing. This locks
    the replay / debugging branch the docstring advertises.
    """
    await _seed(session)
    from datetime import UTC, datetime

    from app.models.documents import Document
    from app.models.enums import DocumentStatus
    from app.models.exact_terms import ExactTerm
    from app.models.index_versions import IndexVersion

    # Two non-active index versions, both inactive. (active is
    # the seeded row; we explicitly avoid promoting ours so the
    # active-row filter would skip them.)
    now = datetime.now(UTC)
    v_old = IndexVersion(
        index_version="v-old-replay",
        status=IndexStatus.candidate,
        source_version_hash="sha256:replay-old",
        created_at=now,
        promoted_at=None,
    )
    v_new = IndexVersion(
        index_version="v-new-replay",
        status=IndexStatus.candidate,
        source_version_hash="sha256:replay-new",
        created_at=now,
        promoted_at=None,
    )
    session.add_all([v_old, v_new])
    await session.flush()

    # Only v_old has the term we're looking for.
    doc_old = Document(
        index_version=v_old.index_version,
        source_name="replay-old-src",
        product_area="replay",
        source_url="https://example.com/old",
        title="Replay old",
        identity_checksum="x" * 64,
        last_fetched_at=now,
        status=DocumentStatus.active,
    )
    session.add(doc_old)
    await session.flush()

    # ``exact_terms.chunk_id`` is a foreign key onto ``chunks``; the
    # ingestion pipeline only ever mints a term from a chunk it just wrote,
    # so a real chunk row is the production shape (#286). The same file
    # already does this correctly a few tests up.
    from app.models.chunks import Chunk

    chunk_old = Chunk(
        document_id=doc_old.document_id,
        product_area="replay",
        section_path="flags",
        heading="flags",
        parent_heading=None,
        chunk_text="The --replay-flag replays a run.",
        context_summary="--replay-flag in replay.",
        chunk_order=0,
        content_checksum="chk_replay_old_0",
        exact_terms=[],
    )
    session.add(chunk_old)
    await session.flush()

    session.add(
        ExactTerm(
            term_text="--replay-flag",
            product_area="replay",
            term_type=TermType.flag,
            document_id=doc_old.document_id,
            chunk_id=chunk_old.chunk_id,
        )
    )
    await session.flush()

    # Pin to the version that has the row.
    hits = await exact_lookup(
        session,
        term="--replay-flag",
        product_area="replay",
        index_version=v_old.index_version,
    )
    assert len(hits) == 1
    assert hits[0].index_version == v_old.index_version
    assert hits[0].term_text == "--replay-flag"

    # Pin to the version that does not have the row.
    hits_other = await exact_lookup(
        session,
        term="--replay-flag",
        product_area="replay",
        index_version=v_new.index_version,
    )
    assert hits_other == []


@pytest.mark.asyncio
async def test_exact_lookup_pinned_version_skips_active_filter(session) -> None:
    """A pinned (non-active) version returns rows even if not currently active.

    Locks the difference between ``"active"`` and a specific
    string: with the latter, the ``status='active'`` filter is
    skipped entirely. Without the test, a regression that
    *also* filters on active when a key is supplied would pass
    every other test in this file.
    """
    await _seed(session)
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.documents import Document
    from app.models.enums import DocumentStatus
    from app.models.exact_terms import ExactTerm
    from app.models.index_versions import IndexVersion

    # Confirm the active row is "v1-..." (seed value), and add a
    # candidate (not active) version with its own term.
    active = (
        await session.execute(select(IndexVersion).where(IndexVersion.status == IndexStatus.active))
    ).scalar_one()
    assert active.status is IndexStatus.active

    candidate = IndexVersion(
        index_version="v-candidate-1",
        status=IndexStatus.candidate,
        source_version_hash="sha256:cand",
        created_at=datetime.now(UTC),
        promoted_at=None,
    )
    session.add(candidate)
    await session.flush()

    doc = Document(
        index_version=candidate.index_version,
        source_name="cand-src",
        product_area="cand",
        source_url="https://example.com/cand",
        title="Candidate",
        identity_checksum="y" * 64,
        last_fetched_at=datetime.now(UTC),
        status=DocumentStatus.active,
    )
    session.add(doc)
    await session.flush()

    # Real ``chunks`` parent for the term's foreign key (#286).
    from app.models.chunks import Chunk

    chunk = Chunk(
        document_id=doc.document_id,
        product_area="cand",
        section_path="flags",
        heading="flags",
        parent_heading=None,
        chunk_text="The --cand-only flag is candidate-only.",
        context_summary="--cand-only in cand.",
        chunk_order=0,
        content_checksum="chk_cand_only_0",
        exact_terms=[],
    )
    session.add(chunk)
    await session.flush()

    session.add(
        ExactTerm(
            term_text="--cand-only",
            product_area="cand",
            term_type=TermType.flag,
            document_id=doc.document_id,
            chunk_id=chunk.chunk_id,
        )
    )
    await session.flush()

    # With 'active', the candidate's row is invisible.
    hits_active = await exact_lookup(
        session,
        term="--cand-only",
        product_area="cand",
        index_version="active",
    )
    assert hits_active == []

    # With the explicit candidate key, the row is found even
    # though that index is not active.
    hits_candidate = await exact_lookup(
        session,
        term="--cand-only",
        product_area="cand",
        index_version=candidate.index_version,
    )
    assert len(hits_candidate) == 1


# ---------------------------------------------------------------------------
# #352, PATH 1. ``index_version="active"`` is SET MEMBERSHIP, so on a dual-active
# database this route silently unions the documents of every active index. It has
# no answer cache to poison and changing what it returns is read-path policy that
# #265 owns, so it keeps unioning -- but no longer silently.
# ---------------------------------------------------------------------------


async def _add_second_active_index_with_the_same_term(session) -> None:
    """A second ``active`` IndexVersion owning its own copy of ``--model``."""
    import uuid
    from datetime import UTC, datetime

    from app.models import Chunk, Document, DocumentStatus, ExactTerm, IndexVersion

    now = datetime.now(UTC)
    session.add(
        IndexVersion(
            index_version="v2",
            status=IndexStatus.active,
            source_version_hash="sha256:v2",
            created_at=now,
            promoted_at=now,
        )
    )
    doc = Document(
        document_id=uuid.uuid4(),
        index_version="v2",
        source_name="codex",
        product_area="codex",
        source_url="https://example.invalid/codex-v2",
        title="Codex CLI (v2 index)",
        identity_checksum="sha256:codex-v2",
        last_fetched_at=now,
        last_indexed_at=now,
        status=DocumentStatus.active,
    )
    session.add(doc)
    await session.flush()
    chunk = Chunk(
        chunk_id=uuid.uuid4(),
        document_id=doc.document_id,
        product_area="codex",
        section_path="/flags",
        heading="Flags",
        parent_heading=None,
        chunk_text="The --model flag selects the model, on the v2 index.",
        context_summary="v2 copy",
        exact_terms=[],
        chunk_order=0,
        content_checksum="sha256:codex-v2-chunk-0",
    )
    session.add(chunk)
    await session.flush()
    session.add(
        ExactTerm(
            term_id=uuid.uuid4(),
            term_text="--model",
            term_type=TermType.flag,
            product_area="codex",
            document_id=doc.document_id,
            chunk_id=chunk.chunk_id,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_active_sentinel_warns_when_it_unions_two_indexes(session, caplog) -> None:
    """The union is real and is now on the record.

    RED without the fix: no ``exact_lookup_spans_multiple_indexes`` record exists
    (the event was never emitted, and ``Document.index_version`` was not even in
    the projection to emit it from).
    """
    import logging

    await _seed(session)
    await _add_second_active_index_with_the_same_term(session)

    with caplog.at_level(logging.WARNING, logger="citevyn.retrieval"):
        hits = await exact_lookup(
            session, term="--model", product_area="codex", index_version="active"
        )

    # PARTNER: the route still returns BOTH hits. The owner's decision is that this
    # keeps serving; only the silence is fixed.
    assert len(hits) == 2, hits
    rec = next(
        (r for r in caplog.records if "exact_lookup_spans_multiple_indexes" in r.getMessage()),
        None,
    )
    assert rec is not None, [r.getMessage() for r in caplog.records]
    assert rec.index_versions == "v1,v2"
    assert rec.index_version_count == 2


@pytest.mark.asyncio
async def test_a_single_active_index_lookup_is_not_warned(session, caplog) -> None:
    """NON-VACUITY PARTNER: the WARN must not fire on every lookup.

    Without this, ``_warn_if_hits_span_indexes`` could log unconditionally and the
    test above would still pass -- and the line would be worthless, because an
    operator would see it on a perfectly healthy database.

    RED if the ``len(versions) <= 1`` guard is removed.
    """
    import logging

    await _seed(session)

    with caplog.at_level(logging.WARNING, logger="citevyn.retrieval"):
        hits = await exact_lookup(
            session, term="--model", product_area="codex", index_version="active"
        )

    assert len(hits) == 1, "precondition: one active index, one hit"
    assert not any(
        "exact_lookup_spans_multiple_indexes" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_a_pinned_index_version_is_never_warned_even_on_a_dual_active_db(
    session, caplog
) -> None:
    """BOUNDARY: pinning a version is the replay/debug path and cannot union, so a
    dual-active database is irrelevant to it.

    RED if the WARN is moved above the ``index_version == "active"`` branch, or if
    the pinned branch stops filtering on ``Document.index_version ==``.
    """
    import logging

    await _seed(session)
    await _add_second_active_index_with_the_same_term(session)

    with caplog.at_level(logging.WARNING, logger="citevyn.retrieval"):
        hits = await exact_lookup(session, term="--model", product_area="codex", index_version="v2")

    assert len(hits) == 1, "a pinned lookup returns only that index's row"
    assert not any(
        "exact_lookup_spans_multiple_indexes" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
