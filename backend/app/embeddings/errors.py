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
    putting it in the user-visible message.
    """

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause
