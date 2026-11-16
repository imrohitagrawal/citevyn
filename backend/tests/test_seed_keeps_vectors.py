"""#626: a stub-embedder re-seed must not silently erase stored vectors.

``db/seed/seed_catalog.py`` re-ingests every shipped source into ``v1`` and the
runner replaces each document's chunks. Under the stub embedder the new chunks
carry no embedding, so a stub re-seed over a ``v1`` that holds real vectors
used to replace them all with NULL, with no warning. The seed now checks ``v1``
before its first write and stops, unless ``--discard-vectors`` is passed.

All hermetic, on SQLite (the ``embedding`` column stores a JSON list there):
local corpus files, the stub embedder or an in-process fake, no network.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models import Base, Chunk, Document, DocumentStatus, IndexStatus, IndexVersion
from app.worker.allowlist import MVP_SOURCES

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_STORED = [0.5] + [0.0] * 1535


class _FakeEmbedder:
    """Deterministic non-stub embedder: no I/O, a fixed vector per text."""

    dim = 1536

    async def embed(self, text: str) -> list[float]:
        return [0.25] + [0.0] * 1535

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.25] + [0.0] * 1535 for _ in texts]


async def _fresh_url(fh: tempfile._TemporaryFileWrapper[bytes]) -> str:
    url = f"sqlite+aiosqlite:///{fh.name}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return url


async def _plant_v1_vector(url: str) -> None:
    """Give ``v1`` one active document whose single chunk has a stored embedding.

    ``claude_code`` is an allowlisted source, so the seed's replace path (not the
    orphan sweep) is what would erase it.
    """
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            IndexVersion(
                index_version="v1",
                status=IndexStatus.active,
                source_version_hash="sha256:real",
                embedding_provider="gemini",
                embedding_model="gemini-embedding-001",
                embedding_dim=1536,
                created_at=datetime.now(UTC),
            )
        )
        doc = Document(
            index_version="v1",
            source_name="claude_code",
            product_area="claude_code",
            title="planted",
            source_url="https://example.com/planted",
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
                chunk_text="planted chunk",
                context_summary="planted chunk",
                exact_terms=[],
                chunk_order=0,
                content_checksum="c",
                embedding=_STORED,
            )
        )
        await session.commit()
    await engine.dispose()


async def _v1_chunks(url: str) -> list[Chunk]:
    engine = create_async_engine(url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            return list(
                (
                    await session.execute(
                        select(Chunk)
                        .join(Document, Document.document_id == Chunk.document_id)
                        .where(Document.index_version == "v1")
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()


async def _job_count(url: str) -> int:
    from app.models import IngestionJob

    engine = create_async_engine(url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            return int(await session.scalar(select(func.count()).select_from(IngestionJob)) or 0)
    finally:
        await engine.dispose()


def _stub(url: str) -> Settings:
    return Settings(database_url=url, _env_file=None)


async def test_stub_reseed_over_stored_vectors_stops_and_keeps_them(monkeypatch) -> None:
    """Red when: the seed no longer checks ``v1`` for stored vectors before its first
    write under the stub embedder (the planted embedding is replaced with NULL and
    nothing is raised), or the check moves after the ingest."""
    import db.seed.seed_catalog as seedmod

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_url(fh)
        await _plant_v1_vector(url)
        monkeypatch.setattr(seedmod, "get_settings", lambda: _stub(url))

        with pytest.raises(seedmod.SeedError, match="--discard-vectors") as exc:
            await seedmod.seed(url)

        chunks = await _v1_chunks(url)
        jobs = await _job_count(url)

    message = str(exc.value)
    assert "1 chunk" in message and "v1" in message and "stub" in message
    # Nothing was written: the planted chunk is the only one, embedding intact,
    # and no ingestion job ran.
    assert [c.chunk_text for c in chunks] == ["planted chunk"]
    assert chunks[0].embedding is not None and list(chunks[0].embedding) == _STORED
    assert jobs == 0


async def test_cli_stops_non_zero_with_the_message(monkeypatch, capsys) -> None:
    """Red when: ``main`` lets the refusal escape as a traceback or exits 0, or its
    message stops naming the override flag."""
    import db.seed.seed_catalog as seedmod

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_url(fh)
        await _plant_v1_vector(url)
        monkeypatch.setattr(seedmod, "get_settings", lambda: _stub(url))

        with pytest.raises(SystemExit) as exc:
            await _run_main(seedmod, [])
        chunks = await _v1_chunks(url)

    assert exc.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "--discard-vectors" in err and "would erase" in err
    assert chunks[0].embedding is not None


async def test_fresh_database_still_seeds_under_the_stub(monkeypatch, capsys) -> None:
    """Partner (CI and ``make demo`` seed fresh databases). Red when: the check
    blocks a database with no stored vectors, e.g. counts chunks instead of
    non-NULL embeddings, so a fresh seed or a stub re-seed of a stub seed stops."""
    import db.seed.seed_catalog as seedmod

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_url(fh)
        monkeypatch.setattr(seedmod, "get_settings", lambda: _stub(url))

        await _run_main(seedmod, [])
        first = await _v1_chunks(url)
        await _run_main(seedmod, [])  # stub re-seed over a stub seed (deploy-style)
        second = await _v1_chunks(url)

    out = capsys.readouterr().out
    assert first and len(first) > len(MVP_SOURCES)
    assert all(c.embedding is None for c in first + second)
    assert len(second) == len(first)
    assert f"chunks={len(first)}" in out
    assert "discard" not in out.lower()


async def test_override_erases_as_before_and_says_so(monkeypatch, capsys) -> None:
    """Red when: ``--discard-vectors`` stops allowing the erase, or the run stops
    printing that it erased stored vectors."""
    import db.seed.seed_catalog as seedmod

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_url(fh)
        await _plant_v1_vector(url)
        monkeypatch.setattr(seedmod, "get_settings", lambda: _stub(url))

        await _run_main(seedmod, ["--discard-vectors"])
        chunks = await _v1_chunks(url)

    out = capsys.readouterr().out
    assert chunks and all(c.embedding is None for c in chunks)
    assert "planted chunk" not in {c.chunk_text for c in chunks}
    assert "--discard-vectors" in out and "erased 1 stored vector" in out


async def test_real_embedder_is_not_blocked(monkeypatch) -> None:
    """Red when: the check ignores which embedder is configured and blocks a real
    one too (a real re-seed replaces vectors with new vectors, nothing is lost)."""
    import db.seed.seed_catalog as seedmod

    import app.worker.cli as cli

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_url(fh)
        await _plant_v1_vector(url)
        real = Settings(
            database_url=url,
            embedding_provider="openrouter",
            embedding_model="openai/text-embedding-3-small",
            openrouter_api_key="or-k",
            _env_file=None,
        )
        monkeypatch.setattr(seedmod, "get_settings", lambda: real)
        monkeypatch.setattr(cli, "build_embedder", lambda _s: _FakeEmbedder())

        summary = await seedmod.seed(url)
        chunks = await _v1_chunks(url)

    assert summary["chunks"] == len(chunks) > len(MVP_SOURCES)
    assert all(c.embedding is not None for c in chunks)
    assert "planted chunk" not in {c.chunk_text for c in chunks}


async def _run_main(seedmod, argv: list[str]) -> None:
    """Run the synchronous CLI (it calls ``asyncio.run``) off this test's loop."""
    import asyncio

    await asyncio.to_thread(seedmod.main, argv)
