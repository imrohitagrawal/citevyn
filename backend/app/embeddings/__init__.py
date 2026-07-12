"""Embedding provider seam.

Single source of truth for computing embedding vectors. The write path
(:mod:`app.worker`) and the read path (:mod:`app.retrieval`) both build their
embedder from this package via :func:`build_embedder`, so a query vector and the
stored document vectors always come from the same model in the same vector space.

See ``docs/ADR/0003-embeddings-provider.md`` for the provider decision (Gemini
``gemini-embedding-001`` @ 1536 dims), the pgvector storage, and the deliberately
deferred Tier 3 cross-provider failover.
"""

from app.embeddings.errors import EmbedderUnavailable
from app.embeddings.factory import (
    ALLOWED_EMBEDDING_PROVIDERS,
    EmbedderIdentity,
    EmbeddingProviderNotConfigured,
    build_embedder,
    configured_embedder_identity,
    get_embedder,
    is_index_embedder_mismatch,
    reset_embedder,
    shutdown_embedder,
    validate_embedder_provider,
)
from app.embeddings.gemini import GeminiEmbedder
from app.embeddings.protocol import Embedder
from app.embeddings.stub import StubEmbedder

__all__ = [
    "ALLOWED_EMBEDDING_PROVIDERS",
    "Embedder",
    "EmbedderIdentity",
    "EmbedderUnavailable",
    "EmbeddingProviderNotConfigured",
    "GeminiEmbedder",
    "StubEmbedder",
    "build_embedder",
    "configured_embedder_identity",
    "get_embedder",
    "is_index_embedder_mismatch",
    "reset_embedder",
    "shutdown_embedder",
    "validate_embedder_provider",
]
