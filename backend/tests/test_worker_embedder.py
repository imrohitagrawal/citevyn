"""Tests for the :class:`app.embeddings.StubEmbedder` via the worker seam.

As of #51 the embedder is the unified async seam in :mod:`app.embeddings`,
re-exported from :mod:`app.worker.embedder` for backward compatibility. These
tests pin the stub contract used by the ingest path:

* ``embed`` / ``embed_documents`` are async and return plain ``list[float]``.
* The output is deterministic — same text → same vector.
* The output has the configured dim and is unit-normalised (so cosine distance
  against the pgvector column is well defined).
* ``build_embedder`` returns the stub by default (provider="stub", no key).
"""

from __future__ import annotations

import math

from app.core.config import Settings
from app.worker.embedder import StubEmbedder, build_embedder


async def test_stub_embedder_returns_plain_list() -> None:
    """``embed`` returns a Python ``list[float]``, not numpy."""
    embedder = StubEmbedder(dim=16)
    vector = await embedder.embed("hello world")
    assert isinstance(vector, list)
    assert all(isinstance(v, float) for v in vector)


async def test_stub_embedder_dim_matches_configured_value() -> None:
    """The output length equals ``embedder.dim``."""
    embedder = StubEmbedder(dim=128)
    assert len(await embedder.embed("anything")) == 128


async def test_stub_embedder_is_deterministic() -> None:
    """Same text → same vector across calls."""
    embedder = StubEmbedder(dim=64)
    a = await embedder.embed("claude opus")
    b = await embedder.embed("claude opus")
    assert a == b


async def test_stub_embedder_distinct_inputs_diverge() -> None:
    """Different text → different vector."""
    embedder = StubEmbedder(dim=64)
    a = await embedder.embed("claude opus")
    b = await embedder.embed("gemini flash")
    assert a != b


async def test_stub_embedder_is_unit_normalised() -> None:
    """Non-empty vectors have length ~1 so cosine distance is well defined."""
    embedder = StubEmbedder(dim=64)
    vector = await embedder.embed("rate limit env var")
    assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, rel_tol=1e-9)


async def test_stub_embedder_empty_text_is_zero_vector() -> None:
    """Empty text embeds to the zero vector (honest 'no signal')."""
    embedder = StubEmbedder(dim=32)
    vector = await embedder.embed("")
    assert vector == [0.0] * 32


async def test_stub_embed_documents_batches_in_order() -> None:
    """``embed_documents`` returns one vector per input, preserving order."""
    embedder = StubEmbedder(dim=8)
    texts = ["alpha", "beta", "gamma"]
    vectors = await embedder.embed_documents(texts)
    assert len(vectors) == 3
    # Each document vector equals the single-embed of the same text (same space).
    for text, vector in zip(texts, vectors, strict=True):
        assert vector == await embedder.embed(text)


async def test_stub_embed_documents_empty_batch() -> None:
    """An empty batch returns an empty list (no error)."""
    embedder = StubEmbedder(dim=8)
    assert await embedder.embed_documents([]) == []


def test_build_embedder_returns_stub_by_default() -> None:
    """The default factory (provider='stub') returns a :class:`StubEmbedder`."""
    embedder = build_embedder(Settings())
    assert isinstance(embedder, StubEmbedder)


def test_build_embedder_stub_dim_follows_settings() -> None:
    """The stub's dim is driven by ``Settings.embedding_dim``."""
    embedder = build_embedder(Settings(embedding_dim=256))
    assert embedder.dim == 256
