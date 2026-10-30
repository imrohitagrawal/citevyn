"""Embedder error type.

Mirrors :class:`app.llm.errors.LLMUnavailable`: the single exception a real
embedder raises when the upstream provider is not currently delivering vectors
(HTTP error, timeout, malformed body, empty vector). The message is deliberately
generic — it MUST NOT carry the upstream response body, so an upstream error
cannot leak to an API caller (see issue #50). The raw body is logged server-side
only.
"""

from __future__ import annotations


class EmbedderUnavailable(RuntimeError):
    """Raised when the embedding provider cannot return a usable vector.

    ``cause`` retains the originating exception for server-side logging without
    putting it in the user-visible message. ``status_code`` is the provider's
    HTTP status when it answered with an error, and ``None`` for a timeout, a
    transport error or an unusable body: the query embedder's health tracker
    (:mod:`app.embeddings.query_health`) tells an account refusal (401-403, or
    402 for an empty prepaid balance) from a transient failure by it.
    """

    def __init__(
        self,
        message: str,
        *,
        cause: Exception | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.cause = cause
        self.status_code = status_code
