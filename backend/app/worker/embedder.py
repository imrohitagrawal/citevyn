"""Worker embedding seam (compatibility shim).

The embedder used to live here as a synchronous, hash-only stub. As of #51 the
single source of truth is :mod:`app.embeddings` — one async provider seam shared
by the ingest (write) path and the retrieval (read) path, so a stored document
vector and a query vector always come from the same model in the same vector
space.

This module re-exports the seam so existing imports (``from app.worker.embedder
import Embedder, StubEmbedder``) keep working. New code should import from
:mod:`app.embeddings` directly.

See ``docs/ADR/0003-embeddings-provider.md``.
"""

from __future__ import annotations

from app.embeddings import Embedder, StubEmbedder, build_embedder

__all__ = [
    "Embedder",
    "StubEmbedder",
    "build_embedder",
]
