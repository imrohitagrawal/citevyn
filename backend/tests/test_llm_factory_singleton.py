"""Tests for the Slice 9a LLM client singleton + lifecycle."""

from __future__ import annotations

import pytest

from app.llm import factory as llm_factory
from app.llm.anthropic import AnthropicLLMClient
from app.llm.stub import StubLLMClient


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Drop the process-wide singleton before AND after each test."""
    llm_factory.reset_llm_client()
    yield
    llm_factory.reset_llm_client()


def test_get_llm_client_returns_singleton_when_settings_match() -> None:
    """A second call returns the same object — no second ``build_llm_client`` invocation."""
    from app.core.config import Settings

    settings = Settings(llm_provider="stub")
    first = llm_factory.get_llm_client(settings)
    second = llm_factory.get_llm_client(settings)
    assert first is second


def test_get_llm_client_returns_stub_by_default() -> None:
    """The default provider is ``stub`` and returns a :class:`StubLLMClient`."""
    from app.core.config import Settings

    settings = Settings(llm_provider="stub")
    client = llm_factory.get_llm_client(settings)
    assert isinstance(client, StubLLMClient)


def test_get_llm_client_builds_anthropic_when_configured() -> None:
    """Anthropic provider returns the real :class:`AnthropicLLMClient`."""
    from app.core.config import Settings

    settings = Settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-ant-test",
        anthropic_api_base="https://api.anthropic.com",
        anthropic_api_version="2023-06-01",
        anthropic_timeout_seconds=5.0,
        llm_model="claude-sonnet-4-6",
    )
    client = llm_factory.get_llm_client(settings)
    # get_llm_client returns the WIRED client, which for a paid provider is wrapped
    # in the cost-metering decorator (#153 Layer 1). Look through it: the assertion
    # here is about which provider the config selects, not about the wiring.
    from app.cost.metered import MeteredLLMClient

    assert isinstance(client, MeteredLLMClient)
    assert isinstance(client.inner, AnthropicLLMClient)


def test_anthropic_factory_raises_on_missing_api_key() -> None:
    """The Anthropic path raises eagerly on missing API key."""
    from app.core.config import Settings

    settings = Settings(llm_provider="anthropic", anthropic_api_key=None)
    with pytest.raises(RuntimeError, match="CITEVYN_ANTHROPIC_API_KEY is required"):
        llm_factory.build_llm_client(settings)


def test_shutdown_llm_client_closes_underlying_httpx() -> None:
    """``shutdown_llm_client`` calls ``aclose`` on the real Anthropic client."""
    from app.core.config import Settings

    settings = Settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-ant-test",
        anthropic_api_base="https://api.anthropic.com",
        anthropic_api_version="2023-06-01",
        anthropic_timeout_seconds=5.0,
    )
    client = llm_factory.get_llm_client(settings)
    # Spy on aclose without monkey-patching the class globally
    closed = {"count": 0}

    async def fake_aclose() -> None:
        closed["count"] += 1

    client.aclose = fake_aclose  # type: ignore[method-assign]
    import asyncio

    asyncio.run(llm_factory.shutdown_llm_client())
    assert closed["count"] == 1


def test_shutdown_llm_client_is_noop_without_singleton() -> None:
    """Calling shutdown twice or without prior build is safe."""
    import asyncio

    asyncio.run(llm_factory.shutdown_llm_client())
    asyncio.run(llm_factory.shutdown_llm_client())


def test_validate_llm_provider_rejects_unknown_value() -> None:
    """An undeclared provider raises with a clear message."""
    from app.core.config import Settings

    settings = Settings(llm_provider="nope")
    with pytest.raises(RuntimeError, match="not supported"):
        llm_factory.validate_llm_provider(settings)


def test_validate_llm_provider_rejects_stub_in_production() -> None:
    """Production deploys cannot ship with the stub provider.

    The check is enforced at two altitudes (this slice moved the
    check into a :func:`Settings.model_validator` so it fires for any
    code path that constructs ``Settings``, and the runtime
    :func:`validate_llm_provider` is a defensive double-check wired
    into the FastAPI lifespan). We use ``model_construct`` to build
    a Settings instance that bypasses the model_validator so this
    test exercises the runtime guard explicitly.
    """
    from app.core.config import Settings

    settings = Settings.model_construct(
        environment="production",
        demo_api_key="prod-demo-key-0123456789",
        llm_provider="stub",
    )
    with pytest.raises(llm_factory.LLMProviderNotConfigured):
        llm_factory.validate_llm_provider(settings)


def test_settings_constructor_rejects_stub_in_production() -> None:
    """The Settings model_validator rejects stub-in-prod at parse time.

    Companion to the runtime check above — the model_validator is
    the canonical guard and fires for any caller (uvicorn, alembic,
    the worker, a one-off script). The runtime guard is wired into
    the FastAPI lifespan as a defensive double-check.
    """
    from app.core.config import Settings

    with pytest.raises(Exception, match="not allowed when.*production"):
        Settings(
            environment="production", demo_api_key="prod-demo-key-0123456789", llm_provider="stub"
        )


def test_settings_constructor_rejects_empty_llm_provider_in_production() -> None:
    """Empty ``CITEVYN_LLM_PROVIDER`` (the Slice 9b router placeholder)
    must be rejected in production. Otherwise the orchestrator would
    resolve a no-op client and the demo would never be reproducible.
    """
    from app.core.config import Settings

    with pytest.raises(Exception, match="not allowed when.*production"):
        Settings(environment="production", demo_api_key="prod-demo-key-0123456789", llm_provider="")


def test_settings_constructor_accepts_empty_llm_provider_in_development() -> None:
    """The router placeholder is allowed in dev so Slice 9b can be
    iterated without a real LLM key. The runner.py lru_caches settings
    by env, so the dev path stays stable.
    """
    from app.core.config import Settings

    Settings(environment="development", llm_provider="")


def test_validate_llm_provider_accepts_stub_in_development() -> None:
    """Stub is fine in local / test environments."""
    from app.core.config import Settings

    for env in ("local", "development", "test", "ci"):
        settings = Settings(environment=env, llm_provider="stub")
        llm_factory.validate_llm_provider(settings)  # does not raise


def test_validate_llm_provider_accepts_real_provider_in_production() -> None:
    """Real providers pass the production guard.

    Uses ``model_construct`` to bypass the model_validator that
    also checks for the API key — we want this test to focus on
    the runtime provider check.
    """
    from app.core.config import Settings

    settings = Settings.model_construct(
        environment="production",
        demo_api_key="prod-demo-key-0123456789",
        llm_provider="anthropic",
        anthropic_api_key="sk-ant-test",
    )
    llm_factory.validate_llm_provider(settings)


def test_settings_constructor_rejects_missing_anthropic_key_in_production() -> None:
    """The model_validator requires the API key when provider is anthropic."""
    from app.core.config import Settings

    with pytest.raises(Exception, match="CITEVYN_ANTHROPIC_API_KEY"):
        Settings(
            environment="production",
            demo_api_key="prod-demo-key-0123456789",
            llm_provider="anthropic",
        )


def test_settings_constructor_rejects_default_admin_key_in_production() -> None:
    """The model_validator rejects the public default admin key in prod.

    Pass an explicit non-stub LLM provider + API key so the
    LLM-in-production guard does not short-circuit this test (the
    LLM guard runs first because validators fire in declaration
    order).
    """
    from app.core.config import Settings

    with pytest.raises(Exception, match="CITEVYN_ADMIN_API_KEY"):
        Settings(
            environment="production",
            demo_api_key="prod-demo-key-0123456789",
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-test",
            admin_api_key="local-admin-key",
        )


def test_settings_constructor_rejects_default_admin_key_in_production_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model_validator reads ``CITEVYN_ADMIN_API_KEY`` from the environment.

    The operator can trigger the rejection by leaving
    ``CITEVYN_ADMIN_API_KEY`` unset in production (so the
    ``local-admin-key`` default fires). This test pins the
    environment-driven path.
    """
    from app.core.config import Settings

    monkeypatch.setenv("CITEVYN_ENVIRONMENT", "production")
    # Satisfies the demo-key prod guard so this test keeps asserting its own subject.
    monkeypatch.setenv("CITEVYN_DEMO_API_KEY", "a-strong-demo-secret")
    monkeypatch.setenv("CITEVYN_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CITEVYN_ANTHROPIC_API_KEY", "sk-ant-test")
    # ``admin_api_key`` is unset → falls back to default 'local-admin-key'.
    monkeypatch.delenv("CITEVYN_ADMIN_API_KEY", raising=False)
    with pytest.raises(Exception, match="CITEVYN_ADMIN_API_KEY"):
        Settings()


