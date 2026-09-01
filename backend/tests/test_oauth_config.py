"""Tests for the OAuth ``Settings`` guards (ADR-0004 PR 12).

Mirrors the existing production-guard test shape (e.g.
``_reject_default_demo_key_in_production``) rather than introducing a new
pattern.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _base_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "environment": "local",
        "llm_provider": "stub",
        "embedding_provider": "stub",
    }
    kwargs.update(overrides)
    return kwargs


def test_half_configured_github_is_rejected_in_any_environment() -> None:
    """A config bug in ANY environment, not just prod -- unlike the
    production-only guards, this validator is unconditional."""
    with pytest.raises(ValidationError, match="GITHUB_OAUTH_CLIENT_ID"):
        Settings(**_base_kwargs(github_oauth_client_id="only-id-no-secret"))


def test_half_configured_google_is_rejected_the_other_direction() -> None:
    with pytest.raises(ValidationError, match="GOOGLE_OAUTH_CLIENT_ID"):
        Settings(**_base_kwargs(google_oauth_client_secret="only-secret-no-id"))


def test_fully_configured_or_fully_unset_providers_are_accepted() -> None:
    Settings(**_base_kwargs())  # both unset
    Settings(
        **_base_kwargs(
            github_oauth_client_id="id",
            github_oauth_client_secret="secret",
            google_oauth_client_id="id",
            google_oauth_client_secret="secret",
        )
    )


def test_missing_redirect_base_url_is_rejected_in_production_when_configured() -> None:
    with pytest.raises(ValidationError, match="OAUTH_REDIRECT_BASE_URL"):
        Settings(
            **_base_kwargs(
                environment="production",
                llm_provider="anthropic",
                anthropic_api_key="test-key",
                embedding_provider="stub",
                demo_api_key="a-strong-demo-key-not-the-default-1234",
                admin_api_key="a-strong-admin-key-not-the-default-1234",
                github_oauth_client_id="id",
                github_oauth_client_secret="secret",
            )
        )


def test_redirect_base_url_not_required_in_production_when_no_provider_configured() -> None:
    Settings(
        **_base_kwargs(
            environment="production",
            llm_provider="anthropic",
            anthropic_api_key="test-key",
            embedding_provider="stub",
            demo_api_key="a-strong-demo-key-not-the-default-1234",
            admin_api_key="a-strong-admin-key-not-the-default-1234",
        )
    )


def test_redirect_base_url_set_satisfies_the_production_guard() -> None:
    Settings(
        **_base_kwargs(
            environment="production",
            llm_provider="anthropic",
            anthropic_api_key="test-key",
            embedding_provider="stub",
            demo_api_key="a-strong-demo-key-not-the-default-1234",
            admin_api_key="a-strong-admin-key-not-the-default-1234",
            github_oauth_client_id="id",
            github_oauth_client_secret="secret",
            oauth_redirect_base_url="https://citevyn.example",
        )
    )
