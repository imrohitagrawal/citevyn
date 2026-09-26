"""The sign-up bot check (ADR-0005 Phase 5): a proof-of-work, used once.

``require_bot_check`` guards ``POST /v1/auth/register`` and
``POST /v1/auth/magic-link/request``: a request must carry a solved challenge
from ``GET /v1/auth/challenge`` in the ``X-CiteVyn-Bot-Check`` header
(base64url JSON). The maths is ``app.core.pow``; this module adds the header,
the setting and single use.

Off, exactly as today, while ``CITEVYN_ACCESS_MODEL_ENABLED`` is off. With it on
and no secret configured it fails CLOSED (production refuses to start that way).
OAuth sign-in is not guarded: it is a browser redirect, and the provider has
already put a human through its own checks.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import Depends, Request
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.core.pow import PowError, verify_solution
from app.models import BotCheckUse

HEADER = "X-CiteVyn-Bot-Check"


def _refuse(request: Request) -> Exception:
    return error_response(
        request_id=str(request.state.request_id),
        code=APIErrorCode.bot_check_failed,
        message="Please complete the check and try again.",
    )


def _decode(value: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload: object = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise PowError("not base64url JSON") from exc
    if not isinstance(payload, dict):
        raise PowError("not a JSON object")
    return cast(dict[str, Any], payload)


async def require_bot_check(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Refuse with 403 ``bot_check_failed`` unless a fresh, unused solution came."""
    if not settings.access_model_enabled:
        return
    value = request.headers.get(HEADER)
    secret = settings.bot_check_secret
    if not value or not secret:
        raise _refuse(request)
    now = datetime.now(UTC)
    try:
        payload = _decode(value)
        challenge = verify_solution(secret, payload, now=int(now.timestamp()))
    except PowError as exc:
        raise _refuse(request) from exc
    expires = int(str(payload["salt"]).rpartition("?expires=")[2])
    # Expired rows can never be replayed (their signed expiry refuses them).
    await db.execute(delete(BotCheckUse).where(BotCheckUse.expires_at < now))
    # Recorded in the request's own transaction: committed only if the request
    # succeeds, so a sign-up refused for another reason can retry.
    if await db.get(BotCheckUse, challenge) is not None:
        raise _refuse(request)
    db.add(
        BotCheckUse(
            challenge=challenge,
            used_at=now,
            expires_at=datetime.fromtimestamp(expires, UTC),
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:  # the same solution, used at the same moment
        await db.rollback()
        raise _refuse(request) from exc


__all__ = ["HEADER", "require_bot_check"]
