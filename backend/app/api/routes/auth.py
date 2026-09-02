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

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_sessions import (
    claim_and_login,
    revoke_current_session,
    revoke_other_sessions,
    step_up_active,
    try_resolve_auth_session,
)
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.core.passwords import hash_password, verify_password, verify_password_or_dummy
from app.core.rate_limit import (
    email_notice_allowed,
    enforce_auth_login_rate_limit,
    enforce_password_change_rate_limit,
    rate_limited_demo,
)
from app.models import AuditAction, AuthSession, MagicLinkToken, User, UserIdentity, UserRole
from app.services.audit import record_audit_event
from app.services.notifications import (
    build_email_client,
    deliver,
    password_changed_message,
    site_base_url,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

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


class PasswordUpdateRequest(BaseModel):
    """Body for ``POST /v1/auth/me/password`` (ADR-0004 PR 14).

    ``current_password`` is optional AT THE SCHEMA LEVEL only. Whether it is
    REQUIRED is decided by the route from the server-loaded account -- see
    :func:`update_password` -- never from whether this field was sent.
    """

    current_password: str | None = Field(default=None, max_length=_PASSWORD_MAX_LENGTH)
    new_password: str = Field(min_length=_PASSWORD_MIN_LENGTH, max_length=_PASSWORD_MAX_LENGTH)


def _looks_like_email(value: str) -> bool:
    """Deliberately permissive (not RFC 5322), and deliberately NOT a regex.

    A CodeQL scan flagged the original ``^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$`` as a
    polynomial-time regex on attacker-controlled input (the adjacent
    ``[^@\\s]+`` character classes both admit ``.``, so the engine can try
    many ways to split the string around the literal ``\\.``). Plain string
    ops below are O(n) with no backtracking, and check exactly the same
    three properties: one ``@``, a non-empty local part, and a ``.`` inside
    the domain part. There is no email provider in v1 (ADR-0004), so
    nothing downstream depends on stricter validation, and a false
    rejection here is worse than a false acceptance.
    """
    if any(c.isspace() for c in value):
        return False
    local, sep, domain = value.partition("@")
    if not sep or not local or not domain or "@" in domain:
        return False
    return "." in domain and not domain.startswith(".") and not domain.endswith(".")


def normalize_email(request_id: str, raw: str) -> str:
    """Lowercase/strip ``raw`` and reject anything that doesn't look like an email."""
    email = raw.strip().lower()
    if not _looks_like_email(email):
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.validation_error,
            message="Enter a valid email address.",
        )
    return email


async def _linked_providers(db: AsyncSession, user_id: str) -> list[str]:
    """Provider names (``"github"``/``"google"``) linked to ``user_id``, sorted, distinct.

    ADR-0004 PR 13: one cheap FK-indexed query, only ever run once a real
    principal has been resolved. Sorted so the wire shape is deterministic;
    DISTINCT because nothing stops one account from linking two different
    GitHub accounts (the unique constraint is on the external identity, not
    on ``(user_id, provider)``) and the wire field is a set of providers.
    """
    rows = (
        await db.execute(
            select(UserIdentity.provider)
            .where(UserIdentity.user_id == user_id)
            .distinct()
            .order_by(UserIdentity.provider)
        )
    ).scalars()
    return list(rows)


