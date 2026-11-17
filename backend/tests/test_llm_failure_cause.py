"""#576 part 1: a bounded, structured cause on every failed provider call.

Each raise site of the shared HTTP helper and of the Gemini client tags its
:class:`LLMUnavailable` with a kind, the HTTP status, Gemini's short error code
(``error.status``), the finish or block reason and, on an empty answer, the token
counts. :meth:`PrimaryFailure.from_error` is the one place those values are held
to a fixed shape. Never the response body, never message text; the exception
messages themselves are unchanged.

Free and hermetic: every response comes from ``httpx.MockTransport``. Each test
names the change that turns it red.
"""

from __future__ import annotations

import importlib
import json
import uuid
from typing import Any

import httpx
import pytest

from app.llm._http import post_json
from app.llm.errors import LLMUnavailable
from app.llm.fallback import FallbackLLMClient
from app.llm.gemini import GeminiLLMClient
from app.llm.types import LLMProvider, LLMResult

GEMINI_MODEL = "gemini-3.6-flash"
# The shape rule's value for a field of another type or shape.
UNRECOGNIZED = "UNRECOGNIZED"


def _attempts() -> Any:
    """The new module, imported per test so each test is red on its own before it exists."""
    return importlib.import_module("app.llm.attempts")


def _marker() -> str:
    """A secret-like marker built at run time (never written as a literal)."""
    return "".join(["leak", "-", "marker", "-", uuid.uuid4().hex])


async def _post(handler: Any) -> dict[str, Any]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await post_json(
            client=client,
            url="https://example.invalid/v1",
            payload={},
            headers={},
            timeout_seconds=5.0,
            provider="Gemini",
            error_event="gemini_error_response",
        )


async def _raised(handler: Any) -> LLMUnavailable:
    with pytest.raises(LLMUnavailable) as info:
        await _post(handler)
    return info.value


def _failure(exc: BaseException) -> Any:
    return _attempts().PrimaryFailure.from_error(
        exc, provider="gemini", model=GEMINI_MODEL, duration_seconds=0.5
    )


def _gemini(handler: Any, **kwargs: Any) -> GeminiLLMClient:
    return GeminiLLMClient(
        model=GEMINI_MODEL,
        api_key="g-test",
        api_base="https://generativelanguage.googleapis.com",
        timeout_seconds=5.0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


async def _gemini_raised(body: Any, **kwargs: Any) -> LLMUnavailable:
    client = _gemini(lambda _r: httpx.Response(200, json=body), **kwargs)
    with pytest.raises(LLMUnavailable) as info:
        await client.complete(system="s", user="u", max_tokens=64, temperature=0.0)
    return info.value


# --- 1-3. the shared HTTP helper -----------------------------------------------------------


@pytest.mark.parametrize("status", [503, 429, 400])
async def test_an_error_status_is_tagged_http_status_with_its_code(status: int) -> None:
    """Red when: a raise site stops passing ``status_code`` (or the kind)."""
    exc = await _raised(lambda _r: httpx.Response(status, text="down"))
    failure = _failure(exc)
    assert (failure.kind, failure.status_code) == ("http_status", status)
    # The message is unchanged (#50: the body never enters it).
    assert str(exc) == f"Gemini returned {status}"


async def test_geminis_short_error_code_is_read_from_the_error_json() -> None:
    """Red when: ``error.status`` (e.g. RESOURCE_EXHAUSTED) is not recorded, or
    anything else of the body is."""
    body = {"error": {"code": 429, "message": "quota text", "status": "RESOURCE_EXHAUSTED"}}
    exc = await _raised(lambda _r: httpx.Response(429, json=body))
    failure = _failure(exc)
    assert failure.provider_status == "RESOURCE_EXHAUSTED"
    assert "quota text" not in json.dumps(failure.as_dict())
    assert str(exc) == "Gemini returned 429"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"not json at all", None),
        (json.dumps({"error": ["a list"]}).encode(), None),
        (json.dumps({"error": {"code": 500}}).encode(), None),
        (json.dumps([1, 2]).encode(), None),
        (json.dumps({"error": {"status": {"nested": 1}}}).encode(), UNRECOGNIZED),
        (json.dumps({"error": {"status": 7}}).encode(), UNRECOGNIZED),
        (json.dumps({"error": {"status": "SAFETY\n"}}).encode(), UNRECOGNIZED),
        (json.dumps({"error": {"status": "UNAVAILABLE <b>x"}}).encode(), UNRECOGNIZED),
    ],
)
async def test_an_odd_error_body_never_breaks_the_status_and_is_held_to_shape(
    body: bytes, expected: str | None
) -> None:
    """Red when: reading ``error.status`` raises on an odd body (the status would be
    lost), or a value of another type or shape is recorded as given."""
    exc = await _raised(lambda _r: httpx.Response(503, content=body))
    failure = _failure(exc)
    assert (failure.kind, failure.status_code) == ("http_status", 503)
    assert failure.provider_status == expected


