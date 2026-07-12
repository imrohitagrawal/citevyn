"""Fallback LLM client.

Wraps a primary and a secondary :class:`LLMClient`. ``complete`` tries the
primary; if it raises :class:`LLMUnavailable` (timeout, 5xx, 429, transport
error, or an upstream 4xx surfaced as unavailable), it transparently retries
the same request against the secondary. Used to make Gemini the primary
provider with OpenRouter as an automatic backstop (see
:mod:`app.llm.factory`).

Only :class:`LLMUnavailable` is caught — any other exception (a programming
error, a cancellation) propagates so it is not masked by a fallback attempt.
If the secondary also fails, its :class:`LLMUnavailable` propagates to the
orchestrator, which maps it to a 5xx.
"""

from __future__ import annotations

import logging

from app.llm.errors import LLMUnavailable
from app.llm.protocol import LLMClient
from app.llm.types import LLMResult

_logger = logging.getLogger("citevyn.llm")


class FallbackLLMClient:
    """Primary LLM client with an automatic secondary on ``LLMUnavailable``."""

    def __init__(self, *, primary: LLMClient, secondary: LLMClient) -> None:
        self._primary = primary
        self._secondary = secondary

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        try:
            return await self._primary.complete(
                system=system, user=user, max_tokens=max_tokens, temperature=temperature
            )
        except LLMUnavailable as exc:
            # Do not log prompt/response bodies — only the failure class, so
            # the fallback is observable without leaking evidence text.
            _logger.warning(
                "llm_primary_unavailable_falling_back",
                extra={"cause": exc.__class__.__name__},
            )
            return await self._secondary.complete(
                system=system, user=user, max_tokens=max_tokens, temperature=temperature
            )

    async def aclose(self) -> None:
        """Close both underlying clients.

        ``aclose`` is part of the :class:`LLMClient` contract, so both children
        are guaranteed to define it (a no-op when they own no resources)."""
        await self._primary.aclose()
        await self._secondary.aclose()
