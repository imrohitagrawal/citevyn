"""One-click unsubscribe from watch alerts (ADR-0005 §5, Phase 7C-2).

* ``GET /alerts/unsubscribe?t=…`` — a page with one button. It changes
  NOTHING: mail scanners open links, and must not remove anyone's watches.
* ``POST /alerts/unsubscribe?t=…`` — removes that account's watches, so no
  more alerts (the button, and RFC 8058 one-click from the mail app, which
  carries no cookie). The token is the one in that email; only its hash is
  stored. Idempotent.

404 unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on.
"""

from __future__ import annotations

import html
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import requires
from app.access.policy import Capability
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.core.token_secrets import verify_token
from app.models import AlertSend, Watch
from app.services.about_page import render_page
from app.services.alerts import UNSUBSCRIBE_PATH

router = APIRouter(tags=["alerts"])


def alerts_enabled(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """404 unless the access model is on: 'off' must look like 'not there'."""
    if not settings.access_model_enabled:
        raise error_response(
            request_id=str(request.state.request_id),
            code=APIErrorCode.not_found,
            message="Not found.",
        )


def _page(title: str, lead: str, main_html: str = "", status: int = 200) -> HTMLResponse:
    page = render_page(
        page_title=title,
        description="CiteVyn watch alerts.",
        lead=lead,
        lead_class="about-lead",
        main_html=main_html,
    )
    return HTMLResponse(
        page, status_code=status, headers={"X-Robots-Tag": "noindex", "Cache-Control": "no-store"}
    )


_INVALID = (
    "This link does not work",
    "It may be mistyped. You can also remove watches in your account menu.",
)


async def _owner(db: AsyncSession, t: str) -> str | None:
    """The account this email's token belongs to, or None (never says why)."""
    email_id, _, token = t.partition(".")
    try:
        key = uuid.UUID(email_id)
    except ValueError:
        return None
    row = (
        await db.execute(select(AlertSend).where(AlertSend.email_id == key).limit(1))
    ).scalar_one_or_none()
    if row is None or not token or not verify_token(token, row.token_hash):
        return None
    return row.user_id


@router.get(
    UNSUBSCRIBE_PATH,
    dependencies=[Depends(requires(Capability.public))],
    response_class=HTMLResponse,
)
async def unsubscribe_page(
    t: Annotated[str, Query(max_length=200)],
    _on: Annotated[None, Depends(alerts_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    if await _owner(db, t) is None:
        return _page(*_INVALID, status=404)
    action = html.escape(f"{UNSUBSCRIBE_PATH}?t={t}", quote=True)
    form = (
        f'<form method="post" action="{action}">'
        '<button type="submit">Stop watch alerts</button></form>'
    )
    return _page(
        "Stop watch alerts?", "This removes all your watches, so no more alert emails.", form
    )


@router.post(
    UNSUBSCRIBE_PATH,
    dependencies=[Depends(requires(Capability.public))],
    response_class=HTMLResponse,
)
async def unsubscribe(
    t: Annotated[str, Query(max_length=200)],
    _on: Annotated[None, Depends(alerts_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    user_id = await _owner(db, t)
    if user_id is None:
        return _page(*_INVALID, status=404)
    await db.execute(delete(Watch).where(Watch.user_id == user_id))
    await db.commit()
    return _page(
        "No more watch alerts",
        "Done: your watches are removed. You can add watches again in your account menu.",
    )


__all__ = ["router"]
