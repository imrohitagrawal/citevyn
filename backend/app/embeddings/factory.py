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

import enum
import inspect
import logging
from typing import NamedTuple

from app.core.config import Settings
from app.embeddings.gemini import GeminiEmbedder
from app.embeddings.openrouter import OpenRouterEmbedder
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


class IndexStampStatus(enum.StrEnum):
    """A non-identity outcome of resolving "which embedder built the index we query".

    Exists because ``None`` was being asked to carry two incompatible meanings
    whose safe answers are opposites (#226):

    * ``None`` — *unknown* provenance. There is no index row to read, or the row
      carries a NULL ``embedding_provider`` (legacy / stub-seeded). Nothing claims
      these vectors were built by anyone in particular, so the read path
      **allows** the vector arm; denying instead would take semantic retrieval
      offline for the seeded demo and every pre-#51 index.
    * :attr:`ambiguous` — *ambiguous* provenance. The resolver found more than one
      candidate index and cannot say whose vector space the arm would be scoring.
      One of them may well be mismatched, so "allow" is a coin flip on silent
      cosine corruption and this **fails closed**.

    A distinct type rather than a magic :class:`EmbedderIdentity` value: an
    identity-shaped sentinel is a ``NamedTuple``, so it would compare equal to any
    real stamp with the same fields and would answer ``.provider`` lookups as
    though it were one. This cannot be mistaken for a stamp, and it narrows
    cleanly for the type checker.
    """

    ambiguous = "ambiguous"


def is_index_embedder_mismatch(
    configured: EmbedderIdentity,
    index_stamp: EmbedderIdentity | IndexStampStatus | None,
) -> bool:
    """Whether the configured query embedder disagrees with the index being queried.

    The single source of truth for the read-time Tier-3 allow/degrade decision
    (#71). The canonical enforcement point
    (:meth:`app.retrieval.hybrid.HybridRetriever._vector_arm_enabled`, #57)
    delegates to it, and :func:`app.services.index_health.active_index_vector_health`
    reuses it so ``GET /health/index`` reports the same verdict the read path
    would reach. Any second implementation of this comparison is a bug.

    The three answers, in the order they are decided:

    * :attr:`IndexStampStatus.ambiguous` ⇒ ``True`` (**fail closed**, #226). More
      than one index could be the one being queried, so the provenance of the
      vectors the arm would score is genuinely unknowable — not merely unrecorded.
    * ``None``, or a stamp whose ``provider`` is ``None`` ⇒ ``False``
      ("unknown provenance ⇒ allow"). Legacy and stub-seeded indexes record no
      provider; refusing them would take the vector arm offline for the seeded
      demo and every pre-#51 index. This arm is load-bearing — do not "harden"
      it into a deny.
    * otherwise ⇒ ``stamp != configured``. Only a provider-bearing stamp that
      differs degrades the arm.
    """
    if isinstance(index_stamp, IndexStampStatus):
        return True
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
ALLOWED_EMBEDDING_PROVIDERS: frozenset[str] = frozenset({"stub", "gemini", "openrouter"})

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
    if settings.embedding_provider == "openrouter":
        return OpenRouterEmbedder(
            model=settings.embedding_model,
            api_key=settings.openrouter_api_key,
            api_base=settings.openrouter_api_base,
            dim=settings.embedding_dim,
            timeout_seconds=settings.embedding_timeout_seconds,
            max_retries=settings.embedding_max_retries,
        )
    return StubEmbedder(dim=settings.embedding_dim)


def metered_embedder(embedder: Embedder, settings: Settings) -> Embedder:
    """Wrap a PAID embedder so every embedding call is recorded as spend (#153).

    Applied at the production construction sites — :func:`get_embedder` (the API
    singleton, query path) and ``app.worker.cli.build_runner`` (ingest) — rather
    than inside :func:`build_embedder`, which stays **provider selection only**.
    That split mirrors ``build_llm_client``/``get_llm_client`` and matters for two
    concrete callers: ``tests/eval/retrieval.py`` and ``tests/eval/distractors.py``
    deliberately use ``build_embedder`` to avoid the singleton, and a test that
    asserts *which provider a config selects* must not have to unwrap a decorator.

    The **stub is deliberately not wrapped**, for the same reason
    :func:`app.llm.factory._metered` does not wrap ``StubLLMClient``: safety
    mechanisms test the client's identity. ``tests/eval/retrieval.py`` and
    ``tests/eval/distractors.py`` both branch on
    ``isinstance(embedder, StubEmbedder)`` to skip the vector arm on the free
    hermetic path, and ``tests/test_eval_semantic_discrimination.py`` asserts
    ``not isinstance(..., StubEmbedder)`` to prove a REAL embedder is configured.
    Behind a decorator the first two go False — the eval would run a hash-bucket
    "vector" arm and quietly report meaningless retrieval numbers — and the third
    would pass while the stub was still in place. The stub also has nothing to
    meter: no network call, no cost.
    """
    if isinstance(embedder, StubEmbedder):
        return embedder
    from app.cost.metered import MeteredEmbedder

    return MeteredEmbedder(
        embedder,
        # Taken from Settings because the Embedder seam returns bare vectors and
        # carries no provider/model metadata. Exact by construction: these are the
        # same three values ``configured_embedder_identity`` calls the vector
        # space's identity, and the embedder above was built from them.
        provider=settings.embedding_provider,
        model=settings.embedding_model,
    )


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
        _embedder = metered_embedder(build_embedder(settings), settings)
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
    "IndexStampStatus",
    "build_embedder",
    "configured_embedder_identity",
    "get_embedder",
    "is_index_embedder_mismatch",
    "metered_embedder",
    "reset_embedder",
    "shutdown_embedder",
    "validate_embedder_provider",
]
