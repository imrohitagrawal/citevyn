"""Cookie-backed principal resolution (ADR-0004 PR 3).

Every request that reaches a session/message route resolves to exactly one
**principal id** — the id used for ownership checks
(``app.api.routes.sessions._get_session_or_404``,
``app.api.routes.messages._require_session``, ADR-0004 PR 1). Before this
module existed, every caller resolved to the constant ``demo_user``, so
ownership was a no-op. This is deliberately separate from the **audit**
identity (``require_demo_api_key`` -> the constant ``DEMO_USER_ID``, which
:func:`app.core.rate_limit.rate_limited_demo` still returns unchanged): the
demo bearer proves "this is a legitimate demo client", while the cookie
proves "this is the same visitor as last time". Conflating the two would
mean a config-only key rotation (which is meant to be an anti-abuse lever,
not an identity change) silently reassigns every session's owner.

Cookie shape, from ``docs/ADR/0004-user-accounts.md``::

    name    __Host-citevyn_session   (production; unprefixed on local http)
    value   <auth_session_id>.<secret>
    flags   HttpOnly; Secure (production only); SameSite=Lax; Path=/; no Domain=

``auth_session_id`` is the ``AuthSession`` row's UUID primary key — a lookup
key, not a credential, so it is safe to send back to the client verbatim.
``secret`` is 32 random bytes (hex-encoded); only its SHA-256 digest is
stored (``AuthSession.secret_hash``), so a leaked database row cannot forge
a session. ``__Host-`` requires ``Secure`` and forbids ``Domain=``, which the
browser enforces on write — a plain :func:`Response.set_cookie` call without
``domain=`` already satisfies that half; ``secure`` is gated on
``settings.environment == "production"`` because ``__Host-`` cookies are
silently REFUSED by the browser over plain HTTP, which would make anonymous
identity work on every dev machine except the one running `uvicorn --reload`
locally.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.rate_limit import rate_limited_demo
from app.models import AuthSession, User, UserRole

_COOKIE_NAME_PROD = "__Host-citevyn_session"
_COOKIE_NAME_LOCAL = "citevyn_session"


def _cookie_name(settings: Settings) -> str:
    return _COOKIE_NAME_PROD if settings.environment == "production" else _COOKIE_NAME_LOCAL


def _now() -> datetime:
    return datetime.now(UTC)


def _to_naive_utc(value: datetime) -> datetime:
    """Return ``value`` as a naive UTC datetime.

    SQLite (the hermetic test engine) strips tzinfo on round-trip, so a
    value written via ``datetime.now(UTC)`` reads back as naive, and a
    naive/aware comparison raises ``TypeError``. Mirrors
    ``app.cache.answer_cache._to_naive``.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


async def _lookup_principal(db: AsyncSession, cookie_value: str) -> str | None:
    """Resolve a cookie value to a principal id, or ``None`` if it does not verify.

    Every failure mode — malformed value, unknown id, wrong secret, expired
    row — collapses to the same ``None``, which the caller treats as "mint a
    fresh principal". There is no reason to distinguish them: none is
    recoverable by anything other than minting, and a client cannot act on
    the difference (there is no cookie-repair flow).
    """
    auth_session_id_part, _, secret = cookie_value.partition(".")
    if not secret:
        return None
    try:
        auth_session_id = uuid.UUID(hex=auth_session_id_part)
    except ValueError:
        return None

    row = await db.get(AuthSession, auth_session_id)
    if row is None:
        return None
    # Constant-time comparison: the secret is a bearer credential, and a
    # naive ``==`` would let response-timing narrow it down one byte at a
    # time, the same class of bug security.py:52 fixed for the demo bearer.
    if not secrets.compare_digest(_hash_secret(secret), row.secret_hash):
        return None
    if _to_naive_utc(row.expires_at) <= _to_naive_utc(_now()):
        return None
    return row.user_id


async def _mint_principal(db: AsyncSession, settings: Settings, response: Response) -> str:
    """Create a new anonymous principal, persist its auth session, and set the cookie.

    Every write here is a ``flush``, not a ``commit`` — the route's own
    ``db.commit()`` (already required for every session/message write) is
    what makes it durable, matching the ``_ensure_user`` pattern this
    mirrors in ``app.api.routes.sessions``. If the route's commit never
    happens (an error mid-request), the cookie the client received points at
    a principal that was never actually persisted — the next request with
    that cookie fails to resolve it (``_lookup_principal`` returns ``None``)
    and transparently mints a replacement. No orphaned, unusable identity
    can be "stuck" client-side.
    """
    principal_id = f"anon_{uuid.uuid4().hex}"
    db.add(User(user_id=principal_id, role=UserRole.demo_user, created_at=_now()))

    auth_session_id = uuid.uuid4()
    secret = secrets.token_hex(32)  # 32 random bytes, hex-encoded (64 chars)
    now = _now()
    db.add(
        AuthSession(
            auth_session_id=auth_session_id,
            secret_hash=_hash_secret(secret),
            user_id=principal_id,
            created_at=now,
            expires_at=now + timedelta(seconds=settings.auth_session_ttl_seconds),
        )
    )
    await db.flush()

    response.set_cookie(
        key=_cookie_name(settings),
        value=f"{auth_session_id.hex}.{secret}",
        max_age=settings.auth_session_ttl_seconds,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/",
    )
    return principal_id


async def resolve_principal(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    # Depended on for its side effects only (bearer auth + per-visitor rate
    # limit) -- the returned value is the AUDIT identity (constant
    # DEMO_USER_ID), deliberately not reused as the ownership principal. See
    # the module docstring.
    _demo_user_id: Annotated[str, Depends(rate_limited_demo)],
) -> str:
    """Resolve the caller's ownership principal, minting one if none exists yet.

    Used by every session/message route in place of the raw
    ``rate_limited_demo`` result, so ownership (ADR-0004 PR 1's predicate)
    checks against a real per-visitor identity instead of the shared
    constant. Minting happens transparently on ANY of the four routes, not
    only ``POST /v1/sessions`` — a GET or DELETE that arrives with no cookie
    genuinely owns nothing yet, so it 404s regardless of which principal is
    minted for it, and minting uniformly here keeps one code path for all
    four routes rather than a POST-only special case.
    """
    cookie_name = _cookie_name(settings)
    cookie_value = request.cookies.get(cookie_name)
    if cookie_value:
        principal_id = await _lookup_principal(db, cookie_value)
        if principal_id is not None:
            return principal_id
    return await _mint_principal(db, settings, response)


__all__ = ["resolve_principal"]
