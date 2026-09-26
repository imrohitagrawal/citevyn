"""``/v1/me/api-keys`` — make, list and revoke your API keys (ADR-0005 Phase 6A).

Making a key is Pro (capability ``api_keys``); listing and revoking need only a
signed-in account (``manage_account``), so a lapsed Pro account can still kill
its keys. All three are 404 unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on.

A key is NOT revoked by signing out everywhere or by a password change: revoke
it here. ``authenticate_api_key`` checks only the key; a route that accepts keys
(the MCP server) must check the owner's plan on every call.

A key is shown ONCE, at creation; lists show only its first characters.
Revoking keeps the row (with ``revoked_at``) so the audit trail survives. At most
:data:`MAX_LIVE_KEYS` live keys per account.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.api_keys import DISPLAY_CHARS, PREFIX, make_key
from app.access.deps import requires
from app.access.policy import Capability
from app.core.auth_sessions import resolve_principal
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.models import ApiKey, User

router = APIRouter(prefix="/v1/me/api-keys", tags=["api-keys"])

MAX_LIVE_KEYS = 10


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def keys_enabled(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """404 unless the access model is on: 'off' must look like 'not there'."""
    if not settings.access_model_enabled:
        raise error_response(
            request_id=_request_id(request), code=APIErrorCode.not_found, message="Not found."
        )


class NewKeyRequest(BaseModel):
    name: str = Field(max_length=200)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 64:
            raise ValueError("a key name is 1 to 64 characters")
        # No control or format characters: a right-to-left override or a
        # zero-width character can make one name look like another in a list.
        if any(unicodedata.category(c) in ("Cc", "Cf") for c in value):
            raise ValueError("a key name cannot contain control or formatting characters")
        return value


def _view(row: ApiKey) -> dict[str, Any]:
    def iso(v: datetime | None) -> str | None:
        return v.isoformat() if v else None

    return {
        "key_id": row.key_id,
        "name": row.name,
        "prefix": (PREFIX + row.key_id)[:DISPLAY_CHARS],
        "scope": row.scope,
        "created_at": iso(row.created_at),
        "last_used_at": iso(row.last_used_at),
    }


@router.post(
    "",
    dependencies=[Depends(requires(Capability.api_keys))],
    status_code=status.HTTP_201_CREATED,
)
async def create_key(
    request: Request,
    body: Annotated[NewKeyRequest, Body()],
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(keys_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    # Serialise key creation per account (a row lock on Postgres), so a burst of
    # requests cannot count 9 at once and all insert (found in review).
    await db.execute(select(User).where(User.user_id == principal_id).with_for_update())
    live = (
        await db.execute(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.user_id == principal_id, ApiKey.revoked_at.is_(None))
        )
    ).scalar_one()
    if live >= MAX_LIVE_KEYS:
        raise error_response(
            request_id=_request_id(request),
            code=APIErrorCode.too_many_api_keys,
            message=f"You can have at most {MAX_LIVE_KEYS} keys. Revoke one first.",
        )
    new = make_key(principal_id, body.name, datetime.now(UTC))
    db.add(new.row)
    await db.commit()
    # The ONLY time the full key leaves the server.
    return {"request_id": _request_id(request), **_view(new.row), "key": new.raw}


# List and revoke need only a signed-in account, NOT Pro: after Pro lapses the
# keys still exist, and their owner must always be able to see and kill them
# (found in review: "revocable" failed for lapsed accounts).
@router.get("", dependencies=[Depends(requires(Capability.manage_account))])
async def list_keys(
    request: Request,
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(keys_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(ApiKey)
            .where(ApiKey.user_id == principal_id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at)
        )
    ).scalars()
    return {"request_id": _request_id(request), "keys": [_view(r) for r in rows]}


@router.delete(
    "/{key_id}",
    dependencies=[Depends(requires(Capability.manage_account))],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_key(
    request: Request,
    key_id: str,
    principal_id: Annotated[str, Depends(resolve_principal)],
    _on: Annotated[None, Depends(keys_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    row = await db.get(ApiKey, key_id)
    # Someone else's key, or one already revoked, is "not found": never confirm
    # that a key id exists for another account.
    if row is None or row.user_id != principal_id or row.revoked_at is not None:
        raise error_response(
            request_id=_request_id(request), code=APIErrorCode.not_found, message="Not found."
        )
    row.revoked_at = datetime.now(UTC)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["MAX_LIVE_KEYS", "router"]
