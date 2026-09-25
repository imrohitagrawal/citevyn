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
from app.worker.runner import (
    IngestionRunner,
    _checksum,
    advance_source_version_hash,
    ensure_index_version,
)

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


async def _run(runner: IngestionRunner, session: AsyncSession, source: object) -> object:
    """Ingest one source the way production does.

    ``app/worker/cli.py``'s ``drive()`` calls ``ensure_index_version`` and
    COMMITS it before the per-source loop ever reaches ``runner.run()``,
    because ``documents.index_version`` is a foreign key onto
    ``index_versions``. These tests drive the runner directly and used to
    skip that step -- invisible until SQLite foreign keys were turned on
    (#286), and it turned every affected test's job row into
    ``JobStatus.failed`` rather than a visible IntegrityError, because
    ``IngestionRunner.run`` records the exception on the job and returns.

    Idempotent, so a test that already made the row is unaffected.
    """
    await ensure_index_version(
        session,
        index_version=runner.index_version,
        source_version_hash=runner.source_version_hash,
    )
    return await runner.run(session, source=source)


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
    result = await _run(runner, session, source=get_source("claude_api"))
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
    await _run(runner, session, source=get_source("codex"))
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
    await _run(runner, session, source=get_source("claude_code"))
    job = (await session.execute(select(IngestionJob))).scalars().one()
    assert job.stage is JobStage.indexing


@pytest.mark.asyncio
async def test_run_extracts_exact_terms(session: AsyncSession, runner: IngestionRunner) -> None:
    """The Claude API fixture's flags and env vars surface as :class:`ExactTerm` rows."""
    await _run(runner, session, source=get_source("claude_api"))
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
    first = await _run(runner, session, source=get_source("gemini_api"))
    assert first.chunk_count >= 1
    second = await _run(runner, session, source=get_source("gemini_api"))
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
        source_version_hash="sha256:test-snapshot",
    )
    result = await _run(runner, session, source=bad_spec)
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
        source_version_hash="sha256:test-snapshot",
        index_version="v-all",
    )
    for source in MVP_SOURCES:
        result = await _run(runner, session, source=source)
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
    first = await _run(runner, session, source=source)
    after_one = (await session.execute(select(Chunk))).scalars().all()
    assert len(after_one) == first.chunk_count

    second = await _run(runner, session, source=source)
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
    first = await _run(runner, session, source=source)
    second = await _run(runner, session, source=source)
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
        source_version_hash="sha256:test-snapshot",
        index_version="v-retitle",
    )
    await _run(runner, session, source=old)
    await _run(runner, session, source=spec)

    docs = (await session.execute(select(Document))).scalars().all()
    assert len(docs) == 1
    assert docs[0].title == spec.title
    assert docs[0].source_url == spec.source_url


@pytest.mark.asyncio
async def test_document_identity_checksum_hashes_identity_not_content(
    session: AsyncSession,
) -> None:
    """``Document.identity_checksum`` is name+title, NOT the document's prose (#163).

    Regression for the misnamed column: it used to be called
    ``content_checksum``, which invited the next person to reach for it as a
    change-detection signal. This pins the honest semantics — it equals
    ``sha256(source_name + title)`` and never equals a hash of the fetched text
    — so a future rewrite that changes one without the other is loud.
    """
    spec = get_source("codex")
    runner = IngestionRunner(
        fetcher=LocalFetcher(),
        embedder=StubEmbedder(dim=8),
        source_version_hash="sha256:test-snapshot",
        index_version="v-identity",
    )
    await _run(runner, session, source=spec)

    document = (await session.execute(select(Document))).scalars().one()
    assert document.identity_checksum == _checksum(spec.name + spec.title)
    # The real prose hashes elsewhere: every chunk's own text, never the doc row.
    raw_text = LocalFetcher().fetch(spec)
    assert document.identity_checksum != _checksum(raw_text)


