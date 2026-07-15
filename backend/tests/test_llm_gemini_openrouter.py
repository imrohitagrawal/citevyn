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
from app.llm._http import post_json
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

    async def aclose(self) -> None:
        return None


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


def test_default_llm_models_encode_cost_priority() -> None:
    """#99: Gemini Flash (free tier) is the priority-1 primary; GPT-4o-mini on
    OpenRouter is the paid priority-2 fallback.

    Locked as a regression guard so a refactor cannot silently repin the retired
    ``gemini-2.5-flash`` (404 "no longer available to new users") on either arm,
    and so the fallback stays a *different* provider family — otherwise a single
    Google-side retirement takes out both the primary and the backstop at once,
    which is exactly the #99 failure.
    """
    # Assert the CLASS defaults (not Settings(), which would absorb a
    # CITEVYN_GEMINI_MODEL / .env override and could mask a reverted default).
    gemini_default = Settings.model_fields["gemini_model"].default
    openrouter_default = Settings.model_fields["openrouter_model"].default
    assert gemini_default == "gemini-flash-latest"
    assert openrouter_default == "openai/gpt-4o-mini"
    assert not openrouter_default.startswith("google/"), (
        "fallback must be a different provider family than the Gemini primary"
    )


def test_factory_gemini_with_both_keys_builds_fallback() -> None:
    # Cost-priority wiring (#99): both keys present ⇒ Gemini primary (free) with
    # the OpenRouter GPT-4o-mini paid backstop. The factory threads
    # settings.gemini_model / settings.openrouter_model into the two arms.
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


async def test_fallback_aclose_closes_both_children() -> None:
    primary, secondary = _AcloseRecorder(), _AcloseRecorder()
    await FallbackLLMClient(primary=primary, secondary=secondary).aclose()
    assert primary.closed and secondary.closed


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


# ---------------------------------------------------------------------------
# Shared transport helper (app.llm._http.post_json) — the surface all three
# clients now delegate to. The per-client tests above exercise it through the
# real wire shapes; these hit it directly for the taxonomy edges.
# ---------------------------------------------------------------------------


def _http_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://provider.test",
    )


async def _post(handler, *, timeout_seconds: float = 5.0):
    client = _http_client(handler)
    try:
        return await post_json(
            client=client,
            url="https://provider.test/v1/call",
            payload={"q": "x"},
            headers={"authorization": "Bearer secret-key"},
            timeout_seconds=timeout_seconds,
            provider="Provider",
            error_event="provider_error_response",
        )
    finally:
        await client.aclose()


async def test_post_json_happy_path_returns_dict() -> None:
    data = await _post(lambda _r: httpx.Response(200, json={"ok": True, "n": 1}))
    assert data == {"ok": True, "n": 1}


async def test_post_json_timeout_raises_unavailable() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("boom")

    with pytest.raises(LLMUnavailable) as info:
        await _post(handler, timeout_seconds=3.0)
    assert str(info.value) == "Provider request timed out after 3.0s"
    assert info.value.cause is not None


async def test_post_json_transport_error_raises_unavailable() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMUnavailable) as info:
        await _post(handler)
    assert str(info.value) == "Provider transport error: ConnectError"
    assert info.value.cause is not None


async def test_post_json_status_error_logs_body_server_side_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # #50 invariant, at the helper: the upstream body is logged server-side but
    # never embedded in the LLMUnavailable message (which reaches the client via
    # error.details.reason). The request headers (API key) are never logged.
    secret_body = '{"error":"upstream secret detail us-east-1"}'
    with (
        caplog.at_level("WARNING", logger="citevyn.llm"),
        pytest.raises(LLMUnavailable) as info,
    ):
        await _post(lambda _r: httpx.Response(503, text=secret_body))

    assert str(info.value) == "Provider returned 503"
    assert secret_body not in str(info.value)
    records = [r for r in caplog.records if r.msg == "provider_error_response"]
    assert records, "expected the per-provider error event to be logged"
    assert any(secret_body in str(r.__dict__.get("body", "")) for r in records)
    # The API key must never reach the log record.
    assert not any("secret-key" in str(r.__dict__) for r in records)


async def test_post_json_non_json_body_raises_unavailable() -> None:
    with pytest.raises(LLMUnavailable) as info:
        await _post(lambda _r: httpx.Response(200, content=b"not json"))
    assert str(info.value) == "Provider returned non-JSON body"
    assert info.value.cause is not None


# ---------------------------------------------------------------------------
# Protocol conformance: aclose is now a required LLMClient member, so every
# implementer must still satisfy isinstance(_, LLMClient) at runtime.
# ---------------------------------------------------------------------------


def test_all_clients_satisfy_llmclient_protocol() -> None:
    gemini = _gemini_client(lambda _r: httpx.Response(200, json={}))
    openrouter = _openrouter_client(lambda _r: httpx.Response(200, json={}))
    assert isinstance(StubLLMClient(), LLMClient)
    assert isinstance(gemini, LLMClient)
    assert isinstance(openrouter, LLMClient)
    assert isinstance(FallbackLLMClient(primary=gemini, secondary=openrouter), LLMClient)