async def test_a_timeout_a_transport_error_and_a_non_json_body_have_their_own_kinds() -> None:
    """Red when: any of these branches is left at ``unknown`` or loses its class name."""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    timed_out = _failure(await _raised(timeout))
    assert (timed_out.kind, timed_out.reason) == ("timeout", "ReadTimeout")
    transport = _failure(await _raised(connect))
    assert (transport.kind, transport.reason) == ("transport", "ConnectError")
    non_json = _failure(await _raised(lambda _r: httpx.Response(200, content=b"<html>")))
    assert non_json.kind == "non_json"
    assert non_json.status_code is None


async def test_the_error_body_reaches_neither_the_message_nor_the_cause() -> None:
    """Red when: a field or the message copies the body."""
    marker = _marker()
    seen: list[str] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            503, json={"error": {"status": "UNAVAILABLE", "message": marker, "detail": marker}}
        )
        seen.append(response.text)
        return response

    exc = await _raised(handler)
    assert marker in seen[0]  # partner: the marker really was in the response
    assert marker not in str(exc)
    assert marker not in json.dumps(_failure(exc).as_dict())
    assert marker not in repr(_failure(exc))


# --- 4-6. the Gemini client ---------------------------------------------------------------


async def test_an_empty_answer_records_the_finish_reason_and_the_token_counts() -> None:
    """Red when: the finish reason, the thinking or answer token counts, or the
    ``maxOutputTokens`` sent are dropped from an empty answer."""
    body = {
        "candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
        "usageMetadata": {
            "promptTokenCount": 900,
            "thoughtsTokenCount": 70,
            "candidatesTokenCount": 0,
        },
    }
    exc = await _gemini_raised(body, thinking_level="minimal", thinking_headroom_tokens=10)
    failure = _failure(exc)
    assert (failure.kind, failure.reason) == ("empty_answer", "MAX_TOKENS")
    assert (failure.thoughts_tokens, failure.answer_tokens) == (70, 0)
    assert failure.max_output_tokens == 74  # 64 asked + 10 headroom, as sent
    # The message is unchanged.
    assert str(exc) == "Gemini returned an empty answer (candidates present, no text part)"


async def test_an_empty_answer_without_a_finish_reason_or_usage_has_none() -> None:
    """Red when: a missing reason or count is invented instead of ``None``."""
    failure = _failure(await _gemini_raised({"candidates": [{"content": {"parts": []}}]}))
    assert (failure.kind, failure.reason) == ("empty_answer", None)
    assert (failure.thoughts_tokens, failure.answer_tokens) == (None, None)
    assert failure.max_output_tokens == 64


@pytest.mark.parametrize("reason", ["SAFETY <b>x", "SAFETY\n", "", "x" * 41, "1SAFETY"])
async def test_a_finish_reason_of_another_shape_is_unrecognized(reason: str) -> None:
    """Red when: the shape check on the reason is bypassed."""
    body = {"candidates": [{"content": {"parts": []}, "finishReason": reason}]}
    assert _failure(await _gemini_raised(body)).reason == UNRECOGNIZED


