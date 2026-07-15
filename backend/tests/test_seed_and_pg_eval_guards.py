"""Hermetic tests for the Phase-1 embedding-aware seeder + Postgres-eval guards.

All hermetic: a fake in-process embedder (no network) exercises the seeder's new
``embedder``/``embedder_identity``/``commit`` behaviour on SQLite, and the
``postgres_session`` safety guards are checked on the paths that raise BEFORE any
DB connection (production / non-Postgres URL / stub embedder). The real
Postgres+pgvector numbers are proven separately by the opt-in ``--postgres`` runner
and ``test_eval_semantic_discrimination`` (both make real API calls).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.embeddings.factory import EmbedderIdentity
from app.models import Base, Chunk, IndexVersion
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
# db/seed/seed_catalog: embedding backfill (hermetic, SQLite + fake embedder)
# ---------------------------------------------------------------------------


async def test_db_seed_stub_provider_leaves_embeddings_null(monkeypatch) -> None:
    """Default (stub) provider: `make seed` embeds nothing (no key, no cost)."""
    import db.seed.seed_catalog as seedmod

    monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = f"sqlite+aiosqlite:///{fh.name}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()
        tally = await seedmod.seed(url)
        assert tally["embedded"] == 0
        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                chunks = (await session.execute(select(Chunk))).scalars().all()
            assert chunks and all(c.embedding is None for c in chunks)
        finally:
            await engine.dispose()


async def test_db_seed_backfill_populates_and_stamps(monkeypatch) -> None:
    """A real provider backfills every NULL chunk vector and stamps provenance."""
    import db.seed.seed_catalog as seedmod

    real = Settings(
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        openrouter_api_key="or-k",
        _env_file=None,
    )
    monkeypatch.setattr(seedmod, "get_settings", lambda: real)
    monkeypatch.setattr(seedmod, "build_embedder", lambda _s: _FakeEmbedder())
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = f"sqlite+aiosqlite:///{fh.name}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()
        tally = await seedmod.seed(url)
        assert tally["embedded"] == tally["chunks"] == 5
        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                chunks = (await session.execute(select(Chunk))).scalars().all()
                iv = (await session.execute(select(IndexVersion))).scalars().one()
            assert chunks and all(
                c.embedding is not None and len(c.embedding) == 1536 for c in chunks
            )
            assert (iv.embedding_provider, iv.embedding_model, iv.embedding_dim) == (
                "openrouter",
                "openai/text-embedding-3-small",
                1536,
            )
        finally:
            await engine.dispose()


async def test_db_seed_backfill_reseed_populates_preexisting_null_chunks(monkeypatch) -> None:
    """The review's footgun: seed under stub (NULL), then re-seed under a real
    provider must backfill the ALREADY-present chunks, not skip them."""
    import db.seed.seed_catalog as seedmod

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = f"sqlite+aiosqlite:///{fh.name}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

        # First seed: stub provider → NULL vectors.
        monkeypatch.setattr(seedmod, "get_settings", lambda: Settings(_env_file=None))
        await seedmod.seed(url)

        # Re-seed: real provider → the pre-existing NULL chunks get backfilled.
        real = Settings(
            embedding_provider="openrouter",
            embedding_model="openai/text-embedding-3-small",
            openrouter_api_key="or-k",
            _env_file=None,
        )
        monkeypatch.setattr(seedmod, "get_settings", lambda: real)
        monkeypatch.setattr(seedmod, "build_embedder", lambda _s: _FakeEmbedder())
        tally = await seedmod.seed(url)
        assert tally["chunks"] == 0  # no new chunks
        assert tally["embedded"] == 5  # but all 5 pre-existing ones backfilled

        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                chunks = (await session.execute(select(Chunk))).scalars().all()
            assert all(c.embedding is not None for c in chunks)
        finally:
            await engine.dispose()


async def test_db_seed_provider_switch_reembeds_all_and_never_stamps_stale(monkeypatch) -> None:
    """Review finding (silent-failure/data-safety): switching providers on an already
    embedded DB must RE-EMBED every chunk under the new provider before re-stamping —
    never leave provider-A vectors stamped as provider-B (a same-dim cross-space
    corruption the Tier-3 gate would miss)."""
    import db.seed.seed_catalog as seedmod

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = f"sqlite+aiosqlite:///{fh.name}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

        # Seed under provider A = gemini (marker 1.0 in every vector; stamp gemini).
        settings_a = Settings(embedding_provider="gemini", gemini_api_key="gk", _env_file=None)
        monkeypatch.setattr(seedmod, "get_settings", lambda: settings_a)
        monkeypatch.setattr(seedmod, "build_embedder", lambda _s: _MarkerEmbedder(1.0))
        await seedmod.seed(url)

        # Switch to provider B = openrouter (marker 2.0; identity differs).
        settings_b = Settings(
            embedding_provider="openrouter",
            embedding_model="openai/text-embedding-3-small",
            openrouter_api_key="or-k",
            _env_file=None,
        )
        monkeypatch.setattr(seedmod, "get_settings", lambda: settings_b)
        monkeypatch.setattr(seedmod, "build_embedder", lambda _s: _MarkerEmbedder(2.0))
        tally = await seedmod.seed(url)

        # ALL chunks re-embedded (not skipped as "already non-NULL").
        assert tally["embedded"] == 5
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
