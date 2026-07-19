"""Tests for :mod:`app.worker.cli` helpers.

Focus: ``content_version_hash`` — the fingerprint that makes the answer cache
self-invalidate when a source doc is edited. Previously the CLI stamped a static
``settings.source_version_hash`` on every ingest, so editing a doc and re-ingesting
left the cache serving the OLD answer (the cache key includes source_version_hash).

The full chain has three links, each pinned somewhere:

1. content edit → new fingerprint (here),
2. new fingerprint → published onto the ``IndexVersion`` row after a clean
   in-place re-ingest, and NOT before (``drive`` tests at the bottom of this
   file, plus ``test_worker_runner.py::test_advance_source_version_hash_*``),
3. changed hash → cache miss (``test_cache_invalidation.py``).

Link 2 is the one that made the fix a no-op before: ``ensure_index_version``
only wrote the hash on INSERT, and the shipped worker image always re-ingests
into the same default ``--index-version v-local``. Publishing it too EARLY is
the opposite failure — see ``advance_source_version_hash``.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models.base import Base
from app.models.index_versions import IndexVersion
from app.worker.allowlist import MVP_SOURCES, SourceSpec, get_source
from app.worker.cli import build_runner, content_version_hash, drive
from app.worker.embedder import StubEmbedder
from app.worker.fetchers import Fetcher, FetchError, LocalFetcher
from app.worker.runner import IngestionRunner

# ---------------------------------------------------------------------------
# Fingerprint properties
# ---------------------------------------------------------------------------


def test_hash_is_sha256_prefixed_and_deterministic() -> None:
    h1 = content_version_hash(MVP_SOURCES)
    h2 = content_version_hash(MVP_SOURCES)
    assert h1.startswith("sha256:")
    assert len(h1) == len("sha256:") + 64  # sha256 hex digest
    assert h1 == h2


def test_hash_is_order_independent() -> None:
    """Sources are hashed in a stable name-sorted order — input order must not matter."""
    assert content_version_hash(MVP_SOURCES) == content_version_hash(list(reversed(MVP_SOURCES)))


def test_hash_changes_when_a_source_edit_changes() -> None:
    """The core property: an edit to a source's CONTENT changes the fingerprint.

    A changed fingerprint changes the answer-cache key, so a re-ingest after a doc
    edit invalidates stale cached answers instead of serving them.
    """
    one = MVP_SOURCES[:1]
    before = content_version_hash(one, fetch=lambda s: "original content")
    after = content_version_hash(one, fetch=lambda s: "EDITED content")
    assert before != after


def test_hash_reflects_the_source_set() -> None:
    """Adding/removing a source changes the corpus fingerprint too."""
    assert content_version_hash(MVP_SOURCES) != content_version_hash(MVP_SOURCES[:2])


def test_hash_changes_when_a_source_is_renamed() -> None:
    """The name is mixed in, so a rename is a corpus change even at identical bytes."""
    spec = MVP_SOURCES[0]
    renamed = SourceSpec(
        name=spec.name + "_renamed",
        product_area=spec.product_area,
        title=spec.title,
        fetcher=spec.fetcher,
        location=spec.location,
    )
    same_bytes = "identical content"
    assert content_version_hash([spec], fetch=lambda s: same_bytes) != content_version_hash(
        [renamed], fetch=lambda s: same_bytes
    )


def test_field_framing_prevents_a_boundary_collision() -> None:
    """Name/content are separated, so shifting the boundary must change the hash.

    Without the ``\\x1f`` field separator, a source named ``ab`` with content ``c``
    and one named ``a`` with content ``bc`` would hash identically.
    """

    def _spec(name: str) -> SourceSpec:
        return SourceSpec(name=name, product_area="p", title="t", fetcher="local", location="l")

    left = content_version_hash([_spec("ab")], fetch=lambda s: "c")
    right = content_version_hash([_spec("a")], fetch=lambda s: "bc")
    assert left != right


# ---------------------------------------------------------------------------
# Real fetch path
# ---------------------------------------------------------------------------


def test_default_fetch_reads_the_real_source_files() -> None:
    """The no-``fetch`` path goes through the real ``build_fetcher`` → ``LocalFetcher``.

    Pins the wiring: hashing something other than the file bytes (e.g. the
    ``location`` string) would still satisfy every property test above.
    """
    assert content_version_hash(MVP_SOURCES) == content_version_hash(
        MVP_SOURCES, fetch=LocalFetcher().fetch
    )


def test_editing_a_real_source_file_changes_the_hash(tmp_path: Path) -> None:
    """End-to-end over the filesystem: edit a doc on disk, get a different hash."""
    (tmp_path / "sources").mkdir()
    doc = tmp_path / "sources" / "thing.md"
    doc.write_text("# Thing\n\nOriginal body.\n", encoding="utf-8")
    spec = SourceSpec(
        name="thing",
        product_area="thing",
        title="Thing",
        fetcher="local",
        location="sources/thing.md",
    )
    read = LocalFetcher(root=tmp_path).fetch

    before = content_version_hash([spec], fetch=read)
    doc.write_text("# Thing\n\nBroadened body.\n", encoding="utf-8")
    after = content_version_hash([spec], fetch=read)
    assert before != after


def test_an_unfetchable_source_degrades_instead_of_aborting_the_run() -> None:
    """One bad source must not kill the whole ingest at runner-build time.

    The fingerprint spans the entire corpus, so without the ``FetchError`` guard a
    single missing fixture — or the first ``fetcher="http"`` source, which
    :class:`HttpFetcher` always rejects — would raise here and prevent even
    ``run --source <a healthy one>`` from writing a single job row.
    """

    def _explode(spec: SourceSpec) -> str:
        raise FetchError(f"cannot read {spec.name}")

    digest = content_version_hash(MVP_SOURCES[:2], fetch=_explode)
    assert digest.startswith("sha256:")
    # Degrading is not the same as ignoring: a readable corpus still differs.
    assert digest != content_version_hash(MVP_SOURCES[:2], fetch=lambda s: "readable")


# ---------------------------------------------------------------------------
# Wiring: the runner actually receives the content hash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index_version", ["v-local", "v-candidate"])
def testbuild_runner_stamps_the_content_hash(index_version: str) -> None:
    """``build_runner`` must hand the runner the CONTENT hash.

    This is the behaviour change itself: before, a static setting was stamped on
    every ingest. Without this test, reverting the one line in ``build_runner``
    leaves every other test in this file green.
    """
    runner = build_runner(Settings(embedding_provider="stub"), index_version=index_version)
    assert runner.source_version_hash == content_version_hash(MVP_SOURCES)
    assert runner.source_version_hash.startswith("sha256:")


# ---------------------------------------------------------------------------
# drive: WHEN the new fingerprint becomes visible
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sessionmaker_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A per-test sessionmaker; ``drive`` opens its own sessions, not one session."""
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        engine = create_async_engine(f"sqlite+aiosqlite:///{fh.name}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        await engine.dispose()


def _runner(hash_value: str, *, fetcher: Fetcher | None = None) -> IngestionRunner:
    return IngestionRunner(
        fetcher=fetcher if fetcher is not None else LocalFetcher(),
        embedder=StubEmbedder(dim=8),
        source_version_hash=hash_value,
        index_version="v-local",
    )


async def _stamped_hash(sm: async_sessionmaker[AsyncSession]) -> str:
    async with sm() as session:
        row = await session.get(IndexVersion, "v-local")
        assert row is not None
        return row.source_version_hash


@pytest.mark.asyncio
async def testdrive_advances_the_hash_after_a_clean_full_run(
    sessionmaker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The happy path: a full successful ingest publishes the new fingerprint."""
    sources = list(MVP_SOURCES)
    assert await drive(_runner("sha256:before"), sessionmaker_factory, sources, "v-local") == 0
    assert await _stamped_hash(sessionmaker_factory) == "sha256:before"

    # Corpus "edited": the runner now carries a different content hash.
    assert await drive(_runner("sha256:after"), sessionmaker_factory, sources, "v-local") == 0
    assert await _stamped_hash(sessionmaker_factory) == "sha256:after"


@pytest.mark.asyncio
async def testdrive_does_not_advance_the_hash_when_a_source_fails(
    sessionmaker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failed source must leave the OLD fingerprint published.

    Advancing it here would publish a hash the corpus does not match: answers
    built from the un-rebuilt chunks get cached under the NEW key, and the retry
    re-hashes the same files so the hash never moves again — the stale answer
    survives the correction until the TTL expires.
    """
    sources = list(MVP_SOURCES)
    assert await drive(_runner("sha256:before"), sessionmaker_factory, sources, "v-local") == 0

    class _FlakyFetcher:
        """Fails only for ``codex`` — a partial-corpus rebuild."""

        def __init__(self) -> None:
            self._real = LocalFetcher()

        def fetch(self, source: SourceSpec) -> str:
            if source.name == "codex":
                raise FetchError("simulated embedder/fetch outage")
            return self._real.fetch(source)

    exit_code = await drive(
        _runner("sha256:after", fetcher=_FlakyFetcher()),
        sessionmaker_factory,
        sources,
        "v-local",
    )
    assert exit_code == 2  # non-zero: the run reported failure
    assert await _stamped_hash(sessionmaker_factory) == "sha256:before"


@pytest.mark.asyncio
async def testdrive_does_not_advance_the_hash_on_a_source_subset(
    sessionmaker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``--source X`` cannot vouch for the sources it skipped.

    The fingerprint spans the whole corpus, so advancing it after a subset run
    would claim the untouched docs were rebuilt too.
    """
    sources = list(MVP_SOURCES)
    assert await drive(_runner("sha256:before"), sessionmaker_factory, sources, "v-local") == 0

    subset = [get_source("codex")]
    assert await drive(_runner("sha256:after"), sessionmaker_factory, subset, "v-local") == 0
    assert await _stamped_hash(sessionmaker_factory) == "sha256:before"
