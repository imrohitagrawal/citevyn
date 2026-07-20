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
from app.cost.metered import MeteredEmbedder
from app.embeddings import factory as emb_factory
from app.embeddings.factory import (
    EmbedderIdentity,
    EmbeddingProviderNotConfigured,
    build_embedder,
    is_index_embedder_mismatch,
    validate_embedder_provider,
)
from app.embeddings.gemini import GeminiEmbedder
from app.embeddings.openrouter import OpenRouterEmbedder
from app.embeddings.stub import StubEmbedder

_STUB = EmbedderIdentity(provider="stub", model="stub-embedding", dim=1536)
_GEMINI = EmbedderIdentity(provider="gemini", model="gemini-embedding-001", dim=1536)


# -- EmbedderIdentity.cache_key_component (#65) -----------------------------


def test_cache_key_component_encodes_triple() -> None:
    assert _GEMINI.cache_key_component() == "gemini|gemini-embedding-001|1536"


def test_cache_key_component_all_none_is_stable_nonempty() -> None:
    """The NULL-stamp/unconfigured trap: an all-None identity must yield a
    stable, non-empty component ("||"), never a value that blanks the key."""
    empty = EmbedderIdentity(provider=None, model=None, dim=None)
    assert empty.cache_key_component() == "||"


def test_cache_key_component_distinguishes_providers() -> None:
    assert _STUB.cache_key_component() != _GEMINI.cache_key_component()


# -- is_index_embedder_mismatch (#65) ---------------------------------------


def test_mismatch_false_when_no_active_stamp() -> None:
    # No active index → not a mismatch (nothing to disagree with).
    assert is_index_embedder_mismatch(_STUB, None) is False


def test_mismatch_false_when_stamp_provider_none() -> None:
    # Legacy / stub-seeded index carries no provider → "unknown provenance, allow".
    legacy = EmbedderIdentity(provider=None, model=None, dim=None)
    assert is_index_embedder_mismatch(_GEMINI, legacy) is False


def test_mismatch_false_when_identities_equal() -> None:
    assert is_index_embedder_mismatch(_GEMINI, _GEMINI) is False


def test_mismatch_true_when_provider_bearing_stamp_differs() -> None:
    # Config on stub, index built with gemini → mismatch (the #65 scenario).
    assert is_index_embedder_mismatch(_STUB, _GEMINI) is True


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


def test_build_embedder_openrouter_builds_real_client() -> None:
    embedder = build_embedder(
        Settings(
            embedding_provider="openrouter",
            embedding_model="openai/text-embedding-3-small",
            openrouter_api_key="or-123",
        )
    )
    assert isinstance(embedder, OpenRouterEmbedder)
    assert embedder.dim == Settings().embedding_dim


def test_build_embedder_openrouter_missing_key_raises() -> None:
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_embedder(
            Settings(
                embedding_provider="openrouter",
                embedding_model="openai/text-embedding-3-small",
                openrouter_api_key=None,
            )
        )


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
    # The singleton is METERED (#153), so the concrete provider lives one level in.
    # ``shutdown_embedder`` must still reach through the decorator and close the
    # real httpx pool — a wrapper that swallowed ``aclose`` would leak a socket per
    # config reload with nothing to show for it.
    assert isinstance(embedder, MeteredEmbedder)
    assert isinstance(embedder.inner, GeminiEmbedder)
    inner = embedder.inner
    # Close via the factory; a second get builds a fresh instance.
    asyncio.run(emb_factory.shutdown_embedder())
    assert inner._http_client.is_closed, "shutdown did not reach the wrapped client"
    rebuilt = emb_factory.get_embedder(settings)
    assert rebuilt is not embedder


# -- startup guard ----------------------------------------------------------


def test_validate_rejects_unknown_provider() -> None:
    with pytest.raises(RuntimeError, match="not supported"):
        validate_embedder_provider(Settings(embedding_provider="voyage"))


def test_validate_rejects_dim_mismatch_with_pgvector_column() -> None:
    # 768 != the pgvector column dim (1536) → fail fast at boot.
    with pytest.raises(RuntimeError, match="does not match the .*pgvector"):
        validate_embedder_provider(Settings(embedding_dim=768))


def test_validate_accepts_matching_dim() -> None:
    # Default dim (1536) matches the column; no raise.
    validate_embedder_provider(Settings())


def test_validate_allows_stub_outside_production() -> None:
    validate_embedder_provider(Settings(environment="local", embedding_provider="stub"))


def test_validate_rejects_stub_in_production() -> None:
    # Production also requires real LLM + admin key; set them so only the
    # embedding guard is under test.
    settings = Settings(
        environment="production",
        demo_api_key="prod-demo-key",
        embedding_provider="stub",
        llm_provider="gemini",
        gemini_api_key="k",
        admin_api_key="a-strong-secret",
    )
    with pytest.raises(EmbeddingProviderNotConfigured):
        validate_embedder_provider(settings)
