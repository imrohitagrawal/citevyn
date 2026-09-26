"""``POST /v1/mcp`` — the CiteVyn MCP server (ADR-0005 §4, Phase 6B).

What the free vendor MCP servers do not give: a FINISHED, cited answer, drawn
only from the official docs, rather than raw pages for the agent to read.

A small JSON-RPC 2.0 endpoint in MCP's streamable-HTTP shape, answering with
JSON (no server-sent stream, so ``GET`` is not served). Written without an MCP
library: five methods over one POST did not justify a new dependency.

* ``initialize``, ``ping``, ``tools/list``; notifications get ``202`` and no body.
* ``tools/call`` of ``ask_docs {question}``: the same orchestrator as chat.

Rules:

* **Authentication:** a per-user API key only (``require_api_key``). A browser
  cookie is not accepted, so another site cannot drive this with a visitor's
  session.
* **Plan and allowance on every call:** the key owner's tier is re-read each
  time (a key made while Pro must stop answering when Pro lapses), then the
  same allowance as chat; an MCP answer is recorded with ``channel="mcp"``. The
  account's hourly limit applies too. Plan, allowance and rate-limit refusals
  are tool results with ``isError: true`` (the agent reads them), not HTTP errors.
* **Clean output:** ``app.mcp.clean`` (see its docstring for what it does and
  what it cannot do).
* 404 while the access model is off.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import enforced_in_handler, tier_for_principal
from app.access.policy import Capability, has_capability
from app.access.quota import is_counted_answer, refuse_if_used_up, usage_for, usage_row
from app.answer.orchestrator import Orchestrator
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.rate_limit import enforce_global_rate_limit, enforce_rate_limit
from app.core.security import require_api_key
from app.cost.call_site import billed_to
from app.mcp.clean import clean_answer, clean_field, clean_url
from app.models import ApiKey, Session

router = APIRouter(tags=["mcp"])

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
# One JSON-RPC request is small; anything larger is refused before it is read.
MAX_BODY_BYTES = 64 * 1024
MAX_QUESTION_CHARS = 4000

ASK_DOCS = {
    "name": "ask_docs",
    "description": (
        "Ask a question about the Claude API, Claude Code, OpenAI Codex or the Gemini "
        "API. Returns a finished answer drawn only from the official documentation, "
        "with the pages it cites. Refuses rather than guesses when the docs do not "
        "support an answer."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question, in plain language."}
        },
        "required": ["question"],
    },
}


def _reject_constant(name: str) -> object:
    """NaN and Infinity are not JSON; echoing one back as an id crashed the
    response writer (found in review)."""
    raise ValueError(name)


def _obj(value: object) -> dict[str, Any]:
    """``value`` if it is a JSON object, else ``{}``: request bodies are untrusted shapes."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _result(rid: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _tool_error(rid: Any, message: str) -> JSONResponse:
    return _result(rid, {"content": [{"type": "text", "text": message}], "isError": True})


@router.post("/v1/mcp", dependencies=[Depends(enforced_in_handler(Capability.mcp_ask))])
async def mcp(
    request: Request,
    key: Annotated[ApiKey, Depends(require_api_key)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return _error(None, -32600, "Request too large.")  # refused unread
    # Read in pieces and stop at the cap: a chunked body has no Content-Length,
    # and reading it whole first would hold it all in memory.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return _error(None, -32600, "Request too large.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        parsed: object = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return _error(None, -32700, "Parse error.")
    message = _obj(parsed)
    method_value: object = message.get("method")
    if not isinstance(parsed, dict) or not isinstance(method_value, str):
        return _error(None, -32600, "Invalid request: send one JSON-RPC 2.0 request object.")
    id_value: object = message.get("id")
    if "id" in message and not (
        id_value is None or isinstance(id_value, str) or type(id_value) is int
    ):
        return _error(None, -32600, "Invalid request: an id is a string, an integer or null.")
    method = method_value
    if "id" not in message:  # a notification: acknowledge, never answer
        return Response(status_code=202)
    rid: object = message["id"]
    params = _obj(message.get("params"))
    if method == "initialize":
        asked: object = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(
            rid,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "citevyn", "version": "1"},
            },
        )
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": [ASK_DOCS]})
    if method != "tools/call":
        return _error(rid, -32601, f"Method not found: {method[:64]}")
    if params.get("name") != "ask_docs":
        return _error(rid, -32602, "Unknown tool. The only tool is ask_docs.")
    question: object = _obj(params.get("arguments")).get("question")
    if not isinstance(question, str) or not question.strip():
        return _error(rid, -32602, "ask_docs needs a non-empty 'question' string.")
    if len(question) > MAX_QUESTION_CHARS:
        return _error(rid, -32602, f"A question can be at most {MAX_QUESTION_CHARS} characters.")
    return await _ask_docs(rid, question, key, settings, db, str(request.state.request_id))


async def _ask_docs(
    rid: Any, question: str, key: ApiKey, settings: Settings, db: AsyncSession, request_id: str
) -> Response:
    now = datetime.now(UTC)
    # The plan is re-read on every call: the key proves identity, not the plan.
    tier = await tier_for_principal(db, key.user_id, now)
    if not has_capability(tier, Capability.mcp_ask):
        return _tool_error(
            rid, "The CiteVyn MCP server needs a Pro plan. Upgrade to Pro on the CiteVyn site."
        )
    usage = await usage_for(db, key.user_id, tier, settings, now)
    try:
        refuse_if_used_up(usage, tier, request_id)
        await enforce_rate_limit(
            user_id=key.user_id, role="demo_user_registered", settings=settings
        )
        # The site-wide backstop chat applies too (found in review: MCP skipped it).
        await enforce_global_rate_limit(settings)
    except HTTPException:
        if usage.remaining == 0:
            return _tool_error(
                rid, "You have used this month's questions. They come back on the 1st."
            )
        return _tool_error(rid, "Too many questions too quickly. Try again in a few minutes.")
    session = Session(
        session_id=uuid.uuid4(),
        user_id=key.user_id,
        channel="mcp",
        created_at=now,
        expires_at=now + timedelta(seconds=settings.index_session_ttl_seconds),
    )
    db.add(session)
    await db.flush()
    with billed_to(key.user_id):
        response = await Orchestrator(settings, db).ask(
            question=question, request_id=request_id, session_id=session.session_id
        )
    answered = is_counted_answer(response)
    if answered:
        db.add(usage_row(key.user_id, tier, response, request_id, datetime.now(UTC), channel="mcp"))
    await db.commit()
    citations: list[dict[str, Any]] = []
    raw_citations: object = response.get("citations")
    for item in cast(list[object], raw_citations) if isinstance(raw_citations, list) else []:
        c = _obj(item)
        url = clean_url(str(c.get("url") or ""))
        if url is None:
            continue
        citations.append(
            {
                "marker": c.get("marker"),
                "title": clean_field(str(c.get("title") or "")),
                "url": url,
                "source": clean_field(str(c.get("source_name") or ""), 80),
            }
        )
    sources = (
        "Sources:\n" + "\n".join(f"[{c['marker']}] {c['title']} — {c['url']}" for c in citations)
        if citations
        else ""
    )
    # A per-request marker frames the answer AND its sources (found in review:
    # sources outside the frame could read as trusted server text).
    text = clean_answer(
        str(response.get("answer") or ""), nonce=secrets.token_hex(6), appendix=sources
    )
    return _result(
        rid,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {
                "answered": answered,
                "answer": text,
                "citations": citations,
                "docs_as_of": response.get("docs_as_of"),
            },
            "isError": False,
        },
    )


__all__ = ["router"]
