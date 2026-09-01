"""Shared HTTP transport for the OAuth token-exchange + userinfo calls.

Mirrors ``app.llm._http.post_json``'s error-handling and security invariants
-- timeout -> clear error, non-2xx -> clear error, upstream error body logged
SERVER-SIDE only, never surfaced to the caller (issue #50's invariant) -- but
speaks the shapes OAuth2 actually needs instead of ``post_json``'s JSON body:

* the token endpoint (RFC 6749 §4.1.3) expects
  ``application/x-www-form-urlencoded``, not JSON -- Google's token endpoint
  specifically rejects a JSON body -- so :func:`post_form` sends one;
* the userinfo endpoint is a plain authenticated GET, handled by
  :func:`get_json`.

Not built on ``post_json`` directly because that function is hard-coded to a
JSON request body; this module copies its SHAPE (the same error taxonomy),
not a parallel, weaker contract.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import httpx

from app.core.middleware import get_current_request_id

_logger = logging.getLogger("citevyn.oauth")

# Cap the upstream body kept in the SERVER log -- mirrors
# ``app.llm._http._ERROR_BODY_LOG_LIMIT``.
_ERROR_BODY_LOG_LIMIT = 500


class OAuthProviderError(RuntimeError):
    """Raised when an OAuth provider call fails (transport, timeout, or non-2xx).

    The caller (``app.api.routes.oauth``) treats this uniformly as "the
    provider is unavailable" and redirects to ``/?auth=error`` -- the
    message here is safe to log but is never placed in an HTTP response.
    """


async def _handle_response(
    response: httpx.Response, *, provider: str, error_event: str
) -> dict[str, Any]:
    if response.status_code >= 400:
        # The upstream body can carry provider identity and raw error text,
        # so it is logged SERVER-SIDE only (never the request headers, which
        # hold the client secret / access token) and kept out of the
        # exception message so it cannot leak to the caller.
        _logger.warning(
            error_event,
            extra={
                "request_id": get_current_request_id(),
                "status_code": response.status_code,
                "body": response.text[:_ERROR_BODY_LOG_LIMIT],
            },
        )
        raise OAuthProviderError(f"{provider} returned {response.status_code}")

    try:
        raw_data: Any = json.loads(response.content)
    except json.JSONDecodeError as exc:
        raise OAuthProviderError(f"{provider} returned non-JSON body") from exc
    return cast(dict[str, Any], raw_data)


async def post_form(
    *,
    client: httpx.AsyncClient,
    url: str,
    data: dict[str, str],
    headers: dict[str, str],
    timeout_seconds: float,
    provider: str,
    error_event: str,
) -> dict[str, Any]:
    """POST ``data`` as ``application/x-www-form-urlencoded`` and return the decoded JSON object.

    Used for the OAuth2 token-exchange call (RFC 6749 §4.1.3).
    """
    try:
        response = await client.post(url, data=data, headers=headers, timeout=timeout_seconds)
    except httpx.TimeoutException as exc:
        raise OAuthProviderError(f"{provider} request timed out after {timeout_seconds}s") from exc
    except httpx.HTTPError as exc:
        raise OAuthProviderError(f"{provider} transport error: {exc.__class__.__name__}") from exc
    return await _handle_response(response, provider=provider, error_event=error_event)


async def get_json(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    provider: str,
    error_event: str,
) -> dict[str, Any]:
    """GET ``url`` and return the decoded JSON object.

    Used for the OAuth userinfo call.
    """
    try:
        response = await client.get(url, headers=headers, timeout=timeout_seconds)
    except httpx.TimeoutException as exc:
        raise OAuthProviderError(f"{provider} request timed out after {timeout_seconds}s") from exc
    except httpx.HTTPError as exc:
        raise OAuthProviderError(f"{provider} transport error: {exc.__class__.__name__}") from exc
    return await _handle_response(response, provider=provider, error_event=error_event)


__all__ = ["OAuthProviderError", "get_json", "post_form"]
