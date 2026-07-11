"""Slice 9b LLM provider tests: Gemini + OpenRouter + fallback + factory.

All HTTP is faked with ``httpx.MockTransport`` — no network. Covers:

* :class:`GeminiLLMClient` / :class:`OpenRouterLLMClient` happy path
  (text + token extraction + provider tag) and error surfacing (5xx, 429,
  timeout, missing key).
* :class:`FallbackLLMClient` uses the secondary only when the primary raises
  :class:`LLMUnavailable`, and never when the primary succeeds.
* :func:`build_llm_client` resolves ``gemini`` / ``router`` per which keys are
  configured, including the Gemini→OpenRouter fallback wiring.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.llm.errors import LLMUnavailable
from app.llm.factory import build_llm_client
from app.llm.fallback import FallbackLLMClient
from app.llm.gemini import GeminiLLMClient
from app.llm.openrouter import OpenRouterLLMClient
from app.llm.protocol import LLMClient
from app.llm.stub import StubLLMClient
from app.llm.types import LLMResult

# ---------------------------------------------------------------------------
# Client builders with a faked transport
# ---------------------------------------------------------------------------


def _gemini_client(handler) -> GeminiLLMClient:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://generativelanguage.googleapis.com",
    )
    return GeminiLLMClient(
        model="gemini-2.5-flash",
        api_key="gm-test",
        api_base="https://generativelanguage.googleapis.com",
        timeout_seconds=5.0,
        http_client=http_client,
    )


def _openrouter_client(handler) -> OpenRouterLLMClient:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    return OpenRouterLLMClient(
        model="google/gemini-2.5-flash",
        api_key="or-test",
        api_base="https://openrouter.ai/api/v1",
        timeout_seconds=5.0,
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# GeminiLLMClient
# ---------------------------------------------------------------------------


async def test_gemini_happy_path_extracts_text_and_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "gm-test"
        assert "generateContent" in str(request.url)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "Use --model to pick [1]."}]}}
                ],
                "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 7},
            },
        )

    client = _gemini_client(handler)
    try:
        result = await client.complete(system="sys", user="q", max_tokens=128, temperature=0.2)
    finally:
        await client.aclose()
    assert result.text == "Use --model to pick [1]."
    assert result.input_tokens == 42
    assert result.output_tokens == 7
    assert result.provider == "gemini"


async def test_gemini_disables_thinking_in_payload() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen.update(_json.loads(request.content))
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "x [1]"}]}}]}
        )

    client = _gemini_client(handler)
    try:
        await client.complete(system="sys", user="q", max_tokens=64, temperature=0.0)
    finally:
        await client.aclose()
    gen_cfg = seen["generationConfig"]
    assert isinstance(gen_cfg, dict)
    assert gen_cfg["thinkingConfig"] == {"thinkingBudget": 0}


async def test_gemini_empty_candidate_raises_unavailable() -> None:
    # 200 OK but the candidate has no text part (e.g. finishReason SAFETY, or
    # MAX_TOKENS with the budget consumed). Must raise so the fallback fires
    # rather than returning a silent blank answer.
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"candidates": [{"content": {"role": "model", "parts": []}}]}
        )

    client = _gemini_client(handler)
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_gemini_4xx_raises_unavailable() -> None:
    # A 400 (e.g. an unsupported thinkingConfig on a model that requires
    # thinking) must surface as LLMUnavailable so the OpenRouter fallback runs.
    client = _gemini_client(lambda _r: httpx.Response(400, json={"error": "bad request"}))
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_gemini_5xx_raises_unavailable() -> None:
    client = _gemini_client(lambda _r: httpx.Response(503, json={"error": "overloaded"}))
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_gemini_timeout_raises_unavailable() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout")

    client = _gemini_client(handler)
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


def test_gemini_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiLLMClient(
            model="gemini-2.5-flash",
            api_key=None,
            api_base="https://x",
            timeout_seconds=5.0,
        )


# ---------------------------------------------------------------------------
# OpenRouterLLMClient
# ---------------------------------------------------------------------------


async def test_openrouter_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer or-test"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "Answer [1]."}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                "model": "google/gemini-2.5-flash",
            },
        )

    client = _openrouter_client(handler)
    try:
        result = await client.complete(system="sys", user="q", max_tokens=128, temperature=0.2)
    finally:
        await client.aclose()
    assert result.text == "Answer [1]."
    assert result.input_tokens == 10
    assert result.output_tokens == 3
    assert result.provider == "router"


async def test_openrouter_empty_content_raises_unavailable() -> None:
    # 200 OK but the assistant message carries no content — raise so a blank
    # answer is never returned.
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant"}}]})

    client = _openrouter_client(handler)
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_openrouter_429_raises_unavailable() -> None:
    client = _openrouter_client(lambda _r: httpx.Response(429, json={"error": "rate_limited"}))
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


def test_openrouter_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        OpenRouterLLMClient(
            model="google/gemini-2.5-flash",
            api_key=None,
            api_base="https://x",
            timeout_seconds=5.0,
        )


# ---------------------------------------------------------------------------
# Security regression (issue #50): upstream error body stays server-side
# ---------------------------------------------------------------------------


# 401 exercises the most sensitive upstream body (auth-failure detail); 429/503
# cover the "unavailable" branch. Both clients funnel every status >= 400 through
# one block, so parametrizing locks the leak closed for both providers.
@pytest.mark.parametrize(
    ("build_client", "provider"),
    [(_gemini_client, "Gemini"), (_openrouter_client, "OpenRouter")],
)
@pytest.mark.parametrize("status", [401, 429, 503])
async def test_error_body_is_logged_not_in_exception(
    build_client, provider: str, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    """All three provider clients behave identically (see the anthropic test):
    the upstream error body is logged SERVER-SIDE but never embedded in the
    LLMUnavailable message, so it cannot reach the client via
    error.details.reason."""
    secret_body = '{"error":"upstream secret detail us-east-1"}'
    client = build_client(lambda _r: httpx.Response(status, text=secret_body))
    try:
        with (
            caplog.at_level("WARNING", logger="citevyn.llm"),
            pytest.raises(LLMUnavailable) as info,
        ):
            await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    finally:
        await client.aclose()

    assert secret_body not in str(info.value)
    assert "upstream secret detail" not in str(info.value)
    assert str(info.value) == f"{provider} returned {status}"
    assert any(secret_body in str(rec.__dict__.get("body", "")) for rec in caplog.records)


# ---------------------------------------------------------------------------
# FallbackLLMClient
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Minimal LLMClient double that records calls and returns a fixed result."""

    def __init__(self, *, result: LLMResult | None = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls = 0

    async def complete(self, *, system: str, user: str, max_tokens: int, temperature: float):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


def _result(provider: str) -> LLMResult:
    return LLMResult(text="ok [1]", input_tokens=1, output_tokens=1, model="m", provider=provider)


async def test_fallback_uses_secondary_when_primary_unavailable() -> None:
    primary = _RecordingClient(raises=LLMUnavailable("gemini down"))
    secondary = _RecordingClient(result=_result("router"))
    client = FallbackLLMClient(primary=primary, secondary=secondary)
    result = await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    assert result.provider == "router"
    assert primary.calls == 1
    assert secondary.calls == 1


async def test_fallback_skips_secondary_when_primary_succeeds() -> None:
    primary = _RecordingClient(result=_result("gemini"))
    secondary = _RecordingClient(result=_result("router"))
    client = FallbackLLMClient(primary=primary, secondary=secondary)
    result = await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    assert result.provider == "gemini"
    assert primary.calls == 1
    assert secondary.calls == 0


async def test_fallback_propagates_non_unavailable_error() -> None:
    primary = _RecordingClient(raises=ValueError("bug"))
    secondary = _RecordingClient(result=_result("router"))
    client = FallbackLLMClient(primary=primary, secondary=secondary)
    with pytest.raises(ValueError):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    assert secondary.calls == 0


# ---------------------------------------------------------------------------
# Factory resolution
# ---------------------------------------------------------------------------


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {"llm_provider": "gemini", "environment": "local"}
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_factory_gemini_with_both_keys_builds_fallback() -> None:
    client = build_llm_client(_settings(gemini_api_key="gm", openrouter_api_key="or"))
    assert isinstance(client, FallbackLLMClient)
    assert isinstance(client, LLMClient)


def test_factory_gemini_key_only_builds_gemini() -> None:
    client = build_llm_client(_settings(gemini_api_key="gm", openrouter_api_key=None))
    assert isinstance(client, GeminiLLMClient)


def test_factory_gemini_provider_router_key_only_builds_openrouter() -> None:
    client = build_llm_client(_settings(gemini_api_key=None, openrouter_api_key="or"))
    assert isinstance(client, OpenRouterLLMClient)


def test_factory_gemini_no_keys_dev_falls_back_to_stub() -> None:
    client = build_llm_client(_settings(gemini_api_key=None, openrouter_api_key=None))
    assert isinstance(client, StubLLMClient)


def test_factory_gemini_no_keys_production_raises() -> None:
    # A production Settings requires a strong admin key (validator); supply one
    # so the factory's own missing-LLM-key guard is what raises.
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        build_llm_client(
            _settings(
                environment="production",
                admin_api_key="prod-strong-admin-secret",
                gemini_api_key=None,
                openrouter_api_key=None,
            )
        )


def test_factory_router_provider_builds_openrouter() -> None:
    client = build_llm_client(_settings(llm_provider="router", openrouter_api_key="or"))
    assert isinstance(client, OpenRouterLLMClient)


def test_factory_router_provider_requires_key(monkeypatch) -> None:
    monkeypatch.setenv("CITEVYN_LLM_PROVIDER", "router")
    monkeypatch.delenv("CITEVYN_OPENROUTER_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            build_llm_client(get_settings())
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Composed end-to-end: Gemini-empty → fallback → real OpenRouter answer
# ---------------------------------------------------------------------------


async def test_fallback_gemini_empty_falls_back_to_real_openrouter() -> None:
    # The path the PR is built around, end to end (both clients real, faked
    # transports): Gemini returns 200-with-no-text → LLMUnavailable → the
    # FallbackLLMClient calls a real OpenRouterLLMClient which answers.
    gemini = _gemini_client(
        lambda _r: httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})
    )
    openrouter = _openrouter_client(
        lambda _r: httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "Fallback answer [1]."}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )
    )
    client = FallbackLLMClient(primary=gemini, secondary=openrouter)
    try:
        result = await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    finally:
        await client.aclose()
    assert result.provider == "router"
    assert result.text == "Fallback answer [1]."


