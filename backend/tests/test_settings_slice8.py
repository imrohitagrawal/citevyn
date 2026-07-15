"""Slice 8: tests for the new :class:`Settings` fields.

Each test pins a single new field's behaviour:

* defaults are stable
* the env override works
* validation rejects bad values
* the CORS env string splits on ``,``
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import (
    DEFAULT_CORS_ALLOWED_ORIGINS,
    Settings,
)


def test_default_cors_origins_is_local_only() -> None:
    """The MVP CORS default is exactly the approved localhost origin.

    A wildcard default would be a cross-origin data leak
    (see ``docs/SECURITY_MODEL.md §11``).
    """
    settings = Settings(_env_file=None)
    assert settings.cors_allowed_origins == list(DEFAULT_CORS_ALLOWED_ORIGINS)
    assert "*" not in settings.cors_allowed_origins


def test_cors_origins_accepts_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CITEVYN_CORS_ALLOWED_ORIGINS`` accepts a comma-separated string.

    Pydantic-settings would otherwise pass a single comma-joined
    string into a ``list[str]`` field, which fails the type check.
    The :func:`field_validator` on :class:`Settings` splits it.
    """
    monkeypatch.setenv(
        "CITEVYN_CORS_ALLOWED_ORIGINS",
        "https://app.example.com, https://staging.example.com,http://localhost:5173",
    )
    settings = Settings(_env_file=None)
    assert settings.cors_allowed_origins == [
        "https://app.example.com",
        "https://staging.example.com",
        "http://localhost:5173",
    ]


def test_admin_api_key_default_present() -> None:
    """The admin API key has a non-empty default for local development."""
    settings = Settings(_env_file=None)
    assert settings.admin_api_key
    assert isinstance(settings.admin_api_key, str)
    assert len(settings.admin_api_key) >= 1


def test_admin_api_key_header_default() -> None:
    """The admin key is read from a custom header (not ``Authorization``)."""
    settings = Settings(_env_file=None)
    assert settings.admin_api_key_header == "X-Admin-API-Key"


def test_rate_limit_defaults() -> None:
    """Default per-hour limits match the security model."""
    settings = Settings(_env_file=None)
    assert settings.rate_limit_enabled is True
    assert settings.rate_limit_demo_user_per_hour == 30
    assert settings.rate_limit_admin_per_hour == 100
    assert settings.rate_limit_window_seconds == 3600


def test_rate_limit_window_must_be_positive() -> None:
    """A zero/negative window is rejected — sliding windows need a width."""
    with pytest.raises(ValidationError):
        Settings(rate_limit_window_seconds=0, _env_file=None)
    with pytest.raises(ValidationError):
        Settings(rate_limit_window_seconds=-1, _env_file=None)


def test_worker_settings_present() -> None:
    """Worker timing defaults are positive and finite."""
    settings = Settings(_env_file=None)
    assert settings.worker_poll_seconds > 0
    assert settings.worker_fetch_timeout_seconds > 0
    assert settings.worker_max_chunks_per_doc >= 1
    # ``worker_max_runtime_seconds == 0`` is the documented "unbounded" sentinel.
    assert settings.worker_max_runtime_seconds == 0


def test_index_promotion_gate_default() -> None:
    """The promotion gate is 0.95, the "golden pass rate >= 95%" threshold."""
    settings = Settings(_env_file=None)
    assert settings.index_promotion_min_pass_rate == 0.95


def test_index_promotion_gate_rejects_out_of_range() -> None:
    """The pass-rate must be a probability in ``[0.0, 1.0]``."""
    with pytest.raises(ValidationError):
        Settings(index_promotion_min_pass_rate=1.5, _env_file=None)
    with pytest.raises(ValidationError):
        Settings(index_promotion_min_pass_rate=-0.1, _env_file=None)


def test_env_override_for_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CITEVYN_RATE_LIMIT_*_PER_HOUR`` env vars override the defaults."""
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_DEMO_USER_PER_HOUR", "5")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_ADMIN_PER_HOUR", "20")
    settings = Settings(_env_file=None)
    assert settings.rate_limit_demo_user_per_hour == 5
    assert settings.rate_limit_admin_per_hour == 20


# ---------------------------------------------------------------------------
# Embedding production guard + dimension coupling (#51)
# ---------------------------------------------------------------------------


def _prod_kwargs(**overrides: object) -> dict[str, object]:
    """Baseline production Settings kwargs that satisfy the OTHER prod guards,
    so a single embedding-specific guard can be exercised in isolation."""
    base: dict[str, object] = {
        "environment": "production",
        "llm_provider": "gemini",  # allowed in prod; key checked at client build
        "admin_api_key": "a-strong-admin-secret",  # not the rejected default
        "_env_file": None,
    }
    base.update(overrides)
    return base


def test_gemini_embeddings_in_production_requires_key() -> None:
    """provider=gemini + no Gemini key in production fails at parse time."""
    with pytest.raises(ValidationError, match="CITEVYN_GEMINI_API_KEY"):
        Settings(**_prod_kwargs(embedding_provider="gemini", gemini_api_key=None))


def test_gemini_embeddings_in_production_ok_with_key() -> None:
    """provider=gemini + key present is accepted in production."""
    settings = Settings(**_prod_kwargs(embedding_provider="gemini", gemini_api_key="gk-123"))
    assert settings.embedding_provider == "gemini"


def test_openrouter_embeddings_in_production_requires_key() -> None:
    """provider=openrouter + no OpenRouter key in production fails at parse time."""
    with pytest.raises(ValidationError, match="CITEVYN_OPENROUTER_API_KEY"):
        Settings(
            **_prod_kwargs(
                embedding_provider="openrouter",
                embedding_model="openai/text-embedding-3-small",
                openrouter_api_key=None,
            )
        )


def test_openrouter_embeddings_in_production_ok_with_key() -> None:
    settings = Settings(
        **_prod_kwargs(
            embedding_provider="openrouter",
            embedding_model="openai/text-embedding-3-small",
            openrouter_api_key="or-123",
        )
    )
    assert settings.embedding_provider == "openrouter"


def test_openrouter_provider_rejects_gemini_shaped_model() -> None:
    """The provider/model coherence guard: openrouter + the Gemini default model
    (the easy misconfig) is rejected at parse time with an actionable message,
    in any environment."""
    with pytest.raises(ValidationError, match="openai/text-embedding-3-small"):
        Settings(
            environment="local",
            embedding_provider="openrouter",
            embedding_model="gemini-embedding-001",
            openrouter_api_key="or-123",
            _env_file=None,
        )


def test_embedding_dim_is_locked_to_pgvector_column_and_migration() -> None:
    """The three dimension sources agree: Settings default, the boot-guard
    constant, and migration 0004's ``vector(<dim>)`` literal. This is the only
    real drift gap across the immutable-migration boundary (arch review)."""
    import importlib.util
    from pathlib import Path

    from app.embeddings.factory import PGVECTOR_COLUMN_DIM

    assert Settings(_env_file=None).embedding_dim == PGVECTOR_COLUMN_DIM

    migration_path = (
        Path(__file__).resolve().parents[2] / "db" / "versions" / "0004_pgvector_embedding.py"
    )
    spec = importlib.util.spec_from_file_location("_mig0004", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._EMBEDDING_DIM == PGVECTOR_COLUMN_DIM
