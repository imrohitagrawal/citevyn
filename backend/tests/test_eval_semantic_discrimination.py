"""Semantic-discrimination proof for the revived vector arm (Phase 1, #97).

Why this exists (PR-plan review, "gameable metric"): the golden retrieval hit-rate
seeds exactly ONE chunk per product area and the vector arm is hard-scoped to the
routed area, so for a correctly-routed paraphrase the arm returns that area's sole
chunk regardless of embedding quality — a *stub* (hash-bucket) embedder yields the
identical 3/5. The golden number therefore proves the vector plumbing is ALIVE on
Postgres, not that the embeddings are semantic.

This test isolates embedding QUALITY instead: it embeds the anchored conftest corpus
(one chunk per area) and the golden **paraphrase** questions (zero lexical overlap
with the answer text) and asks, GLOBALLY (no product-area scoping), whether each
paraphrase's nearest corpus chunk is the correct area. A real semantic embedder
separates them; the deterministic stub performs at chance. Both corpus and queries
come from the two anchored artifacts (``conftest.seed_catalog`` + ``golden.jsonl``),
so there is no hand-maintained parallel corpus to drift.

Opt-in only — it makes real embedding API calls. Gated on ``CITEVYN_EVAL_PG=1`` plus
a configured non-stub embedder, so the hermetic CI suite never runs it.
"""

from __future__ import annotations

import math
import os
import tempfile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.embeddings.factory import build_embedder
from app.embeddings.protocol import Embedder
from app.embeddings.stub import StubEmbedder
from app.models import Base
from tests.conftest import seed_catalog
from tests.eval.cases import load_cases
from tests.eval.paths import GOLDEN_PATH

pytestmark = pytest.mark.skipif(
    os.environ.get("CITEVYN_EVAL_PG") != "1" or get_settings().embedding_provider == "stub",
    reason=(
        "opt-in semantic proof: needs CITEVYN_EVAL_PG=1 and a real "
        "(non-stub) CITEVYN_EMBEDDING_PROVIDER"
    ),
)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


async def _corpus_by_area(embedder: Embedder) -> dict[str, list[float]]:
    """Seed the anchored conftest corpus with ``embedder`` into a throwaway SQLite
    DB and read back ``{product_area: embedding}`` — the single-source corpus."""
    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        engine = create_async_engine(f"sqlite+aiosqlite:///{fh.name}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
            async with factory() as session:
                catalog = await seed_catalog(session, embedder=embedder)
            return {
                str(c.product_area): list(c.embedding)  # type: ignore[attr-defined]
                for c in catalog["chunks"]
            }
        finally:
            await engine.dispose()


async def _global_top1_accuracy(embedder: Embedder) -> tuple[int, int]:
    """Fraction of golden paraphrases whose GLOBAL nearest corpus chunk is the
    correct area (no product-area scoping — pure embedding separation)."""
    corpus = await _corpus_by_area(embedder)
    areas = list(corpus)
    paraphrases = [c for c in load_cases(GOLDEN_PATH) if c.kind == "paraphrase"]
    assert paraphrases, "golden set must contain paraphrase cases"
    correct = 0
    for case in paraphrases:
        qv = await embedder.embed(case.question)
        best = max(areas, key=lambda a: _cosine(qv, corpus[a]))
        correct += best == case.expected_source
    return correct, len(paraphrases)


async def test_real_embedder_separates_paraphrases_globally() -> None:
    """The configured real embedder routes paraphrases to the right area globally.

    Empirically ``openai/text-embedding-3-small`` scores 5/5 here; require >= 4/5 so
    the proof is robust to a single borderline case without being vacuous.
    """
    embedder = build_embedder(get_settings())
    assert not isinstance(embedder, StubEmbedder)
    try:
        correct, total = await _global_top1_accuracy(embedder)
    finally:
        aclose = getattr(embedder, "aclose", None)
        if callable(aclose):
            await aclose()
    assert correct >= total - 1, f"real embedder global top-1 only {correct}/{total}"


async def test_stub_embedder_cannot_separate_paraphrases() -> None:
    """Control: the hash-bucket stub performs at ~chance on the SAME task.

    This is what makes the golden metric's plumbing-level 3/5 insufficient on its
    own — and what makes the test above a real semantic signal rather than a
    tautology. With 5 areas, chance is ~1/5; require the stub to score no better
    than 2/5 so a future regression that makes the stub 'accidentally semantic'
    (or the real embedder degrade to stub) fails loudly.
    """
    stub = StubEmbedder(dim=get_settings().embedding_dim)
    correct, total = await _global_top1_accuracy(stub)
    assert correct <= 2, (
        f"stub unexpectedly separated {correct}/{total} (metric not discriminating)"
    )