# ---------------------------------------------------------------------------
# FallbackLLMClient.aclose closes both children
# ---------------------------------------------------------------------------


class _AcloseRecorder:
    def __init__(self) -> None:
        self.closed = False

    async def complete(self, *, system: str, user: str, max_tokens: int, temperature: float):
        return _result("stub")

    async def aclose(self) -> None:
        self.closed = True


class _NoAclose:
    async def complete(self, *, system: str, user: str, max_tokens: int, temperature: float):
        return _result("stub")


async def test_fallback_aclose_closes_both_children() -> None:
    primary, secondary = _AcloseRecorder(), _AcloseRecorder()
    await FallbackLLMClient(primary=primary, secondary=secondary).aclose()
    assert primary.closed and secondary.closed


async def test_fallback_aclose_tolerates_child_without_aclose() -> None:
    primary = _AcloseRecorder()
    secondary = _NoAclose()  # no aclose attribute
    await FallbackLLMClient(primary=primary, secondary=secondary).aclose()  # must not raise
    assert primary.closed


# ---------------------------------------------------------------------------
# Malformed-response and OpenRouter error-symmetry coverage
# ---------------------------------------------------------------------------


async def test_gemini_non_json_body_raises_unavailable() -> None:
    client = _gemini_client(lambda _r: httpx.Response(200, content=b"not json"))
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_gemini_missing_candidates_raises_unavailable() -> None:
    client = _gemini_client(lambda _r: httpx.Response(200, json={}))
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_gemini_transport_error_raises_unavailable() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _gemini_client(handler)
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_openrouter_non_json_body_raises_unavailable() -> None:
    client = _openrouter_client(lambda _r: httpx.Response(200, content=b"not json"))
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_openrouter_missing_choices_raises_unavailable() -> None:
    client = _openrouter_client(lambda _r: httpx.Response(200, json={}))
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_openrouter_5xx_raises_unavailable() -> None:
    client = _openrouter_client(lambda _r: httpx.Response(503, json={"error": "overloaded"}))
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_openrouter_400_raises_unavailable() -> None:
    client = _openrouter_client(lambda _r: httpx.Response(400, json={"error": "bad request"}))
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()


async def test_openrouter_timeout_raises_unavailable() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout")

    client = _openrouter_client(handler)
    with pytest.raises(LLMUnavailable):
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    await client.aclose()
