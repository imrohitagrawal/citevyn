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

import os
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


async def _plant_v1_vector(url: str, index_version: str = "v1") -> None:
    """Give ``v1`` one active document whose single chunk has a stored embedding.

    ``claude_code`` is an allowlisted source, so the seed's replace path (not the
    orphan sweep) is what would erase it.
    """
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            IndexVersion(
                index_version=index_version,
                status=IndexStatus.active if index_version == "v1" else IndexStatus.candidate,
                source_version_hash="sha256:real",
                embedding_provider="gemini",
                embedding_model="gemini-embedding-001",
                embedding_dim=1536,
                created_at=datetime.now(UTC),
            )
        )
        doc = Document(
            index_version=index_version,
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

        with pytest.raises(seedmod.StoredVectorsError, match="--discard-vectors") as exc:
            await seedmod.seed(url)

        chunks = await _v1_chunks(url)
        jobs = await _job_count(url)

    message = str(exc.value)
    # Exactly this type: a refusal is not a failed source (SeedError's meaning).
    assert type(exc.value) is seedmod.StoredVectorsError
    assert not isinstance(exc.value, seedmod.SeedError)
    assert "1 chunk" in message and "v1" in message and "stub" in message
    # Names the setting, says a real provider re-embeds (paid), offers skipping, then
    # the flag; never promises to "keep" anything.
    assert "CITEVYN_EMBEDDING_PROVIDER" in message and "re-embed" in message
    assert "paid" in message
    assert message.index("skip") < message.index("--discard-vectors")
    assert "keep" not in message.lower()
    # Nothing was written: the planted chunk is the only one, embedding intact,
    # and no ingestion job ran.
    assert [c.chunk_text for c in chunks] == ["planted chunk"]
    assert chunks[0].embedding is not None and list(chunks[0].embedding) == _STORED
    assert jobs == 0


async def test_cli_stops_non_zero_with_the_message(monkeypatch, capsys) -> None:
    """Red when: ``main`` lets the refusal escape as a traceback, exits with anything
    but 3 (the refusal's own code; other seed failures stay 1), or its message stops
    naming the override flag."""
    import db.seed.seed_catalog as seedmod

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_url(fh)
        await _plant_v1_vector(url)
        monkeypatch.setattr(seedmod, "get_settings", lambda: _stub(url))

        with pytest.raises(SystemExit) as exc:
            await _run_main(seedmod, [])
        chunks = await _v1_chunks(url)

    assert exc.value.code == 3
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
        await _run_main(seedmod, [])  # a `make seed` re-run over a stub seed
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
    assert summary["discarded_vectors"] == 0
    assert all(c.embedding is not None for c in chunks)
    assert "planted chunk" not in {c.chunk_text for c in chunks}


async def test_override_announces_the_erase_before_writing(monkeypatch, capsys) -> None:
    """Red when: the "erasing" line moves after the ingest (or goes), so a run that
    fails part-way never said it had started erasing stored vectors."""
    import db.seed.seed_catalog as seedmod

    async def _failing_drive(*_a, **_k) -> int:
        return 1

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_url(fh)
        await _plant_v1_vector(url)
        monkeypatch.setattr(seedmod, "get_settings", lambda: _stub(url))
        monkeypatch.setattr(seedmod, "drive", _failing_drive)
        with pytest.raises(seedmod.SeedError):
            await seedmod.seed(url, discard_vectors=True)

    assert "--discard-vectors: erasing 1 stored vector(s) in v1" in capsys.readouterr().out


async def test_an_abbreviated_flag_is_refused_and_erases_nothing(monkeypatch) -> None:
    """Red when: argparse accepts prefixes again (``allow_abbrev``), so ``--disc``
    silently means ``--discard-vectors`` and erases."""
    import db.seed.seed_catalog as seedmod

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_url(fh)
        await _plant_v1_vector(url)
        monkeypatch.setattr(seedmod, "get_settings", lambda: _stub(url))
        with pytest.raises(SystemExit) as exc:
            await _run_main(seedmod, ["--disc"])
        chunks = await _v1_chunks(url)

    assert exc.value.code == 2
    assert [c.chunk_text for c in chunks] == ["planted chunk"]
    assert chunks[0].embedding is not None


async def test_main_reads_the_flag_from_the_real_command_line(monkeypatch, capsys) -> None:
    """Red when: ``main()`` called with no argument (as ``python -m`` does) stops
    reading ``sys.argv``, so the flag on the command line is ignored."""
    import db.seed.seed_catalog as seedmod

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_url(fh)
        await _plant_v1_vector(url)
        monkeypatch.setattr(seedmod, "get_settings", lambda: _stub(url))
        monkeypatch.setattr(sys, "argv", ["seed_catalog", "--discard-vectors"])
        await _run_main(seedmod, None)
        chunks = await _v1_chunks(url)

    assert chunks and all(c.embedding is None for c in chunks)
    assert "erased 1 stored vector" in capsys.readouterr().out


async def test_vectors_in_another_index_do_not_block_v1(monkeypatch) -> None:
    """Red when: the check stops filtering on ``v1`` and counts stored vectors in any
    index, so an operator's ``v2`` blocks the bootstrap seed it cannot be harmed by."""
    import db.seed.seed_catalog as seedmod

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        url = await _fresh_url(fh)
        await _plant_v1_vector(url, index_version="v2")
        monkeypatch.setattr(seedmod, "get_settings", lambda: _stub(url))
        summary = await seedmod.seed(url)

    assert summary["discarded_vectors"] == 0
    assert summary["chunks"] > len(MVP_SOURCES)


def test_make_seed_passes_catalog_args_to_the_catalog_seed_only() -> None:
    """Red when: ``SEED_CATALOG_ARGS`` stops reaching the ``seed_catalog`` line of
    ``make seed``, or leaks onto ``seed_users`` (which takes no such flag)."""
    import shutil
    import subprocess

    make = shutil.which("make")
    assert make, "this test needs make on PATH"
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MAKELEVEL"}}

    def dry(*extra: str) -> list[str]:
        out = subprocess.run(
            [make, "--no-print-directory", "-n", "seed", *extra],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
            check=True,
            timeout=60,
        ).stdout
        return [ln for ln in out.splitlines() if "python -m db.seed." in ln]

    lines = dry("SEED_CATALOG_ARGS=--discard-vectors")
    catalog = [ln for ln in lines if "db.seed.seed_catalog" in ln]
    users = [ln for ln in lines if "db.seed.seed_users" in ln]
    assert len(catalog) == 1 and len(users) == 1
    assert catalog[0].rstrip().endswith("db.seed.seed_catalog --discard-vectors")
    assert "--discard-vectors" not in users[0]
    # Partner: without the variable the catalog line carries no flag.
    assert all("--discard-vectors" not in ln for ln in dry())


async def _run_main(seedmod, argv: list[str] | None) -> None:
    """Run the synchronous CLI (it calls ``asyncio.run``) off this test's loop."""
    import asyncio

    await asyncio.to_thread(seedmod.main, argv)
