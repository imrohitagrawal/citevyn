"""Auth HTTP routes: register, login, logout, me (ADR-0004 PR 6).

The first PR in the ADR-0004 sequence that can mint a REAL second
principal — every earlier PR ran with exactly one anonymous visitor at a
time, which is why the ownership predicate (PR 1) and the per-visitor
cookie (PR 3) were behaviourally inert until now. From here on, the
IDOR the ADR exists to prevent is a live scenario, not a hypothetical one.

All four routes sit behind :func:`app.core.rate_limit.rate_limited_demo`
like every other public route — the demo bearer stays required (it
doubles as a CSRF guard per the ADR: a cross-site form POST cannot set an
``Authorization`` header) and per-visitor IP volume limiting still
applies. ``register``/``login`` additionally go through
:func:`app.core.rate_limit.enforce_auth_login_rate_limit`, a second,
much stricter limiter keyed on the TARGET EMAIL rather than the client —
the IP-keyed limit alone would let a distributed attacker spread one
password guess across many source addresses against a single account.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_sessions import claim_and_login, revoke_current_session, try_resolve_principal
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.core.passwords import hash_password, verify_password_or_dummy
from app.core.rate_limit import enforce_auth_login_rate_limit, rate_limited_demo
from app.models import AuditAction, User, UserRole
from app.services.audit import record_audit_event

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Deliberately permissive (not RFC 5322): this only screens obviously
# malformed input before it reaches the DB / hasher. There is no email
# provider in v1 (ADR-0004), so nothing downstream depends on stricter
# validation, and a false rejection here is worse than a false acceptance.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_PASSWORD_MIN_LENGTH = 8
_PASSWORD_MAX_LENGTH = 128


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=_PASSWORD_MIN_LENGTH, max_length=_PASSWORD_MAX_LENGTH)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX_LENGTH)


def _normalize_email(request_id: str, raw: str) -> str:
    """Lowercase/strip ``raw`` and reject anything that doesn't look like an email."""
    email = raw.strip().lower()
    if not _EMAIL_RE.match(email):
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.validation_error,
            message="Enter a valid email address.",
        )
    return email


def _auth_user_payload(request_id: str, user: User) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "user_id": user.user_id,
        "email": user.email,
        "anonymous": user.email is None,
    }


# ---------------------------------------------------------------------------
# POST /v1/auth/register
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account.",
    description=(
        "Creates a registered account and logs the caller in, claiming any "
        "chat history from their prior anonymous session (ADR-0004 PR 6)."
    ),
)
async def register(
    request: Request,
    response: Response,
    body: RegisterRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _demo_user_id: Annotated[str, Depends(rate_limited_demo)],
) -> dict[str, Any]:
    """Register + log in, in one step.

    Registration LEAKS email existence (a 422 below) — deliberate, recorded
    in ``docs/ADR/0004-user-accounts.md``: an always-202 response needs an
    email provider this project does not have. The credential-stuffing
    limiter is checked before the existence lookup so a registration-spam
    loop against one address is bounded regardless of which branch it hits.
    """
    request_id = _request_id(request)
    email = _normalize_email(request_id, body.email)
    await enforce_auth_login_rate_limit(email, settings)

    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.validation_error,
            message="This email is already registered.",
        )

    password_hash = await hash_password(body.password)
    new_user = User(
        user_id=f"usr_{uuid.uuid4().hex}",
        role=UserRole.demo_user,
        created_at=datetime.now(UTC),
        email=email,
        password_hash=password_hash,
    )
    db.add(new_user)
    try:
        await db.flush()  # claim_and_login's UPDATE needs this FK target to exist
    except IntegrityError as exc:
        # Two concurrent registrations for the same email both pass the
        # existence check above before either commits — the loser hits
        # ix_users_email here instead. Surface the SAME 422 the sequential
        # case returns rather than letting it fall through to a generic 500
        # (found by adversarial review of this PR).
        await db.rollback()
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.validation_error,
            message="This email is already registered.",
        ) from exc

    await claim_and_login(request, response, db, settings, user_id=new_user.user_id)
    await record_audit_event(
        db,
        action=AuditAction.login,
        user_id=new_user.user_id,
        role=UserRole.demo_user,
        metadata={"event": "register"},
    )
    await db.commit()
    return _auth_user_payload(request_id, new_user)


# ---------------------------------------------------------------------------
# POST /v1/auth/login
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    summary="Log in to an existing account.",
    description=(
        "Verifies email + password and logs the caller in, claiming any "
        "chat history from their prior anonymous session (ADR-0004 PR 6)."
    ),
)
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _demo_user_id: Annotated[str, Depends(rate_limited_demo)],
) -> dict[str, Any]:
    """Verify credentials and log in.

    Uses ``verify_password_or_dummy`` (ADR-0004 PR 4), never
    ``verify_password`` directly: an unknown email must burn the same CPU
    time as a real verification, or response latency is a free
    account-enumeration oracle. The credential-stuffing limiter runs before
    the lookup, so it bounds the dummy-hash path too.
    """
    request_id = _request_id(request)
    email = _normalize_email(request_id, body.email)
    await enforce_auth_login_rate_limit(email, settings)

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    password_hash = user.password_hash if user is not None else None
    ok = await verify_password_or_dummy(body.password, password_hash)
    if not ok or user is None:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.auth_required,
            message="Invalid email or password.",
        )

    await claim_and_login(request, response, db, settings, user_id=user.user_id)
    await record_audit_event(
        db,
        action=AuditAction.login,
        user_id=user.user_id,
        role=UserRole.demo_user,
        metadata={"event": "login"},
    )
    await db.commit()
    return _auth_user_payload(request_id, user)


# ---------------------------------------------------------------------------
# POST /v1/auth/logout
# ---------------------------------------------------------------------------


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out.",
    description="Revokes the current session cookie. Idempotent.",
)
async def logout(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _demo_user_id: Annotated[str, Depends(rate_limited_demo)],
) -> Response:
    principal_id = await revoke_current_session(request, response, db, settings)
    if principal_id is not None:
        await record_audit_event(
            db,
            action=AuditAction.login,
            user_id=principal_id,
            role=UserRole.demo_user,
            metadata={"event": "logout"},
        )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# GET /v1/auth/me
# ---------------------------------------------------------------------------


@router.get(
    "/me",
    summary="Return the caller's identity.",
    description=(
        "Resolves the current session cookie WITHOUT minting a fresh one — "
        "unlike the session/message routes' resolver, a stale or missing "
        "cookie here is a 401, not a silent new anonymous identity. This is "
        "also why a login/register rotation makes the OLD cookie value "
        "401 immediately: its AuthSession row was deleted, not superseded."
    ),
)
async def me(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _demo_user_id: Annotated[str, Depends(rate_limited_demo)],
) -> dict[str, Any]:
    request_id = _request_id(request)
    principal_id = await try_resolve_principal(request, db, settings)
    if principal_id is None:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.auth_required,
            message="Not authenticated.",
        )
    user = await db.get(User, principal_id)
    assert user is not None, "a live AuthSession must reference an existing user (FK CASCADE)"
    return _auth_user_payload(request_id, user)


__all__ = ["router"]