@pytest.mark.asyncio
async def test_identity_checksum_tracks_retitle_and_ignores_body_edits(
    session: AsyncSession,
) -> None:
    """Edge case that names the column: a retitle moves it, a body edit does not.

    Two runs of the SAME source with a different ``title`` must produce a
    different ``identity_checksum``; a run whose fetched body differs but whose
    identity is unchanged must leave it alone. That asymmetry is precisely why
    the old name was a lie.
    """
    spec = get_source("codex")
    renamed = SourceSpec(
        name=spec.name,
        product_area=spec.product_area,
        title="Codex CLI Reference (2026 edition)",
        fetcher=spec.fetcher,
        location=spec.location,
        source_url=spec.source_url,
    )
    runner = IngestionRunner(
        fetcher=LocalFetcher(),
        embedder=StubEmbedder(dim=8),
        source_version_hash="sha256:test-snapshot",
        index_version="v-retitle-checksum",
    )
    await _run(runner, session, source=spec)
    before = (await session.execute(select(Document))).scalars().one().identity_checksum

    await _run(runner, session, source=renamed)
    after = (await session.execute(select(Document))).scalars().one().identity_checksum
    assert after != before, "a retitle must move the identity checksum"

    # Same identity, different body: the fetcher returns edited prose but the
    # doc row's checksum is identity-derived, so it must NOT move.
    class _EditedBodyFetcher:
        def fetch(self, source: SourceSpec) -> str:
            return LocalFetcher().fetch(source) + "\n\nAn edit to the prose.\n"

    edited_runner = IngestionRunner(
        fetcher=_EditedBodyFetcher(),
        embedder=StubEmbedder(dim=8),
        source_version_hash="sha256:test-snapshot",
        index_version="v-retitle-checksum",
    )
    await _run(edited_runner, session, source=renamed)
    unmoved = (await session.execute(select(Document))).scalars().one().identity_checksum
    assert unmoved == after, "a body edit must NOT move an identity checksum"


def test_ingestion_runner_requires_an_explicit_source_version_hash() -> None:
    """The retired ``sha256:mvp-snapshot-2`` placeholder default is gone (#163).

    The answer-cache key includes this hash, so a default let an ad-hoc caller
    silently stamp a fingerprint that does not describe the corpus it indexed.
    Failing loudly at construction is the whole point of the change.
    """
    with pytest.raises(TypeError, match="source_version_hash"):
        IngestionRunner(  # type: ignore[call-arg]
            fetcher=LocalFetcher(),
            embedder=StubEmbedder(dim=8),
        )


@pytest.mark.asyncio
async def test_ensure_index_version_does_not_advance_the_hash_on_reingest(
    session: AsyncSession,
) -> None:
    """Merely starting a re-ingest must NOT publish the new fingerprint.

    The answer-cache key derives from this column. Advancing it here — before
    the chunks are rewritten — would publish a hash that lies: a query landing
    mid-run is answered from the OLD chunks but cached under the NEW key, and a
    retry re-hashes the same files so nothing evicts it until the TTL.
    """
    created = await ensure_index_version(
        session,
        index_version="v-local",
        source_version_hash="sha256:corpus-before-edit",
    )
    assert created.source_version_hash == "sha256:corpus-before-edit"

    unchanged = await ensure_index_version(
        session,
        index_version="v-local",
        source_version_hash="sha256:corpus-after-edit",
    )
    assert unchanged.source_version_hash == "sha256:corpus-before-edit"
    assert await _index_version_count(session) == 1


@pytest.mark.asyncio
async def test_advance_source_version_hash_publishes_the_new_fingerprint(
    session: AsyncSession,
) -> None:
    """The explicit post-run advance is what retires stale cached answers."""
    await ensure_index_version(
        session,
        index_version="v-local",
        source_version_hash="sha256:corpus-before-edit",
    )
    await advance_source_version_hash(
        session,
        index_version="v-local",
        source_version_hash="sha256:corpus-after-edit",
    )
    row = await session.get(IndexVersion, "v-local")
    assert row is not None
    assert row.source_version_hash == "sha256:corpus-after-edit"
    assert await _index_version_count(session) == 1


# ---------------------------------------------------------------------------
# A failed re-ingest must not wipe the previous generation (#461)
# ---------------------------------------------------------------------------


class _FixedTextFetcher:
    """Returns the same raw text for every source — a stand-in for an edited file."""

    def __init__(self, raw: str) -> None:
        self._raw = raw

    def fetch(self, source: SourceSpec) -> str:
        return self._raw


class _ExplodingEmbedder(StubEmbedder):
    """Embeds nothing: every batch raises, as a provider outage would mid-run."""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("simulated embedder outage")


def _relevelled(source: SourceSpec) -> str:
    """The shipped file with every ``## `` section demoted to ``### ``.

    The #461 scenario: a corpus edit that re-levels headings leaves the parser
    with no ``## `` section at all, so the source parses to zero chunks.
    """
    return "\n".join(
        "#" + line if line.startswith("## ") else line
        for line in LocalFetcher().fetch(source).splitlines()
    )


