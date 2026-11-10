"""#601: the shipped-corpus seam on real pgvector (``postgres`` job).

Each test gets its own migrated schema, and the database fixtures run before the module
under test is imported, so a red run here is a red run with a real database behind it.

The vectors are synthetic. Each of the 44 real chunks gets a stable pseudo-random vector
derived from its checksum, and a question recorded with a chunk's vector ranks that chunk
first by cosine. That proves the mechanism and calls no provider; the real recording is
a separate committed fixture. The contract is in ``test_eval_shipped_fixture.py``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import random
import uuid
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
from alembic.command import upgrade as alembic_upgrade
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.embeddings.factory import EmbedderIdentity
from app.models import Chunk, Document, ExactTerm, IndexVersion, IngestionJob
from app.models.enums import IndexStatus, JobStatus
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.types import VectorDegrade
from app.routing.intent import Intent
from app.services.index_resolution import ActiveIndexState, resolve_active_index
from app.worker.allowlist import MVP_SOURCES
from app.worker.chunker import chunk_document
from app.worker.parser import parse_markdown
from app.worker.runner import IngestionRunner, RunResult
from tests.conftest import seed_catalog
from tests.test_eval_shipped_fixture import (
    CHUNK_HEADER,
    QUESTION_HEADER,
    sha256_text,
    write_chunk_file,
    write_question_file,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("CITEVYN_PG_TEST_URL") is None and get_settings().pg_test_url is None,
        reason="Set CITEVYN_PG_TEST_URL to a postgresql+psycopg:// URL.",
    ),
]

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
DIM = 1536
SHIPPED_PASSAGES = 44
SHIPPED_DOCUMENTS = 6
INDEX_IDENTITY = EmbedderIdentity("gemini", "gemini-embedding-001", DIM)

SANDBOX_KEY = "codex#CLI sandbox"
SCOPED_QUESTION = "How does the Codex sandbox work?"
GLOBAL_QUESTION = "Please explain that three letter acronym for text systems"
GLOBAL_KEY = "concepts#What a large language model (LLM) is"
MULTI_QUESTION = "Compare how streaming works in the Claude API and the Gemini API."
MULTI_KEYS = ("claude_api#Streaming", "gemini_api#Streaming responses")


def _shipped() -> ModuleType:
    """The module under test. It does not exist until the builder writes it."""
    return importlib.import_module("tests.eval.shipped")


def _pg_url() -> str:
    url = os.environ.get("CITEVYN_PG_TEST_URL") or get_settings().pg_test_url
    assert url is not None
    return url


def _url_with_schema(schema: str) -> str:
    base = _pg_url()
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}options=-c search_path={schema},public"


@pytest.fixture
def pg_schema() -> Iterator[str]:
    """A migrated schema of this test's own, dropped afterwards."""
    schema = f"citevyn_shipped_{uuid.uuid4().hex[:8]}"
    engine = create_engine(_pg_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f"CREATE SCHEMA {schema}"))
        cfg = AlembicConfig(str(REPO_ROOT / "db" / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "db"))
        cfg.set_main_option("sqlalchemy.url", _url_with_schema(schema))
        cfg.set_main_option("version_table_schema", schema)
        alembic_upgrade(cfg, "head")
        yield schema
    finally:
        with engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        engine.dispose()


@pytest.fixture
def settings(pg_schema: str) -> Any:
    return get_settings().model_copy(
        update={"database_url": _url_with_schema(pg_schema), "environment": "test"}
    )


def _shipped_chunks() -> dict[str, str]:
    """``source#heading`` to checksum, computed without the module under test."""
    chunks: dict[str, str] = {}
    for spec in MVP_SOURCES:
        parsed = parse_markdown((BACKEND / spec.location).read_text(encoding="utf-8"))
        for draft in chunk_document(parsed, source=spec):
            chunks[f"{spec.name}#{draft.heading}"] = sha256_text(draft.text)
    return chunks


def _vector_for(seed: str) -> list[float]:
    rng = random.Random(hashlib.sha256(seed.encode("utf-8")).digest())
    return [rng.uniform(-1.0, 1.0) for _ in range(DIM)]


def _embedder(
    tmp_path: pathlib.Path,
    *,
    chunk_header: dict[str, Any] | None = None,
    question_header: dict[str, Any] | None = None,
    questions: dict[str, list[float]] | None = None,
) -> Any:
    """A ``RecordedEmbedder`` over synthetic vectors for all 44 real chunks."""
    shipped = _shipped()
    current = _shipped_chunks()
    chunk_path = write_chunk_file(
        tmp_path / "chunks.jsonl",
        {key: (checksum, _vector_for(checksum)) for key, checksum in current.items()},
        header={**CHUNK_HEADER, "dim": DIM, **(chunk_header or {})},
    )
    sandbox_vector = _vector_for(current[SANDBOX_KEY])
    question_vectors = {
        SCOPED_QUESTION: sandbox_vector,
        GLOBAL_QUESTION: _vector_for(current[GLOBAL_KEY]),
        MULTI_QUESTION: sandbox_vector,
        **(questions or {}),
    }
    question_path = write_question_file(
        tmp_path / "questions.jsonl",
        question_vectors,
        header={**QUESTION_HEADER, "dim": DIM, **(question_header or {})},
    )
    return shipped.RecordedEmbedder(
        chunks=shipped.load_chunk_vectors(chunk_path),
        questions=shipped.load_question_vectors(question_path),
    )


