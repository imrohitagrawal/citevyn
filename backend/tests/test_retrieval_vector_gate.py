"""Hermetic coverage for the VectorRetriever global confidence-gate GLUE (Phase 2).

The pure gate function is tested in ``test_retrieval_confidence.py``; the orchestrator
seam via ``_FakeRetriever`` in ``test_answer_orchestrator.py``. Neither exercises the
actual wiring in :meth:`VectorRetriever.retrieve` — the score extraction
(``1 - distance``), the ``product_area is None and global_confidence is not None``
guard, and the ``return []`` — because on the hermetic SQLite engine the dialect guard
returns ``[]`` before the gate is ever reached (review finding). Here we mock the
session to present a ``postgresql`` dialect and synthetic ``(chunk, doc, distance)``
rows so the gate glue runs without pgvector.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.retrieval.hybrid import HybridRetriever
from app.retrieval.vector import VectorRetriever

pytestmark = pytest.mark.asyncio


class _FakeEmbedder:
    dim = 4

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.1, 0.1, 0.1]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.1, 0.1, 0.1] for _ in texts]


def _chunk(area: str):
    return SimpleNamespace(
        chunk_id=uuid4(),
        document_id=uuid4(),
        product_area=area,
        section_path="/s",
        heading="H",
        parent_heading=None,
        chunk_text=f"{area} chunk",
        context_summary="summary",
    )


def _doc(area: str):
    return SimpleNamespace(source_name=area, title=f"{area} doc", source_url=f"https://x/{area}")


def _mock_session(rows: list[tuple]):
    """A session that reports a postgresql dialect and returns ``rows`` from execute.

    ``rows`` are ``(chunk, doc, distance)`` tuples, ordered by distance ascending
    (closest first) exactly as the real ``ORDER BY distance`` query would.
    """
    result = SimpleNamespace(all=lambda: rows)
    return SimpleNamespace(
        bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=AsyncMock(return_value=result),
    )


def _rows(sims: list[tuple[str, float]]) -> list[tuple]:
    # sim → distance (score = 1 - distance), sorted by distance asc (sim desc).
    return [(_chunk(a), _doc(a), 1.0 - s) for a, s in sims]


async def test_gate_keeps_confident_global_result() -> None:
    """Clear winner (top sim 0.60, margin 0.30) → hits returned, best first."""
    rows = _rows([("codex", 0.60), ("claude_api", 0.30), ("gemini_api", 0.20)])
    vr = VectorRetriever(
        _mock_session(rows), embedder=_FakeEmbedder(), global_confidence=(0.30, 0.04)
    )
    hits = await vr.retrieve("q", product_area=None, limit=10)
    assert [h.product_area for h in hits] == ["codex", "claude_api", "gemini_api"]
    assert hits[0].score == pytest.approx(0.60)


async def test_gate_drops_low_margin_global_result() -> None:
    """A muddle of ~equal weak matches (top 0.40, margin 0.02 < 0.04) → dropped."""
    rows = _rows([("gemini_api", 0.40), ("codex", 0.38), ("claude_api", 0.36)])
    vr = VectorRetriever(
        _mock_session(rows), embedder=_FakeEmbedder(), global_confidence=(0.30, 0.04)
    )
    assert await vr.retrieve("q", product_area=None, limit=10) == []


async def test_gate_drops_below_floor_global_result() -> None:
    """Nearest chunk barely related (top sim 0.20 < 0.30 floor) → dropped."""
    rows = _rows([("codex", 0.20), ("claude_api", 0.08)])
    vr = VectorRetriever(
        _mock_session(rows), embedder=_FakeEmbedder(), global_confidence=(0.30, 0.04)
    )
    assert await vr.retrieve("q", product_area=None, limit=10) == []


async def test_gate_not_applied_when_scoped_to_a_product_area() -> None:
    """In-domain retrieval (product_area set) never gates — a low-margin same-area
    result is legitimate evidence and must survive."""
    rows = _rows([("codex", 0.40), ("codex", 0.38)])
    vr = VectorRetriever(
        _mock_session(rows), embedder=_FakeEmbedder(), global_confidence=(0.30, 0.04)
    )
    hits = await vr.retrieve("q", product_area="codex", limit=10)
    assert len(hits) == 2  # gate skipped despite margin 0.02


async def test_no_gate_configured_returns_all_global_hits() -> None:
    """Without a configured gate, a global result is returned unfiltered (legacy)."""
    rows = _rows([("gemini_api", 0.40), ("codex", 0.39)])
    vr = VectorRetriever(_mock_session(rows), embedder=_FakeEmbedder(), global_confidence=None)
    assert len(await vr.retrieve("q", product_area=None, limit=10)) == 2


async def test_hybrid_global_threads_confidence_into_vector_arm() -> None:
    """HybridRetriever._retrieve_global must pass its global_confidence through to the
    VectorRetriever it builds — otherwise the gate silently never fires in prod."""
    rows = _rows([("gemini_api", 0.40), ("codex", 0.38)])  # low margin → gated if threaded
    hybrid = HybridRetriever(
        _mock_session(rows), embedder=_FakeEmbedder(), global_confidence=(0.30, 0.04)
    )
    from app.routing.intent import Intent

    result = await hybrid.retrieve("q", product_area=None, intent=Intent.how_to, limit=10, top_k=6)
    assert result.hits == []  # the gate fired → confirms the tuple was threaded through
