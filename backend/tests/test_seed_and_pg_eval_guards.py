"""Hermetic tests for the seeders (conftest + db/seed) and the Postgres-eval guards.

All hermetic: a fake in-process embedder (no network) exercises the conftest
seeder's ``embedder``/``embedder_identity``/``commit`` behaviour on SQLite; the
``db/seed`` bootstrap seeder is exercised end-to-end against the real shipped
markdown corpus (local files, stub embedder — no key, no network, no cost); and
the ``postgres_session`` safety guards are checked on the paths that raise BEFORE
any DB connection (production / non-Postgres URL / stub embedder). The real
Postgres+pgvector numbers are proven separately by the opt-in ``--postgres`` runner
and ``test_eval_semantic_discrimination`` (both make real API calls).
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.embeddings.factory import EmbedderIdentity
from app.models import Base, Chunk, Document, DocumentStatus, IndexStatus, IndexVersion
from app.worker.allowlist import MVP_SOURCES, SourceSpec
from tests.conftest import seed_catalog
from tests.eval.retrieval import PostgresEvalError, postgres_session

# The ``db`` package lives at the repo root; pytest's pythonpath is ``backend/``.
# Add the repo root so the db/seed backfill tests can import ``db.seed.seed_catalog``
# (the same on-disk module ``make seed`` runs). Done after the top-level imports so
# it does not trip E402; the ``db.seed`` import itself is lazy inside each test.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class _FakeEmbedder:
    """Deterministic non-stub embedder: distinct unit-ish vector per text, no I/O."""

    def __init__(self, dim: int = 1536) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _vec(self, text: str) -> list[float]:
        seed = float(len(text) % 7 + 1)
        return [seed] + [0.0] * (self._dim - 1)

    async def embed(self, text: str) -> list[float]:
        return self._vec(text)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class _BoomEmbedder(_FakeEmbedder):
    """Fails mid-batch on ``embed_documents`` to exercise the fail-loud path."""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding provider exploded")


class _MarkerEmbedder(_FakeEmbedder):
    """Emits a constant, provider-distinguishing vector so a test can assert WHICH
    provider's space a stored vector came from (marker never collides with
    ``_FakeEmbedder``'s length-derived first coordinate of 1..7)."""

    def __init__(self, marker: float, dim: int = 1536) -> None:
        super().__init__(dim)
        self._marker = marker

    def _vec(self, text: str) -> list[float]:
        return [self._marker] + [0.0] * (self._dim - 1)


