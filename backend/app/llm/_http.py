"""Shared HTTP transport surface for the LLM clients.

The Anthropic, Gemini, and OpenRouter clients speak different wire shapes
(URL, payload, headers, response fields) but share an identical transport /
error / JSON-decode contract. :func:`post_json` owns that shared surface so the
error taxonomy lives in one place; each client keeps only its genuinely
provider-specific pieces.

Error taxonomy
--------------

Every failure surfaces as :class:`LLMUnavailable` so the factory's fallback
wrapper (and the orchestrator) react uniformly:

* transport timeout / connection / protocol errors,
* any ``status_code >= 400`` (both "provider unavailable" 5xx/408/429 and other
  4xx client errors — none is retried here; the orchestrator decides), and
* a non-JSON body.

Security invariant (issue #50)
------------------------------

The upstream error body can carry provider identity and raw error text, so it is
logged SERVER-SIDE only, capped at :data:`_ERROR_BODY_LOG_LIMIT`, NEVER includes
the request headers (which hold the API key), and is NEVER placed in the
:class:`LLMUnavailable` message — so it cannot leak to the caller through
``error.details.reason``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import httpx

from app.core.middleware import get_current_request_id
from app.llm.errors import LLMUnavailable

_logger = logging.getLogger("citevyn.llm")

# Cap the upstream body we keep in the SERVER log. Enough to debug the
# provider failure without hoarding an unbounded response.
_ERROR_BODY_LOG_LIMIT = 500


async def post_json(
    *,
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float,
    provider: str,
    error_event: str,
) -> dict[str, Any]:
    """POST ``payload`` and return the decoded JSON object.

    ``provider`` is the human-readable label ("Anthropic" / "Gemini" /
    "OpenRouter") woven into the :class:`LLMUnavailable` messages. ``error_event``
    is the per-provider log event name ("anthropic_error_response", …) kept stable
    so operator dashboards continue to match.

    Raises :class:`LLMUnavailable` on transport failure, any ``status_code >=
    400``, or a non-JSON body. The status branch collapses to ``>= 400`` because
    every 4xx/5xx is surfaced as unavailable uniformly (the fallback wrapper
    retries on any :class:`LLMUnavailable`); no status subset is treated
    specially here.
    """
    try:
        response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise LLMUnavailable(
            f"{provider} request timed out after {timeout_seconds}s",
            cause=exc,
        ) from exc
    except httpx.HTTPError as exc:
        # Covers connection errors, protocol errors, etc.
        raise LLMUnavailable(
            f"{provider} transport error: {exc.__class__.__name__}",
            cause=exc,
        ) from exc

    if response.status_code >= 400:
        # Covers both "provider unavailable" (5xx/408/429) and other client
        # errors (400/401/403) — neither is retried here; the orchestrator
        # decides. The upstream body can carry provider identity and raw error
        # text, so it is logged SERVER-SIDE only (never the request headers,
        # which hold the API key) and kept out of the exception message so it
        # cannot leak to the caller through error.details.reason.
        _logger.warning(
            error_event,
            extra={
                "request_id": get_current_request_id(),
                "status_code": response.status_code,
                "body": response.text[:_ERROR_BODY_LOG_LIMIT],
            },
        )
        raise LLMUnavailable(f"{provider} returned {response.status_code}")

    try:
        raw_data: Any = json.loads(response.content)
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"{provider} returned non-JSON body", cause=exc) from exc
    # The provider JSON shapes are documented but the wire is not type-narrowed;
    # cast to a dict and let each client validate fields defensively as it reads.
    return cast(dict[str, Any], raw_data)
