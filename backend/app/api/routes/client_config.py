"""What the web client needs to know before anyone signs in (ADR-0005 Phase 5).

* ``GET /v1/config`` — whether the access model is on, so the pricing section,
  the usage meter and the sign-up check render only then. With it off the page
  is exactly today's. Nothing here is secret: "off" is already visible as the
  billing routes' 404.
* ``GET /v1/auth/challenge`` — a proof-of-work challenge for the sign-up bot
  check (``app.core.pow``; ``app.access.bot_check`` verifies it). 404 while the
  access model is off. Issuing one is an HMAC and a random number: cheap, and
  worthless until it is solved and checked.

Both are open (no credential): the landing page reads them before any sign-in.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.access.deps import requires
from app.access.policy import Capability
from app.core.config import Settings, get_settings
from app.core.errors import APIErrorCode, error_response
from app.core.pow import make_challenge

router = APIRouter(tags=["config"])


@router.get("/v1/config", dependencies=[Depends(requires(Capability.public))])
async def client_config(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    on = settings.access_model_enabled
    return {
        "request_id": str(request.state.request_id),
        "access_model": on,
        "bot_check": {"kind": "pow"} if on else None,
    }


@router.get("/v1/auth/challenge", dependencies=[Depends(requires(Capability.public))])
async def bot_check_challenge(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    request_id = str(request.state.request_id)
    if not settings.access_model_enabled or not settings.bot_check_secret:
        raise error_response(
            request_id=request_id, code=APIErrorCode.not_found, message="Not found."
        )
    return {
        "request_id": request_id,
        "challenge": make_challenge(
            settings.bot_check_secret,
            max_number=settings.bot_check_max_number,
            ttl_s=settings.bot_check_ttl_seconds,
            now=int(time.time()),
        ),
    }


__all__ = ["router"]
