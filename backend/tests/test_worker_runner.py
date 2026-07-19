"""Tests for :mod:`app.worker.runner` (Slice 8 step 6).

The runner is exercised end-to-end with the real
:class:`LocalFetcher` and the real :class:`StubEmbedder`.
The session is the per-test ``session`` fixture, which
runs against the in-memory SQLite engine.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunks import Chunk
from app.models.documents import Document
from app.models.enums import (
    DocumentStatus,
    IndexStatus,
    JobStage,
    JobStatus,
)
from app.models.exact_terms import ExactTerm
from app.models.index_versions import IndexVersion
from app.models.ingestion_jobs import IngestionJob
from app.worker.allowlist import MVP_SOURCES, SourceSpec, get_source
from app.worker.embedder import StubEmbedder
from app.worker.fetchers import LocalFetcher
from app.worker.runner import IngestionRunner, ensure_index_version

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> IngestionRunner:
    """A runner with the local fetcher + a 32-dim stub embedder."""
    return IngestionRunner(
        fetcher=LocalFetcher(),
        embedder=StubEmbedder(dim=32),
        source_version_hash="sha256:test-snapshot",
        index_version="v-test",
    )


async def _index_version_count(session: AsyncSession) -> int:
    stmt = select(IndexVersion)
    result = await session.execute(stmt)
    return len(result.scalars().all())


async def _ingestion_job_count(session: AsyncSession) -> int:
    stmt = select(IngestionJob)
    result = await session.execute(stmt)
    return len(result.scalars().all())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_completes_full_pipeline(session: AsyncSession, runner: IngestionRunner) -> None:
    """A complete run produces a Document, Chunks, and ExactTerm rows."""
    result = await runner.run(session, source=get_source("claude_api"))
    assert result.status is JobStatus.completed
    assert result.chunk_count >= 1
    assert result.document_id is not None

    # Verify the rows landed.
    docs = (
        (await session.execute(select(Document).where(Document.source_name == "claude_api")))
        .scalars()
        .all()
    )
    assert len(docs) == 1
    assert docs[0].status is DocumentStatus.active
    assert docs[0].index_version == "v-test"

    chunks = (
        (await session.execute(select(Chunk).where(Chunk.document_id == docs[0].document_id)))
        .scalars()
        .all()
    )
    assert len(chunks) == result.chunk_count
    # Each chunk has an embedding of the right shape.
    for chunk in chunks:
        assert chunk.embedding is not None
        assert len(chunk.embedding) == 32
        assert all(isinstance(v, float) for v in chunk.embedding)


@pytest.mark.asyncio
async def test_run_writes_ingestion_job_row(session: AsyncSession, runner: IngestionRunner) -> None:
    """A single :class:`IngestionJob` row is written for the run."""
    assert await _ingestion_job_count(session) == 0
    await runner.run(session, source=get_source("codex"))
    assert await _ingestion_job_count(session) == 1
    job = (await session.execute(select(IngestionJob))).scalars().one()
    assert job.source_name == "codex"
    assert job.status is JobStatus.completed
    assert job.stage is JobStage.indexing
    assert job.completed_at is not None
    assert job.error_type is None
    assert job.error_message is None


@pytest.mark.asyncio
async def test_run_advances_stages_in_order(session: AsyncSession, runner: IngestionRunner) -> None:
    """The job's final stage is ``indexing`` (the last stage of the pipeline)."""
    await runner.run(session, source=get_source("claude_code"))
    job = (await session.execute(select(IngestionJob))).scalars().one()
    assert job.stage is JobStage.indexing