async def _generation(session: AsyncSession) -> tuple[set[object], int]:
    """``(chunk ids, exact-term count)`` currently in the database."""
    chunk_ids = {c.chunk_id for c in (await session.execute(select(Chunk))).scalars().all()}
    terms = (await session.execute(select(ExactTerm))).scalars().all()
    return chunk_ids, len(terms)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("# Codex\nprose only\n", id="h1-and-prose-only"),
        pytest.param(_relevelled(get_source("codex")), id="headings-relevelled-to-h3"),
    ],
)
async def test_reingest_that_parses_to_zero_sections_fails_and_keeps_the_previous_generation(
    session: AsyncSession, runner: IngestionRunner, raw: str
) -> None:
    """A source with no ``## `` section must FAIL the job, not replace its chunks with none.

    Before #461 the runner deleted the old chunks and then persisted the empty
    draft list: status ``completed``, ``chunk_count`` 0, the document still
    ``active`` — every question about that product refused, with nothing red.

    Turns red when the zero-draft check in ``IngestionRunner.run`` is removed.
    """
    source = get_source("codex")
    first = await _run(runner, session, source=source)
    assert first.status is JobStatus.completed
    before_ids, before_terms = await _generation(session)
    # Partner: there IS a generation to lose, so "unchanged" below means something.
    assert len(before_ids) == first.chunk_count > 0
    assert before_terms == first.term_count > 0

    edited = IngestionRunner(
        fetcher=_FixedTextFetcher(raw),
        embedder=StubEmbedder(dim=32),
        source_version_hash="sha256:test-snapshot",
        index_version="v-test",
    )
    second = await _run(edited, session, source=source)

    assert second.status is JobStatus.failed
    assert second.error_type == "ParseError"
    assert "codex" in (second.error_message or "")
    assert second.chunk_count == 0
    assert await _generation(session) == (before_ids, before_terms)

    job = await session.get(IngestionJob, second.job_id)
    assert job is not None
    assert job.status is JobStatus.failed
    assert job.error_type == "ParseError"
    document = (await session.execute(select(Document))).scalars().one()
    assert document.status is DocumentStatus.active


@pytest.mark.asyncio
async def test_first_ingest_that_parses_to_zero_sections_creates_no_document(
    session: AsyncSession,
) -> None:
    """Edge case: with nothing to keep, a zero-section source still fails.

    Without the check a first ingest would leave an ``active`` document with no
    chunks — a source that looks ingested and can never be cited.

    Turns red when the zero-draft check in ``IngestionRunner.run`` is removed.
    """
    runner = IngestionRunner(
        fetcher=_FixedTextFetcher("# Codex\nprose only\n"),
        embedder=StubEmbedder(dim=8),
        source_version_hash="sha256:test-snapshot",
        index_version="v-test",
    )
    result = await _run(runner, session, source=get_source("codex"))
    assert result.status is JobStatus.failed
    assert result.error_type == "ParseError"
    assert (await session.execute(select(Document))).scalars().all() == []
    assert (await session.execute(select(Chunk))).scalars().all() == []


@pytest.mark.asyncio
async def test_failed_reingest_keeps_the_previous_generation(
    session: AsyncSession, runner: IngestionRunner
) -> None:
    """A re-ingest that fails AFTER the old chunks were deleted must roll the delete back.

    ``_materialize_chunks`` deletes the previous generation before it embeds, so
    an embedder that raises lands in the failure path with the delete already
    flushed. Only the rollback in ``IngestionRunner.run`` stands between that and
    a committed wipe — this test is what pins it.

    Turns red when ``await session.rollback()`` is deleted from the failure path
    of ``IngestionRunner.run``.
    """
    source = get_source("codex")
    first = await _run(runner, session, source=source)
    assert first.status is JobStatus.completed
    before_ids, before_terms = await _generation(session)
    assert len(before_ids) == first.chunk_count > 0
    assert before_terms == first.term_count > 0

    failing = IngestionRunner(
        fetcher=LocalFetcher(),
        embedder=_ExplodingEmbedder(dim=32),
        source_version_hash="sha256:test-snapshot",
        index_version="v-test",
    )
    second = await _run(failing, session, source=source)
    assert second.status is JobStatus.failed
    assert second.error_type == "RuntimeError"

    assert await _generation(session) == (before_ids, before_terms)
