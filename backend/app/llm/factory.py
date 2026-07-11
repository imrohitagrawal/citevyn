"""Factory and process-wide singleton for the :class:`LLMClient`.

The factory builds the LLM client selected by :attr:`Settings.llm_provider`.
In production the singleton is reused across requests so the underlying
``httpx.AsyncClient`` and its connection pool are not recreated per call
(Slice 8 review finding: per-request construction leaked sockets).
:func:`shutdown_llm_client` closes the singleton and is wired to the
FastAPI ``lifespan`` shutdown event in :mod:`app.main`.

The factory never raises on missing API keys for the stub path; the
anthropic path raises eagerly so a misconfigured production deploy
fails at startup instead of on the first request.
"""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.llm.anthropic import AnthropicLLMClient
from app.llm.fallback import FallbackLLMClient
from app.llm.gemini import GeminiLLMClient
from app.llm.openrouter import OpenRouterLLMClient
from app.llm.protocol import LLMClient
from app.llm.stub import StubLLMClient

_logger = logging.getLogger("citevyn.llm")

# ---------------------------------------------------------------------------
# Production guard
# ---------------------------------------------------------------------------

# Production deploys MUST override the default ``CITEVYN_LLM_PROVIDER="stub"``
# to a real provider. A startup check in :mod:`app.main` raises on this
# combination so an operator cannot accidentally ship canned answers.
ALLOWED_LLM_PROVIDERS: frozenset[str] = frozenset({"stub", "anthropic", "gemini", "router"})


class LLMProviderNotConfigured(RuntimeError):
    """Raised at startup when production deploys use the stub provider."""


def validate_llm_provider(settings: Settings) -> None:
    """Reject ``stub`` in production; tolerate it everywhere else.

    Called from :func:`app.main.create_app` so a misconfigured deploy
    fails immediately at boot rather than on the first ask.
    """
    if settings.llm_provider not in ALLOWED_LLM_PROVIDERS:
        raise RuntimeError(
            f"CITEVYN_LLM_PROVIDER={settings.llm_provider!r} is not supported. "
            f"Allowed values: {sorted(ALLOWED_LLM_PROVIDERS)}."
        )
    if settings.environment == "production" and settings.llm_provider == "stub":
        raise LLMProviderNotConfigured(
            "CITEVYN_LLM_PROVIDER='stub' is not allowed when "
            "CITEVYN_ENVIRONMENT='production'. Set "
            "CITEVYN_LLM_PROVIDER to 'anthropic', 'gemini', or '' "
            "and provide the matching API key."
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_llm_client(settings: Settings) -> LLMClient:
    """Return the LLM client selected by ``settings.llm_provider``.

    The factory never raises on missing API keys for the stub path;
    the anthropic path raises eagerly so a misconfigured production
    deploy fails at startup instead of on the first request.
    """
    if settings.llm_provider == "anthropic":
        return AnthropicLLMClient(
            model=settings.llm_model,
            api_key=settings.anthropic_api_key,
            api_base=settings.anthropic_api_base,
            api_version=settings.anthropic_api_version,
            timeout_seconds=settings.anthropic_timeout_seconds,
        )
    if settings.llm_provider == "gemini":
        return _build_gemini_with_fallback(settings)
    if settings.llm_provider == "router":
        return _build_openrouter(settings)
    return StubLLMClient(model=f"stub-{settings.llm_model}")


def _build_openrouter(settings: Settings) -> OpenRouterLLMClient:
    """Build the OpenRouter client. Raises if no key is configured."""
    return OpenRouterLLMClient(
        model=settings.openrouter_model,
        api_key=settings.openrouter_api_key,
        api_base=settings.openrouter_api_base,
        timeout_seconds=settings.openrouter_timeout_seconds,
    )


def _build_gemini_with_fallback(settings: Settings) -> LLMClient:
    """Build the Gemini client, wrapped with an OpenRouter fallback.

    Resolution order for ``CITEVYN_LLM_PROVIDER=gemini``:

    * Gemini key set + OpenRouter key set → Gemini primary, OpenRouter
      backstop (:class:`FallbackLLMClient`).
    * Gemini key set only → Gemini alone.
    * Gemini key unset but OpenRouter key set → OpenRouter alone (so a
      single key of either kind is enough to get real answers).
    * Neither key set → the stub in non-production so the app still boots;
      production is already blocked from ``stub`` by the startup validator,
      and a keyless real provider raises there via the client constructors.
    """
    has_gemini = bool(settings.gemini_api_key)
    has_router = bool(settings.openrouter_api_key)

    if not has_gemini and not has_router:
        if settings.environment == "production":
            raise RuntimeError(
                "CITEVYN_LLM_PROVIDER=gemini requires CITEVYN_GEMINI_API_KEY "
                "(or CITEVYN_OPENROUTER_API_KEY for the fallback) in production."
            )
        _logger.warning("gemini_provider_no_key_using_stub")
        return StubLLMClient(model=f"stub-{settings.gemini_model}")

    if not has_gemini:
        # The no-key case returned above, so reaching here without a Gemini key
        # means the OpenRouter key is present — use it directly.
        return _build_openrouter(settings)

    gemini = GeminiLLMClient(
        model=settings.gemini_model,
        api_key=settings.gemini_api_key,
        api_base=settings.gemini_api_base,
        timeout_seconds=settings.gemini_timeout_seconds,
        thinking_budget=settings.gemini_thinking_budget,
    )
    if not has_router:
        return gemini
    return FallbackLLMClient(primary=gemini, secondary=_build_openrouter(settings))


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


# The process-wide singleton holds the shared ``LLMClient`` (and, for
# the real HTTP clients, its ``httpx.AsyncClient`` connection pool).
# Constructed lazily on first use so test code that imports the module
# without a settings instance does not trigger a network open.
_client: LLMClient | None = None


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Return the process-wide :class:`LLMClient`, building it lazily.

    On the first call the factory builds the client from ``settings``
    (or :func:`get_settings` if not provided) and caches it. Subsequent
    calls return the same instance so the underlying ``httpx.AsyncClient``
    is reused.

    The factory is the only place where the construction lives — when
    the settings change (e.g. in tests via ``clear_settings_cache``)
    callers should use :func:`reset_llm_client` and rebuild.
    """
    global _client
    if _client is None:
        if settings is None:
            from app.core.config import get_settings

            settings = get_settings()
        _client = build_llm_client(settings)
        _logger.info(
            "llm_client_initialized",
            extra={"provider": settings.llm_provider, "model": settings.llm_model},
        )
    return _client


async def shutdown_llm_client() -> None:
    """Close the shared :class:`LLMClient` if it owns resources.

    Wired to the FastAPI ``lifespan`` shutdown event so the underlying
    ``httpx.AsyncClient`` connection pool is released cleanly when the
    process exits. Calling this when no client is built, or after a
    previous shutdown, is a no-op.
    """
    global _client
    if _client is None:
        return
    aclose = getattr(_client, "aclose", None)
    if callable(aclose):
        try:
            result = aclose()
            import inspect

            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - defensive: shutdown must never raise
            _logger.exception("llm_client_close_failed")
    _client = None


def reset_llm_client() -> None:
    """Drop the singleton without closing its resources.

    Test-only helper. Production code paths must use
    :func:`shutdown_llm_client` so the ``httpx`` connection pool is
    released.
    """
    global _client
    _client = None


__all__ = [
    "ALLOWED_LLM_PROVIDERS",
    "LLMProviderNotConfigured",
    "build_llm_client",
    "get_llm_client",
    "reset_llm_client",
    "shutdown_llm_client",
    "validate_llm_provider",
]
