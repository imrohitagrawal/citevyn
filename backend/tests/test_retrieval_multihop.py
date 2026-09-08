"""Hermetic tests for HybridRetriever.retrieve_multi (Phase 3 multi-hop merge).

Stubs the per-area ``retrieve`` so the round-robin merge, dedup, degrade combination,
and fan-out cap are exercised without a DB. The per-area retrieval itself is the
existing single-domain path (covered elsewhere).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.enums import RetrievalType
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.types import EvidenceHit, RetrievalResult, VectorDegrade
from app.routing.intent import Intent

pytestmark = pytest.mark.asyncio


def _hit(area: str) -> EvidenceHit:
    return EvidenceHit(
        chunk_id=uuid4(),
        document_id=uuid4(),
        product_area=area,
        source_name=area,
        document_title=f"{area} doc",
        section_path="/s",
        heading="H",
        parent_heading=None,
        chunk_text=f"{area} chunk",
        context_summary="s",
        source_url=f"https://x/{area}",
        score=1.0,
        retrieval_type=RetrievalType.hybrid,
        rank=1,
    )


def _hybrid(per_area: dict[str, RetrievalResult]) -> tuple[HybridRetriever, list[str]]:
    calls: list[str] = []
    h = HybridRetriever(SimpleNamespace())  # session unused — retrieve is stubbed

    async def _fake_retrieve(question, *, product_area, intent, limit, top_k):
        calls.append(product_area)
        return per_area[product_area]

    h.retrieve = _fake_retrieve  # type: ignore[method-assign]
    return h, calls


async def test_retrieve_multi_round_robin_merges_each_area() -> None:
    a1, a2, b1 = _hit("claude_api"), _hit("claude_api"), _hit("gemini_api")
    h, calls = _hybrid(
        {
            "claude_api": RetrievalResult(hits=[a1, a2], vector_degrade=VectorDegrade.none),
            "gemini_api": RetrievalResult(hits=[b1], vector_degrade=VectorDegrade.none),
        }
    )
    result = await h.retrieve_multi(
        "q", product_areas=["claude_api", "gemini_api"], intent=Intent.how_to, limit=20, top_k=6
    )
    # Round-robin by rank: a1 (claude r1), b1 (gemini r1), a2 (claude r2) — both areas represented.
    assert [hit.chunk_id for hit in result.hits] == [a1.chunk_id, b1.chunk_id, a2.chunk_id]
    assert calls == ["claude_api", "gemini_api"]  # sequential, both areas


async def test_retrieve_multi_combines_degrade_mismatch_wins() -> None:
    h, _ = _hybrid(
        {
            "claude_api": RetrievalResult(
                hits=[_hit("claude_api")], vector_degrade=VectorDegrade.none
            ),
            "gemini_api": RetrievalResult(
                hits=[_hit("gemini_api")], vector_degrade=VectorDegrade.mismatch
            ),
        }
    )
    result = await h.retrieve_multi(
        "q", product_areas=["claude_api", "gemini_api"], intent=Intent.how_to, limit=20, top_k=6
    )
    # One area degraded → combined is degraded (mismatch), so the answer is not cached.
    assert result.vector_degrade is VectorDegrade.mismatch


async def test_retrieve_multi_degrade_none_only_when_all_clean() -> None:
    h, _ = _hybrid(
        {
            "claude_api": RetrievalResult(
                hits=[_hit("claude_api")], vector_degrade=VectorDegrade.none
            ),
            "gemini_api": RetrievalResult(
                hits=[_hit("gemini_api")], vector_degrade=VectorDegrade.none
            ),
        }
    )
    result = await h.retrieve_multi(
        "q", product_areas=["claude_api", "gemini_api"], intent=Intent.how_to, limit=20, top_k=6
    )
    assert result.vector_degrade is VectorDegrade.none


async def test_retrieve_multi_caps_fan_out() -> None:
    areas = ["claude_api", "claude_code", "codex", "gemini_api", "citevyn"]
    per = {a: RetrievalResult(hits=[_hit(a)], vector_degrade=VectorDegrade.none) for a in areas}
    h, calls = _hybrid(per)
    await h.retrieve_multi("q", product_areas=areas, intent=Intent.how_to, limit=20, top_k=6)
    assert len(calls) == 3  # _MAX_MULTIHOP_DOMAINS


# ---------------------------------------------------------------------------
# #352: ``_combine_degrades`` used to be a two-test ladder with a ``none``
# fall-through, so any reason it had not been taught about was merged into "clean"
# and the multi-hop answer was cached. It fails OPEN, which is the wrong
# direction, and nothing here would have noticed.
# ---------------------------------------------------------------------------


async def test_retrieve_multi_does_not_cache_when_one_area_saw_an_ambiguous_index() -> None:
    """The concrete #352 case at the multi-hop seam.

    RED if ``ambiguous_index`` is left out of ``_DEGRADE_PRECEDENCE`` -- the loop
    falls through to ``VectorDegrade.none`` and the union answer is cached.
    """
    h, _ = _hybrid(
        {
            "claude_api": RetrievalResult(
                hits=[_hit("claude_api")], vector_degrade=VectorDegrade.ambiguous_index
            ),
            "gemini_api": RetrievalResult(
                hits=[_hit("gemini_api")], vector_degrade=VectorDegrade.none
            ),
        }
    )
    result = await h.retrieve_multi(
        "q", product_areas=["claude_api", "gemini_api"], intent=Intent.how_to, limit=20, top_k=6
    )
    assert result.vector_degrade is VectorDegrade.ambiguous_index
    # PARTNER: the hits are still merged and served -- "not cached" is not
    # "not answered".
    assert len(result.hits) == 2


async def test_every_degrade_reason_is_ranked_so_none_can_only_mean_clean() -> None:
    """THE GUARD FOR THE NEXT MEMBER, not for this one.

    ``_combine_degrades`` returns ``VectorDegrade.none`` for anything it does not
    recognise, and ``Orchestrator._respond_answer`` reads ``none`` as "safe to
    freeze for 24 hours". So an unranked member is silently a cache-poisoning bug
    on the multi-hop path. Asserting the SET rather than adding one more example
    test is what makes this hold for a member nobody has written yet.

    RED if a ``VectorDegrade`` member is added without ranking it.
    """
    from app.retrieval.hybrid import _DEGRADE_PRECEDENCE, _combine_degrades

    assert set(_DEGRADE_PRECEDENCE) == set(VectorDegrade) - {VectorDegrade.none}
    assert VectorDegrade.none not in _DEGRADE_PRECEDENCE, (
        "ranking 'none' would make it win over a real reason"
    )
    # PARTNER: the ranking is actually CONSULTED, so the set assertion above is
    # not pinning a constant nothing reads.
    for reason in _DEGRADE_PRECEDENCE:
        assert _combine_degrades([VectorDegrade.none, reason]) is reason, reason
    assert _combine_degrades([VectorDegrade.none, VectorDegrade.none]) is VectorDegrade.none