async def _sqlite_factory():
    # The temp file must outlive this function (the caller runs the test against it
    # and closes it in a finally), so a with-block would close it too early.
    fh = tempfile.NamedTemporaryFile(suffix=".db")  # noqa: SIM115
    engine = create_async_engine(f"sqlite+aiosqlite:///{fh.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False, autoflush=False), fh


# ---------------------------------------------------------------------------
# Seeder: embedding-aware behaviour
# ---------------------------------------------------------------------------


async def test_seed_embedder_none_leaves_embeddings_null() -> None:
    """Backward-compat: the default (no embedder) leaves every embedding NULL."""
    engine, factory, fh = await _sqlite_factory()
    try:
        async with factory() as session:
            catalog = await seed_catalog(session)
        async with factory() as session:
            chunks = (await session.execute(select(Chunk))).scalars().all()
        assert chunks and all(c.embedding is None for c in chunks)
        assert len(catalog["chunks"]) == len(chunks)
        # Provenance stays unstamped (unknown ⇒ allow at read time).
        async with factory() as session:
            iv = (await session.execute(select(IndexVersion))).scalars().one()
        assert iv.embedding_provider is None
    finally:
        await engine.dispose()
        fh.close()


async def test_seed_with_embedder_populates_vectors_and_stamps_provenance() -> None:
    engine, factory, fh = await _sqlite_factory()
    identity = EmbedderIdentity(
        provider="openrouter", model="openai/text-embedding-3-small", dim=1536
    )
    try:
        async with factory() as session:
            await seed_catalog(session, embedder=_FakeEmbedder(), embedder_identity=identity)
        async with factory() as session:
            chunks = (await session.execute(select(Chunk))).scalars().all()
            iv = (await session.execute(select(IndexVersion))).scalars().one()
        assert chunks and all(c.embedding is not None and len(c.embedding) == 1536 for c in chunks)
        assert (iv.embedding_provider, iv.embedding_model, iv.embedding_dim) == (
            "openrouter",
            "openai/text-embedding-3-small",
            1536,
        )
    finally:
        await engine.dispose()
        fh.close()


async def test_seed_commit_false_is_not_persisted_after_rollback() -> None:
    """``commit=False`` leaves the caller in control; a rollback discards everything."""
    engine, factory, fh = await _sqlite_factory()
    try:
        async with factory() as session:
            await seed_catalog(session, embedder=_FakeEmbedder(), commit=False)
            # Visible within the same uncommitted transaction...
            assert (await session.scalar(select(Chunk).limit(1))) is not None
            await session.rollback()
        # ...and gone once rolled back (nothing was committed).
        async with factory() as session:
            assert (await session.execute(select(Chunk))).scalars().all() == []
            assert (await session.execute(select(IndexVersion))).scalars().all() == []
    finally:
        await engine.dispose()
        fh.close()


async def test_seed_embed_failure_propagates_and_leaves_no_partial_vectors() -> None:
    """A mid-batch embed failure raises and commits nothing (no partial rows)."""
    engine, factory, fh = await _sqlite_factory()
    try:
        async with factory() as session:
            with pytest.raises(RuntimeError, match="exploded"):
                await seed_catalog(session, embedder=_BoomEmbedder(), commit=True)
            await session.rollback()
        async with factory() as session:
            assert (await session.execute(select(Chunk))).scalars().all() == []
    finally:
        await engine.dispose()
        fh.close()


# ---------------------------------------------------------------------------
# postgres_session: safety guards that fire before any DB connection
# ---------------------------------------------------------------------------


async def test_postgres_session_refuses_production() -> None:
    settings = Settings(
        environment="production",
        demo_api_key="prod-demo-key",
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        openrouter_api_key="or-k",
        llm_provider="gemini",
        gemini_api_key="gk",
        admin_api_key="strong-secret",
        _env_file=None,
    )
    with pytest.raises(PostgresEvalError, match="production"):
        async with postgres_session(settings) as _:
            pass


async def test_postgres_session_refuses_non_postgres_url() -> None:
    settings = Settings(
        environment="local",
        database_url="sqlite+aiosqlite:///:memory:",
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        openrouter_api_key="or-k",
        _env_file=None,
    )
    with pytest.raises(PostgresEvalError, match="Postgres URL"):
        async with postgres_session(settings) as _:
            pass


async def test_postgres_session_refuses_stub_embedder() -> None:
    """A stub embedder must fail loud, never emit a fabricated semantic number."""
    settings = Settings(
        environment="local",
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        embedding_provider="stub",
        _env_file=None,
    )
    with pytest.raises(PostgresEvalError, match="REAL embedder"):
        async with postgres_session(settings) as _:
            pass


# ---------------------------------------------------------------------------
# db/seed/seed_catalog: seeds by INGESTING the shipped corpus (#178)
# ---------------------------------------------------------------------------
#
# These cover the structural fix: the bootstrap seeder no longer carries a
# hand-written copy of the corpus, it runs the real ingestion pipeline over
# ``MVP_SOURCES``. All hermetic — the sources are read off the local filesystem
# and the default stub embedder needs no key and no network.


def _shipped_chunk_texts() -> set[str]:
    """The chunk texts the ingestion pipeline produces from the shipped sources.

    Computed INDEPENDENTLY of the seeder (parser + chunker only, no DB) so the
    "the seed is derived, not copied" assertion cannot be satisfied by the
    seeder agreeing with itself.
    """
    from app.worker.chunker import chunk_document
    from app.worker.fetchers import build_fetcher
    from app.worker.parser import parse_markdown

    texts: set[str] = set()
    for spec in MVP_SOURCES:
        parsed = parse_markdown(build_fetcher(spec).fetch(spec))
        texts.update(draft.text for draft in chunk_document(parsed, source=spec))
    return texts


async def _fresh_sqlite_url(fh: tempfile._TemporaryFileWrapper[bytes]) -> str:
    """Create the schema in ``fh`` and return its async URL."""
    url = f"sqlite+aiosqlite:///{fh.name}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return url


async def _read_rows(url: str) -> tuple[list[Chunk], IndexVersion]:
    engine = create_async_engine(url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            chunks = list((await session.execute(select(Chunk))).scalars().all())
            iv = (await session.execute(select(IndexVersion))).scalars().one()
        return chunks, iv
    finally:
        await engine.dispose()


async def test_db_seed_is_derived_from_the_shipped_corpus(monkeypatch) -> None:
    """Happy path + the #178 acceptance criterion.

    Every chunk the bootstrap seed writes is exactly what the ingestion pipeline
    produces from ``backend/app/worker/sources/*.md``. Editing a source doc is
    therefore the ONLY way to change what ``make demo`` serves — there is no
    second copy left to forget (which is how #170's install content reached the
    worker source and the conftest fixture but not this seeder).
    """
    import db.seed.seed_catalog as seedmod

    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        summary = await seedmod.seed(url)
        chunks, iv = await _read_rows(url)

    assert {c.chunk_text for c in chunks} == _shipped_chunk_texts()
    assert summary["documents"] == summary["sources"] == len(MVP_SOURCES)
    assert summary["chunks"] == len(chunks) > len(MVP_SOURCES)
    assert (summary["index_version"], summary["status"]) == ("v1", "promoted")
    assert iv.status is IndexStatus.active


async def test_db_seed_carries_the_claude_code_install_content(monkeypatch) -> None:
    """Regression for #170-via-#178: the correction now reaches ``make demo``.

    The install content was added to ``claude_code.md`` and mirrored into the
    conftest fixture, but the hand-written bootstrap catalog still held only the
    Permissions text — so "How do I install Claude Code?" refused on a freshly
    bootstrapped stack. Pinned on the literal command, not on the word
    "install", so a chunk that merely mentions installing cannot satisfy it.
    """
    import db.seed.seed_catalog as seedmod

    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        await seedmod.seed(url)
        chunks, _ = await _read_rows(url)

    corpus = " ".join(c.chunk_text for c in chunks)
    assert "curl -fsSL https://claude.ai/install.sh | bash" in corpus
    assert "npm install -g @anthropic-ai/claude-code" in corpus


async def test_db_seed_stub_provider_leaves_the_vector_arm_dead_not_nonsense(monkeypatch) -> None:
    """SUCCESS path: the default (stub) bootstrap must NOT ship hash-bucketed vectors.

    ``StubEmbedder`` is deterministic but carries no meaning. Persisting its
    vectors would flip ``make demo``'s pgvector arm from DEAD to
    LIVE-WITH-NONSENSE: the demo API is configured with the same stub, so the
    Tier-3 stamp check sees a MATCH and enables the arm, which then ranks by
    hash distance. Silent mis-ranking beats no arm at all in exactly zero ways.

    Asserted at the level that matters to the read path: NULL embeddings (which
    ``VectorRetriever`` filters out) and no provenance stamp (so a later
    real-embedder deploy is not wedged into a permanent mismatch degrade by a
    stamp that was never true).

    Nothing here inspects *how* that is achieved, but the mechanism is "never
    written" (``build_runner(..., write_vectors=False)``) rather than
    "written then stripped" — see the failure-path twin below, which is the case
    a post-hoc strip could not cover.
    """
    import db.seed.seed_catalog as seedmod

    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        await seedmod.seed(url)
        chunks, iv = await _read_rows(url)

    # Guard against a vacuous pass: "every embedding is NULL" is trivially true
    # of an empty index and would prove nothing.
    assert chunks, "the seed produced no chunks at all"
    embedded = [c for c in chunks if c.embedding is not None]
    assert not embedded, (
        f"{len(embedded)} of {len(chunks)} chunks were persisted with stub vectors; "
        "the vector arm would rank by hash distance while /health/index reports healthy"
    )
    assert (iv.embedding_provider, iv.embedding_model, iv.embedding_dim) == (None, None, None)


async def test_db_seed_stub_index_reports_a_dead_vector_arm_to_operators(monkeypatch) -> None:
    """...and the honesty is visible on ``GET /health/index``, not just in the DB.

    A demo with no semantic recall is fine; a demo that CLAIMS semantic recall is
    not. ``derive_vector_arm_status`` must call the stub-seeded index ``dead`` —
    the operator signal #97 exists for — instead of ``healthy``.
    """
    import db.seed.seed_catalog as seedmod

    from app.services.index_health import STATUS_DEAD, active_index_vector_health

    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        await seedmod.seed(url)
        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                iv = (await session.execute(select(IndexVersion))).scalars().one()
                health = await active_index_vector_health(session, iv, Settings(_env_file=None))
        finally:
            await engine.dispose()

    assert health["status"] == STATUS_DEAD
    assert health["healthy"] is False
    assert health["chunks_embedded"] == 0 and health["chunks_total"] > 0


async def test_db_seed_stub_bootstrap_does_not_touch_other_index_versions(monkeypatch) -> None:
    """Edge case: the bootstrap is scoped to ``v1``.

    An operator's real-embedder index living in the same database must keep its
    vectors and its stamp — a bootstrap re-seed that blanked them would silently
    kill production's vector arm.
    """
    import db.seed.seed_catalog as seedmod

    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                IndexVersion(
                    index_version="v-op",
                    status=IndexStatus.candidate,
                    source_version_hash="sha256:op",
                    embedding_provider="openrouter",
                    embedding_model="openai/text-embedding-3-small",
                    embedding_dim=1536,
                    created_at=datetime.now(UTC),
                )
            )
            doc = Document(
                index_version="v-op",
                source_name="claude_code",
                product_area="claude_code",
                title="op",
                source_url="https://example.com",
                status=DocumentStatus.active,
                identity_checksum="c",
                last_fetched_at=datetime.now(UTC),
                last_indexed_at=datetime.now(UTC),
            )
            session.add(doc)
            await session.flush()
            session.add(
                Chunk(
                    document_id=doc.document_id,
                    product_area="claude_code",
                    section_path="s",
                    heading="s",
                    chunk_text="t",
                    context_summary="t",
                    exact_terms=[],
                    chunk_order=0,
                    content_checksum="c",
                    embedding=[0.5] + [0.0] * 1535,
                )
            )
            await session.commit()
        await engine.dispose()

        await seedmod.seed(url)

        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                op = await session.get(IndexVersion, "v-op")
                kept = (
                    (
                        await session.execute(
                            select(Chunk)
                            .join(Document, Document.document_id == Chunk.document_id)
                            .where(Document.index_version == "v-op")
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

    assert op is not None and op.embedding_provider == "openrouter"
    assert kept and all(c.embedding is not None for c in kept)


async def test_db_seed_real_provider_embeds_and_stamps_provenance(monkeypatch) -> None:
    """A real provider's identity is stamped so the Tier-3 read gate can check it.

    The contrast case for the stub test above: the stub strip is about the
    stub's meaninglessness, not about the bootstrap path disliking vectors. A
    real provider must ship a live, stamped arm.
    """
    import db.seed.seed_catalog as seedmod

    import app.worker.cli as cli

    real = Settings(
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        openrouter_api_key="or-k",
        _env_file=None,
    )
    monkeypatch.setattr(seedmod, "get_settings", lambda: real)
    monkeypatch.setattr(cli, "build_embedder", lambda _s: _FakeEmbedder())
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        await seedmod.seed(url)
        chunks, iv = await _read_rows(url)

    assert chunks and all(c.embedding is not None and len(c.embedding) == 1536 for c in chunks)
    assert (iv.embedding_provider, iv.embedding_model, iv.embedding_dim) == (
        "openrouter",
        "openai/text-embedding-3-small",
        1536,
    )


async def test_db_seed_reseed_replaces_chunks_instead_of_appending(monkeypatch) -> None:
    """Edge case + #162 regression: ``make seed`` twice must not double the corpus.

    ``deploy.sh`` re-runs the seed on every deploy, so a re-seed that appended
    would leave two copies of every chunk (the exact shape of #162) and the old
    wording would stay retrievable after a corpus correction.
    """
    import db.seed.seed_catalog as seedmod

    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        first = await seedmod.seed(url)
        second = await seedmod.seed(url)
        chunks, _ = await _read_rows(url)

    assert second["chunks"] == first["chunks"] == len(chunks)
    assert second["status"] == "already-active"


async def test_db_seed_failed_source_raises_and_leaves_v1_unpromoted(monkeypatch) -> None:
    """Failure path on a FRESH database: an incomplete corpus never becomes active.

    This is the real (narrow) guarantee: on a first-time bootstrap ``v1`` stays a
    candidate, so a broken corpus edit does not become the live index. It does
    NOT generalise to a re-seed — see
    :func:`test_db_seed_failed_source_on_an_active_index_is_already_live`.
    """
    import db.seed.seed_catalog as seedmod

    broken = SourceSpec(
        name="claude_code",
        product_area="claude_code",
        title="Claude Code Reference",
        fetcher="local",
        location="app/worker/sources/does_not_exist.md",
    )
    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(seedmod, "MVP_SOURCES", (broken,))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        with pytest.raises(seedmod.SeedError, match="incomplete"):
            await seedmod.seed(url)
        _, iv = await _read_rows(url)

    assert iv.status is IndexStatus.candidate
    assert iv.promoted_at is None


async def test_db_seed_failed_source_on_an_active_index_is_already_live(monkeypatch) -> None:
    """The documented failure-path guarantee, pinned to what it ACTUALLY is.

    The docstring used to claim that on a failed source "``v1`` is NOT activated
    ... so a broken corpus edit cannot go live". That is vacuous on the common
    case, and the review that found it was right: ``deploy.sh`` re-seeds an
    existing stack where ``v1`` is ALREADY active, and
    :func:`app.worker.cli.drive` commits each source as it goes. So the sources
    that succeeded are live the moment they commit — declining to promote an
    index that is already promoted changes nothing.

    This test pins the true behaviour so nobody re-derives the comfortable
    wrong one from the code: after a partial failure the succeeded source's NEW
    text is readable in the ACTIVE index. What the failure path does buy is
    asserted alongside: it raises, and it does not advance
    ``source_version_hash`` (so the answer cache is not re-keyed to a snapshot
    that was never fully built).
    """
    import db.seed.seed_catalog as seedmod

    import app.worker.cli as cli

    good = next(s for s in MVP_SOURCES if s.name == "codex")
    broken = SourceSpec(
        name="claude_code",
        product_area="claude_code",
        title="Claude Code Reference",
        fetcher="local",
        location="app/worker/sources/does_not_exist.md",
    )
    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        # First seed: healthy corpus, v1 becomes active — the deployed stack.
        # ``cli.MVP_SOURCES`` too: that is what the corpus fingerprint and
        # ``drive``'s "was this the WHOLE corpus?" check read.
        monkeypatch.setattr(seedmod, "MVP_SOURCES", (good,))
        monkeypatch.setattr(cli, "MVP_SOURCES", (good,))
        assert (await seedmod.seed(url))["status"] == "promoted"
        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                before_hash = (await session.get(IndexVersion, "v1")).source_version_hash  # type: ignore[union-attr]
        finally:
            await engine.dispose()

        # Second seed: one source now unreadable (the "broken corpus edit").
        monkeypatch.setattr(seedmod, "MVP_SOURCES", (good, broken))
        monkeypatch.setattr(cli, "MVP_SOURCES", (good, broken))
        with pytest.raises(seedmod.SeedError, match="incomplete"):
            await seedmod.seed(url)

        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                iv = await session.get(IndexVersion, "v1")
                live_docs = (
                    (await session.execute(select(Document).where(Document.index_version == "v1")))
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

    assert iv is not None
    # The half-refreshed corpus IS the live one: v1 never stopped being active.
    assert iv.status is IndexStatus.active
    assert {d.source_name for d in live_docs} == {"codex"}
    # ...but the failed run did not re-key the answer cache to it.
    assert iv.source_version_hash == before_hash


async def test_db_seed_failed_source_leaves_the_vector_arm_DEAD_not_nonsense(
    monkeypatch,
) -> None:
    """FAILURE path: a half-finished re-seed must not leave stub vectors LIVE either.

    This is the case that forced the design. ``drive`` COMMITS each source as it
    goes and, on a re-seed, ``v1`` is already ``active`` — so the sources that
    succeeded before the failure are visible to readers *immediately*, whatever
    the seeder does afterwards. Under a matching ``stub`` stamp,
    ``is_index_embedder_mismatch`` returns False, the vector arm is ENABLED, and
    retrieval ranks by SHA-256 hash distance while ``/health/index`` reports the
    index healthy. Strictly worse than a dead arm: a dead arm falls back to the
    lexical ones, a live-with-nonsense arm silently returns garbage rankings.

    An after-the-fact strip cannot close this (there is always a window between
    the per-source commit and the strip, and on the failure path the strip is
    racing an exception), which is why the bootstrap runs a
    :class:`~app.embeddings.null.NullEmbedder` and never writes the vectors at
    all. The assertion is unchanged and end-to-end: every chunk in ``v1`` is
    unembedded after a FAILED re-seed.
    """
    import db.seed.seed_catalog as seedmod

    import app.worker.cli as cli

    good = next(s for s in MVP_SOURCES if s.name == "codex")
    broken = SourceSpec(
        name="claude_code",
        product_area="claude_code",
        title="Claude Code Reference",
        fetcher="local",
        location="app/worker/sources/does_not_exist.md",
    )
    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        monkeypatch.setattr(seedmod, "MVP_SOURCES", (good,))
        monkeypatch.setattr(cli, "MVP_SOURCES", (good,))
        assert (await seedmod.seed(url))["status"] == "promoted"

        monkeypatch.setattr(seedmod, "MVP_SOURCES", (good, broken))
        monkeypatch.setattr(cli, "MVP_SOURCES", (good, broken))
        with pytest.raises(seedmod.SeedError, match="incomplete"):
            await seedmod.seed(url)

        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                # Chunk carries no index_version of its own; it reaches v1
                # through its Document.
                chunks = (
                    (
                        await session.execute(
                            select(Chunk)
                            .join(Document, Chunk.document_id == Document.document_id)
                            .where(Document.index_version == "v1")
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

    # Guard against a vacuous pass: if the failed re-seed left NO chunks at all,
    # "every embedding is NULL" would be trivially true and prove nothing.
    assert chunks, "no chunks survived the failed re-seed; the assertion below would be vacuous"
    live = [c for c in chunks if c.embedding is not None]
    assert not live, (
        f"{len(live)} of {len(chunks)} chunks kept stub vectors after a FAILED re-seed; "
        "the vector arm would rank by hash distance while /health/index reports healthy"
    )


async def test_db_seed_retires_a_pre_178_hand_written_catalog(monkeypatch) -> None:
    """Regression: upgrading an EXISTING database must not serve the corpus twice.

    Found by the pre-ship review. The old hand-written seeder wrote its five
    documents under ``source_name="docs.test"`` with fabricated
    ``https://docs.test/...`` URLs. The runner is idempotent on
    ``(source_name, index_version)``, so it does not recognise those rows and
    leaves them alone — meaning a re-seed after this change left ``v1`` holding
    the real corpus AND the stale copy, with bogus citation links, on every
    developer volume and every pre-#178 deploy. Verified failing before the
    ``_retire_orphans`` sweep was added (11 documents / 47 chunks).
    """
    import db.seed.seed_catalog as seedmod

    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            legacy_doc = Document(
                document_id=uuid.uuid4(),
                index_version=seedmod.INDEX_VERSION,
                source_name="docs.test",  # the pre-#178 seeder's constant
                product_area="claude_api",
                source_url="https://docs.test/claude",
                title="Claude API",
                identity_checksum="sha256:demo-claude_api",
                status=DocumentStatus.active,
                last_fetched_at=datetime.now(UTC),
                last_indexed_at=datetime.now(UTC),
            )
            session.add(legacy_doc)
            await session.flush()
            session.add(
                Chunk(
                    chunk_id=uuid.uuid4(),
                    document_id=legacy_doc.document_id,
                    product_area="claude_api",
                    section_path="/section",
                    heading="Rate limits",
                    chunk_text="The Claude API enforces a rate limit of 50 requests per minute.",
                    context_summary="Claude API rate limits",
                    chunk_order=1,
                    content_checksum="sha256:demo-chunk-claude_api",
                )
            )
            await session.commit()
        await engine.dispose()

        summary = await seedmod.seed(url)
        chunks, _ = await _read_rows(url)

        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                names = {
                    d.source_name for d in (await session.execute(select(Document))).scalars().all()
                }
        finally:
            await engine.dispose()

    assert summary["retired_documents"] == 1
    assert "docs.test" not in names
    assert names == {spec.name for spec in MVP_SOURCES}
    assert summary["documents"] == len(MVP_SOURCES)
    # The stale chunk and its fabricated citation URL are gone, not merely outvoted.
    assert not any("docs.test" in c.chunk_text for c in chunks)
    assert not any("50 requests per minute." in c.chunk_text for c in chunks)


async def test_db_seed_leaves_other_index_versions_alone(monkeypatch) -> None:
    """Edge case: the orphan sweep is scoped to ``v1``.

    An operator's worker index (``v-local``, ``v-candidate``, …) is not the
    bootstrap seeder's to clean up — deleting from it would destroy a build that
    is live or about to be promoted.
    """
    import db.seed.seed_catalog as seedmod

    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                IndexVersion(
                    index_version="v-operator",
                    status=IndexStatus.candidate,
                    source_version_hash="sha256:operator",
                    created_at=datetime.now(UTC),
                )
            )
            await session.flush()
            session.add(
                Document(
                    document_id=uuid.uuid4(),
                    index_version="v-operator",
                    source_name="some_future_source",
                    product_area="claude_api",
                    source_url="https://example.test/x",
                    title="Operator doc",
                    identity_checksum="sha256:x",
                    status=DocumentStatus.active,
                    last_fetched_at=datetime.now(UTC),
                    last_indexed_at=datetime.now(UTC),
                )
            )
            await session.commit()
        await engine.dispose()

        summary = await seedmod.seed(url)

        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                survivors = {
                    d.source_name
                    for d in (await session.execute(select(Document))).scalars().all()
                    if d.index_version == "v-operator"
                }
        finally:
            await engine.dispose()

    assert summary["retired_documents"] == 0
    assert survivors == {"some_future_source"}


async def test_db_seed_does_not_steal_active_from_a_promoted_index(monkeypatch) -> None:
    """Edge case: a re-seed must not demote an operator-promoted worker index.

    The production flow is ``citevyn-worker run`` into a fresh index version +
    an admin promote. ``deploy.sh`` runs the seed again on the next deploy; if
    that blindly activated ``v1`` it would silently roll the live index back to
    the bootstrap corpus.
    """
    import db.seed.seed_catalog as seedmod

    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_sqlite_url(fh)
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                IndexVersion(
                    index_version="v-operator",
                    status=IndexStatus.active,
                    source_version_hash="sha256:operator",
                    created_at=datetime.now(UTC),
                    promoted_at=datetime.now(UTC),
                )
            )
            await session.commit()
        await engine.dispose()

        summary = await seedmod.seed(url)

        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                rows = {
                    r.index_version: r.status
                    for r in (await session.execute(select(IndexVersion))).scalars().all()
                }
        finally:
            await engine.dispose()

    assert rows["v-operator"] is IndexStatus.active
    assert rows["v1"] is IndexStatus.candidate
    assert summary["status"].startswith("left-as-candidate")


# ---------------------------------------------------------------------------
# db/seed: never log the DB password to stdout (#93)
# ---------------------------------------------------------------------------

_SECRET_PW = "sup3r-s3cret-pw"  # noqa: S105 — test fixture, not a real credential
_URL_WITH_PW = f"postgresql+psycopg://citevyn:{_SECRET_PW}@db:5432/citevyn"


def test_redact_database_url_masks_password() -> None:
    from db.seed import redact_database_url

    redacted = redact_database_url(_URL_WITH_PW)
    assert _SECRET_PW not in redacted
    # Still useful for operators: driver, user, host, db survive.
    assert "postgresql+psycopg" in redacted
    assert "db:5432/citevyn" in redacted


def test_redact_database_url_never_echoes_unparseable_value() -> None:
    """A malformed URL must not be echoed verbatim — it could still hold a secret."""
    from db.seed import redact_database_url

    out = redact_database_url(f"::::not a url::::{_SECRET_PW}")
    assert _SECRET_PW not in out
    # Lock the exact placeholder so a future bug returning "" / None still fails.
    assert out == "<unparseable database url>"


def test_redact_database_url_empty_returns_placeholder() -> None:
    """An empty URL yields the placeholder, never a bare/echoed value."""
    from db.seed import redact_database_url

    assert redact_database_url("") == "<unparseable database url>"


def test_redact_database_url_percent_encoded_special_chars_masked() -> None:
    """The SUPPORTED form (special chars percent-encoded) masks cleanly."""
    from db.seed import redact_database_url

    # p@ss/word → p%40ss%2Fword (correctly encoded), single raw '@'.
    redacted = redact_database_url("postgresql+psycopg://u:p%40ss%2Fword@h:5432/d")
    assert "p%40ss%2Fword" not in redacted
    assert "p@ss/word" not in redacted
    assert "***" in redacted
    assert "h:5432/d" in redacted


def test_redact_database_url_raw_at_in_password_bails_to_placeholder() -> None:
    """A raw (unencoded) '@' in the password would make make_url mis-split and
    leak a fragment into the host — the >1-'@' guard bails to the placeholder."""
    from db.seed import redact_database_url

    leaky = "postgresql+psycopg://u:pa@ssword@h:5432/d"
    out = redact_database_url(leaky)
    assert out == "<unparseable database url>"
    assert "ssword" not in out


def test_seed_scripts_import_under_deploy_layout(tmp_path) -> None:
    """Regression (#93 review, F1): the deploy image runs ``python -m seed.seed_users``
    with ``PYTHONPATH=/db`` (package ``seed``, no top-level ``db``). A package-relative
    import must resolve there; an absolute ``from db.seed import`` raised
    ``ModuleNotFoundError: No module named 'db'`` and broke the prod seed step whose
    logs #93 set out to protect."""
    import shutil
    import subprocess

    repo_root = Path(__file__).resolve().parents[2]
    dst = tmp_path / "db"
    shutil.copytree(repo_root / "db", dst, ignore=shutil.ignore_patterns("__pycache__"))
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(dst), str(repo_root / "backend")])
    # Import both seed modules as the deploy image does (package ``seed``).
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import seed.seed_users, seed.seed_catalog; "
            "from seed import redact_database_url; "
            "print(redact_database_url('postgresql+psycopg://u:sekret@h:5432/d'))",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"deploy-layout import failed: {result.stderr}"
    assert "sekret" not in result.stdout
    assert "***" in result.stdout


def test_seed_scripts_import_under_repo_root_layout() -> None:
    """The repo-root / CI layout (``python -m db.seed.*``, package ``db.seed``)
    must also resolve the relative import."""
    import importlib

    for mod in ("db.seed.seed_users", "db.seed.seed_catalog"):
        assert importlib.import_module(mod) is not None


def test_seed_users_main_does_not_print_password(monkeypatch, capsys) -> None:
    """Regression (#93): ``seed_users`` success line must not leak the password.

    Synchronous by design: ``main`` calls ``asyncio.run`` internally, which
    cannot run inside a pytest-asyncio event loop.
    """
    import db.seed.seed_users as seedmod

    real = Settings(database_url=_URL_WITH_PW, _env_file=None)
    monkeypatch.setattr(seedmod, "get_settings", lambda: real)

    # Stub the DB work so the test stays hermetic (no real Postgres connection).
    async def _noop_seed(_url: str) -> None:
        return None

    monkeypatch.setattr(seedmod, "seed", _noop_seed)
    seedmod.main()
    out = capsys.readouterr().out
    assert _SECRET_PW not in out
    assert "db:5432/citevyn" in out


def test_seed_catalog_main_does_not_print_password(monkeypatch, capsys) -> None:
    """Regression (#93): ``seed_catalog`` success line must not leak the password.

    Synchronous by design: ``main`` calls ``asyncio.run`` internally.
    """
    import db.seed.seed_catalog as seedmod

    real = Settings(database_url=_URL_WITH_PW, _env_file=None)
    monkeypatch.setattr(seedmod, "get_settings", lambda: real)

    async def _fake_seed(_url: str) -> dict[str, int | str]:
        return {
            "sources": 0,
            "documents": 0,
            "chunks": 0,
            "exact_terms": 0,
            "retired_documents": 0,
            "index_version": "v1",
            "status": "promoted",
        }

    monkeypatch.setattr(seedmod, "seed", _fake_seed)
    seedmod.main()
    out = capsys.readouterr().out
    assert _SECRET_PW not in out
    assert "db:5432/citevyn" in out


async def test_db_seed_provider_switch_reembeds_all_and_never_stamps_stale(monkeypatch) -> None:
    """Review finding (silent-failure/data-safety): switching providers on an already
    embedded DB must RE-EMBED every chunk under the new provider before re-stamping —
    never leave provider-A vectors stamped as provider-B (a same-dim cross-space
    corruption the Tier-3 gate would miss).

    Since #178 the guarantee is structural rather than a special case: a re-seed
    runs the ingestion pipeline, which REPLACES a document's chunks, so every
    vector is necessarily produced by the currently configured embedder. This
    test pins the observable outcome so a future "skip unchanged documents"
    optimisation cannot silently reintroduce the cross-space corruption.
    """
    import db.seed.seed_catalog as seedmod

    import app.worker.cli as cli

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = f"sqlite+aiosqlite:///{fh.name}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

        # Seed under provider A = gemini (marker 1.0 in every vector; stamp gemini).
        settings_a = Settings(embedding_provider="gemini", gemini_api_key="gk", _env_file=None)
        monkeypatch.setattr(seedmod, "get_settings", lambda: settings_a)
        monkeypatch.setattr(cli, "build_embedder", lambda _s: _MarkerEmbedder(1.0))
        await seedmod.seed(url)

        # Switch to provider B = openrouter (marker 2.0; identity differs).
        settings_b = Settings(
            embedding_provider="openrouter",
            embedding_model="openai/text-embedding-3-small",
            openrouter_api_key="or-k",
            _env_file=None,
        )
        monkeypatch.setattr(seedmod, "get_settings", lambda: settings_b)
        monkeypatch.setattr(cli, "build_embedder", lambda _s: _MarkerEmbedder(2.0))
        await seedmod.seed(url)

        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                chunks = (await session.execute(select(Chunk))).scalars().all()
                iv = (await session.execute(select(IndexVersion))).scalars().one()
            # Vectors are now in B's space (marker 2.0), never left in A's (1.0)...
            assert all(c.embedding[0] == 2.0 for c in chunks)
            # ...and the stamp matches the vectors (B), never a stale/mismatched stamp.
            assert iv.embedding_provider == "openrouter"
        finally:
            await engine.dispose()