async def _auth_user_payload(
    db: AsyncSession,
    request_id: str,
    user: User,
    *,
    session: AuthSession | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """The one body shape ``register``, ``login`` and ``me`` all return.

    Computes ``providers`` here (not only in ``me``) so the frontend's
    shared ``AuthUserResponse`` type can treat it as always present rather
    than optional -- a freshly registered account genuinely has none, and
    a password login may already have some.

    ``has_password`` (ADR-0004 PR 14) is the same ``password_hash is not
    None`` predicate ``update_password`` keys its current-password
    requirement off, computed in one place so the two can never drift.

    ``password_step_up`` (ADR-0004 PR 15, #293) is whether the CALLER'S
    session may currently set a new password without the current one --
    ``step_up_active`` on the caller's own row, the same predicate
    ``update_password`` uses. Callers that have no session in hand (a fresh
    register/login response, whose brand-new session carries no stamp)
    report ``false``.
    """
    step_up = session is not None and settings is not None and step_up_active(session, settings)
    return {
        "request_id": request_id,
        "user_id": user.user_id,
        "email": user.email,
        "anonymous": user.email is None,
        "providers": await _linked_providers(db, user.user_id),
        "has_password": user.password_hash is not None,
        "password_step_up": step_up,
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
    email = normalize_email(request_id, body.email)
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
    return await _auth_user_payload(db, request_id, new_user)


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
    email = normalize_email(request_id, body.email)
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
    return await _auth_user_payload(db, request_id, user)


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
# POST /v1/auth/me/password
# ---------------------------------------------------------------------------


@router.post(
    "/me/password",
    summary="Set or change the signed-in account's password.",
    description=(
        "First-time set (an OAuth-created account that never set one) needs only "
        "new_password; a change needs current_password too -- decided by the "
        "server from the stored account, never from the request body -- unless "
        "the caller's own session redeemed a magic link within the step-up "
        "window (one shot). Every success revokes the account's other sessions "
        "and emails the account (docs/API_SPEC.md §4c)."
    ),
)
async def update_password(
    request: Request,
    body: PasswordUpdateRequest,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _demo_user_id: Annotated[str, Depends(rate_limited_demo)],
) -> dict[str, Any]:
    """Set (first time) or change (thereafter) the caller's password.

    **Whether ``current_password`` is required is decided ONLY from the
    server-loaded ``user.password_hash``, never from body-field presence.**
    Branching on "did the client send the field" would let anyone holding a
    hijacked, already-authenticated session set a new password with zero
    proof of the old one (the planning review's second CRITICAL finding).
    If the account has a password: a missing ``current_password`` is a 422,
    a wrong one is a 422 (not 401 -- the caller IS authenticated, and a 401
    would trip the frontend's global sign-out interceptor). If it has none,
    ``current_password`` is ignored entirely.

    **Step-up (ADR-0004 PR 15, #293):** the current-password requirement is
    waived when -- and only when -- the CALLER'S OWN session row carries a
    ``magic_link_verified_at`` stamp younger than
    ``password_step_up_window_seconds``. That is a second server-held fact
    (the row the cookie resolves to), still never the body: another live
    session of the same account, a stamp older than the window, or any
    other login method gets the normal 422. The stamp is cleared on use, so
    the waiver is one shot. Guardrail 2: every set/change emails the account.

    Every success revokes the account's OTHER live sessions
    (``revoke_other_sessions``) -- the credential surface changed, force
    re-auth everywhere else -- uniformly for first-time set and change, and
    deletes any still-pending magic-link token for the same reason (an
    unread link is a live credential too; review finding).

    Brute-forcing ``current_password`` through this route is bounded by the
    per-user ``password_change`` cap (``rate_limit_password_change_per_hour``,
    default 3 -- a wrong guess spends a slot too) plus Argon2's cost; it
    deliberately does not touch the ``auth_login`` bucket, which would let a
    hijacked session lock the real owner out of password login for an hour.
    A session-only intruder CAN spend the three slots and force the owner
    onto the magic-link path for one window; the stepped-up recovery set is
    exempt from the cap, so the owner always has a way through.
    """
    request_id = _request_id(request)
    current = await try_resolve_auth_session(request, db, settings)
    if current is None or not current.user_id.startswith("usr_"):
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.auth_required,
            message="Sign in to set a password.",
        )
    user = await db.get(User, current.user_id)
    assert user is not None, "a live AuthSession must reference an existing user (FK CASCADE)"

    had_password = user.password_hash is not None
    stepped_up = had_password and step_up_active(current, settings)
    if user.password_hash is not None and not stepped_up:
        # Per-user cap on changes that supply the current password (review
        # finding: an intruder who learned it must not be able to loop
        # changes, each revoking the owner's sessions). Checked BEFORE the
        # verify so a wrong guess spends a slot too.
        await enforce_password_change_rate_limit(user.user_id, settings)
        if not body.current_password:
            raise error_response(
                request_id=request_id,
                code=APIErrorCode.validation_error,
                message="Enter your current password.",
            )
        if not await verify_password(body.current_password, user.password_hash):
            await record_audit_event(
                db,
                action=AuditAction.auth_failed,
                user_id=user.user_id,
                role=UserRole.demo_user,
                metadata={"event": "password_current_mismatch"},
            )
            await db.commit()
            raise error_response(
                request_id=request_id,
                code=APIErrorCode.validation_error,
                message="Current password is incorrect.",
            )

    user.password_hash = await hash_password(body.new_password)
    revoked = await revoke_other_sessions(
        db, user_id=user.user_id, keep_auth_session_id=current.auth_session_id
    )
    await db.execute(delete(MagicLinkToken).where(MagicLinkToken.user_id == user.user_id))
    # One shot: a second change on this session needs the (new) current password.
    current.magic_link_verified_at = None
    # Guardrail 2 (#293): the inbox owner learns of every set/change, so a
    # hijacked session's password change is a race they can win. The NOTICE
    # is throttled per address (never the change): registration does not
    # verify addresses, so an unthrottled notice would be a mail cannon. A
    # suppressed notice is recorded on the audit row (review finding).
    client = build_email_client(settings)
    can_notify = client is not None and bool(user.email)
    notify = can_notify and await email_notice_allowed(user.email or "", settings)
    metadata: dict[str, Any] = {
        "event": "password_changed" if had_password else "password_set",
        "sessions_revoked": revoked,
    }
    if stepped_up:
        metadata["step_up"] = "magic_link"
    if can_notify and not notify:
        metadata["notice_suppressed"] = True
    await record_audit_event(
        db,
        action=AuditAction.login,
        user_id=user.user_id,
        role=UserRole.demo_user,
        metadata=metadata,
    )
    await db.commit()

    if notify and client is not None and user.email:
        background_tasks.add_task(
            deliver,
            client,
            password_changed_message(
                to_addr=user.email,
                at=datetime.now(UTC),
                first_time=not had_password,
                base_url=site_base_url(settings),
            ),
            request_id,
        )
    return await _auth_user_payload(db, request_id, user, session=current, settings=settings)


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
    current = await try_resolve_auth_session(request, db, settings)
    if current is None:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.auth_required,
            message="Not authenticated.",
        )
    user = await db.get(User, current.user_id)
    assert user is not None, "a live AuthSession must reference an existing user (FK CASCADE)"
    return await _auth_user_payload(db, request_id, user, session=current, settings=settings)


__all__ = ["normalize_email", "router"]
