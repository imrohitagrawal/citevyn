"""``GET /v1/me/usage`` — your own use against your allowance (ADR-0005 §4,
Phase 6D, "usage insights").

Pro (capability ``usage_insights``), and 404 unless
``CITEVYN_ACCESS_MODEL_ENABLED`` is on. Returns the allowance position (the same
numbers as the usage meter) and one row per UTC day of the current month, split
into chat and MCP. The days count exactly the rows the allowance counts
(``app.access.quota.counted_rows``), so they always add up to ``usage.used``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import requires, tier_for_principal
from app.access.policy import Capability
from app.access.quota import daily_usage, usage_for
from app.core.auth_sessions import resolve_principal
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response

router = APIRouter(tags=["usage"])


@router.get("/v1/me/usage", dependencies=[Depends(requires(Capability.usage_insights))])
async def my_usage(
    request: Request,
    principal_id: Annotated[str, Depends(resolve_principal)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    request_id = str(request.state.request_id)
    if not settings.access_model_enabled:
        raise error_response(
            request_id=request_id, code=APIErrorCode.not_found, message="Not found."
        )
    now = datetime.now(UTC)
    tier = await tier_for_principal(db, principal_id, now)
    usage = await usage_for(db, principal_id, tier, settings, now)
    start, days = await daily_usage(db, principal_id, now)
    chat = sum(d["chat"] for d in days)
    mcp = sum(d["mcp"] for d in days)
    return {
        "request_id": request_id,
        "usage": usage.as_dict(),
        "period_start": start.isoformat(),
        "days": days,
        "totals": {"chat": chat, "mcp": mcp, "total": chat + mcp},
    }


__all__ = ["router"]
