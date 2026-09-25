"""Feedback, "report wrong answer" and "request this source" (ADR-0005 §6).

Trust must-haves, so they ship without the access setting. Both routes act only
on the caller's OWN conversations, found through the same session-ownership
loaders the message routes use (``require_session`` / ``require_message``), so
a stranger's session or message is a 404, never a 403.

With ``CITEVYN_ACCESS_MODEL_ENABLED`` on, both need the ``feedback`` capability,
which an account has and an anonymous caller does not (docs/ACCESS_POLICY.md).

Free text is stored for operators (``GET /v1/admin/feedback`` and
``/v1/admin/source_requests``) and is never logged.
"""

from __future__ import annotations

import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Path, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import requires
from app.access.policy import Capability
from app.api.routes.messages import require_message, require_session
from app.core.auth_sessions import resolve_principal
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.models import AnswerFeedback, Message, Session, SourceRequest
from app.models.enums import MessageRole

router = APIRouter(tags=["feedback"])

# Why an answer is wrong. A closed set, validated here; stored as a plain string.
FeedbackReason = Literal["wrong", "outdated", "missing_source", "unclear", "other"]


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"]
    reason: FeedbackReason | None = None
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reason_explains_a_thumbs_down(self) -> FeedbackRequest:
        if self.reason is not None and self.rating != "down":
            raise ValueError("a reason explains a thumbs-down; send rating 'down'")
        return self


class SourceRequestBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    message_id: uuid.UUID | None = None
    requested_url: str | None = Field(default=None, max_length=2048)
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("requested_url", mode="before")
    @classmethod
    def _trim(cls, value: object) -> object:
        # Trimmed BEFORE the length limit, so surrounding spaces the stored value
        # will not keep do not count against it.
        return value.strip() if isinstance(value, str) else value

    @field_validator("requested_url")
    @classmethod
    def _plain_http_link(cls, value: str | None) -> str | None:
        # Operators may open these links, so only a plain http(s) URL: no
        # whitespace or control/format characters (a stored newline, tab or a
        # right-to-left override can disguise a link) and no user:password@
        # (https://docs.example.com@evil.example/ reads as the wrong host).
        if value is None:
            return None
        if any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
            raise ValueError("requested_url must not contain spaces or control characters")
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("requested_url must be an http(s) URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError("requested_url must not contain a user name or password")
        return value


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


async def _find_vote(
    db: AsyncSession, message_id: uuid.UUID, user_id: str
) -> AnswerFeedback | None:
    return (
        await db.execute(
            select(AnswerFeedback).where(
                AnswerFeedback.message_id == message_id,
                AnswerFeedback.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


@router.put(
    "/v1/sessions/{session_id}/messages/{message_id}/feedback",
    dependencies=[Depends(requires(Capability.feedback))],
    summary="Rate an answer, or report it as wrong.",
)
async def put_feedback(
    request: Request,
    session_id: Annotated[uuid.UUID, Path()],
    message_id: Annotated[uuid.UUID, Path()],
    body: Annotated[FeedbackRequest, Body()],
    db: Annotated[AsyncSession, Depends(get_session)],
    principal_id: Annotated[str, Depends(resolve_principal)],
) -> dict[str, Any]:
    """One vote per (answer, user): a second call replaces the first."""
    request_id = _request_id(request)
    await require_session(db, request_id=request_id, session_id=session_id, user_id=principal_id)
    message = await require_message(
        db, request_id=request_id, session_id=session_id, message_id=message_id
    )
    if str(message.role) != MessageRole.assistant.value:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.validation_error,
            message="Only answers can be rated.",
        )
    now = datetime.now(UTC)
    # Filtered by user as well as answer. Today only an answer's owner can reach
    # this line (require_session above), so the user filter changes nothing a
    # test can observe; it is kept for shareable answers (ADR-0005 Phase 6), which
    # will put other people in front of an answer, and it matches the table's
    # one-vote-per-(answer, user) constraint.
    row = await _find_vote(db, message_id, principal_id)
    if row is None:
        db.add(
            AnswerFeedback(
                message_id=message_id,
                user_id=principal_id,
                rating=body.rating,
                reason=body.reason,
                comment=body.comment,
                created_at=now,
                updated_at=now,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent vote (a double click) inserted the row between our
            # lookup and our insert: on Postgres READ COMMITTED both see "none".
            # Update the winner's row rather than answering 500.
            await db.rollback()
            row = await _find_vote(db, message_id, principal_id)
            # The unique constraint that just refused our insert says it exists.
            assert row is not None, "IntegrityError on insert, yet no vote row"
    if row is not None:
        row.rating = body.rating
        row.reason = body.reason
        row.comment = body.comment
        row.updated_at = now
        await db.commit()
    return {
        "request_id": request_id,
        "message_id": str(message_id),
        "rating": body.rating,
        "reason": body.reason,
    }


@router.post(
    "/v1/source-requests",
    dependencies=[Depends(requires(Capability.feedback))],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for a source the answer could not cite (the gap log).",
)
async def post_source_request(
    request: Request,
    body: Annotated[SourceRequestBody, Body()],
    db: Annotated[AsyncSession, Depends(get_session)],
    principal_id: Annotated[str, Depends(resolve_principal)],
) -> dict[str, Any]:
    request_id = _request_id(request)
    if body.message_id is not None:
        # The linked message must be in one of the caller's own live sessions.
        owned = (
            await db.execute(
                select(Message.message_id)
                .join(Session, Session.session_id == Message.session_id)
                .where(
                    Message.message_id == body.message_id,
                    Session.user_id == principal_id,
                    Session.expires_at > datetime.now(UTC),
                )
            )
        ).scalar_one_or_none()
        if owned is None:
            raise error_response(
                request_id=request_id,
                code=APIErrorCode.not_found,
                message=f"Message {body.message_id} not found.",
            )
    row = SourceRequest(
        user_id=principal_id,
        message_id=body.message_id,
        question=body.question,
        requested_url=body.requested_url,
        note=body.note,
        status="open",
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    return {
        "request_id": request_id,
        "source_request_id": str(row.request_id),
        "status": "received",
    }


__all__ = ["FeedbackReason", "router"]
