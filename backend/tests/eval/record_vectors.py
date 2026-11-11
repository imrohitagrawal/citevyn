"""Record the vectors the shipped-corpus eval reads (#601).

Run from ``backend/``. ``--dry-run`` lists what would be embedded, drives the real
question path over a capturing embedder, and needs no key, database or network::

    python -m tests.eval.record_vectors --dry-run

A real run spends only on what is missing: question texts not yet recorded, and chunks
whose text changed. Chunk vectors are exported for free from a local database that holds
the current index; embedding changed chunks is a paid step and needs its own flag::

    python -m tests.eval.record_vectors --export-chunks-from <local postgres URL>
    python -m tests.eval.record_vectors --embed-changed-chunks

The files are rewritten atomically after every embedded text, with sorted, stable lines,
so a crash loses nothing and a re-run resumes. The first failure stops the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import Settings, get_settings
from app.cost.call_site import CallSite, call_site
from app.embeddings import EmbedderUnavailable, build_embedder, metered_embedder
from app.embeddings.factory import EmbedderIdentity, configured_embedder_identity
from app.embeddings.protocol import Embedder
from app.models import Chunk, Document, IndexVersion
from app.models.enums import IndexStatus
from app.worker.allowlist import MVP_SOURCES
from app.worker.cli import content_version_hash

from .paths import RECORDED_CHUNKS_PATH, RECORDED_QUESTIONS_PATH
from .recorded import (
    SCHEMA_VERSION,
    TASK_TYPE_DOCUMENT,
    TASK_TYPE_QUERY,
    ChunkVector,
    Recording,
    ShippedEvalError,
    encode_vector,
    load_chunk_vectors,
    load_question_vectors,
    sha256_text,
)
from .shipped import (
    ShippedCase,
    all_shipped_cases,
    check_production_identity,
    known_question_texts,
    retrieve_routed,
    route_case,
    shipped_chunk_texts,
)

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


# --- Capturing the exact texts ------------------------------------------------------


class CapturingEmbedder:
    """Records what the real path asks to embed. It reaches no provider."""

    def __init__(self, dim: int) -> None:
        self._dim = dim
        self.texts: set[str] = set()

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        self.texts.add(text)
        return [0.0] * self._dim

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise ShippedEvalError("the question path must not embed documents")


class _EmptyResult:
    def all(self) -> list[Any]:
        return []

    def one(self) -> tuple[Any, ...]:
        return ()

    def mappings(self) -> _EmptyResult:
        return self


class _CaptureSession:
    """A Postgres-shaped session with no rows, so every question reaches the vector arm.

    An empty exact arm falls through to the vector arm, so the capture holds every text
    that could be embedded whatever the corpus contains.
    """

    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, _statement: object) -> _EmptyResult:
        return _EmptyResult()


async def capture_question_texts(cases: Iterable[ShippedCase], settings: Settings) -> list[str]:
    """Every exact text the real question path embeds for ``cases``, sorted."""
    embedder = CapturingEmbedder(settings.embedding_dim)
    session = cast("AsyncSession", _CaptureSession())
    for case in cases:
        await retrieve_routed(
            session,
            route_case(case, settings),
            embedder=embedder,
            identity=None,
            settings=settings,
        )
    return sorted(embedder.texts)


async def question_texts_to_record(settings: Settings) -> list[str]:
    """The captured texts, refused if the eval's own list has drifted from the real path."""
    captured = await capture_question_texts(all_shipped_cases(), settings)
    known = known_question_texts()
    if set(captured) != known:
        drifted = sorted(sha256_text(text) for text in set(captured) ^ known)
        raise ShippedEvalError(f"the known texts differ from the real path: {drifted}")
    return captured


# --- Files --------------------------------------------------------------------------


def question_header(identity: EmbedderIdentity) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "provider": identity.provider,
        "model": identity.model,
        "dim": identity.dim,
        "task_type": TASK_TYPE_QUERY,
    }


def chunk_header(identity: EmbedderIdentity, corpus_hash: str) -> dict[str, Any]:
    return {
        **question_header(identity),
        "task_type": TASK_TYPE_DOCUMENT,
        "corpus_hash": corpus_hash,
    }