def test_settings_constructor_rejects_default_demo_key_in_production() -> None:
    """``local-demo-key`` is the bearer for EVERY /v1/* route and is published
    in this repo, so accepting it in production leaves the demo effectively
    unauthenticated to anyone who has read the source.

    The admin key has had this guard since Slice 8; the demo key did not, and
    ``infra/docker/prod.env.example`` did not even list the variable — so a
    production deploy silently inherited the default. Found by running
    ``make deploy-verify``, which requires the key and died at preflight.
    """
    from app.core.config import Settings

    with pytest.raises(Exception, match="CITEVYN_DEMO_API_KEY"):
        Settings(
            environment="production",
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-test",
            admin_api_key="a-strong-admin-secret",
            _env_file=None,
        )


def test_settings_accepts_a_real_demo_key_in_production() -> None:
    """The mirror: a strong key passes, so the guard is not simply blocking prod."""
    from app.core.config import Settings

    Settings(
        environment="production",
        llm_provider="anthropic",
        anthropic_api_key="sk-ant-test",
        admin_api_key="a-strong-admin-secret",
        demo_api_key="a-strong-demo-secret",
        _env_file=None,
    )


@pytest.mark.parametrize(
    "weak",
    [
        "local-demo-key",  # the published default
        "local-demo-key ",  # trailing space
        " local-demo-key",  # leading space
        "LOCAL-DEMO-KEY",  # case variant
        "x",  # short
        "short-key",  # under the 16-char floor
        "   ",  # whitespace only
    ],
)
def test_production_rejects_weak_demo_keys(weak: str) -> None:
    """Raw equality was not enough — verified bypasses, now closed.

    Adversarial review confirmed that ``'local-demo-key '``, ``' local-demo-key'``
    and ``'LOCAL-DEMO-KEY'`` all PASSED a plain ``==`` check against a production
    Settings, leaving the effective bearer guessable in one or two attempts.
    Compose's env-file parser happens to strip quotes and trailing whitespace, so
    that path was incidentally safe — but these guards exist for the NON-compose
    entry points (bare uvicorn, alembic, a one-off script), where an exported
    ``KEY='local-demo-key '`` sails straight through.
    """
    from app.core.config import Settings

    with pytest.raises(Exception, match="CITEVYN_DEMO_API_KEY"):
        Settings(
            environment="production",
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-test",
            admin_api_key="a-strong-admin-secret",
            demo_api_key=weak,
            _env_file=None,
        )


def test_production_rejects_weak_admin_keys_too() -> None:
    """The admin guard had the identical weakness and got the identical fix."""
    from app.core.config import Settings

    for weak in ("local-admin-key ", "LOCAL-ADMIN-KEY", "short"):
        with pytest.raises(Exception, match="CITEVYN_ADMIN_API_KEY"):
            Settings(
                environment="production",
                llm_provider="anthropic",
                anthropic_api_key="sk-ant-test",
                admin_api_key=weak,
                demo_api_key="a-strong-demo-secret",
                _env_file=None,
            )