@pytest.mark.asyncio
async def test_run_extracts_exact_terms(session: AsyncSession, runner: IngestionRunner) -> None:
    """The Claude API fixture's flags and env vars surface as :class:`ExactTerm` rows."""
    await runner.run(session, source=get_source("claude_api"))
    terms = (
        (await session.execute(select(ExactTerm).where(ExactTerm.product_area == "claude_api")))
        .scalars()
        .all()
    )
    texts = {t.term_text for t in terms}
    # Flags from the fixture.
    assert "--model" in texts
    # Env vars from the fixture.
    assert "CLAUDE_API_RATE_LIMIT" in texts
    assert "ANTHROPIC_API_KEY" in texts
    # Header from the fixture.
    assert "x-api-key" in texts


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_is_idempotent_on_existing_document(
    session: AsyncSession, runner: IngestionRunner
) -> None:
    """A second run for the same (source, index_version) reuses the document."""
    first = await runner.run(session, source=get_source("gemini_api"))
    assert first.chunk_count >= 1
    second = await runner.run(session, source=get_source("gemini_api"))
    assert second.chunk_count == first.chunk_count

    docs = (
        (await session.execute(select(Document).where(Document.source_name == "gemini_api")))
        .scalars()
        .all()
    )
    assert len(docs) == 1


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_marks_job_failed_on_fetch_error(
    session: AsyncSession,
) -> None:
    """A missing fixture is a :class:`FetchError`; the job is marked failed."""
    bad_spec = SourceSpec(
        name="missing",
        product_area="missing",
        title="Missing Source",
        fetcher="local",
        location="app/worker/sources/does-not-exist.md",
    )
    runner = IngestionRunner(
        fetcher=LocalFetcher(),
        embedder=StubEmbedder(dim=8),
    )
    result = await runner.run(session, source=bad_spec)
    assert result.status is JobStatus.failed
    assert result.error_type == "FetchError"

    job = (await session.execute(select(IngestionJob))).scalars().one()
    assert job.status is JobStatus.failed
    assert job.error_type == "FetchError"
    assert "not found" in (job.error_message or "").lower()


# ---------------------------------------------------------------------------
# ensure_index_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_index_version_creates_candidate(
    session: AsyncSession,
) -> None:
    """A first call creates a candidate row; a second call returns it."""
    row = await ensure_index_version(
        session,
        index_version="v-new",
        source_version_hash="sha256:abc",
    )
    assert row.status is IndexStatus.candidate
    assert row.index_version == "v-new"

    again = await ensure_index_version(
        session,
        index_version="v-new",
        source_version_hash="sha256:abc",
    )
    assert again.index_version == row.index_version
    # Only one row exists.
    assert await _index_version_count(session) == 1


@pytest.mark.asyncio
async def test_ensure_index_version_stamps_and_refreshes_embedding_provenance(
    session: AsyncSession,
) -> None:
    """The Tier-3 stamp is written on create and refreshed on re-ingest.

    Covers ``ensure_index_version``'s existing-row branch: a rebuild under a
    different embedder must not keep a stale provider/model/dim stamp."""
    created = await ensure_index_version(
        session,
        index_version="v-stamp",
        source_version_hash="sha256:s1",
        embedding_provider="stub",
        embedding_model="gemini-embedding-001",
        embedding_dim=1536,
    )
    assert created.embedding_provider == "stub"
    assert created.embedding_model == "gemini-embedding-001"
    assert created.embedding_dim == 1536

    # Re-ingest the same index_version under a different embedder → stamp refreshed.
    refreshed = await ensure_index_version(
        session,
        index_version="v-stamp",
        source_version_hash="sha256:s1",
        embedding_provider="gemini",
        embedding_model="gemini-embedding-001",
        embedding_dim=1536,
    )
    assert refreshed.index_version == created.index_version
    assert refreshed.embedding_provider == "gemini"
    assert await _index_version_count(session) == 1


# ---------------------------------------------------------------------------
# End-to-end over all sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_all_mvp_sources(
    session: AsyncSession,
) -> None:
    """A full run over all MVP sources lands one document per source."""
    runner = IngestionRunner(
        fetcher=LocalFetcher(),
        embedder=StubEmbedder(dim=16),
        index_version="v-all",
    )
    for source in MVP_SOURCES:
        result = await runner.run(session, source=source)
        assert result.status is JobStatus.completed, (
            f"source {source.name!r} failed: {result.error_type}: {result.error_message}"
        )
    docs = (await session.execute(select(Document))).scalars().all()
    assert {d.source_name for d in docs} == {s.name for s in MVP_SOURCES}
    assert len(docs) == len(MVP_SOURCES)


