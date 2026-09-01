"""``GET /v1/me/sessions`` — the caller's own session history (ADR-0004 PR 10).

Backs the history drawer: "sign in to keep your chat history across
visits" only means something if there is a way to list it back. Lives in
its own module (not ``sessions.py``) because the URL is ``/v1/me/...``, a
different prefix, and because "my own sessions" is a distinct concern from
"a specific session by id" — the former needs no ``session_id`` path
parameter and no ownership-mismatch 404 (it can only ever return the
caller's own rows by construction).

Same principal resolution as every other session/message route
(:func:`app.core.auth_sessions.resolve_principal`) — this works for an
anonymous visitor too, listing whatever sessions exist under their
current browser cookie. Anonymous history is real but ephemeral (it dies
with the cookie); the honest-copy work in PR 7 is what tells a visitor
that signing in is what makes it durable, not this endpoint refusing
anonymous callers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_sessions import resolve_principal
from app.core.db import get_session
from app.models import Message, Session

router = APIRouter(prefix="/v1/me", tags=["me"])

# A history drawer is a UI affordance, not a data export -- bounded the
# same way the rest of the app bounds list endpoints (AGENTS.md: no
# unbounded queries).
_MAX_SESSIONS = 50


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


@router.get(
    "/sessions",
    summary="List the caller's own chat sessions.",
    description=(
        "Newest first, capped at 50. Backs the history drawer (ADR-0004 "
        "PR 10). Works for an anonymous visitor too (whatever sessions "
        "exist under their current cookie) -- it is signing in that makes "
        "the history durable across visits, not this endpoint."
    ),
)
async def list_my_sessions(
    request: Request,
    principal_id: Annotated[str, Depends(resolve_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    stmt = (
        select(Session)
        .where(Session.user_id == principal_id, Session.expires_at > datetime.now(UTC))
        .order_by(Session.created_at.desc())
        .limit(_MAX_SESSIONS)
    )
    sessions = list((await db.execute(stmt)).scalars().all())

    session_ids = [row.session_id for row in sessions]
    counts: dict[Any, int] = {}
    if session_ids:
        count_stmt = (
            select(Message.session_id, func.count(Message.message_id))
            .where(Message.session_id.in_(session_ids))
            .group_by(Message.session_id)
        )
        counts = {sid: count for sid, count in (await db.execute(count_stmt)).all()}

    return {
        "request_id": _request_id(request),
        "sessions": [
            {
                "session_id": str(row.session_id),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "current_product_area": row.current_product_area,
                "message_count": counts.get(row.session_id, 0),
            }
            for row in sessions
        ],
    }


__all__ = ["router"]
