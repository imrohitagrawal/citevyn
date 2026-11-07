"""OpenRouter reasoning effort, and a startup log that names the models in use (#596).

The judged eval reaches production's Gemini model through OpenRouter. Production
sends Gemini a thinking level; OpenRouter's equivalent is ``reasoning.effort``.
Production's own OpenRouter client (the fallback, ``gpt-4o-mini``) sends none.

Each test names the change that turns it red.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.core.config import Settings
from app.llm import factory as llm_factory
from app.llm.openrouter import OpenRouterLLMClient

_OK = {"choices": [{"message": {"content": "x"}}], "usage": {}}


async def _payload(**client_kwargs: object) -> dict[str, object]:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_OK)

    client = OpenRouterLLMClient(
        model="google/gemini-3.6-flash",
        api_key="or-test",
        api_base="https://openrouter.ai/api/v1",
        timeout_seconds=5.0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **client_kwargs,  # type: ignore[arg-type]
    )
    try:
        await client.complete(system="s", user="q", max_tokens=64, temperature=0.0)
    finally:
        await client.aclose()
    return seen


async def test_reasoning_effort_is_sent_with_headroom() -> None:
    """Red when: the effort is not sent, or the thinking allowance is not added."""
    seen = await _payload(reasoning_effort="minimal", reasoning_headroom_tokens=256)
    assert seen["reasoning"] == {"effort": "minimal"}
    assert seen["max_tokens"] == 64 + 256


async def test_no_reasoning_by_default() -> None:
    """Red when: a client built without an effort (production's fallback) sends a
    reasoning field or a raised output cap."""
    seen = await _payload()
    assert "reasoning" not in seen
    assert seen["max_tokens"] == 64


async def test_headroom_alone_changes_nothing() -> None:
    """Red when: the allowance is added although no reasoning was asked for."""
    seen = await _payload(reasoning_headroom_tokens=256)
    assert "reasoning" not in seen
    assert seen["max_tokens"] == 64


def test_factory_passes_the_effort_from_settings() -> None:
    """Red when: ``CITEVYN_OPENROUTER_REASONING_EFFORT`` does not reach the client."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="router",
        openrouter_api_key="or-test",
        openrouter_reasoning_effort="minimal",
        gemini_thinking_headroom_tokens=128,
    )
    client = llm_factory.build_llm_client(settings)
    assert isinstance(client, OpenRouterLLMClient)
    assert client._reasoning_effort == "minimal"
    assert client._reasoning_headroom_tokens == 128


def test_reasoning_effort_rejects_an_unknown_value() -> None:
    """Red when: the setting accepts a value OpenRouter does not define."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, openrouter_reasoning_effort="extreme")  # type: ignore[call-arg,arg-type]


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