async def _catalog_counts(settings: Any) -> dict[str, int]:
    """Row counts read over a separate connection, so only committed rows are visible."""
    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine)
        async with factory() as session:
            return {
                model.__tablename__: int(
                    await session.scalar(select(func.count()).select_from(model)) or 0
                )
                for model in (Document, Chunk, ExactTerm, IndexVersion, IngestionJob)
            }
    finally:
        await engine.dispose()


async def _count(session: AsyncSession, model: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def _retriever(session: AsyncSession, embedder: Any, identity: EmbedderIdentity):
    return HybridRetriever(session, embedder=embedder, embedder_identity=identity)


async def _scoped(retriever: HybridRetriever) -> Any:
    return await retriever.retrieve(
        SCOPED_QUESTION,
        product_area="codex",
        intent=Intent.how_to,
        limit=20,
        top_k=6,
    )


async def test_rollback_leaves_zero_catalog_residue(settings, tmp_path) -> None:
    """Red when: the seam commits for real, so ingestion survives its rollback."""
    embedder = _embedder(tmp_path)
    async with _shipped().shipped_corpus_session(settings, embedder, embedder.query_identity) as (
        session,
        yielded,
    ):
        # Partner to the zeroes below: inside the seam the corpus really is there.
        assert yielded is embedder
        assert await _count(session, Chunk) == SHIPPED_PASSAGES
        assert await _count(session, Document) == SHIPPED_DOCUMENTS
        assert await _count(session, IndexVersion) == 1
        assert await _count(session, IngestionJob) == SHIPPED_DOCUMENTS
        assert await _count(session, ExactTerm) > 0
    assert await _catalog_counts(settings) == {
        "documents": 0,
        "chunks": 0,
        "exact_terms": 0,
        "index_versions": 0,
        "ingestion_jobs": 0,
    }


async def test_the_seam_uses_the_real_runner_so_exact_terms_are_real(settings, tmp_path) -> None:
    """Red when: the seam bulk-loads chunks and skips the real extractor."""
    embedder = _embedder(tmp_path)
    async with _shipped().shipped_corpus_session(settings, embedder, embedder.query_identity) as (
        session,
        _,
    ):
        terms = set((await session.scalars(select(ExactTerm.term_text))).all())
        assert {"--model", "OPENAI_API_KEY", "x-goog-api-key", "/compact"} <= terms
        statuses = set((await session.scalars(select(IngestionJob.status))).all())
        assert statuses == {JobStatus.completed}


async def test_one_failed_source_stops_the_seam_and_names_it(
    settings, tmp_path, monkeypatch
) -> None:
    """Red when: the seam stops checking each ``RunResult``; a corpus could run short."""
    original = IngestionRunner.run

    async def fail_codex(self: IngestionRunner, session: AsyncSession, *, source: Any) -> RunResult:
        result = await original(self, session, source=source)
        if source.name != "codex":
            return result
        return RunResult(
            job_id=result.job_id,
            source_name=source.name,
            status=JobStatus.failed,
            error_type="Boom",
            error_message="injected",
        )

    monkeypatch.setattr(IngestionRunner, "run", fail_codex)
    embedder = _embedder(tmp_path)
    shipped = _shipped()
    with pytest.raises(shipped.ShippedEvalError, match="codex"):
        async with shipped.shipped_corpus_session(settings, embedder, embedder.query_identity):
            pytest.fail("the seam yielded over a failed source")
    assert (await _catalog_counts(settings))["chunks"] == 0


async def test_a_non_empty_catalog_is_refused(settings, tmp_path) -> None:
    """Red when: the seam's non-empty catalog refusal is removed."""
    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            await seed_catalog(session, index_version="pre-existing")
    finally:
        await engine.dispose()
    # Partner: the refusal below is about a catalog that really holds rows.
    assert (await _catalog_counts(settings))["chunks"] > 0
    embedder = _embedder(tmp_path)
    shipped = _shipped()
    with pytest.raises(shipped.ShippedEvalError, match="not empty"):
        async with shipped.shipped_corpus_session(settings, embedder, embedder.query_identity):
            pytest.fail("the seam yielded over a non-empty catalog")


async def test_the_promoted_index_is_the_one_the_gate_reads(settings, tmp_path) -> None:
    """Red when: the index stays candidate; its stamp would read None and open the gate."""
    embedder = _embedder(tmp_path)
    async with _shipped().shipped_corpus_session(settings, embedder, embedder.query_identity) as (
        session,
        _,
    ):
        resolution = await resolve_active_index(session)
        assert resolution.state is ActiveIndexState.one
        row = (await session.execute(select(IndexVersion))).scalars().one()
        assert row.status is IndexStatus.active
        assert (row.embedding_provider, row.embedding_model, row.embedding_dim) == INDEX_IDENTITY
        stamp = await _retriever(session, embedder, embedder.query_identity)._active_index_stamp()
        assert stamp == INDEX_IDENTITY


async def test_an_altered_query_header_yields_mismatch_and_a_clean_one_does_not(
    settings, tmp_path
) -> None:
    """Red when: the seam stamps the index from the query header, so a swap goes unseen."""
    altered = _embedder(tmp_path, question_header={"model": "some-other-model"})
    identity = altered.query_identity
    assert identity.model == "some-other-model"
    async with _shipped().shipped_corpus_session(settings, altered, identity) as (session, _):
        mismatched = await _scoped(_retriever(session, altered, identity))
        assert mismatched.vector_degrade is VectorDegrade.mismatch
        # Partner: the same corpus and question under the index's own stamp run clean,
        # concurrently across all three arms, and do reach the recorded chunk.
        clean = await _scoped(_retriever(session, altered, altered.index_identity))
        assert clean.vector_degrade is VectorDegrade.none
        assert clean.hits[0].heading == "CLI sandbox"


async def test_two_active_indexes_yield_ambiguous_index(settings, tmp_path) -> None:
    """Red when: a dual-active catalog stops failing closed on the seam's path."""
    embedder = _embedder(tmp_path)
    async with _shipped().shipped_corpus_session(settings, embedder, embedder.query_identity) as (
        session,
        _,
    ):
        before = await _scoped(_retriever(session, embedder, embedder.query_identity))
        assert before.vector_degrade is VectorDegrade.none
        row = (await session.execute(select(IndexVersion))).scalars().one()
        session.add(
            IndexVersion(
                index_version="second-active",
                status=IndexStatus.active,
                source_version_hash=row.source_version_hash,
                created_at=row.created_at,
                embedding_provider=row.embedding_provider,
                embedding_model=row.embedding_model,
                embedding_dim=row.embedding_dim,
            )
        )
        await session.flush()
        after = await _scoped(_retriever(session, embedder, embedder.query_identity))
        assert after.vector_degrade is VectorDegrade.ambiguous_index


async def test_a_missing_question_text_is_not_swallowed_on_the_real_path(
    settings, tmp_path
) -> None:
    """Red when: an unrecorded question degrades to keyword search instead of raising."""
    embedder = _embedder(tmp_path)
    shipped = _shipped()
    async with shipped.shipped_corpus_session(settings, embedder, embedder.query_identity) as (
        session,
        _,
    ):
        with pytest.raises(shipped.MissingRecordedVector):
            await _retriever(session, embedder, embedder.query_identity).retrieve(
                "A question nobody recorded about the Codex sandbox",
                product_area="codex",
                intent=Intent.how_to,
                limit=20,
                top_k=6,
            )


async def test_each_outcome_carries_its_route_and_winning_arm(settings, tmp_path) -> None:
    """Red when: the evaluator mislabels a route, so #370 and #553 cannot be told apart."""
    shipped = _shipped()
    current = _shipped_chunks()
    multi_vector = _vector_for(current[MULTI_KEYS[0]])
    embedder = _embedder(tmp_path, questions={MULTI_QUESTION: multi_vector})
    questions_path = tmp_path / "shipped.jsonl"
    rows = [
        (SCOPED_QUESTION, "scoped_case", [SANDBOX_KEY]),
        (GLOBAL_QUESTION, "global_case", [GLOBAL_KEY]),
        (MULTI_QUESTION, "multi_case", list(MULTI_KEYS)),
    ]
    questions_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": case_id,
                    "question": question,
                    "coverage_kind": "hybrid",
                    "shipped_gold": gold,
                }
            )
            for question, case_id, gold in rows
        )
        + "\n",
        encoding="utf-8",
    )
    cases = shipped.load_shipped_questions(questions_path, current)
    async with shipped.shipped_corpus_session(settings, embedder, embedder.query_identity) as (
        session,
        _,
    ):
        outcomes = await shipped.evaluate_cases(
            session, embedder, embedder.query_identity, cases, settings
        )
    by_id = {outcome.case_id: outcome for outcome in outcomes}
    assert len(by_id) == 3
    assert by_id["scoped_case"].route == "scoped"
    assert by_id["scoped_case"].retrieved[0] == SANDBOX_KEY
    assert by_id["scoped_case"].winning_arm in {"exact", "keyword", "vector", "hybrid"}
    assert by_id["global_case"].route == "global_vector_only"
    assert by_id["global_case"].retrieved[0] == GLOBAL_KEY
    assert by_id["global_case"].winning_arm == "vector"
    assert by_id["multi_case"].route == "multi_product"
    assert MULTI_KEYS[0] in by_id["multi_case"].retrieved
    # The vector is the Claude chunk's, so only area coverage is promised for Gemini.
    assert {"claude_api", "gemini_api"} <= {
        key.split("#")[0] for key in by_id["multi_case"].retrieved
    }
    assert all(outcome.degrade is VectorDegrade.none for outcome in outcomes)
    report = shipped.build_report(outcomes)
    assert report["by_coverage_kind"]["hybrid"]["n"] == 3
