"""Freshness stamps: "docs as of <date>" on every answer and citation (ADR-0005 §6).

The honest per-source fact is ``documents.content_as_of``: the day our copy of
that source was last updated (the sources are hand-written summaries shipped in
the repo; see ``test_source_content_dates.py``). It is stamped at ingest, so it
travels with the indexed text. Neither the ingest run's clock
(``last_fetched_at``: every deploy re-ingests) nor an index's ``created_at`` says
how old the text is.

* Each citation carries ``docs_as_of``: its own document's ``content_as_of``
  (``YYYY-MM-DD``).
* The answer carries ``docs_as_of``: the OLDEST of its citations' dates, because
  that is the one bound that holds for every source the answer used ("every
  source here was fetched on or after this date").
* No citations (a refusal) means ``docs_as_of`` is ``null``: there is no source
  to date.

These are trust features, so they ship without the access setting.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.answer.orchestrator import docs_as_of
from app.core.db import get_sessionmaker
from app.models import Document
from app.retrieval.types import RetrievedChunk, chunk_to_citation
from tests.test_messages_routes import in_memory_client, seeded_app  # noqa: F401 (fixtures)

DEMO = {"Authorization": "Bearer local-demo-key"}
OLD = date(2026, 9, 1)
NEW = date(2026, 9, 20)


def _date_documents(old_area: str) -> dict[str, str]:
    """Give every document in ``old_area`` the OLD date and the rest NEW.

    Returns ``{source_url: expected_date}`` so each citation can be checked
    against ITS OWN document, not against a date shared by all of them.
    """
    factory = get_sessionmaker()

    async def _run() -> dict[str, str]:
        async with factory() as session:
            docs = (await session.execute(select(Document))).scalars().all()
            expected: dict[str, str] = {}
            for doc in docs:
                doc.content_as_of = OLD if doc.product_area == old_area else NEW
                expected[doc.source_url] = doc.content_as_of.isoformat()
            await session.commit()
            return expected

    return asyncio.run(_run())


def _ask(client: TestClient, question: str) -> dict[str, object]:
    sid = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO).json()["session_id"]
    res = client.post(f"/v1/sessions/{sid}/messages", json={"message": question}, headers=DEMO)
    assert res.status_code == 200
    return res.json()


def test_each_citation_carries_its_own_documents_fetch_date(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: a retrieval arm or the hybrid merge drops the fetch date, or
    a citation is dated from anything but its own document."""
    expected = _date_documents(old_area="claude_code")
    body = _ask(seeded_app, "How do I configure Claude Code permissions?")
    citations = body["citations"]
    assert isinstance(citations, list) and citations  # partner: there is something to check
    for c in citations:
        assert c["docs_as_of"] == expected[c["url"]], c


def test_the_answer_is_dated_by_its_oldest_citation(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: the answer-level date is the newest, the index build date,
    or "now" instead of the oldest cited source."""
    _date_documents(old_area="claude_code")
    body = _ask(seeded_app, "How do I configure Claude Code permissions?")
    dates = sorted(c["docs_as_of"] for c in body["citations"])  # type: ignore[union-attr]
    assert body["docs_as_of"] == dates[0]
    assert body["docs_as_of"] == OLD.isoformat()


async def test_a_cached_answer_keeps_its_dates(session: Any) -> None:
    """A cache hit replays stored citations. Turns red if: the replay path loses
    the per-citation date or does not recompute the answer-level one.

    Driven at the orchestrator, like the other cache tests: through the route the
    stub embedder degrades retrieval, and degraded answers are never cached.
    """
    from app.answer.orchestrator import Orchestrator
    from app.llm.stub import StubLLMClient
    from tests.test_answer_orchestrator import (
        _evidence,
        _FakeRetriever,
        _seed_index_version,
        _settings,
    )

    await _seed_index_version(session)
    hits = await _evidence(session, count=2)
    dated = [
        hits[0].model_copy(update={"content_as_of": NEW}),
        hits[1].model_copy(update={"content_as_of": OLD}),
    ]
    orchestrator = Orchestrator(
        _settings(), session, llm=StubLLMClient(), retriever=_FakeRetriever(dated)
    )
    q = "How do I configure Claude Code permissions?"
    first = await orchestrator.ask(question=q, request_id="r1", session_id=uuid.uuid4())
    second = await orchestrator.ask(question=q, request_id="r2", session_id=uuid.uuid4())
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True  # partner: this really was the replay path
    assert first["citations"]  # partner: there are dated citations to replay
    assert second["docs_as_of"] == first["docs_as_of"]
    assert first["docs_as_of"] == min(c["docs_as_of"] for c in first["citations"])  # type: ignore[union-attr]
    assert [c["docs_as_of"] for c in second["citations"]] == [  # type: ignore[union-attr]
        c["docs_as_of"]
        for c in first["citations"]  # type: ignore[union-attr]
    ]


def test_a_refusal_has_no_date(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: a refusal claims a date for sources it does not have."""
    body = _ask(seeded_app, "What is the best laptop to buy this year?")
    assert body["no_answer"] is True or body["unsupported"] is True  # partner
    assert body["citations"] == []
    assert body["docs_as_of"] is None


def test_docs_as_of_is_the_oldest_known_date_and_ignores_unknowns() -> None:
    """Old cached rows have no date; they must not make the answer look fresher
    or break the computation. Turns red if: min/max is swapped, or a missing
    date is treated as a date."""
    assert docs_as_of([]) is None
    assert docs_as_of([{"docs_as_of": None}]) is None
    assert docs_as_of([{"title": "legacy row, no key"}]) is None
    assert (
        docs_as_of(
            [{"docs_as_of": "2026-09-20"}, {"docs_as_of": None}, {"docs_as_of": "2026-09-01"}]
        )
        == "2026-09-01"
    )


def test_a_citation_without_a_known_fetch_date_says_null() -> None:
    """Turns red if: an unknown date is invented (e.g. today) instead of null."""
    chunk = RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        product_area="claude_code",
        source_name="claude_code",
        document_title="T",
        section_path="s",
        heading="h",
        chunk_text="x",
        context_summary="c",
        source_url="https://example.com",
    )
    assert chunk_to_citation(chunk)["docs_as_of"] is None
    dated = chunk.model_copy(update={"content_as_of": NEW})
    assert chunk_to_citation(dated)["docs_as_of"] == "2026-09-20"
