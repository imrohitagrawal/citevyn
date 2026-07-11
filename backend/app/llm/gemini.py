"""Google Gemini (Generative Language API) client.

Speaks to ``{gemini_api_base}/v1beta/models/{model}:generateContent`` with the
API key in the ``x-goog-api-key`` header. The transport is :mod:`httpx`;
callers may inject a pre-built ``AsyncClient`` for testing (e.g. via
:class:`httpx.MockTransport`).

Thinking is disabled (``thinkingConfig.thinkingBudget = 0``) so a
``gemini-2.5-flash`` call spends its ``maxOutputTokens`` budget on the visible
answer rather than internal reasoning — CiteVyn answers are short, extractive,
and grounded in the evidence block, so chain-of-thought adds latency and can
starve the answer of tokens.

Errors
------

5xx responses, 408/429, and transport timeouts raise :class:`LLMUnavailable`
so the factory's fallback wrapper (and the orchestrator) can react. 4xx
responses (bad request, auth failure) also surface as :class:`LLMUnavailable`;
from the caller's perspective the provider is not currently delivering an
answer, and the fallback provider gets a chance.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import httpx

from app.core.middleware import get_current_request_id
from app.llm.errors import LLMUnavailable
from app.llm.types import LLMProvider, LLMResult

_logger = logging.getLogger("citevyn.llm")

# Status codes treated as "provider unavailable" — the fallback wrapper
# retries these against the secondary provider.
_UNAVAILABLE_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

# Cap the upstream body we keep in the SERVER log. Enough to debug the
# provider failure without hoarding an unbounded response.
_ERROR_BODY_LOG_LIMIT = 500


def _extract_text(candidates: list[dict[str, Any]]) -> str:
    """Concatenate the text parts of the first candidate.

    Defensive: a candidate may carry multiple parts (or none, e.g. when
    ``finishReason`` is ``SAFETY`` or ``MAX_TOKENS``). We join every part
    that carries a string ``text`` field and return the empty string when
    none do. The caller treats an empty result as :class:`LLMUnavailable`
    (see :meth:`GeminiLLMClient.complete`) so the fallback provider is tried
    rather than a blank answer being returned.
    """
    if not candidates:
        return ""
    content = candidates[0].get("content")
    if not isinstance(content, dict):
        return ""
    parts = cast(dict[str, Any], content).get("parts")
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in cast(list[Any], parts):
        if isinstance(part, dict):
            value = cast(dict[str, Any], part).get("text")
            if isinstance(value, str):
                texts.append(value)
    return "".join(texts)


class GeminiLLMClient:
    """Real HTTP client for the Gemini Generative Language API."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        api_base: str,
        timeout_seconds: float,
        thinking_budget: int = 0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise RuntimeError(
                "CITEVYN_GEMINI_API_KEY is required when CITEVYN_LLM_PROVIDER=gemini"
            )
        self._model = model
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._thinking_budget = thinking_budget
        self._owns_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        """Close the underlying httpx client if we own it."""
        if self._owns_client:
            await self._http_client.aclose()

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        url = f"{self._api_base}/v1beta/models/{self._model}:generateContent"
        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
                # Disable "thinking" so the token budget funds the answer.
                "thinkingConfig": {"thinkingBudget": self._thinking_budget},
            },
        }
        headers = {
            "x-goog-api-key": self._api_key,
            "content-type": "application/json",
        }
        try:
            response = await self._http_client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMUnavailable(
                f"Gemini request timed out after {self._timeout_seconds}s",
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable(
                f"Gemini transport error: {exc.__class__.__name__}",
                cause=exc,
            ) from exc

        if response.status_code in _UNAVAILABLE_STATUSES or response.status_code >= 400:
            # The upstream body can carry provider identity and raw error
            # text, so it is logged SERVER-SIDE only (never the request
            # headers, which hold the API key) and kept out of the exception
            # message so it cannot leak to the caller through
            # error.details.reason.
            _logger.warning(
                "gemini_error_response",
                extra={
                    "request_id": get_current_request_id(),
                    "status_code": response.status_code,
                    "body": response.text[:_ERROR_BODY_LOG_LIMIT],
                },
            )
            raise LLMUnavailable(f"Gemini returned {response.status_code}")

        try:
            raw_data: Any = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable("Gemini returned non-JSON body", cause=exc) from exc
        data = cast(dict[str, Any], raw_data)

        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            raise LLMUnavailable("Gemini response missing 'candidates' array")
        text = _extract_text(cast(list[dict[str, Any]], candidates))
        if not text.strip():
            # 200 OK but no usable text — e.g. finishReason SAFETY / RECITATION,
            # or MAX_TOKENS with the whole budget consumed. This is not a valid
            # answer; raise so the factory's fallback provider is tried instead
            # of returning a blank answer that would slip past citation
            # validation as a silent empty response.
            raise LLMUnavailable(
                "Gemini returned an empty answer (candidates present, no text part)"
            )

        usage_raw: Any = data.get("usageMetadata") or {}
        usage = cast(dict[str, Any], usage_raw)
        input_tokens = int(cast(int, usage.get("promptTokenCount", 0)))
        output_tokens = int(cast(int, usage.get("candidatesTokenCount", 0)))

        return LLMResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=self._model,
            provider=LLMProvider.gemini.value,
        )
