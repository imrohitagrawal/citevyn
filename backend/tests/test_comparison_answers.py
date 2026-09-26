"""Cross-vendor comparison answers (ADR-0005 §5, Phase 6E).

A question naming two or more products ("how do Claude and Gemini each set a
system prompt?") must be answered for EACH, with each product's own citations;
where the docs do not cover one, the answer says so instead of quietly answering
half. Behind ``CITEVYN_COMPARISON_ANSWERS`` (default off), except the evidence
guarantee, which is a no-op at today's settings and always on.

Each test says which change turns it red.
"""

from __future__ import annotations

import uuid

from app.answer.comparison import (
    comparison_instruction,
    missing_areas,
    missing_note,
    product_name,
)
from app.retrieval.hybrid import _keep_each_area
from app.retrieval.types import EvidenceHit


def _hit(area: str, n: int) -> EvidenceHit:
    return EvidenceHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        source_name=area,
        product_area=area,
        document_title=f"{area} doc {n}",
        section_path="s",
        heading="h",
        context_summary="c",
        source_url=f"https://docs.example.com/{area}/{n}",
        chunk_text=f"{area} text {n}",
        score=1.0 / n,
        rank=n,
    )


# ---------------------------------------------------------------------------
# The evidence guarantee: every named product that returned evidence keeps one
# ---------------------------------------------------------------------------


def test_more_products_than_slots_stays_within_top_k_without_repeats() -> None:
    """top_k=2 over three products: the guarantee cannot hold for all three, so
    it must not grow the list or repeat a product.
    Turns red if: the result exceeds top_k or keeps two chunks of one product."""
    a = [_hit("claude_api", i) for i in (1, 2)]
    b = [_hit("gemini_api", i) for i in (1, 2)]
    c = [_hit("codex", i) for i in (1, 2)]
    merged = [a[0], b[0], c[0], a[1], b[1], c[1]]
    kept = _keep_each_area(merged[:2], merged, ["claude_api", "gemini_api", "codex"], top_k=2)
    assert kept == [a[0], b[0]]


def test_the_guarantee_replaces_a_second_chunk_not_a_first() -> None:
    """A reranker put two claude_api chunks on top and dropped gemini_api.
    Turns red if: gemini_api's best chunk is not put back, or it displaces the
    only chunk of another product."""
    a = [_hit("claude_api", i) for i in (1, 2, 3)]
    b = [_hit("gemini_api", 1)]
    merged = [a[0], b[0], a[1], a[2]]
    reranked = [a[0], a[1], a[2]]  # gemini_api fell out
    kept = _keep_each_area(reranked, merged, ["claude_api", "gemini_api"], top_k=3)
    assert kept == [a[0], a[1], b[0]]


def test_the_only_chunk_of_another_product_is_never_the_one_replaced() -> None:
    """The LAST chunk is codex's only one; the spare is a claude_api chunk.
    Turns red if: the guarantee replaces a product's only chunk."""
    a = [_hit("claude_api", i) for i in (1, 2)]
    b = [_hit("gemini_api", 1)]
    c = [_hit("codex", 1)]
    merged = [a[0], b[0], c[0], a[1]]
    reranked = [a[0], a[1], c[0]]  # gemini_api fell out
    kept = _keep_each_area(reranked, merged, ["claude_api", "gemini_api", "codex"], top_k=3)
    assert kept == [a[0], b[0], c[0]]


def test_a_short_rerank_gets_the_missing_product_added() -> None:
    """A reranker returned fewer chunks than top_k, one per product except one.
    Turns red if: a missing product is not added when there is room."""
    a = [_hit("claude_api", 1)]
    b = [_hit("gemini_api", 1)]
    kept = _keep_each_area(a, a + b, ["claude_api", "gemini_api"], top_k=6)
    assert kept == [a[0], b[0]]


def test_when_every_product_is_present_nothing_changes() -> None:
    """Today's production case (identity reranker, top_k 6, at most 3 products).
    Turns red if: the guarantee reorders or replaces anything it need not."""
    a = [_hit("claude_api", i) for i in (1, 2)]
    b = [_hit("gemini_api", i) for i in (1, 2)]
    merged = [a[0], b[0], a[1], b[1]]
    assert _keep_each_area(merged, merged, ["claude_api", "gemini_api"], top_k=6) == merged


def test_a_product_with_no_evidence_is_not_invented() -> None:
    """Turns red if: the guarantee fabricates or errors for a product that
    returned nothing."""
    a = [_hit("claude_api", i) for i in (1, 2)]
    kept = _keep_each_area(a, a, ["claude_api", "gemini_api"], top_k=6)
    assert kept == a


# ---------------------------------------------------------------------------
# The instruction and the honesty check
# ---------------------------------------------------------------------------


def test_the_instruction_names_each_product_in_plain_words() -> None:
    """Turns red if: a product is missing or shown by its internal id."""
    text = comparison_instruction(["claude_api", "gemini_api"])
    assert "the Claude API" in text and "the Gemini API" in text
    assert "claude_api" not in text
    assert "citing" in text


def test_missing_areas_are_the_named_products_nothing_cites() -> None:
    """Citations map to products through their evidence chunk.
    Turns red if: a cited product is reported missing, or a missing one is not."""
    evidence = [_hit("claude_api", 1), _hit("gemini_api", 1), _hit("claude_api", 2)]
    assert missing_areas(["claude_api", "gemini_api"], evidence, [1, 3]) == ["gemini_api"]
    assert missing_areas(["claude_api", "gemini_api"], evidence, [1, 2]) == []
    # An out-of-range marker (the validator rejects these first) is ignored, not a crash.
    assert missing_areas(["claude_api"], evidence, [9]) == ["claude_api"]


def test_the_note_says_plainly_which_product_is_not_covered() -> None:
    """Turns red if: the note is empty for a missing product, or names ids."""
    assert missing_note([]) == ""
    note = missing_note(["gemini_api"])
    assert note == "The documentation I have does not cover the Gemini API for this question."
    two = missing_note(["gemini_api", "codex"])
    assert (
        two == "The documentation I have does not cover the Gemini API or Codex for this question."
    )


def test_every_product_domain_has_a_plain_name() -> None:
    """Turns red if: a product domain is added without a name (it would print its id)."""
    from app.guardrails.domain import Domain

    for d in Domain:
        if d.value in {"unsupported", "general", "citevyn"}:
            continue
        assert product_name(d.value) != d.value
