"""Shareable answers (ADR-0005 §4, Phase 6C).

* ``POST /v1/sessions/{sid}/messages/{mid}/share`` — share one of your own
  cited answers (Pro, capability ``share_answer``). The question, answer,
  citations and "docs as of" date are COPIED, so the shared page never changes.
  Sharing the same answer again returns the live share (200), not a second one.
* ``GET /v1/me/shares`` and ``DELETE /v1/me/shares/{share_id}`` — list and
  revoke. Only a signed-in account (``manage_account``), NOT Pro: after Pro
  lapses the links still work, and their owner must always be able to kill them.
* ``GET /s/{share_id}`` — the public page, for anyone with the link. 404 once
  revoked, and ``Cache-Control: no-store`` so a revoke takes effect at once.

All of them are 404 unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on. A share that
is not the caller's, or does not exist, is 404: never confirm a share exists.
The page renderer (``app.services.shared_answer_page``) is the XSS and phishing
boundary; read its docstring before changing what reaches it.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import requires
from app.access.policy import Capability
from app.answer.orchestrator import docs_as_of
from app.api.routes.messages import require_message, require_session
from app.core.auth_sessions import resolve_principal
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.models import Message, SharedAnswer, User
from app.models.enums import MessageRole
from app.services.shared_answer_page import render_shared_answer_page

router = APIRouter(tags=["shares"])

MAX_LIVE_SHARES = 100
_SHARE_ID = re.compile(r"[0-9a-f]{32}")


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def _not_found(request: Request) -> Exception:
    return error_response(
        request_id=_request_id(request), code=APIErrorCode.not_found, message="Not found."
    )


def shares_enabled(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """404 unless the access model is on: 'off' must look like 'not there'."""
    if not settings.access_model_enabled:
        raise _not_found(request)


def _view(row: SharedAnswer) -> dict[str, Any]:
    return {
        "share_id": row.share_id,
        "url": f"/s/{row.share_id}",
        "question": row.question[:200],
        "created_at": row.created_at.isoformat(),
    }


async def _question_for(db: AsyncSession, answer: Message) -> str:
    """The user's question: the last user message before the answer, in its session."""
    row = (
        await db.execute(
            select(Message.content)
            .where(
                Message.session_id == answer.session_id,
                Message.role == MessageRole.user,
                Message.created_at <= answer.created_at,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row or ""


@router.post(
    "/v1/sessions/{session_id}/messages/{message_id}/share",
    dependencies=[Depends(requires(Capability.share_answer))],
    summary="Share one of your answers as a public, frozen page.",
)
async def share_answer(
    request: Request,
    response: Response,
    session_id: Annotated[uuid.UUID, Path()],
    message_id: Annotated[uuid.UUID, Path()],
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(shares_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    request_id = _request_id(request)
    await require_session(db, request_id=request_id, session_id=session_id, user_id=principal_id)
    answer = await require_message(
        db, request_id=request_id, session_id=session_id, message_id=message_id
    )
    citations = answer.citations or []
    # Only a cited answer: a refusal or a question has nothing worth sharing.
    if str(answer.role) != MessageRole.assistant.value or not citations:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.validation_error,
            message="Only an answer with sources can be shared.",
        )
    # Serialise per account (a row lock on Postgres), so a burst cannot pass the
    # cap together or share one answer twice.
    await db.execute(select(User).where(User.user_id == principal_id).with_for_update())
    live = select(SharedAnswer).where(
        SharedAnswer.user_id == principal_id, SharedAnswer.revoked_at.is_(None)
    )
    existing = (
        await db.execute(live.where(SharedAnswer.message_id == message_id))
    ).scalar_one_or_none()
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return {"request_id": request_id, **_view(existing)}
    count = (await db.execute(select(func.count()).select_from(live.subquery()))).scalar_one()
    if count >= MAX_LIVE_SHARES:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.too_many_shares,
            message=f"You can have at most {MAX_LIVE_SHARES} shared answers. Revoke one first.",
        )
    row = SharedAnswer(
        share_id=secrets.token_hex(16),
        user_id=principal_id,
        message_id=message_id,
        question=await _question_for(db, answer),
        answer=answer.content,
        citations=[dict(c) for c in citations],
        docs_as_of=docs_as_of(citations),
        answered_at=answer.created_at,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    response.status_code = status.HTTP_201_CREATED
    return {"request_id": request_id, **_view(row)}


@router.get("/v1/me/shares", dependencies=[Depends(requires(Capability.manage_account))])
async def list_shares(
    request: Request,
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(shares_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(SharedAnswer)
            .where(SharedAnswer.user_id == principal_id, SharedAnswer.revoked_at.is_(None))
            .order_by(SharedAnswer.created_at.desc())
        )
    ).scalars()
    return {"request_id": _request_id(request), "shares": [_view(r) for r in rows]}


@router.delete(
    "/v1/me/shares/{share_id}",
    dependencies=[Depends(requires(Capability.manage_account))],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_share(
    request: Request,
    share_id: str,
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(shares_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    row = await db.get(SharedAnswer, share_id) if _SHARE_ID.fullmatch(share_id) else None
    if row is None or row.user_id != principal_id or row.revoked_at is not None:
        raise _not_found(request)
    row.revoked_at = datetime.now(UTC)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/s/{share_id}",
    dependencies=[Depends(requires(Capability.public))],
    response_class=HTMLResponse,
    summary="A shared answer (404 unless the access model is on, or once revoked).",
)
async def shared_page(
    request: Request,
    share_id: str,
    _on: Annotated[None, Depends(shares_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    row = await db.get(SharedAnswer, share_id) if _SHARE_ID.fullmatch(share_id) else None
    if row is None or row.revoked_at is not None:
        raise _not_found(request)
    page = render_shared_answer_page(
        question=row.question,
        answer=row.answer,
        citations=row.citations,
        docs_as_of=row.docs_as_of,
        shared_on=row.created_at.date().isoformat(),
    )
    return HTMLResponse(page, headers={"X-Robots-Tag": "noindex", "Cache-Control": "no-store"})


__all__ = ["MAX_LIVE_SHARES", "router"]