def _line(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _write_atomic(path: pathlib.Path, lines: Iterable[str]) -> None:
    """Replace ``path`` in one step, so a crash leaves the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    os.replace(temporary, path)


def _write_questions(
    path: pathlib.Path, header: Mapping[str, Any], rows: Mapping[str, list[float]]
) -> None:
    _write_atomic(
        path,
        [
            _line(header),
            *(
                _line({"text_sha256": key, "vector": encode_vector(rows[key])})
                for key in sorted(rows)
            ),
        ],
    )


def _write_chunks(
    path: pathlib.Path, header: Mapping[str, Any], rows: Mapping[str, ChunkVector]
) -> None:
    _write_atomic(
        path,
        [
            _line(header),
            *(
                _line(
                    {
                        "chunk": key,
                        "content_checksum": rows[key].content_checksum,
                        "vector": encode_vector(rows[key].vector),
                    }
                )
                for key in sorted(rows)
            ),
        ],
    )


def _refuse_a_different_space(
    recording: Recording[Any], header: Mapping[str, Any], name: str
) -> None:
    """Vectors from two spaces must never share a file."""
    for field in ("schema", "provider", "model", "dim", "task_type"):
        if recording.header[field] != header[field]:
            raise ShippedEvalError(
                f"{name} was recorded with {field}={recording.header[field]!r}, "
                f"this run uses {header[field]!r}"
            )


def _checked(vector: list[float], header: Mapping[str, Any], *, name: str) -> list[float]:
    if len(vector) != header["dim"]:
        raise ShippedEvalError(f"{name}: got {len(vector)} values, expected {header['dim']}")
    return vector


# --- Recording ----------------------------------------------------------------------


async def record_questions(
    path: pathlib.Path,
    texts: Iterable[str],
    *,
    embedder: Embedder,
    header: Mapping[str, Any],
) -> int:
    """Embed every text not yet recorded; returns how many were embedded."""
    rows: dict[str, list[float]] = {}
    if path.is_file():
        recording = load_question_vectors(path)
        _refuse_a_different_space(recording, header, path.name)
        rows.update(recording.rows)
    embedded = 0
    for text in sorted(set(texts)):
        key = sha256_text(text)
        if key in rows:
            continue
        rows[key] = _checked(await embedder.embed(text), header, name=key)
        _write_questions(path, header, rows)
        embedded += 1
    return embedded


def chunks_still_needed(
    path: pathlib.Path,
    current: Mapping[str, str],
    exported: Mapping[str, ChunkVector],
) -> list[str]:
    """Chunk keys with no vector for their current text, in the file or the export."""
    recorded: Mapping[str, ChunkVector] = load_chunk_vectors(path).rows if path.is_file() else {}
    return sorted(
        key
        for key, checksum in current.items()
        if not any(
            key in rows and rows[key].content_checksum == checksum for rows in (recorded, exported)
        )
    )


async def record_chunks(
    path: pathlib.Path,
    texts: Mapping[str, str],
    *,
    header: Mapping[str, Any],
    exported: Mapping[str, ChunkVector],
    embedder: Embedder | None,
) -> int:
    """Bring the chunk file up to date; returns how many chunks were embedded.

    A row survives only while its checksum still matches the shipped text, so an edited
    or removed passage leaves. ``embedder=None`` forbids the paid step and stops, naming
    the chunks that need it.
    """
    current = {key: sha256_text(text) for key, text in texts.items()}
    rows: dict[str, ChunkVector] = {}
    if path.is_file():
        recording = load_chunk_vectors(path)
        _refuse_a_different_space(recording, header, path.name)
        rows.update(
            (key, row)
            for key, row in recording.rows.items()
            if current.get(key) == row.content_checksum
        )
    for key, row in exported.items():
        if key not in rows and current.get(key) == row.content_checksum:
            rows[key] = ChunkVector(row.content_checksum, _checked(row.vector, header, name=key))
    needed = sorted(key for key in current if key not in rows)
    _write_chunks(path, header, rows)
    if needed and embedder is None:
        raise ShippedEvalError(
            f"these chunks have no vector for their current text: {', '.join(needed)}. "
            "Embedding them is a provider call; re-run with --embed-changed-chunks."
        )
    if embedder is None:
        return 0
    for key in needed:
        vectors = await embedder.embed_documents([texts[key]])
        if len(vectors) != 1:
            raise ShippedEvalError(f"{key}: got {len(vectors)} vectors, expected 1")
        rows[key] = ChunkVector(current[key], _checked(vectors[0], header, name=key))
        _write_chunks(path, header, rows)
    return len(needed)


async def export_chunk_vectors(
    database_url: str, *, identity: EmbedderIdentity
) -> dict[str, ChunkVector]:
    """Read chunk vectors from the single active index of a local database.

    The URL must be given explicitly and must be local. The index's own stamp has to equal
    the configured embedder, or its vectors would be filed under the wrong space.
    """
    url = make_url(database_url)
    hidden = {"host", "hostaddr", "service"} & set(url.query)
    if hidden or (url.host is not None and url.host not in _LOCAL_HOSTS):
        raise ShippedEvalError("--export-chunks-from must name a local database")
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            active = (
                await conn.execute(
                    select(
                        IndexVersion.index_version,
                        IndexVersion.embedding_provider,
                        IndexVersion.embedding_model,
                        IndexVersion.embedding_dim,
                    ).where(IndexVersion.status == IndexStatus.active)
                )
            ).all()
            if len(active) != 1:
                raise ShippedEvalError(
                    f"the export database has {len(active)} active indexes; expected one"
                )
            version, *stamp = active[0]
            if EmbedderIdentity(*stamp) != identity:
                raise ShippedEvalError(
                    f"the active index is stamped {tuple(stamp)}; "
                    f"the configured embedder is {tuple(identity)}"
                )
            result = await conn.execute(
                select(
                    Document.source_name,
                    Chunk.heading,
                    Chunk.content_checksum,
                    Chunk.embedding,
                )
                .join(Document, Chunk.document_id == Document.document_id)
                .where(Document.index_version == version, Chunk.embedding.is_not(None))  # type: ignore[attr-defined]
            )
            rows: dict[str, ChunkVector] = {}
            for source, heading, checksum, embedding in result.all():
                key = f"{source}#{heading}"
                if key in rows:
                    raise ShippedEvalError(f"the export database has two chunks keyed {key!r}")
                rows[key] = ChunkVector(checksum, [float(value) for value in embedding])
            return rows
    finally:
        await engine.dispose()


# --- Command line -------------------------------------------------------------------


def _print_plan(texts: list[str], needed_chunks: list[str]) -> None:
    recorded: Mapping[str, Any] = (
        load_question_vectors(RECORDED_QUESTIONS_PATH).rows
        if RECORDED_QUESTIONS_PATH.is_file()
        else {}
    )
    missing = [text for text in texts if sha256_text(text) not in recorded]
    print(f"distinct question texts: {len(texts)}")
    print(f"already recorded: {len(texts) - len(missing)}; to embed: {len(missing)}")
    for text in missing:
        print(f"embed {sha256_text(text)} {json.dumps(text, ensure_ascii=False)}")
    print(f"chunks with no vector for their current text: {len(needed_chunks)}")
    for key in needed_chunks:
        print(f"chunk {key}")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    texts = await question_texts_to_record(settings)
    chunk_texts = shipped_chunk_texts()
    if args.dry_run:
        current = {key: sha256_text(text) for key, text in chunk_texts.items()}
        _print_plan(texts, chunks_still_needed(RECORDED_CHUNKS_PATH, current, {}))
        return 0
    if settings.embedding_provider == "stub":
        raise ShippedEvalError("recording needs a real embedder; the configured one is the stub")
    identity = configured_embedder_identity(settings)
    check_production_identity(question_header(identity))
    exported: dict[str, ChunkVector] = {}
    if args.export_chunks_from:
        exported = await export_chunk_vectors(args.export_chunks_from, identity=identity)
    embedder = metered_embedder(build_embedder(settings), settings)
    try:
        with call_site(CallSite.eval):
            chunks = await record_chunks(
                RECORDED_CHUNKS_PATH,
                chunk_texts,
                header=chunk_header(identity, content_version_hash(MVP_SOURCES)),
                exported=exported,
                embedder=embedder if args.embed_changed_chunks else None,
            )
            questions = await record_questions(
                RECORDED_QUESTIONS_PATH,
                texts,
                embedder=embedder,
                header=question_header(identity),
            )
    finally:
        aclose: Callable[[], Awaitable[None]] | None = getattr(embedder, "aclose", None)
        if aclose is not None:
            await aclose()
    print(f"embedded {questions} question texts and {chunks} chunks")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record shipped-corpus eval vectors.")
    parser.add_argument("--dry-run", action="store_true", help="list what would be embedded")
    parser.add_argument("--export-chunks-from", help="a local Postgres URL holding the index")
    parser.add_argument(
        "--embed-changed-chunks", action="store_true", help="allow the paid chunk embedding"
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (ShippedEvalError, EmbedderUnavailable) as exc:
        print(f"record_vectors stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
