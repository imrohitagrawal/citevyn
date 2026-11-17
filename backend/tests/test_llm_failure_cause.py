"""#576 part 1: each raise site tags a bounded cause; ``from_error`` holds it to shape."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.llm._http import post_json
from app.llm.attempts import UNRECOGNIZED, PrimaryFailure
from app.llm.errors import LLMUnavailable
from app.llm.fallback import FallbackLLMClient
from app.llm.gemini import GeminiLLMClient
from app.llm.types import LLMProvider, LLMResult

_EMPTY = "Gemini returned an empty answer (candidates present, no text part)"
_NO_PARTS: dict[str, Any] = {"content": {"parts": []}}


def _failure(exc: BaseException) -> PrimaryFailure:
    return PrimaryFailure.from_error(exc, provider="gemini", model="m", duration_seconds=0.5)


def _cause(f: PrimaryFailure) -> tuple[Any, ...]:
    return (f.kind, f.status_code, f.provider_status, f.reason)


async def _raised(handler: Any) -> LLMUnavailable:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMUnavailable) as info:
            await post_json(
                client=client,
                url="https://example.invalid/v1",
                payload={},
                headers={},
                timeout_seconds=5.0,
                provider="Gemini",
                error_event="gemini_error_response",
            )
    return info.value


def _gemini(body: Any, **kwargs: Any) -> GeminiLLMClient:
    return GeminiLLMClient(
        model="gemini-3.6-flash",
        api_key="g-test",
        api_base="https://generativelanguage.googleapis.com",
        timeout_seconds=5.0,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        ),
        **kwargs,
    )


async def _gemini_raised(body: Any, **kwargs: Any) -> LLMUnavailable:
    with pytest.raises(LLMUnavailable) as info:
        await _gemini(body, **kwargs).complete(system="s", user="u", max_tokens=64, temperature=0)
    return info.value


def _status(code: int, body: Any) -> Any:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return lambda _r: httpx.Response(code, content=content)


def _raise(cls: Any) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        raise cls("x", request=request)

    return handler


_QUOTA = {"error": {"code": 429, "message": "quota text", "status": "RESOURCE_EXHAUSTED"}}
_HTTP = "http_status"


@pytest.mark.parametrize(
    ("handler", "message", "cause"),
    [
        (_status(503, b"down"), "Gemini returned 503", (_HTTP, 503, None, None)),
        (_status(429, _QUOTA), "Gemini returned 429", (_HTTP, 429, "RESOURCE_EXHAUSTED", None)),
        (_status(503, {"error": ["a list"]}), None, (_HTTP, 503, None, None)),
        (_status(503, {"error": {"status": {"a": 1}}}), None, (_HTTP, 503, UNRECOGNIZED, None)),
        (_status(503, {"error": {"status": 7}}), None, (_HTTP, 503, UNRECOGNIZED, None)),
        (_status(503, {"error": {"status": "SAFETY\n"}}), None, (_HTTP, 503, UNRECOGNIZED, None)),
        (_raise(httpx.ReadTimeout), None, ("timeout", None, None, "ReadTimeout")),
        (_raise(httpx.ConnectError), None, ("transport", None, None, "ConnectError")),
        (_status(200, b"<html>"), None, ("non_json", None, None, "JSONDecodeError")),
    ],
)
async def test_the_http_helper_tags_each_failure(handler: Any, message: Any, cause: Any) -> None:
    """Red when: a raise site drops its kind, status or class name, an odd body loses
    the status or skips the shape rule, or a message changes."""
    exc = await _raised(handler)
    assert _cause(_failure(exc)) == cause
    assert message is None or str(exc) == message
    assert "quota text" not in json.dumps(_failure(exc).as_dict())


def _finish(reason: str) -> dict[str, Any]:
    return {"candidates": [{**_NO_PARTS, "finishReason": reason}]}


@pytest.mark.parametrize(
    ("body", "cause", "message"),
    [
        (_finish("MAX_TOKENS"), ("empty_answer", "MAX_TOKENS"), _EMPTY),
        ({"candidates": [_NO_PARTS]}, ("empty_answer", None), _EMPTY),
        (_finish("SAFETY <b>x"), ("empty_answer", UNRECOGNIZED), _EMPTY),
        (_finish("x" * 41), ("empty_answer", UNRECOGNIZED), _EMPTY),
        (_finish("1SAFETY"), ("empty_answer", UNRECOGNIZED), _EMPTY),
        ({"promptFeedback": {"blockReason": "SAFETY"}}, ("blocked", "SAFETY"), None),
        (
            {"candidates": [], "promptFeedback": {"blockReason": "OTHER"}},
            ("blocked", "OTHER"),
            None,
        ),
        ({"x": 1}, ("malformed_response", None), "Gemini response missing 'candidates' array"),
    ],
)
async def test_the_gemini_client_tells_empty_blocked_and_malformed_apart(
    body: Any, cause: Any, message: Any
) -> None:
    """Red when: a finish or block reason is dropped or skips the shape check, a
    blocked prompt reads as a malformed body, or a message changes."""
    exc = await _gemini_raised(body)
    assert (_failure(exc).kind, _failure(exc).reason) == cause
    assert message is None or str(exc) == message


async def test_an_empty_answer_records_its_token_counts_and_the_budget_sent() -> None:
    """Red when: the thinking or answer token counts or the maxOutputTokens sent are dropped."""
    body = {
        **_finish("MAX_TOKENS"),
        "usageMetadata": {"thoughtsTokenCount": 70, "candidatesTokenCount": 0},
    }
    f = _failure(await _gemini_raised(body, thinking_level="minimal", thinking_headroom_tokens=10))
    assert (f.thoughts_tokens, f.answer_tokens, f.max_output_tokens) == (70, 0, 74)


class _Secondary:
    calls = 0

    async def complete(self, **_: Any) -> LLMResult:
        self.calls += 1
        return LLMResult(text="ok", input_tokens=1, output_tokens=1, model="m", provider="router")

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize(
    "body",
    [
        {"candidates": []},
        {"candidates": [{}]},
        {"candidates": [{"finishReason": {"a": 1}}]},
        {"candidates": [{"finishReason": 3}]},
        {"candidates": [], "promptFeedback": "a string"},
        {"promptFeedback": "a string"},
        {"promptFeedback": {"blockReason": ["SAFETY"]}},
        {"candidates": [_NO_PARTS], "usageMetadata": "odd"},
        {"candidates": [_NO_PARTS], "usageMetadata": {"thoughtsTokenCount": "9"}},
    ],
)
async def test_an_odd_gemini_body_never_raises_while_its_cause_is_read(body: Any) -> None:
    """Red when: reading the reason or counts raises on an odd body (no fallback)."""
    secondary = _Secondary()
    fallback = FallbackLLMClient(primary=_gemini(body), secondary=secondary)  # type: ignore[arg-type]
    assert (await fallback.complete(system="s", user="u", max_tokens=8, temperature=0)).text == "ok"
    assert secondary.calls == 1
    f = _failure(await _gemini_raised(body))
    assert f.kind in {"blocked", "empty_answer", "malformed_response"}
    assert f.reason in {None, UNRECOGNIZED} and f.thoughts_tokens is None


async def test_the_provider_label_is_the_one_a_successful_result_carries() -> None:
    """Red when: the fallback would name the primary with another label than its results carry."""
    ok = _gemini({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
    result = await ok.complete(system="s", user="u", max_tokens=8, temperature=0)
    assert GeminiLLMClient._PROVIDER == result.provider == LLMProvider.gemini.value == "gemini"  # pyright: ignore[reportPrivateUsage]


def test_defaults_normalisation_and_no_text_field() -> None:
    """Red when: the new arguments become required, an unlisted kind or odd status or
    count is recorded as given, or a field that could hold message text is added."""
    assert _cause(_failure(LLMUnavailable("x"))) == ("unknown", None, None, None)
    assert _failure(LLMUnavailable("x", kind="bogus")).kind == "unknown"
    for odd in (True, 99, 600, "503"):
        assert _failure(LLMUnavailable("x", status_code=odd)).status_code is None  # type: ignore[arg-type]
    assert _failure(LLMUnavailable("x", status_code=599)).status_code == 599
    for count in (True, -1):
        assert _failure(LLMUnavailable("x", thoughts_tokens=count)).thoughts_tokens is None
    plain = _failure(ValueError("boom"))
    assert (plain.kind, plain.error_type) == ("unknown", "ValueError")
    record = _failure(LLMUnavailable("secret text", cause=RuntimeError("secret text"))).as_dict()
    assert "secret text" not in json.dumps(record)
    assert set(record) == {
        *("provider", "model", "error_type", "kind", "status_code", "provider_status"),
        *("reason", "duration_seconds", "thoughts_tokens", "answer_tokens", "max_output_tokens"),
    }
