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
from app.models import Chunk, Document, ExactTerm
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


async def test_the_arm_switches_really_remove_arms(settings: Any) -> None:  # noqa: F811
    """Red when: switching the keyword or exact arm off does nothing, or the keyword-only
    rank is not computed from the keyword-only run.
    """
    embedder = _recorded()
    cases = shipped.all_shipped_cases()
    identity = embedder.query_identity
    async with shipped.shipped_corpus_session(settings, embedder, identity) as (session, _):
        outcomes = {
            o.case_id: o
            for o in await shipped.evaluate_cases(session, embedder, identity, cases, settings)
        }

        async def keys(routed: Any, off: set[str]) -> tuple[str, ...]:
            result = await shipped.retrieve_routed(
                session,
                routed,
                embedder=embedder,
                identity=identity,
                settings=settings,
                arms_off=frozenset(off),
            )
            return tuple(f"{hit.source_name}#{hit.heading}" for hit in result.hits)

        scoped = with_hits = differs = keyword_vs_vector = 0
        for case in cases:
            routed = shipped.route_case(case, settings)
            keyword_only = await keys(routed, {"vector", "exact"})
            # (b) Every arm off finds nothing, whatever the route.
            assert await keys(routed, {"vector", "keyword", "exact"}) == (), case.id
            # (c) The reported keyword-only rank is this run's rank.
            outcome = outcomes[case.id]
            gold = set(case.shipped_gold)
            expected = next((i for i, k in enumerate(keyword_only, 1) if k in gold), None)
            assert outcome.rank_keyword_only == expected, case.id
            keyword_vs_vector += outcome.rank_keyword_only != outcome.rank_vector_only
            if routed.route == "scoped":
                scoped += 1
                with_hits += bool(keyword_only)
                differs += keyword_only[:1] != outcome.retrieved[:1]
    # (a) The keyword arm alone serves scoped questions, and is not the full result.
    assert scoped > 50
    assert with_hits >= 0.8 * scoped, (with_hits, scoped)
    assert differs >= 1
    # Partner to (c): the two single-arm ranks really differ somewhere.
    assert keyword_vs_vector >= 1


RATE_LIMIT_QUESTION = "What is the rate limit for the Claude API?"


async def test_the_exact_arm_takes_part_in_the_removal(settings: Any) -> None:  # noqa: F811
    """Red when: the removal loop leaves the exact arm out.

    A full question never equals an extracted term on this corpus, so the exact arm finds
    nothing for any recorded question. A synthetic term equal to one question, pointing at
    another passage, gives it something to decide.
    """
    embedder = _recorded()
    case = next(c for c in shipped.all_shipped_cases() if c.question == RATE_LIMIT_QUESTION)
    identity = embedder.query_identity
    async with shipped.shipped_corpus_session(settings, embedder, identity) as (session, _):
        (clean,) = await shipped.evaluate_cases(session, embedder, identity, [case], settings)
        # Partner: without the synthetic term, removing the exact arm changes nothing.
        assert "exact" not in clean.rank1_changed_by
        streaming = (
            await session.execute(
                select(Chunk)
                .join(Document)
                .where(Document.source_name == "claude_api", Chunk.heading == "Streaming")
            )
        ).scalar_one()
        term_type = (await session.scalars(select(ExactTerm.term_type).limit(1))).one()
        session.add(
            ExactTerm(
                term_text=RATE_LIMIT_QUESTION,
                term_type=term_type,
                product_area="claude_api",
                document_id=streaming.document_id,
                chunk_id=streaming.chunk_id,
            )
        )
        await session.flush()
        (seeded,) = await shipped.evaluate_cases(session, embedder, identity, [case], settings)
    assert seeded.retrieved[0] == "claude_api#Streaming"
    assert "exact" in seeded.rank1_changed_by
    assert seeded.winning_arm in {"exact", "both"}
