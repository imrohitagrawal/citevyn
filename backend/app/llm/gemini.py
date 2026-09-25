"""Google Gemini (Generative Language API) client.

Speaks to ``{gemini_api_base}/v1beta/models/{model}:generateContent`` with the
API key in the ``x-goog-api-key`` header. The transport is :mod:`httpx`;
callers may inject a pre-built ``AsyncClient`` for testing (e.g. via
:class:`httpx.MockTransport`).

Thinking is kept as low as the model allows, so a call spends its
``maxOutputTokens`` budget on the visible answer rather than internal reasoning —
CiteVyn answers are short, extractive, and grounded in the evidence block, so
chain-of-thought adds latency and can starve the answer of tokens. Gemini 3.x
takes ``thinkingConfig.thinkingLevel`` (lowest "minimal" on 3.6 Flash) and cannot
turn thinking off; 2.x takes ``thinkingConfig.thinkingBudget`` (0 = off). Exactly
one of the two is sent. Thought tokens are billed at the output rate, so they are
added to ``output_tokens`` (#492).

Errors
------

5xx responses, 408/429, and transport timeouts raise :class:`LLMUnavailable`
so the factory's fallback wrapper (and the orchestrator) can react. 4xx
responses (bad request, auth failure) also surface as :class:`LLMUnavailable`;
from the caller's perspective the provider is not currently delivering an
answer, and the fallback provider gets a chance.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from app.llm._http import post_json
from app.llm.errors import LLMUnavailable
from app.llm.types import LLMProvider, LLMResult


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
        thinking_level: str | None = None,
        thinking_headroom_tokens: int = 0,
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
        self._thinking_level = thinking_level
        self._thinking_headroom_tokens = thinking_headroom_tokens
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
        # Thought tokens count against maxOutputTokens, and a level cannot turn
        # thinking off, so level mode asks for headroom on top of the answer budget.
        # Without it a 4-token call can think its whole budget away and return no text.
        max_output_tokens = (
            max_tokens + self._thinking_headroom_tokens
            if self._thinking_level is not None
            else max_tokens
        )
        thinking: dict[str, Any] = (
            {"thinkingLevel": self._thinking_level}
            if self._thinking_level is not None
            else {"thinkingBudget": self._thinking_budget}
        )
        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": max_output_tokens,
                "temperature": temperature,
                # Keep "thinking" minimal so the token budget funds the answer.
                "thinkingConfig": thinking,
            },
        }
        headers = {
            "x-goog-api-key": self._api_key,
            "content-type": "application/json",
        }
        data = await post_json(
            client=self._http_client,
            url=url,
            payload=payload,
            headers=headers,
            timeout_seconds=self._timeout_seconds,
            provider="Gemini",
            error_event="gemini_error_response",
        )

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
        # Thought tokens are billed at the output rate; leaving them out would
        # under-report every call made with thinking on (#492).
        output_tokens = int(cast(int, usage.get("candidatesTokenCount", 0))) + int(
            cast(int, usage.get("thoughtsTokenCount", 0))
        )

        return LLMResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=self._model,
            provider=LLMProvider.gemini.value,
        )
