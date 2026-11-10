"""#601: the shipped-corpus report over the committed recording (``postgres`` job).

This is what puts the number in CI: it runs every question on real pgvector with the
recorded vectors, and appends the report to ``$GITHUB_STEP_SUMMARY``. There are no floors
here on purpose; the partners below only prove the report measured something.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.embeddings.factory import EmbedderIdentity
from app.embeddings.stub import StubEmbedder
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


async def test_the_report_runs_on_the_real_recording_and_reaches_the_summary(
    settings: Any,  # noqa: F811
) -> None:
    """Red when: the recording goes stale, a question degrades, or the report stops printing."""
    chunks = shipped.load_chunk_vectors(RECORDED_CHUNKS_PATH)
    assert shipped.check_chunk_vectors(chunks, shipped.shipped_heading_keys()) == 44
    embedder = shipped.RecordedEmbedder(
        chunks=chunks, questions=shipped.load_question_vectors(RECORDED_QUESTIONS_PATH)
    )
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
    assert report["decision_a"]["pool"] > 0
    assert report["decision_a"]["precision_at_1"] is not None
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    shipped.write_report(report, SHIPPED_REPORT_PATH, pathlib.Path(summary) if summary else None)
    assert SHIPPED_REPORT_PATH.is_file()


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
