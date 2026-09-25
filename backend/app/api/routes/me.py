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

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import requires
from app.access.policy import Capability
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
    dependencies=[Depends(requires(Capability.history))],
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


# ---------------------------------------------------------------------------
# GET /v1/me/export — history export (ADR-0005 §6 trust must-haves)
# ---------------------------------------------------------------------------

# Bounded like every list here. Far above the drawer's 50: an export is meant to
# be the whole history, and the file says when it hit this cap.
_MAX_EXPORT_SESSIONS = 500


def _markdown(sessions: list[dict[str, Any]], truncated: bool) -> str:
    lines = ["# CiteVyn conversation history", ""]
    if truncated:
        n = len(sessions)
        lines += [
            f"_This export holds only the newest {n} conversation{'s' if n != 1 else ''}._",
            "",
        ]
    if not sessions:
        lines.append("No conversations to export.")
    for convo in sessions:
        lines += [f"## Conversation of {convo['created_at'] or 'unknown date'}", ""]
        for m in convo["messages"]:
            who = "You" if m["role"] == "user" else "CiteVyn"
            # Quoted line by line, so a message containing "## ..." or a fake
            # "**CiteVyn:**" line cannot forge structure in the document.
            quoted = "\n".join(f"> {ln}" for ln in str(m["content"]).split("\n"))
            lines += [f"**{who}:**", "", quoted, ""]
            if m["citations"]:
                lines.append("Sources:")
                for c in m["citations"]:
                    date = f" (docs as of {c['docs_as_of']})" if c.get("docs_as_of") else ""
                    title = str(c.get("title") or c.get("source_name") or "Source")
                    lines.append(f"- [{title}]({c.get('url', '')}){date}")
                lines.append("")
    return "\n".join(lines) + "\n"


@router.get(
    "/export",
    dependencies=[Depends(requires(Capability.history))],
    summary="Download the caller's own conversation history.",
    description=(
        "Every conversation the history drawer can show (the caller's own, not "
        "closed), newest first, with each answer's citations and 'docs as of' "
        "dates. JSON or Markdown, as a file download."
    ),
)
async def export_my_history(
    principal_id: Annotated[str, Depends(resolve_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    fmt: Literal["json", "markdown"] = Query(default="json", alias="format"),  # noqa: B008
) -> Response:
    now = datetime.now(UTC)
    rows = list(
        (
            await db.execute(
                select(Session)
                .where(Session.user_id == principal_id, Session.expires_at > now)
                .order_by(Session.created_at.desc(), Session.session_id.desc())
                .limit(_MAX_EXPORT_SESSIONS + 1)
            )
        )
        .scalars()
        .all()
    )
    truncated = len(rows) > _MAX_EXPORT_SESSIONS
    rows = rows[:_MAX_EXPORT_SESSIONS]
    by_session: dict[Any, list[Message]] = {row.session_id: [] for row in rows}
    if rows:
        messages = (
            await db.execute(
                select(Message)
                .where(Message.session_id.in_(list(by_session)))
                .order_by(Message.created_at.asc(), Message.message_id.asc())
            )
        ).scalars()
        for m in messages:
            by_session[m.session_id].append(m)
    sessions = [
        {
            "session_id": str(row.session_id),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "messages": [
                {
                    "role": "user" if str(m.role) == "user" else "assistant",
                    "content": m.content,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "citations": m.citations or [],
                }
                for m in by_session[row.session_id]
            ],
        }
        for row in rows
    ]
    stamp = now.strftime("%Y-%m-%d")
    if fmt == "markdown":
        return Response(
            content=_markdown(sessions, truncated),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="citevyn-history-{stamp}.md"',
                "Cache-Control": "no-store",  # personal data
            },
        )
    return Response(
        content=json.dumps(
            {"exported_at": now.isoformat(), "truncated": truncated, "sessions": sessions},
            ensure_ascii=False,
            indent=2,
        ),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="citevyn-history-{stamp}.json"',
            "Cache-Control": "no-store",  # personal data
        },
    )