async def test_a_blocked_prompt_is_told_apart_from_a_malformed_body() -> None:
    """Red when: a blocked prompt cannot be told apart from a malformed body, on the
    missing- and on the empty-candidates branch."""
    blocked = _failure(await _gemini_raised({"promptFeedback": {"blockReason": "SAFETY"}}))
    assert (blocked.kind, blocked.reason) == ("blocked", "SAFETY")
    empty = _failure(
        await _gemini_raised({"candidates": [], "promptFeedback": {"blockReason": "OTHER"}})
    )
    assert (empty.kind, empty.reason) == ("blocked", "OTHER")
    malformed_exc = await _gemini_raised({"unexpected": True})
    assert _failure(malformed_exc).kind == "malformed_response"
    assert str(malformed_exc) == "Gemini response missing 'candidates' array"


class _Secondary:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_: Any) -> LLMResult:
        self.calls += 1
        return LLMResult(
            text="ok [1]", input_tokens=1, output_tokens=1, model="m", provider="router"
        )

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
        {"candidates": [{"content": {"parts": []}}], "usageMetadata": "odd"},
        {"candidates": [{"content": {"parts": []}}], "usageMetadata": {"thoughtsTokenCount": "9"}},
    ],
)
async def test_an_odd_gemini_body_never_raises_while_its_cause_is_read(body: Any) -> None:
    """Red when: reading the finish or block reason or the counts raises on an odd
    body, so the fallback provider is not tried."""
    secondary = _Secondary()
    fallback = FallbackLLMClient(
        primary=_gemini(lambda _r: httpx.Response(200, json=body)),
        secondary=secondary,  # type: ignore[arg-type]
    )
    result = await fallback.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    assert result.text == "ok [1]" and secondary.calls == 1
    exc = await _gemini_raised(body)
    failure = _failure(exc)
    assert failure.kind in {"blocked", "empty_answer", "malformed_response"}
    assert failure.reason in {None, UNRECOGNIZED}
    assert failure.thoughts_tokens is None or isinstance(failure.thoughts_tokens, int)


async def test_the_provider_label_is_the_one_a_successful_result_carries() -> None:
    """Red when: the fallback names the primary with a label other than the one the
    same client puts on a successful ``LLMResult`` (a cause keyed under "Gemini" or
    a class name would not match the report's provider)."""
    ok = _gemini(
        lambda _r: httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
        )
    )
    result = await ok.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    assert GeminiLLMClient._PROVIDER == result.provider == LLMProvider.gemini.value == "gemini"  # pyright: ignore[reportPrivateUsage]


# --- 8. defaults and normalisation -------------------------------------------------------


def test_a_bare_error_is_unknown_and_an_unlisted_kind_is_normalised() -> None:
    """Red when: the new arguments become required, or a kind outside the allowed
    set (or an out-of-range status) is recorded as given."""
    bare = _failure(LLMUnavailable("x"))
    assert (bare.kind, bare.status_code, bare.reason, bare.provider_status) == (
        "unknown",
        None,
        None,
        None,
    )
    assert bare.error_type == "LLMUnavailable"
    assert _failure(LLMUnavailable("x", kind="bogus")).kind == "unknown"
    assert _failure(LLMUnavailable("x", kind="timeout")).kind == "timeout"
    for odd in (True, 99, 600, "503"):
        assert _failure(LLMUnavailable("x", status_code=odd)).status_code is None  # type: ignore[arg-type]
    assert _failure(LLMUnavailable("x", status_code=599)).status_code == 599
    assert _failure(LLMUnavailable("x", thoughts_tokens=True)).thoughts_tokens is None  # type: ignore[arg-type]
    assert _failure(LLMUnavailable("x", thoughts_tokens=-1)).thoughts_tokens is None
    # Not an LLMUnavailable at all: still a bounded record, never a raise.
    plain = _failure(ValueError("boom"))
    assert (plain.kind, plain.error_type) == ("unknown", "ValueError")


def test_the_record_has_no_message_cause_or_body_field() -> None:
    """Red when: a field that could carry message or body text is added."""
    marker = _marker()
    exc = LLMUnavailable(marker, cause=RuntimeError(marker), kind="timeout", reason="ReadTimeout")
    record = _failure(exc).as_dict()
    assert marker not in json.dumps(record)
    assert set(record) == {
        "provider",
        "model",
        "error_type",
        "kind",
        "status_code",
        "provider_status",
        "reason",
        "duration_seconds",
        "thoughts_tokens",
        "answer_tokens",
        "max_output_tokens",
    }
