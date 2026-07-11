"""Factory and process-wide singleton for the :class:`Embedder`.

Mirrors :mod:`app.llm.factory`:

* :func:`build_embedder` selects the embedder by ``settings.embedding_provider``.
* The stub is the safe offline default and never raises on a missing key; the real
  ``gemini`` provider raises eagerly on a missing key so a misconfigured deploy
  fails at startup, not on the first ingest/query.
* :func:`get_embedder` caches a process-wide singleton so the underlying
  ``httpx.AsyncClient`` connection pool is reused across requests and across the
  worker's per-chunk calls.
* :func:`shutdown_embedder` closes it, wired to the FastAPI ``lifespan`` shutdown.
* :func:`validate_embedder_provider` is the startup guard: reject unknown providers
  everywhere, and reject the ``stub`` in production.
"""

from __future__ import annotations

import inspect
import logging

from app.core.config import Settings
from app.embeddings.gemini import GeminiEmbedder
from app.embeddings.protocol import Embedder
from app.embeddings.stub import StubEmbedder

_logger = logging.getLogger("citevyn.embeddings")

# Production deploys MUST override the default ``CITEVYN_EMBEDDING_PROVIDER="stub"``
# to a real provider so retrieval is semantic, not hash-bucketed.
ALLOWED_EMBEDDING_PROVIDERS: frozenset[str] = frozenset({"stub", "gemini"})


class EmbeddingProviderNotConfigured(RuntimeError):
    """Raised at startup when a production deploy uses the stub embedder."""


def validate_embedder_provider(settings: Settings) -> None:
    """Reject unknown providers everywhere and the ``stub`` in production.

    Called from :func:`app.main.create_app`'s lifespan so a misconfigured deploy
    fails at boot rather than silently serving hash-bucketed (non-semantic)
    retrieval.
    """
    if settings.embedding_provider not in ALLOWED_EMBEDDING_PROVIDERS:
        raise RuntimeError(
            f"CITEVYN_EMBEDDING_PROVIDER={settings.embedding_provider!r} is not supported. "
            f"Allowed values: {sorted(ALLOWED_EMBEDDING_PROVIDERS)}."
        )
    if settings.environment == "production" and settings.embedding_provider == "stub":
        raise EmbeddingProviderNotConfigured(
            "CITEVYN_EMBEDDING_PROVIDER='stub' is not allowed when "
            "CITEVYN_ENVIRONMENT='production'. Set CITEVYN_EMBEDDING_PROVIDER='gemini' "
            "and provide CITEVYN_GEMINI_API_KEY."
        )


def build_embedder(settings: Settings) -> Embedder:
    """Return the embedder selected by ``settings.embedding_provider``.

    The stub path never raises on a missing key; the ``gemini`` path raises eagerly
    (via the client constructor) so a misconfigured production deploy fails fast.
    """
    if settings.embedding_provider == "gemini":
        return GeminiEmbedder(
            model=settings.embedding_model,
            api_key=settings.gemini_api_key,
            api_base=settings.gemini_api_base,
            dim=settings.embedding_dim,
            timeout_seconds=settings.embedding_timeout_seconds,
            max_retries=settings.embedding_max_retries,
        )
    return StubEmbedder(dim=settings.embedding_dim)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_embedder: Embedder | None = None


def get_embedder(settings: Settings | None = None) -> Embedder:
    """Return the process-wide :class:`Embedder`, building it lazily.

    Subsequent calls return the same instance so a real client's
    ``httpx.AsyncClient`` pool is reused. Use :func:`reset_embedder` in tests when
    settings change.
    """
    global _embedder
    if _embedder is None:
        if settings is None:
            from app.core.config import get_settings

            settings = get_settings()
        _embedder = build_embedder(settings)
        _logger.info(
            "embedder_initialized",
            extra={
                "provider": settings.embedding_provider,
                "model": settings.embedding_model,
                "dim": settings.embedding_dim,
            },
        )
    return _embedder


async def shutdown_embedder() -> None:
    """Close the shared :class:`Embedder` if it owns resources.

    Wired to the FastAPI ``lifespan`` shutdown so a real client's connection pool
    is released cleanly. A no-op when no embedder was built. Never raises.
    """
    global _embedder
    if _embedder is None:
        return
    aclose = getattr(_embedder, "aclose", None)
    if callable(aclose):
        try:
            result = aclose()
            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - defensive: shutdown must never raise
            _logger.exception("embedder_close_failed")
    _embedder = None


def reset_embedder() -> None:
    """Drop the singleton without closing its resources (test-only)."""
    global _embedder
    _embedder = None


__all__ = [
    "ALLOWED_EMBEDDING_PROVIDERS",
    "EmbeddingProviderNotConfigured",
    "build_embedder",
    "get_embedder",
    "reset_embedder",
    "shutdown_embedder",
    "validate_embedder_provider",
]
