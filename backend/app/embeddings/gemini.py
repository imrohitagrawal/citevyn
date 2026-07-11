"""Google Gemini embeddings client.

Mirrors :class:`app.llm.gemini.GeminiLLMClient` almost exactly — same auth
(``x-goog-api-key``), same base URL, sibling endpoints (``:embedContent`` /
``:batchEmbedContents`` vs ``:generateContent``), same injectable
:class:`httpx.AsyncClient`, same **error-body-not-leaked** discipline (issue #50):
the upstream body is logged server-side only and never placed in the raised
exception message.

Model & dimension
-----------------
``gemini-embedding-001`` outputs 3072 dimensions by default and supports Matryoshka
truncation to a requested ``outputDimensionality`` (recommended: 768 / 1536 / 3072).
CiteVyn requests 1536 (``Settings.embedding_dim``) — the largest recommended size
under pgvector's 2000-dim index limit. See ``docs/ADR/0003-embeddings-provider.md``.

Task types
----------
Retrieval quality improves when the query and the document are embedded with their
respective task types. :meth:`embed` (query path) uses ``RETRIEVAL_QUERY``;
:meth:`embed_documents` (ingest path) uses ``RETRIEVAL_DOCUMENT``.

Resilience (Tier 1)
-------------------
Transient failures (429/5xx/timeout) are retried a bounded number of times against
the *same* provider before raising :class:`EmbedderUnavailable`. Cross-provider
fallback is deliberately NOT implemented — mixing vector spaces silently corrupts
retrieval (see the ADR). ``embed`` never returns a wrong-space vector.
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

# Provider hard limit is ~2048 input tokens per text; we guard on characters as a
# cheap proxy so an oversized chunk fails fast with a clear message instead of a
# cryptic 400 from upstream. ~8k chars comfortably exceeds any real doc chunk.
_MAX_INPUT_CHARS = 8000

# ``batchEmbedContents`` caps the number of contents per request (~100). A single
# document can produce more chunks than that, so ``embed_documents`` splits the
# input into provider-safe sub-batches rather than sending one oversized request
# that the provider would reject with a 400.
_EMBED_BATCH_SIZE = 100


def _extract_values(embedding: Any, *, dim: int) -> list[float]:
    """Pull a validated ``list[float]`` of length ``dim`` from a ContentEmbedding.

    Raises :class:`EmbedderUnavailable` on a malformed or wrong-dimension body so
    a bad upstream response can never silently poison the index with a vector in
    the wrong space.
    """
    if not isinstance(embedding, dict):
        raise EmbedderUnavailable("Gemini embeddings returned a malformed embedding object")
    values = cast(dict[str, Any], embedding).get("values")
    if not isinstance(values, list) or not values:
        raise EmbedderUnavailable("Gemini embeddings response missing 'values' array")
    try:
        vector = [float(v) for v in cast(list[Any], values)]
    except (TypeError, ValueError) as exc:
        # A non-numeric value in the body would otherwise raise a raw ValueError
        # carrying provider-supplied content, which at ingest lands in
        # IngestionJob.error_message (admin-visible). Keep it generic (issue #50).
        raise EmbedderUnavailable(
            "Gemini embeddings response contained a non-numeric value", cause=exc
        ) from exc
    if len(vector) != dim:
        raise EmbedderUnavailable(f"Gemini embeddings returned dim {len(vector)}, expected {dim}")
    return vector


class GeminiEmbedder:
    """Real HTTP client for the Gemini embeddings API."""

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
                "CITEVYN_GEMINI_API_KEY is required when CITEVYN_EMBEDDING_PROVIDER=gemini"
            )
        self._model = model
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._dim = dim
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        # Exponential backoff base between retries. On a 429 an immediate retry
        # just hammers the provider that asked us to slow down. Tests pass 0.0.
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
        """Embed a single query (``RETRIEVAL_QUERY``)."""
        self._guard_input(text)
        url = f"{self._api_base}/v1beta/models/{self._model}:embedContent"
        payload = {
            "model": f"models/{self._model}",
            "content": {"parts": [{"text": text}]},
            "taskType": "RETRIEVAL_QUERY",
            "outputDimensionality": self._dim,
        }
        data = await self._post(url, payload, label="embedContent")
        return _extract_values(data.get("embedding"), dim=self._dim)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents (``RETRIEVAL_DOCUMENT``).

        The input is split into provider-safe sub-batches of at most
        ``_EMBED_BATCH_SIZE`` so a document with more chunks than the provider's
        per-request cap does not fail as one oversized request. Results are
        concatenated in input order.
        """
        if not texts:
            return []
        for text in texts:
            self._guard_input(text)
        out: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            out.extend(await self._embed_batch(texts[start : start + _EMBED_BATCH_SIZE]))
        return out

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """Embed one provider-safe sub-batch (``<= _EMBED_BATCH_SIZE`` texts)."""
        url = f"{self._api_base}/v1beta/models/{self._model}:batchEmbedContents"
        payload = {
            "requests": [
                {
                    "model": f"models/{self._model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": "RETRIEVAL_DOCUMENT",
                    "outputDimensionality": self._dim,
                }
                for text in batch
            ]
        }
        data = await self._post(url, payload, label="batchEmbedContents")
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list):
            raise EmbedderUnavailable("Gemini embeddings response missing 'embeddings' array")
        embeddings_list = cast(list[Any], embeddings)
        if len(embeddings_list) != len(batch):
            raise EmbedderUnavailable(
                "Gemini embeddings batch response count did not match the request"
            )
        # Order dependency: batchEmbedContents returns embeddings in the same
        # order as the requests (the API provides no per-item index to validate
        # against), so we pair by position. The count check above is the only
        # structural guard the response allows.
        return [_extract_values(item, dim=self._dim) for item in embeddings_list]

    # -- internals --------------------------------------------------------

    def _guard_input(self, text: str) -> None:
        """Reject inputs the provider will reject, with a clear local message."""
        if not text or not text.strip():
            raise EmbedderUnavailable("Cannot embed empty text")
        if len(text) > _MAX_INPUT_CHARS:
            raise EmbedderUnavailable(
                f"Input text of {len(text)} chars exceeds the {_MAX_INPUT_CHARS}-char limit"
            )

    async def _post(self, url: str, payload: dict[str, Any], *, label: str) -> dict[str, Any]:
        """POST with bounded in-provider retry; parse and return the JSON body.

        Retries transient failures (429/5xx/timeout) against the same provider.
        Never lets the upstream body reach the caller — it is logged server-side
        and the raised exception carries only the status code.
        """
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
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
                    f"Gemini embeddings transport error: {exc.__class__.__name__}",
                    cause=exc,
                ) from exc

            if response.status_code in _UNAVAILABLE_STATUSES:
                self._log_error_body(label, response)
                last_exc = EmbedderUnavailable(f"Gemini embeddings returned {response.status_code}")
                continue
            if response.status_code >= 400:
                # Non-transient client error (e.g. 401/403 auth, 400 bad request).
                # Do not retry; surface a generic message.
                self._log_error_body(label, response)
                raise EmbedderUnavailable(f"Gemini embeddings returned {response.status_code}")

            try:
                raw: Any = json.loads(response.content)
            except json.JSONDecodeError as exc:
                raise EmbedderUnavailable(
                    "Gemini embeddings returned non-JSON body", cause=exc
                ) from exc
            return cast(dict[str, Any], raw)

        # Retries exhausted.
        if isinstance(last_exc, EmbedderUnavailable):
            raise last_exc
        raise EmbedderUnavailable(
            f"Gemini embeddings request timed out after {self._timeout_seconds}s",
            cause=last_exc,
        )

    def _log_error_body(self, label: str, response: httpx.Response) -> None:
        """Log the upstream body SERVER-SIDE only (never the API-key header)."""
        _logger.warning(
            "gemini_embeddings_error_response",
            extra={
                "request_id": get_current_request_id(),
                "endpoint": label,
                "status_code": response.status_code,
                "body": response.text[:_ERROR_BODY_LOG_LIMIT],
            },
        )
