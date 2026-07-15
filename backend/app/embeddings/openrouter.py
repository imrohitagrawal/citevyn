"""OpenRouter embeddings client (OpenAI-compatible ``/embeddings``).

A second real embedder behind the :class:`app.embeddings.protocol.Embedder` seam,
alongside :class:`app.embeddings.gemini.GeminiEmbedder`. It exists because the
Gemini free tier caps embedding requests at 1000/day (issue #59 / the RAG-quality
run), which is too tight to embed even a bounded dev corpus plus iterate on the
eval. OpenRouter proxies OpenAI's ``text-embedding-3-*`` models, whose native
1536-dim output matches the pgvector ``chunks.embedding`` column exactly (migration
0004) — so no migration is needed to adopt it.

Contract & discipline (mirrors :class:`GeminiEmbedder`)
------------------------------------------------------
* ``Authorization: Bearer`` auth against ``{openrouter_api_base}/embeddings``.
* Same **error-body-not-leaked** rule (issue #50): the upstream body is logged
  server-side only and never placed in the raised exception message.
* Same injectable :class:`httpx.AsyncClient`, bounded in-provider retry+backoff on
  transient (408/429/5xx/timeout) failures, and no-retry on fatal 4xx.
* Strict dimension validation so a malformed / wrong-space body can never silently
  poison the index (:func:`EmbedderUnavailable` on any mismatch).

Vector-space consistency
------------------------
OpenAI embeddings have **no** query/document task-type distinction (unlike Gemini's
``RETRIEVAL_QUERY``/``RETRIEVAL_DOCUMENT``). :meth:`embed` and :meth:`embed_documents`
therefore hit the identical model with identical parameters — the query vector and
the stored document vector come from the same space, which is exactly what cosine
retrieval requires. ``dimensions`` is sent explicitly so a change to the model's
default output size can never silently shift the space out from under a built index.
Cross-provider fallback is deliberately NOT implemented (mixing spaces corrupts
retrieval — see ``docs/ADR/0003-embeddings-provider.md``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

import httpx

from app.core.middleware import get_current_request_id
from app.embeddings.errors import EmbedderUnavailable

_logger = logging.getLogger("citevyn.embeddings")

# Status codes treated as transient "provider unavailable" — retried in-provider.
_UNAVAILABLE_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

# Cap the upstream body kept in the SERVER log — enough to debug, never unbounded,
# and never surfaced to the caller.
_ERROR_BODY_LOG_LIMIT = 500

# Guard on characters as a cheap proxy for the provider's per-input token limit so
# an oversized chunk fails fast locally with a clear message instead of a cryptic
# upstream 400. ~8k chars comfortably exceeds any real doc chunk.
_MAX_INPUT_CHARS = 8000

# The OpenAI embeddings endpoint accepts many inputs per request (thousands), but a
# smaller sub-batch keeps a single failed request cheap to retry and the payload
# bounded. A document with more chunks than this is split into ordered sub-batches.
_EMBED_BATCH_SIZE = 96


def _extract_vector(item: Any, *, dim: int) -> list[float]:
    """Pull a validated ``list[float]`` of length ``dim`` from one data item.

    Raises :class:`EmbedderUnavailable` on a malformed or wrong-dimension body so a
    bad upstream response can never silently poison the index with a vector in the
    wrong space.
    """
    if not isinstance(item, dict):
        raise EmbedderUnavailable("OpenRouter embeddings returned a malformed data item")
    values = cast(dict[str, Any], item).get("embedding")
    if not isinstance(values, list) or not values:
        raise EmbedderUnavailable("OpenRouter embeddings response missing 'embedding' array")
    try:
        vector = [float(v) for v in cast(list[Any], values)]
    except (TypeError, ValueError) as exc:
        # A non-numeric value would otherwise raise a raw ValueError carrying
        # provider-supplied content, which at ingest lands in
        # IngestionJob.error_message (admin-visible). Keep it generic (issue #50).
        raise EmbedderUnavailable(
            "OpenRouter embeddings response contained a non-numeric value", cause=exc
        ) from exc
    if len(vector) != dim:
        raise EmbedderUnavailable(
            f"OpenRouter embeddings returned dim {len(vector)}, expected {dim}"
        )
    return vector


class OpenRouterEmbedder:
    """Real HTTP client for the OpenRouter (OpenAI-compatible) embeddings API."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        api_base: str,
        dim: int,
        timeout_seconds: float,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise RuntimeError(
                "CITEVYN_OPENROUTER_API_KEY is required when CITEVYN_EMBEDDING_PROVIDER=openrouter"
            )
        self._model = model
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._dim = dim
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        # Exponential backoff base between retries. On a 429 an immediate retry just
        # hammers the provider that asked us to slow down. Tests pass 0.0.
        self._retry_backoff_seconds = retry_backoff_seconds
        self._owns_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def dim(self) -> int:
        return self._dim

    async def aclose(self) -> None:
        """Close the underlying httpx client if we own it."""
        if self._owns_client:
            await self._http_client.aclose()

    # -- public API -------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        """Embed a single query string."""
        self._guard_input(text)
        data = await self._embed_inputs([text])
        return data[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents, preserving input order.

        Split into provider-safe sub-batches of at most ``_EMBED_BATCH_SIZE`` so a
        document with more chunks than the sub-batch cap does not ride on one
        oversized request. Results are concatenated in input order.
        """
        if not texts:
            return []
        for text in texts:
            self._guard_input(text)
        out: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            out.extend(await self._embed_inputs(texts[start : start + _EMBED_BATCH_SIZE]))
        return out

    # -- internals --------------------------------------------------------

    async def _embed_inputs(self, inputs: list[str]) -> list[list[float]]:
        """POST one sub-batch and return its vectors in input order."""
        url = f"{self._api_base}/embeddings"
        payload = {
            "model": self._model,
            "input": inputs,
            # Sent explicitly so a change to the model's default output size cannot
            # silently shift the vector space out from under an already-built index.
            "dimensions": self._dim,
        }
        data = await self._post(url, payload)
        rows = data.get("data")
        if not isinstance(rows, list):
            raise EmbedderUnavailable("OpenRouter embeddings response missing 'data' array")
        rows_list = cast(list[Any], rows)
        if len(rows_list) != len(inputs):
            raise EmbedderUnavailable(
                "OpenRouter embeddings response count did not match the request"
            )
        # The OpenAI schema does not guarantee response order, but every item carries
        # an ``index`` into the request. Order by it so a reordered response can never
        # misalign a stored vector with the wrong chunk. Fall back to positional order
        # only if an index is absent/duplicated (validated by the count check above).
        ordered = self._order_by_index(rows_list, count=len(inputs))
        return [_extract_vector(item, dim=self._dim) for item in ordered]

    @staticmethod
    def _order_by_index(rows: list[Any], *, count: int) -> list[Any]:
        """Return ``rows`` ordered by each item's ``index`` field.

        Falls back to the original positional order when the indices are not a clean
        ``0..count-1`` permutation (missing, duplicated, or out of range), rather
        than raising — the per-item dim validation downstream is the real integrity
        gate, and positional order is the OpenAI de-facto behavior.
        """
        try:
            indices = [int(cast(dict[str, Any], r).get("index")) for r in rows]  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return rows
        if sorted(indices) != list(range(count)):
            return rows
        ordered: list[Any] = [None] * count
        for idx, row in zip(indices, rows, strict=True):
            ordered[idx] = row
        return ordered

    def _guard_input(self, text: str) -> None:
        """Reject inputs the provider will reject, with a clear local message."""
        if not text or not text.strip():
            raise EmbedderUnavailable("Cannot embed empty text")
        if len(text) > _MAX_INPUT_CHARS:
            raise EmbedderUnavailable(
                f"Input text of {len(text)} chars exceeds the {_MAX_INPUT_CHARS}-char limit"
            )

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with bounded in-provider retry; parse and return the JSON body.

        Retries transient failures (408/429/5xx/timeout) against the same provider.
        Never lets the upstream body reach the caller — it is logged server-side and
        the raised exception carries only the status code.
        """
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            "x-title": "CiteVyn",
        }
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if attempt > 0 and self._retry_backoff_seconds > 0:
                # Exponential backoff before a retry (attempt 1 → base, 2 → 2×base…).
                await asyncio.sleep(self._retry_backoff_seconds * (2 ** (attempt - 1)))
            try:
                response = await self._http_client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                last_exc = exc
                continue
            except httpx.HTTPError as exc:
                # Transport-level errors are not retried — they usually indicate a
                # misconfiguration (bad host) rather than a transient blip.
                raise EmbedderUnavailable(
                    f"OpenRouter embeddings transport error: {exc.__class__.__name__}",
                    cause=exc,
                ) from exc

            if response.status_code in _UNAVAILABLE_STATUSES:
                self._log_error_body(response)
                last_exc = EmbedderUnavailable(
                    f"OpenRouter embeddings returned {response.status_code}"
                )
                continue
            if response.status_code >= 400:
                # Non-transient client error (e.g. 401/403 auth, 400 bad request).
                # Do not retry; surface a generic message.
                self._log_error_body(response)
                raise EmbedderUnavailable(f"OpenRouter embeddings returned {response.status_code}")

            try:
                raw: Any = json.loads(response.content)
            except json.JSONDecodeError as exc:
                raise EmbedderUnavailable(
                    "OpenRouter embeddings returned non-JSON body", cause=exc
                ) from exc
            return cast(dict[str, Any], raw)

        # Retries exhausted.
        if isinstance(last_exc, EmbedderUnavailable):
            raise last_exc
        raise EmbedderUnavailable(
            f"OpenRouter embeddings request timed out after {self._timeout_seconds}s",
            cause=last_exc,
        )

    def _log_error_body(self, response: httpx.Response) -> None:
        """Log the upstream body SERVER-SIDE only (never the Authorization header)."""
        _logger.warning(
            "openrouter_embeddings_error_response",
            extra={
                "request_id": get_current_request_id(),
                "endpoint": "embeddings",
                "status_code": response.status_code,
                "body": response.text[:_ERROR_BODY_LOG_LIMIT],
            },
        )