# ---------------------------------------------------------------------------
# In-place re-ingest (same index_version)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reingest_replaces_chunks_instead_of_appending(
    session: AsyncSession, runner: IngestionRunner
) -> None:
    """Re-running a source must REPLACE its chunks, not add a second copy.

    Previously an in-place re-ingest appended a whole new generation (7 chunks
    became 14 for ``claude_api``), so an edited source doc left the OLD text in
    the corpus next to the new — and retrieval could still surface the stale
    wording. That silently defeats any content correction.
    """
    source = get_source("claude_api")
    first = await runner.run(session, source=source)
    after_one = (await session.execute(select(Chunk))).scalars().all()
    assert len(after_one) == first.chunk_count

    second = await runner.run(session, source=source)
    after_two = (await session.execute(select(Chunk))).scalars().all()

    assert second.status is JobStatus.completed
    assert len(after_two) == second.chunk_count == first.chunk_count
    # One document, one generation of chunks — no duplicates.
    docs = (await session.execute(select(Document))).scalars().all()
    assert len(docs) == 1
    assert {c.chunk_id for c in after_two}.isdisjoint({c.chunk_id for c in after_one})


@pytest.mark.asyncio
async def test_reingest_drops_the_previous_generations_exact_terms(
    session: AsyncSession, runner: IngestionRunner
) -> None:
    """Exact terms are rebuilt with the chunks, so they cannot accumulate either."""
    source = get_source("codex")
    first = await runner.run(session, source=source)
    second = await runner.run(session, source=source)
    terms = (await session.execute(select(ExactTerm))).scalars().all()
    assert first.term_count == second.term_count
    assert len(terms) == second.term_count
    chunk_ids = {c.chunk_id for c in (await session.execute(select(Chunk))).scalars().all()}
    # Every surviving term points at a chunk that still exists.
    assert {t.chunk_id for t in terms} <= chunk_ids


@pytest.mark.asyncio
async def test_reingest_refreshes_document_title_and_source_url(session: AsyncSession) -> None:
    """A retitled/retargeted source must update the live document.

    ``title`` and ``source_url`` are stamped onto every rendered citation, so a
    stale value meant an allowlist correction never reached users on the
    in-place re-ingest path.
    """
    spec = get_source("codex")
    old = SourceSpec(
        name=spec.name,
        product_area=spec.product_area,
        title="Codex CLI Reference",
        fetcher=spec.fetcher,
        location=spec.location,
        source_url="https://example.invalid/old",
    )
    runner = IngestionRunner(
        fetcher=LocalFetcher(),
        embedder=StubEmbedder(dim=16),
        index_version="v-retitle",
    )
    await runner.run(session, source=old)
    await runner.run(session, source=spec)

    docs = (await session.execute(select(Document))).scalars().all()
    assert len(docs) == 1
    assert docs[0].title == spec.title
    assert docs[0].source_url == spec.source_url


@pytest.mark.asyncio
async def test_ensure_index_version_refreshes_the_source_version_hash(
    session: AsyncSession,
) -> None:
    """A re-ingest with new content must update the hash on the EXISTING row.

    This is the link that made the content-derived fingerprint a no-op: the hash
    was only written on INSERT, while the shipped worker image and
    ``docs/RUNBOOK.md`` both re-ingest into the same default ``v-local``. The
    answer-cache key is derived from this column, so a stale value meant edited
    docs kept serving cached answers built from the old text.
    """
    created = await ensure_index_version(
        session,
        index_version="v-local",
        source_version_hash="sha256:corpus-before-edit",
    )
    assert created.source_version_hash == "sha256:corpus-before-edit"

    refreshed = await ensure_index_version(
        session,
        index_version="v-local",
        source_version_hash="sha256:corpus-after-edit",
    )
    assert refreshed.source_version_hash == "sha256:corpus-after-edit"
    assert await _index_version_count(session) == 1
