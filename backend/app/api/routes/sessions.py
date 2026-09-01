"""Session HTTP routes (Slice 7).

Implements the three public endpoints defined in
``docs/API_SPEC.md`` §4–§5:

* ``POST /v1/sessions`` — create a new chat session for the
  authenticated user.
* ``GET /v1/sessions/{session_id}`` — fetch a session with its
  messages (citation hydration).
* ``DELETE /v1/sessions/{session_id}`` — close the session by setting
  ``expires_at`` to the current timestamp (the schema has no separate
  ``closed`` column).

The answer endpoint lives in :mod:`app.api.routes.messages` because
the URL nests under a session; sharing one module would couple the
two concerns unnecessarily.

All three endpoints require a valid bearer token via
:func:`app.core.security.require_demo_api_key`. Auth failures raise the
standard envelope from :func:`app.core.errors.error_response`, which the
route does not need to intercept.

Ownership is checked against a separate, per-visitor **principal**
(:func:`app.core.auth_sessions.resolve_principal`, ADR-0004 PR 3) resolved
from a cookie, not the bearer token — the bearer proves "legitimate demo
client", the cookie proves "same visitor as last time". A caller with no
cookie yet gets one minted transparently on its first request.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_sessions import resolve_principal
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.models import Message, Session

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


def _request_id(request: Request) -> str:
    """Return the request id stamped on :class:`Request` by the middleware."""
    return str(request.state.request_id)


def _now() -> datetime:
    """Return the current UTC datetime as a tz-aware value."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    """Body for ``POST /v1/sessions`` (per ``docs/API_SPEC.md`` §4).

    The MVP pins every session to the authenticated ``demo_user``; the
    ``user_id`` and ``channel`` fields are accepted to keep the wire
    shape stable for the V1 multi-tenant work but only ``channel`` is
    acted on. ``user_id`` is ignored so a misconfigured client cannot
    impersonate another caller.
    """

    user_id: str | None = Field(default=None)
    channel: str = Field(default="chat")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _message_payload(message: Message) -> dict[str, Any]:
    """Project a :class:`app.models.Message` row into the response shape."""
    # ``Message.role`` is stored as a ``String(32)``; SQLAlchemy
    # returns the raw string on read so we coerce defensively.
    role = message.role
    role_value = role.value if hasattr(role, "value") else str(role)
    return {
        "message_id": str(message.message_id),
        "role": role_value,
        "content": message.content,
        "normalized_query": message.normalized_query,
        "domain": message.domain,
        "intent": message.intent,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        # ADR-0004 PR 10: the exact citations shown for this message,
        # persisted at write time (migration 0009). [] for a user message
        # or any assistant reply with none, never reconstructed from
        # retrieved_evidence -- see the migration docstring for why.
        "citations": message.citations or [],
    }


def _session_payload(session: Session, *, messages: list[Message] | None = None) -> dict[str, Any]:
    """Project a :class:`Session` row (and optional messages) into the API shape."""
    return {
        "session_id": str(session.session_id),
        "user_id": session.user_id,
        "channel": session.channel,
        "summary": session.summary,
        "current_product_area": session.current_product_area,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        "messages": [_message_payload(m) for m in (messages or [])],
    }


async def _get_session_or_404(
    session: AsyncSession,
    *,
    request_id: str,
    session_id: uuid.UUID,
    user_id: str,
) -> Session:
    """Load a session row owned by ``user_id`` or raise the standard 404 envelope.

    Ownership and expiry are both folded into one predicate. A session that
    exists but belongs to a different principal, or that has already expired
    (including a soft-closed session, whose ``DELETE`` sets ``expires_at`` to
    now), returns the SAME 404 as a genuine miss — never a 403, which would
    confirm the id is real and turn this into a membership oracle over the
    UUID space. Behaviourally a no-op today (every caller resolves to the one
    constant ``demo_user`` principal); load-bearing the moment a second
    principal exists (see ``docs/ADR/0004-user-accounts.md`` PR 1).
    """
    stmt = select(Session).where(
        Session.session_id == session_id,
        Session.user_id == user_id,
        Session.expires_at > _now(),
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.not_found,
            message=f"Session {session_id} not found.",
        )
    return row


# ---------------------------------------------------------------------------
# POST /v1/sessions
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a chat session.",
    description=(
        "Per ``docs/API_SPEC.md`` §4. Creates a fresh chat session for "
        "the authenticated user and returns its id and expiration."
    ),
    response_description="The newly-created session metadata.",
)
async def create_session(
    request: Request,
    response: Response,
    principal_id: Annotated[str, Depends(resolve_principal)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    body: CreateSessionRequest,
) -> dict[str, Any]:
    """Create a session row owned by the caller's resolved principal.

    ``principal_id`` (ADR-0004 PR 3) is either the caller's existing cookie
    identity or a freshly minted anonymous one — ``resolve_principal``
    already persisted the owning ``User`` row in either case, so no FK
    upsert is needed here. ``body.user_id`` is accepted for spec
    compliance but ignored, so a misconfigured client cannot impersonate
    another caller. Only the ``chat`` channel is supported.
    """
    request_id = _request_id(request)
    if body.channel != "chat":
        # The MVP only supports the chat channel. Reject everything else
        # so an unconfigured client cannot accidentally create a row
        # with an unsupported channel value.
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.validation_error,
            message="Only the 'chat' channel is supported in MVP.",
        )

    expires_at = _now() + timedelta(seconds=settings.index_session_ttl_seconds)
    new_session = Session(
        session_id=uuid.uuid4(),
        user_id=principal_id,
        channel=body.channel,
        summary=None,
        current_product_area=None,
        created_at=_now(),
        expires_at=expires_at,
    )
    db.add(new_session)
    await db.commit()

    response.headers["Location"] = f"/v1/sessions/{new_session.session_id}"
    return {
        "request_id": request_id,
        "session_id": str(new_session.session_id),
        "expires_at": expires_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# DELETE /v1/sessions/{session_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Close (soft-delete) a session.",
    description=(
        "Closes the session by setting ``expires_at`` to the current "
        "timestamp. The schema has no ``closed`` column; per Slice 7 "
        "scope, a closed session is simply one whose ``expires_at`` "
        "is in the past."
    ),
    response_description="No content; the session is closed.",
)
async def close_session(
    request: Request,
    session_id: Annotated[uuid.UUID, Path()],
    principal_id: Annotated[str, Depends(resolve_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Close a session by setting its ``expires_at`` to now."""
    row = await _get_session_or_404(
        db, request_id=_request_id(request), session_id=session_id, user_id=principal_id
    )
    row.expires_at = _now()
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# GET /v1/sessions/{session_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{session_id}",
    summary="Fetch a session and its messages.",
    description=(
        "Per ``docs/API_SPEC.md`` §5. Returns the session metadata "
        "plus the ordered list of messages. The route fires a single "
        "ordered SELECT after the session row is found."
    ),
    response_description="Session metadata and the ordered message list.",
)
async def get_session_route(
    request: Request,
    session_id: Annotated[uuid.UUID, Path()],
    principal_id: Annotated[str, Depends(resolve_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Return the session metadata plus the ordered messages list."""
    request_id = _request_id(request)
    row = await _get_session_or_404(
        db, request_id=request_id, session_id=session_id, user_id=principal_id
    )

    stmt = (
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc(), Message.message_id.asc())
    )
    messages = list((await db.execute(stmt)).scalars().all())

    return {
        "request_id": request_id,
        **_session_payload(row, messages=messages),
    }


__all__ = ["router"]
