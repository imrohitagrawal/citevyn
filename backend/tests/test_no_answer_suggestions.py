"""Tests for graceful-fallback nearest-doc suggestions (Phase 4a).

``build_suggestions`` (pure) + the ``build_no_answer_response`` wiring. The orchestrator
integration (a declined answer with evidence surfaces suggestions; a clean off-corpus
refusal does not) lives in ``test_answer_orchestrator.py``.
"""

from __future__ import annotations

import uuid

from app.answer.no_answer import build_no_answer_response, build_suggestions
from app.models.enums import RetrievalType
from app.retrieval.types import EvidenceHit
from app.routing.intent import Intent


def _hit(source: str, *, title: str, url: str, area: str, rank: int) -> EvidenceHit:
    return EvidenceHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        product_area=area,
        source_name=source,
        document_title=title,
        section_path="/x",
        heading="H",
        parent_heading=None,
        chunk_text="snippet",
        context_summary="summary",
        source_url=url,
        score=1.0,
        retrieval_type=RetrievalType.hybrid,
        rank=rank,
    )


def test_build_suggestions_empty_evidence_is_empty() -> None:
    """A truly off-corpus refusal (no evidence) yields no suggestions — clean refusal."""
    assert build_suggestions([]) == []


def test_build_suggestions_projects_title_url_area() -> None:
    hits = [_hit("claude_api", title="Claude API Reference", url="/c", area="claude_api", rank=1)]
    assert build_suggestions(hits) == [
        {"title": "Claude API Reference", "url": "/c", "product_area": "claude_api"}
    ]


def test_build_suggestions_dedupes_by_source_keeping_first() -> None:
    """Two chunks from the same doc collapse to one suggestion (highest-ranked wins)."""
    hits = [
        _hit("claude_api", title="Claude API Reference", url="/c", area="claude_api", rank=1),
        _hit("claude_api", title="Claude API Reference", url="/c", area="claude_api", rank=2),
        _hit("gemini_api", title="Gemini API Reference", url="/g", area="gemini_api", rank=3),
    ]
    out = build_suggestions(hits)
    assert [s["title"] for s in out] == ["Claude API Reference", "Gemini API Reference"]


def test_build_suggestions_caps_at_three_distinct_docs() -> None:
    hits = [_hit(f"src{i}", title=f"Doc {i}", url=f"/{i}", area="codex", rank=i) for i in range(5)]
    out = build_suggestions(hits)
    assert len(out) == 3
    assert [s["title"] for s in out] == ["Doc 0", "Doc 1", "Doc 2"]


def test_no_answer_response_includes_suggestions_field() -> None:
    """The field is always present (additive), empty by default."""
    without = build_no_answer_response(
        request_id="r",
        domain_value="unsupported",
        intent=Intent.unsupported,
        reason="unsupported",
        copy="nope",
    )
    assert without["suggestions"] == []

    with_sugg = build_no_answer_response(
        request_id="r",
        domain_value="claude_api",
        intent=Intent.faq,
        reason="no_answer",
        copy="nope",
        suggestions=[{"title": "Claude API Reference", "url": "/c", "product_area": "claude_api"}],
    )
    assert with_sugg["suggestions"] == [
        {"title": "Claude API Reference", "url": "/c", "product_area": "claude_api"}
    ]
