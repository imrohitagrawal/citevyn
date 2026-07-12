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
from typing import NamedTuple

from app.core.config import Settings
from app.embeddings.gemini import GeminiEmbedder
from app.embeddings.protocol import Embedder
from app.embeddings.stub import StubEmbedder

_logger = logging.getLogger("citevyn.embeddings")


class EmbedderIdentity(NamedTuple):
    """The provenance triple that identifies an embedding vector space.

    The same shape describes both the *configured* query embedder
    (:func:`configured_embedder_identity`, always fully populated) and the
    *stamp* written onto an ``IndexVersion`` at ingest
    (``embedding_provider/model/dim``, which may be ``None`` for legacy/stub
    indexes). Two indexes are query-compatible only when their identities are
    equal — cosine distance across different providers/models/dims is
    meaningless. See ``docs/ADR/0003-embeddings-provider.md`` (Tier 3).
    """

    provider: str | None
    model: str | None
    dim: int | None

    def cache_key_component(self) -> str:
        """A stable string encoding of the identity for the answer-cache key (#65).

        The answer cache keys on the *configured* query embedder so a
        config-only embedder swap (which leaves ``source_version_hash``
        unchanged) invalidates affected entries instead of serving an answer
        built in a different vector space. Only the ``provider/model/dim``
        triple is encoded — never an API key or any secret — so the key
        pre-image carries no sensitive material.

        ``None`` fields (a legacy / unstamped identity) collapse to empty
        strings, so an all-``None`` identity yields the stable, non-empty
        ``"||"`` rather than a value that could blank or destabilize the key.
        """
        return "|".join(
            (
                self.provider or "",
                self.model or "",
                "" if self.dim is None else str(self.dim),
            )
        )


def is_index_embedder_mismatch(
    configured: EmbedderIdentity, index_stamp: EmbedderIdentity | None
) -> bool:
    """Whether the configured query embedder disagrees with the active index stamp.

    Mirrors the allow-semantics of the read-time Tier-3 gate
    (:meth:`app.retrieval.hybrid.HybridRetriever._vector_arm_enabled`, the
    canonical enforcement point, #57) so the orchestrator can predict the vector
    arm degrade *before* retrieval runs and skip caching a degraded answer (#65)
    without changing the retriever's return shape. The two must stay in agreement
    — both resolve the same active-index stamp and compare it to
    ``configured_embedder_identity``.

    Returns ``False`` (no mismatch — the vector arm runs) when there is no active
    index stamp, when the stamp carries no provider (legacy / stub-seeded,
    "unknown provenance ⇒ allow"), or when the stamp equals ``configured``.
    Returns ``True`` only when a provider-bearing stamp differs from the
    configured identity.
    """
    if index_stamp is None or index_stamp.provider is None:
        return False
    return index_stamp != configured


def configured_embedder_identity(settings: Settings) -> EmbedderIdentity:
    """The identity of the embedder that :func:`get_embedder` builds from ``settings``.

    The process-wide embedder singleton is built from these same three
    ``Settings`` values, so this triple *is* the query embedder's vector-space
    identity. The read-path enforcement (Tier 3, #57) compares it against the
    active ``IndexVersion``'s stamp and degrades the vector arm on a mismatch.
    """
    return EmbedderIdentity(
        provider=settings.embedding_provider,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
    )


# Production deploys MUST override the default ``CITEVYN_EMBEDDING_PROVIDER="stub"``
# to a real provider so retrieval is semantic, not hash-bucketed.
ALLOWED_EMBEDDING_PROVIDERS: frozenset[str] = frozenset({"stub", "gemini"})

# The dimension of the pgvector ``chunks.embedding`` column created by migration
# ``0004`` (``vector(1536)``). ``Settings.embedding_dim`` MUST equal this, because
# the ORM emits a vector of ``settings.embedding_dim`` against a fixed-width
# column; a mismatch fails cryptically at insert time on Postgres. The startup
# guard below turns that into a clear boot-time error. Changing the dimension
# means writing a new migration AND updating this constant in lock-step.
PGVECTOR_COLUMN_DIM: int = 1536


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
    if settings.embedding_dim != PGVECTOR_COLUMN_DIM:
        # The pgvector column is a fixed vector(PGVECTOR_COLUMN_DIM); a mismatched
        # embedding_dim would fail cryptically at first insert on Postgres. Fail
        # fast at boot instead, with a clear message.
        raise RuntimeError(
            f"CITEVYN_EMBEDDING_DIM={settings.embedding_dim} does not match the "
            f"pgvector column dimension ({PGVECTOR_COLUMN_DIM}). Changing the "
            "embedding dimension requires a new migration; see migration 0004 and "
            "docs/ADR/0003-embeddings-provider.md."
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

    ``settings`` is honored ONLY on the first call that builds the singleton;
    subsequent calls return the cached instance and ignore any ``settings`` passed
    (so a real client's ``httpx.AsyncClient`` pool is reused). To rebuild after a
    settings change, call :func:`reset_embedder` (tests) or
    :func:`shutdown_embedder` (production) first.
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
    "EmbedderIdentity",
    "EmbeddingProviderNotConfigured",
    "build_embedder",
    "configured_embedder_identity",
    "get_embedder",
    "is_index_embedder_mismatch",
    "reset_embedder",
    "shutdown_embedder",
    "validate_embedder_provider",
]
