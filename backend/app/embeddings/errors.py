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
    transport error or an unusable body (server-side logging only).

    ``account_refused`` is the provider client's verdict that the provider
    refuses THIS ACCOUNT rather than one request: an empty prepaid balance, a
    bad or revoked key. Only the client can tell, because the statuses differ by
    provider (Gemini names a bad key in a 400; OpenRouter's 403 can be one
    flagged input). The query embedder's health tracker
    (:mod:`app.embeddings.query_health`) pages on this flag alone.
    """

    def __init__(
        self,
        message: str,
        *,
        cause: Exception | None = None,
        status_code: int | None = None,
        account_refused: bool = False,
    ) -> None:
        super().__init__(message)
        self.cause = cause
        self.status_code = status_code
        self.account_refused = account_refused
