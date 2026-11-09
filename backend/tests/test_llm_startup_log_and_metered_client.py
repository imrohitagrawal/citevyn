"""The startup log names the models that answer, and the judged eval's extra
clients are metered (#596).

Each test names the change that turns it red.
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import Settings
from app.core.logging import build_log_formatter
from app.llm import factory as llm_factory
from app.llm.openrouter import OpenRouterLLMClient


@pytest.mark.parametrize(
    ("overrides", "model", "fallback"),
    [
        (
            {"llm_provider": "gemini", "gemini_api_key": "g", "openrouter_api_key": "o"},
            "gemini-3.6-flash",
            "openai/gpt-4o-mini",
        ),
        ({"llm_provider": "gemini", "gemini_api_key": "g"}, "gemini-3.6-flash", None),
        ({"llm_provider": "gemini", "openrouter_api_key": "o"}, "openai/gpt-4o-mini", None),
        ({"llm_provider": "router", "openrouter_api_key": "o"}, "openai/gpt-4o-mini", None),
        ({"llm_provider": "anthropic", "anthropic_api_key": "a"}, "claude-opus-4-8", None),
    ],
)
def test_startup_log_names_the_models_in_use(
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, str],
    model: str,
    fallback: str | None,
) -> None:
    """Red when: the log prints ``llm_model`` for a provider that ignores it (the
    production log said ``provider='gemini' model='claude-opus-4-8'``), or omits the
    fallback model that answers when Gemini fails."""
    settings = Settings(_env_file=None, **overrides)  # type: ignore[call-arg,arg-type]
    llm_factory.reset_llm_client()
    try:
        with caplog.at_level(logging.INFO, logger="citevyn.llm"):
            llm_factory.get_llm_client(settings)
    finally:
        llm_factory.reset_llm_client()
    records = [r for r in caplog.records if r.getMessage() == "llm_client_initialized"]
    assert len(records) == 1
    assert records[0].model == model  # type: ignore[attr-defined]
    assert records[0].fallback_model == fallback  # type: ignore[attr-defined]
    # The line production writes, through production's own formatter: a field the
    # redactor does not allow prints as ``<str len=N>``, which a raw-record check
    # cannot see. Red when: ``fallback_model`` or ``model`` leaves the allowlist.
    line = build_log_formatter().format(records[0])
    assert f"model={model!r}" in line
    assert f"fallback_model={fallback!r}" in line


def test_build_metered_llm_client_meters_a_new_client_and_leaves_the_stub_bare() -> None:
    """Red when: the fallback pass's or grader's client is built unmetered (their
    spend would not be recorded), or it reuses the process-wide client, or the
    stub gets wrapped (which hides the identity the judged half's self-skip reads)."""
    from app.cost.metered import MeteredLLMClient
    from app.llm.stub import StubLLMClient

    llm_factory.reset_llm_client()
    try:
        router = Settings(
            _env_file=None,  # type: ignore[call-arg]
            llm_provider="router",
            openrouter_api_key="or-test",
            openrouter_model="openai/gpt-4o-mini",
        )
        first = llm_factory.build_metered_llm_client(router)
        second = llm_factory.build_metered_llm_client(router)
        assert isinstance(first, MeteredLLMClient)
        # Built from the settings it was given, as its own client, not a second
        # wrapper around the shared one's inner client.
        assert isinstance(first.inner, OpenRouterLLMClient)
        assert first.inner._model == "openai/gpt-4o-mini"
        assert first.inner is not llm_factory.get_llm_client(router).inner  # type: ignore[attr-defined]
        assert first is not second
        assert first is not llm_factory.get_llm_client(router)
        stub = llm_factory.build_metered_llm_client(Settings(_env_file=None))  # type: ignore[call-arg]
        assert isinstance(stub, StubLLMClient)
    finally:
        llm_factory.reset_llm_client()
