"""``/v1/me/watches`` — Pro watch alerts (ADR-0005 §5, Phase 7C).

"Watch a specific flag, model, command or page and get an alert when it
changes." Making a watch is Pro (capability ``watch_alerts``); listing and
removing need only an account (``manage_account``), so a lapsed Pro user can
still clear theirs. All of it is 404 unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on.

* ``page``: one of the watched vendor pages (``app/watch/pages.py``), by its
  URL (the readable one or its Markdown twin).
* ``term``: 2 to 64 characters, stored ignoring case, looked for (ignoring
  case) anywhere in the lines a change added or removed: a flag
  (``--permission-mode``), a model id, a command. It is a substring match, so
  ``--resume`` also matches ``--resume-session``.

At most :data:`MAX_WATCHES` per account. Watching the same thing twice returns
the existing watch.
"""

from __future__ import annotations

import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Request, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import requires
from app.access.policy import Capability
from app.answer.comparison import product_name
from app.core.auth_sessions import resolve_principal
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.models import User, Watch
from app.watch.pages import WATCHED_PAGES, human_url

router = APIRouter(prefix="/v1/me/watches", tags=["watches"])

MAX_WATCHES = 25
_PAGES = {p.url: p for p in WATCHED_PAGES}
_BY_HUMAN = {human_url(p.url): p.url for p in WATCHED_PAGES}


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def watches_enabled(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """404 unless the access model is on: 'off' must look like 'not there'."""
    if not settings.access_model_enabled:
        raise error_response(
            request_id=_request_id(request), code=APIErrorCode.not_found, message="Not found."
        )


class NewWatch(BaseModel):
    kind: Literal["page", "term"]
    value: str = Field(max_length=512)

    @model_validator(mode="after")
    def _valid(self) -> NewWatch:
        value = self.value.strip()
        if self.kind == "page":
            # A fragment or a trailing slash is the same page.
            value = value.split("#", 1)[0].rstrip("/")
            url = value if value in _PAGES else _BY_HUMAN.get(value)
            if url is None:
                raise ValueError("value must be one of the watched pages (GET /v1/me/watches)")
            self.value = url
            return self
        if not 2 <= len(value) <= 64:
            raise ValueError("a term is 2 to 64 characters")
        # No control, format or line characters: a term is matched against
        # single lines, and must not hide text (a right-to-left override).
        if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value):
            raise ValueError("a term cannot contain control or formatting characters")
        # Matching ignores case, so the stored term does too: "--Foo" and
        # "--foo" are one watch (the unique constraint sees one value).
        self.value = value.casefold()
        return self


def _view(row: Watch) -> dict[str, Any]:
    if row.kind == "page" and row.value in _PAGES:
        page = _PAGES[row.value]
        label = f"{product_name(page.product_area)}: {page.title}"
    else:
        label = row.value
    return {
        "watch_id": str(row.watch_id),
        "kind": row.kind,
        "value": row.value,
        "label": label,
        "created_at": row.created_at.isoformat(),
    }


@router.post("", dependencies=[Depends(requires(Capability.watch_alerts))])
async def create_watch(
    request: Request,
    response: Response,
    body: Annotated[NewWatch, Body()],
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(watches_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    # Serialise per account (a row lock on Postgres): a burst cannot pass the cap.
    await db.execute(select(User).where(User.user_id == principal_id).with_for_update())
    existing = (
        await db.execute(
            select(Watch).where(
                Watch.user_id == principal_id, Watch.kind == body.kind, Watch.value == body.value
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return {"request_id": _request_id(request), **_view(existing)}
    count = (
        await db.execute(
            select(func.count()).select_from(Watch).where(Watch.user_id == principal_id)
        )
    ).scalar_one()
    if count >= MAX_WATCHES:
        raise error_response(
            request_id=_request_id(request),
            code=APIErrorCode.too_many_watches,
            message=f"You can have at most {MAX_WATCHES} watches. Remove one first.",
        )
    row = Watch(
        watch_id=uuid.uuid4(),
        user_id=principal_id,
        kind=body.kind,
        value=body.value,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    response.status_code = status.HTTP_201_CREATED
    return {"request_id": _request_id(request), **_view(row)}


@router.get("", dependencies=[Depends(requires(Capability.manage_account))])
async def list_watches(
    request: Request,
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(watches_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(Watch).where(Watch.user_id == principal_id).order_by(Watch.created_at)
        )
    ).scalars()
    return {
        "request_id": _request_id(request),
        "watches": [_view(r) for r in rows],
        # What a page watch can point at, for the UI's picker.
        "pages": [
            {"url": p.url, "title": p.title, "product": product_name(p.product_area)}
            for p in WATCHED_PAGES
        ],
    }


@router.delete(
    "/{watch_id}",
    dependencies=[Depends(requires(Capability.manage_account))],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_watch(
    request: Request,
    watch_id: str,
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(watches_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    try:
        key = uuid.UUID(watch_id)
    except ValueError:
        key = None
    row = await db.get(Watch, key) if key is not None else None
    if row is None or row.user_id != principal_id:
        raise error_response(
            request_id=_request_id(request), code=APIErrorCode.not_found, message="Not found."
        )
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["MAX_WATCHES", "router"]
