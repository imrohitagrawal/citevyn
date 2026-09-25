"""Bearer-token auth dependencies for the public + admin routes.

The MVP uses a static API key (no rotation, no per-user identities).
A future slice will replace this with a proper token issuer. The
*shape* of the dependency — return a stable ``user_id`` string, raise
the standard error envelope on failure — is the contract the routes
and the rate limiter rely on.
"""

from __future__ import annotations

import secrets
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.errors import APIErrorCode, error_response
from app.core.middleware import get_current_request_id
from app.core.request_origin import is_same_site_request

bearer_scheme = HTTPBearer(auto_error=False)

# Sentinel for the admin user. The orchestrator / audit log stamps
# this as ``user_id`` whenever an admin-only route is called, so
# downstream code can tell demo traffic from admin traffic without
# inspecting the API key itself.
ADMIN_USER_ID: str = "admin"
DEMO_USER_ID: str = "demo_user"

# The fixed header that replaces the public token (ADR-0005 §3). It is not a
# secret and does not need to be: a cross-site page cannot set a custom header
# without a CORS preflight, and CORS grants that only to our own origins.
CLIENT_HEADER = "X-CiteVyn-Client"
CLIENT_HEADER_VALUE = "web"


def _allowed_origins(settings: Settings) -> set[str]:
    """Our own site plus the CORS allowlist."""
    allowed = set(settings.cors_allowed_origins)
    if settings.magic_link_base_url:
        parts = urlsplit(settings.magic_link_base_url)
        allowed.add(f"{parts.scheme}://{parts.netloc}")
    return allowed


def require_public_client_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """Check the request came from our own web client; return the demo user id.

    TWO ACCEPTED PATHS during the token's retirement (ADR-0005 §3, step 1):

    1. **The fixed header** ``X-CiteVyn-Client: web``, plus the browser's own
       ``Origin`` / ``Sec-Fetch-Site`` saying the request is from our site
       (:func:`app.core.request_origin.is_same_site_request`). The header is not
       a secret: a cross-site page cannot set a custom header without a CORS
       preflight, and CORS grants that only to our origins.
    2. **The old public token** as ``Authorization: Bearer``, unchanged, so a
       bundle already open in someone's browser keeps working. It is PUBLIC
       (baked into the bundle): a CSRF guard and a rate-limit turnstile, not an
       identity control (#430). Step 2 removes it, owner-deployed, after step 1
       is live; this function is renamed then.

    A bearer that is present but WRONG is refused even when the header is also
    present: the new path never papers over a bad credential on the old one.

    Auth failures raise the standard envelope via
    :func:`app.core.errors.error_response` so the client can parse
    one shape for every 401 (instead of FastAPI's default
    ``{"detail": "..."}`` body).
    """
    request_id = str(getattr(request.state, "request_id", "") or get_current_request_id())
    if credentials is None or credentials.scheme.lower() != "bearer":
        if request.headers.get(CLIENT_HEADER) != CLIENT_HEADER_VALUE:
            raise error_response(
                request_id=request_id,
                code=APIErrorCode.auth_required,
                message="Missing bearer token.",
            )
        if not is_same_site_request(
            fetch_site=request.headers.get("sec-fetch-site"),
            origin=request.headers.get("origin"),
            allowed=_allowed_origins(settings),
        ):
            raise error_response(
                request_id=request_id,
                code=APIErrorCode.auth_required,
                message="Cross-site request refused.",
            )
        return DEMO_USER_ID

    # ``secrets.compare_digest`` to avoid a timing oracle, matching
    # ``require_admin_api_key`` below — a naive ``==``/``!=`` would let an
    # attacker measure prefix-match time to narrow down the key.
    if not secrets.compare_digest(credentials.credentials, settings.public_client_token):
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.auth_required,
            message="Invalid bearer token.",
        )

    return DEMO_USER_ID


def require_admin_api_key(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    x_admin_api_key: Annotated[str | None, Header(alias="X-Admin-API-Key")] = None,
) -> str:
    """Validate the ``X-Admin-API-Key`` header and return ``"admin"``.

    A separate header (not ``Authorization: Bearer``) is used because:

    * the admin key never reaches the public /v1/* routes — the demo
      SPA sends a different token via ``Authorization``
    * header separation makes accidental cross-use loud: sending the
      admin key in the public bearer field is a 401, not a silent
      privilege escalation
    * rotating the admin key is a single env-var change, not a
      rotation of the public key
    """
    request_id = str(getattr(request.state, "request_id", "") or get_current_request_id())
    expected = settings.admin_api_key
    if not expected:
        # ``admin_api_key == ""`` is a deploy-time misconfiguration.
        # Fail closed with 503 so the operator notices immediately
        # (instead of accidentally returning 401 on every call).
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.internal_error,
            message="Admin API key is not configured on the server.",
        )
    if not x_admin_api_key:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.auth_required,
            message="Missing admin API key header.",
        )
    # ``secrets.compare_digest`` to avoid a timing oracle — a naive
    # ``==`` would let an attacker measure the prefix-match time to
    # narrow down the key.
    if not secrets.compare_digest(x_admin_api_key, expected):
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.auth_required,
            message="Invalid admin API key.",
        )
    return ADMIN_USER_ID


__all__ = [
    "ADMIN_USER_ID",
    "DEMO_USER_ID",
    "require_admin_api_key",
    "require_public_client_token",
]
