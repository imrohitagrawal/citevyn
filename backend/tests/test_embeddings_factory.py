"""Tests for :mod:`app.embeddings.factory` — selection, singleton, guards.

Mirrors ``tests/test_llm_factory_singleton.py``:

* ``build_embedder`` selects stub vs gemini per ``embedding_provider``.
* The stub needs no key; the gemini provider raises eagerly on a missing key.
* ``get_embedder`` returns a process-wide singleton; ``shutdown_embedder`` closes
  it and is a no-op when nothing was built.
* ``validate_embedder_provider`` rejects unknown providers and the stub in prod.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.embeddings import factory as emb_factory
from app.embeddings.factory import (
    EmbeddingProviderNotConfigured,
    build_embedder,
    validate_embedder_provider,
)
from app.embeddings.gemini import GeminiEmbedder
from app.embeddings.stub import StubEmbedder


@pytest.fixture(autouse=True)
def _reset_singleton():
    emb_factory.reset_embedder()
    yield
    emb_factory.reset_embedder()


# -- selection --------------------------------------------------------------


def test_build_embedder_stub_by_default() -> None:
    assert isinstance(build_embedder(Settings()), StubEmbedder)


def test_build_embedder_stub_needs_no_key() -> None:
    # No GEMINI key set, provider stub → no raise.
    embedder = build_embedder(Settings(embedding_provider="stub", gemini_api_key=None))
    assert isinstance(embedder, StubEmbedder)


def test_build_embedder_gemini_builds_real_client() -> None:
    embedder = build_embedder(Settings(embedding_provider="gemini", gemini_api_key="k-123"))
    assert isinstance(embedder, GeminiEmbedder)
    assert embedder.dim == Settings().embedding_dim


def test_build_embedder_gemini_missing_key_raises() -> None:
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        build_embedder(Settings(embedding_provider="gemini", gemini_api_key=None))


# -- singleton --------------------------------------------------------------


def test_get_embedder_returns_singleton() -> None:
    settings = Settings()
    first = emb_factory.get_embedder(settings)
    second = emb_factory.get_embedder(settings)
    assert first is second


def test_shutdown_embedder_noop_without_singleton() -> None:
    # Should not raise when nothing was built.
    asyncio.run(emb_factory.shutdown_embedder())


def test_shutdown_embedder_closes_real_client() -> None:
    settings = Settings(embedding_provider="gemini", gemini_api_key="k-123")
    embedder = emb_factory.get_embedder(settings)
    assert isinstance(embedder, GeminiEmbedder)
    # Close via the factory; a second get builds a fresh instance.
    asyncio.run(emb_factory.shutdown_embedder())
    rebuilt = emb_factory.get_embedder(settings)
    assert rebuilt is not embedder


# -- startup guard ----------------------------------------------------------


def test_validate_rejects_unknown_provider() -> None:
    with pytest.raises(RuntimeError, match="not supported"):
        validate_embedder_provider(Settings(embedding_provider="voyage"))


def test_validate_allows_stub_outside_production() -> None:
    validate_embedder_provider(Settings(environment="local", embedding_provider="stub"))


def test_validate_rejects_stub_in_production() -> None:
    # Production also requires real LLM + admin key; set them so only the
    # embedding guard is under test.
    settings = Settings(
        environment="production",
        embedding_provider="stub",
        llm_provider="gemini",
        gemini_api_key="k",
        admin_api_key="a-strong-secret",
    )
    with pytest.raises(EmbeddingProviderNotConfigured):
        validate_embedder_provider(settings)
