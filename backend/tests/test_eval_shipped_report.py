"""#601: the shipped-corpus report over the committed recording (``postgres`` job).

This is what puts the number in CI: it runs every question on real pgvector with the
recorded vectors, and appends the report to ``$GITHUB_STEP_SUMMARY``. There are no floors
here on purpose; the partners below only prove the report measured something.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.embeddings.factory import EmbedderIdentity
from app.embeddings.stub import StubEmbedder
from app.models import Chunk
from tests.conftest import seed_catalog
from tests.eval import record_vectors, shipped
from tests.eval.paths import RECORDED_CHUNKS_PATH, RECORDED_QUESTIONS_PATH, SHIPPED_REPORT_PATH
from tests.eval.recorded import ShippedEvalError
from tests.test_eval_shipped_pg import (  # noqa: F401
    pg_schema,  # pyright: ignore[reportUnusedImport]
    settings,  # pyright: ignore[reportUnusedImport]
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("CITEVYN_PG_TEST_URL") is None and get_settings().pg_test_url is None,
        reason="Set CITEVYN_PG_TEST_URL to a postgresql+psycopg:// URL.",
    ),
]


def _recorded() -> Any:
    chunks = shipped.load_chunk_vectors(RECORDED_CHUNKS_PATH)
    questions = shipped.load_question_vectors(RECORDED_QUESTIONS_PATH)
    shipped.check_production_identity(chunks.header, questions.header)
    assert shipped.check_chunk_vectors(chunks, shipped.shipped_heading_keys()) == 44
    return shipped.RecordedEmbedder(chunks=chunks, questions=questions)


async def test_the_report_runs_on_the_real_recording_and_reaches_the_summary(
    settings: Any,  # noqa: F811
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the recording goes stale, a question degrades, or the report stops printing.

    Also red when the arm attribution is not measured by removing each arm, or the summary
    and JSON written are not this run's.
    """
    embedder = _recorded()
    cases = shipped.all_shipped_cases()
    async with shipped.shipped_corpus_session(settings, embedder, embedder.query_identity) as (
        session,
        _,
    ):
        outcomes = await shipped.evaluate_cases(
            session, embedder, embedder.query_identity, cases, settings
        )
    report = shipped.build_report(outcomes)
    # Partners: the report covers every question and the decision pool holds some.
    assert len(report["questions"]) == len(cases) >= 120
    decision = report["decision_a"]
    assert decision["pool"] > 0
    assert decision["precision_at_1"] is not None
    assert 0 < decision["distinct_passages"] <= decision["pool"]
    for row in report["questions"]:
        assert row["arm"] in {"vector", "keyword", "exact", "both", "none", None}, row["id"]
        assert "rank_vector_only" in row and "rank_keyword_only" in row
    # Removing the vector arm changes rank 1 somewhere: the removal really ran.
    assert any(row["arm"] in {"vector", "both"} for row in report["questions"])
    assert report["arm_attribution"]["total"] == len(cases)

    real_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    SHIPPED_REPORT_PATH.unlink(missing_ok=True)
    shipped.write_report(
        report, SHIPPED_REPORT_PATH, pathlib.Path(os.environ["GITHUB_STEP_SUMMARY"])
    )
    written = summary.read_text(encoding="utf-8")
    assert (
        f"**Decision A**: precision@1 {decision['precision_at_1']:.3f}, "
        f"MRR {decision['mrr']:.3f}, recall@1 {decision['recall_at_1']:.3f}, "
        f"over a pool of {decision['pool']} of {decision['total']} questions."
    ) in written
    assert "a non-vector arm changed rank 1:" in written
    assert json.loads(SHIPPED_REPORT_PATH.read_text(encoding="utf-8"))["decision_a"] == decision
    if real_summary:
        with pathlib.Path(real_summary).open("a", encoding="utf-8") as job_summary:
            job_summary.write(written)


async def test_a_dead_vector_arm_fails_the_report(settings: Any) -> None:  # noqa: F811
    """Red when: a corpus with no live embeddings still produces a number."""
    embedder = _recorded()
    cases = shipped.all_shipped_cases()
    async with shipped.shipped_corpus_session(settings, embedder, embedder.query_identity) as (
        session,
        _,
    ):
        await session.execute(update(Chunk).values(embedding=None))
        await session.flush()
        with pytest.raises(ShippedEvalError, match="embedded"):
            await shipped.evaluate_cases(
                session, embedder, embedder.query_identity, cases, settings
            )


async def test_one_unembedded_chunk_fails_the_report(settings: Any) -> None:  # noqa: F811
    """Red when: a partly dead vector arm, one chunk short, still produces a number."""
    embedder = _recorded()
    cases = shipped.all_shipped_cases()[:1]
    async with shipped.shipped_corpus_session(settings, embedder, embedder.query_identity) as (
        session,
        _,
    ):
        # Partner: the untouched corpus evaluates.
        assert await shipped.evaluate_cases(
            session, embedder, embedder.query_identity, cases, settings
        )
        first = (await session.scalars(select(Chunk.chunk_id).limit(1))).one()
        await session.execute(update(Chunk).where(Chunk.chunk_id == first).values(embedding=None))
        await session.flush()
        with pytest.raises(ShippedEvalError, match="43 of 44"):
            await shipped.evaluate_cases(
                session, embedder, embedder.query_identity, cases, settings
            )


STUB_IDENTITY = EmbedderIdentity("stub", "gemini-embedding-001", 1536)


async def _seed_stamped_index(settings: Any) -> dict[str, list[Any]]:  # noqa: F811
    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            return await seed_catalog(
                session,
                index_version="exported",
                embedder=StubEmbedder(dim=1536),
                embedder_identity=STUB_IDENTITY,
            )
    finally:
        await engine.dispose()


async def test_the_export_reads_the_active_index_keyed_by_heading(settings: Any) -> None:  # noqa: F811
    """Red when: the export reads the wrong key, column, or a row with no vector."""
    seeded = await _seed_stamped_index(settings)
    rows = await record_vectors.export_chunk_vectors(settings.database_url, identity=STUB_IDENTITY)
    assert len(rows) == len(seeded["chunks"]) > 0
    assert all(len(row.vector) == 1536 for row in rows.values())
    assert {key.split("#", 1)[1] for key in rows} == {c.heading for c in seeded["chunks"]}
    assert {row.content_checksum for row in rows.values()} == {
        c.content_checksum for c in seeded["chunks"]
    }


async def test_the_export_refuses_an_index_from_another_space(settings: Any) -> None:  # noqa: F811
    """Red when: an index's vectors can be exported under a different model's header."""
    await _seed_stamped_index(settings)
    with pytest.raises(ShippedEvalError, match="stamped"):
        await record_vectors.export_chunk_vectors(
            settings.database_url,
            identity=EmbedderIdentity("gemini", "gemini-embedding-001", 1536),
        )
