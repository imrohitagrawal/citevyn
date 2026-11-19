"""Typed LLM errors.

The answer engine catches :class:`LLMUnavailable` and surfaces it as
``internal_error`` (or ``cost_limit_reached``, depending on cause). The
separation lets the orchestrator distinguish a transport problem
(provider down, 5xx, timeout) from a malformed-prompt problem, which
it would not want to retry.
"""

from __future__ import annotations


class LLMUnavailable(RuntimeError):
    """Raised when the LLM provider is unreachable or returns 5xx.

    Carries the original cause so the orchestrator can log it. The
    client never swallows the underlying exception.

    The other keyword fields are a structured cause, all optional (#576): the
    failure ``kind``, the HTTP ``status_code``, the provider's short error code
    (``provider_status``, Gemini's ``error.status``), a finish or block
    ``reason``, and on an empty answer the token counts and the
    ``max_output_tokens`` sent. They are stored as given; the one place they are
    held to a fixed shape is :meth:`app.llm.attempts.PrimaryFailure.from_error`.
    None of them is ever part of the message.
    """

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
        kind: str = "unknown",
        status_code: int | None = None,
        provider_status: object = None,
        reason: object = None,
        thoughts_tokens: object = None,
        answer_tokens: object = None,
        max_output_tokens: int | None = None,
    ) -> None:
        super().__init__(message)
        self.cause = cause
        self.kind = kind
        self.status_code = status_code
        self.provider_status = provider_status
        self.reason = reason
        self.thoughts_tokens = thoughts_tokens
        self.answer_tokens = answer_tokens
        self.max_output_tokens = max_output_tokens


class CostLimitReached(LLMUnavailable):
    """Raised when the §9 daily spend cap is reached, before any paid call.

    Subclasses :class:`LLMUnavailable` **deliberately**. Every caller in the
    answer path already treats that as a TRANSIENT transport failure and surfaces
    a 5xx — which is exactly the required shape here. Returning a content refusal
    instead would tell the client the corpus lacks an answer and suppress retry:
    that is the #142 bug, and it is far worse than a 503, because the user is
    taught something false about the product rather than about its availability.

    Subclassing also means a budget trip cannot be silently mis-handled by a path
    that predates this class — the worst case is a slightly generic 5xx, never a
    fabricated no-answer.
    """
