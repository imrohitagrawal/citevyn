"""OpenRouter client (OpenAI-compatible Chat Completions).

Speaks to ``{openrouter_api_base}/chat/completions`` with the API key in an
``Authorization: Bearer`` header. OpenRouter is a multi-model gateway; the
configured ``model`` (e.g. ``google/gemini-2.5-flash``) selects the upstream.
It serves as the secondary provider behind the primary Gemini client (see
:mod:`app.llm.factory`) and can also be selected directly with
``CITEVYN_LLM_PROVIDER=router``.

The transport is :mod:`httpx`; callers may inject a pre-built ``AsyncClient``
for testing. Error handling mirrors the other clients: 5xx / 408 / 429 /
transport failures and any 4xx surface as :class:`LLMUnavailable`.
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx

from app.llm.errors import LLMUnavailable
from app.llm.types import LLMProvider, LLMResult

_UNAVAILABLE_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})


def _extract_text(choices: list[dict[str, Any]]) -> str:
    """Return the assistant message content of the first choice.

    Defensive against a missing/oddly-typed ``message.content`` — returns
    the empty string when absent. The caller treats an empty result as
    :class:`LLMUnavailable` (see :meth:`OpenRouterLLMClient.complete`) so a
    blank answer is never returned to the orchestrator.
    """
    if not choices:
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = cast(dict[str, Any], message).get("content")
    return content if isinstance(content, str) else ""


class OpenRouterLLMClient:
    """Real HTTP client for the OpenRouter Chat Completions API."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        api_base: str,
        timeout_seconds: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise RuntimeError(
                "CITEVYN_OPENROUTER_API_KEY is required when CITEVYN_LLM_PROVIDER=router "
                "(or when it is the configured fallback for the gemini provider)"
            )
        self._model = model
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._timeout_seconds = timeout_seconds
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
        url = f"{self._api_base}/chat/completions"
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            # OpenRouter uses these for attribution/rankings; both optional.
            "x-title": "CiteVyn",
        }
        try:
            response = await self._http_client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMUnavailable(
                f"OpenRouter request timed out after {self._timeout_seconds}s",
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable(
                f"OpenRouter transport error: {exc.__class__.__name__}",
                cause=exc,
            ) from exc

        if response.status_code in _UNAVAILABLE_STATUSES or response.status_code >= 400:
            raise LLMUnavailable(
                f"OpenRouter returned {response.status_code}: {response.text[:200]}"
            )

        try:
            raw_data: Any = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable("OpenRouter returned non-JSON body", cause=exc) from exc
        data = cast(dict[str, Any], raw_data)

        choices = data.get("choices")
        if not isinstance(choices, list):
            raise LLMUnavailable("OpenRouter response missing 'choices' array")
        text = _extract_text(cast(list[dict[str, Any]], choices))
        if not text.strip():
            # 200 OK but no message content — not a valid answer. Raise so the
            # caller (or fallback chain) does not surface a silent blank answer.
            raise LLMUnavailable("OpenRouter returned an empty answer (no message content)")

        usage_raw: Any = data.get("usage") or {}
        usage = cast(dict[str, Any], usage_raw)
        input_tokens = int(cast(int, usage.get("prompt_tokens", 0)))
        output_tokens = int(cast(int, usage.get("completion_tokens", 0)))
        model = str(cast(str, data.get("model", self._model)))

        return LLMResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider=LLMProvider.PROVIDER_ROUTER.value,
        )
