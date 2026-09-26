"""The weekly digest: opt in, opt out, one-click unsubscribe (ADR-0005 §5, 7B).

* ``GET /v1/me/digest`` and ``PUT /v1/me/digest`` ``{"subscribed": bool}`` —
  capability ``weekly_digest`` (any account). Subscribing needs a verified
  address (403 ``verification_required``); unsubscribing never does.
* ``GET /digest/unsubscribe?t=…`` — a page with one button. It changes
  NOTHING: mail scanners open links, and must not unsubscribe people.
* ``POST /digest/unsubscribe?t=…`` — does it (the button, and RFC 8058
  one-click from the mail app, which carries no cookie). The token is the one
  in that email; only its hash is stored.

All of it is 404 unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on.
"""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import requires
from app.access.policy import Capability
from app.core.auth_sessions import resolve_principal
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.core.token_secrets import verify_token
from app.models import DigestSend, User
from app.services.about_page import render_page
from app.services.digest import UNSUBSCRIBE_PATH

router = APIRouter(tags=["digest"])


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def digest_enabled(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """404 unless the access model is on: 'off' must look like 'not there'."""
    if not settings.access_model_enabled:
        raise error_response(
            request_id=_request_id(request), code=APIErrorCode.not_found, message="Not found."
        )


class DigestPreference(BaseModel):
    subscribed: bool


async def _user(db: AsyncSession, request: Request, principal_id: str) -> User:
    user = await db.get(User, principal_id)
    if user is None:
        raise error_response(
            request_id=_request_id(request), code=APIErrorCode.not_found, message="Not found."
        )
    return user


def _view(request: Request, user: User) -> dict[str, Any]:
    return {
        "request_id": _request_id(request),
        "subscribed": user.digest_opt_in_at is not None,
        "verified": user.email_verified_at is not None,
    }


@router.get("/v1/me/digest", dependencies=[Depends(requires(Capability.weekly_digest))])
async def get_digest(
    request: Request,
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(digest_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    return _view(request, await _user(db, request, principal_id))


@router.put("/v1/me/digest", dependencies=[Depends(requires(Capability.weekly_digest))])
async def put_digest(
    request: Request,
    body: Annotated[DigestPreference, Body()],
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(digest_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    user = await _user(db, request, principal_id)
    if body.subscribed:
        if user.email_verified_at is None:
            raise error_response(
                request_id=_request_id(request),
                code=APIErrorCode.verification_required,
                message="Verify your email address to get the weekly digest.",
            )
        if user.digest_opt_in_at is None:
            user.digest_opt_in_at = datetime.now(UTC)
    else:
        user.digest_opt_in_at = None
    await db.commit()
    return _view(request, user)


def _page(title: str, lead: str, main_html: str = "", status: int = 200) -> HTMLResponse:
    page = render_page(
        page_title=title,
        description="CiteVyn weekly digest.",
        lead=lead,
        lead_class="about-lead",
        main_html=main_html,
    )
    return HTMLResponse(
        page, status_code=status, headers={"X-Robots-Tag": "noindex", "Cache-Control": "no-store"}
    )


_INVALID = (
    "This link does not work",
    "It may be mistyped. You can also turn the digest off in your account menu.",
)


async def _send_for(db: AsyncSession, t: str) -> DigestSend | None:
    """The send this token belongs to, or None. Never says which part was wrong."""
    send_id, _, token = t.partition(".")
    try:
        key = uuid.UUID(send_id)
    except ValueError:
        return None
    row = await db.get(DigestSend, key)
    if row is None or not token or not verify_token(token, row.token_hash):
        return None
    return row


@router.get(
    UNSUBSCRIBE_PATH,
    dependencies=[Depends(requires(Capability.public))],
    response_class=HTMLResponse,
)
async def unsubscribe_page(
    t: Annotated[str, Query(max_length=200)],
    _on: Annotated[None, Depends(digest_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    if await _send_for(db, t) is None:
        return _page(*_INVALID, status=404)
    action = html.escape(f"{UNSUBSCRIBE_PATH}?t={t}", quote=True)
    form = (
        f'<form method="post" action="{action}">'
        '<button type="submit">Stop the weekly digest</button></form>'
    )
    return _page("Stop the weekly digest?", "One click and no more digest emails.", form)


@router.post(
    UNSUBSCRIBE_PATH,
    dependencies=[Depends(requires(Capability.public))],
    response_class=HTMLResponse,
)
async def unsubscribe(
    t: Annotated[str, Query(max_length=200)],
    _on: Annotated[None, Depends(digest_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    row = await _send_for(db, t)
    if row is None:
        return _page(*_INVALID, status=404)
    user = await db.get(User, row.user_id)
    if user is not None:
        user.digest_opt_in_at = None
    if row.unsubscribed_at is None:
        row.unsubscribed_at = datetime.now(UTC)
    await db.commit()
    return _page(
        "You will not get the weekly digest", "Done. You can turn it back on in your account menu."
    )


__all__ = ["router"]
