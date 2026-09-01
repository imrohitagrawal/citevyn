"""OAuth login routes: GitHub + Google (ADR-0004 PR 12).

Separate from ``auth.py`` (which frames itself as "register, login, logout,
me" against a JSON request body) because this is a meaningfully different
shape: external HTTP calls, redirects, no JSON body — same one-file-one-
router pattern ``me.py`` (PR 10) established.

Both routes are provider-parameterized, not duplicated per provider. The
``provider`` path segment is validated against :data:`_PROVIDERS` and any
other value 404s — this codebase's existing "unknown → quiet 404, not
informative" convention (the ownership-mismatch-is-404 rule from PR 1).

State / PKCE mechanism
-----------------------

DB-backed (``oauth_nonces``), not Redis. Redis is documented optional infra
in this codebase (the in-process rate limiter is the fallback when it's
absent) — making login hard-depend on Redis would block *all* login,
including brand-new signups with no fallback path, on a Redis outage. A DB
row also gets transactional consistency with the ``AuthSession``/``User``
writes at callback time for free.

Validation at callback: the row is claimed via an ATOMIC ``DELETE ...
RETURNING`` before any of its fields are checked, not a plain ``SELECT``
followed by a separate delete — two near-simultaneous callbacks racing the
same ``state`` would otherwise both read a live row and both pass validation
before either commit landed. Only one concurrent claim can ever get a
non-``None`` row back; the loser fails closed exactly like a forged state.
The claimed row (or ``None``) is then checked, in full, before any other
side effect:

1. A row was actually claimed (``state`` resolved to a live row at all).
2. It was unexpired at claim time.
3. Its ``provider`` matches the URL path's provider.
4. Its ``auth_session_id`` equals the CURRENT request's cookie-resolved
   ``auth_session_id`` — the concrete meaning of "state bound to session":
   the browser that started the flow must be the one completing it, not
   merely someone who observed the ``state`` value.

Identity resolution — the core security logic
-----------------------------------------------

Look up ``UserIdentity`` by ``(provider, provider_account_id)``. Found →
that row's ``user_id`` logs in. Not found → **do not search by email**;
create a brand-new ``User``/``UserIdentity`` pair. Per the owner's confirmed
decision (see the PR 12 plan), this is unconditional: there is no
"link to an existing password account by matching email" branch in this
PR — that would let anyone who controls a matching email on GitHub/Google
take over an existing password account, without ever proving they know its
password.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_sessions import (
    claim_and_login,
    ensure_auth_session,
    try_resolve_auth_session_id,
)
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.core.oauth_http import OAuthProviderError, get_json, post_form
from app.core.rate_limit import rate_limited_oauth_navigation
from app.models import AuditAction, OAuthNonce, User, UserIdentity, UserRole
from app.services.audit import record_audit_event

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Short — the whole round trip (redirect to provider, consent, redirect
# back) normally takes seconds; 5 minutes covers a slow consent screen
# without leaving a meaningfully long forgery window.
_NONCE_TTL_SECONDS = 300


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def _not_found(request: Request) -> Any:
    return error_response(
        request_id=_request_id(request),
        code=APIErrorCode.not_found,
        message="Not found.",
    )


# ---------------------------------------------------------------------------
# Provider registry — deploy-time config, not a DB table.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Identity:
    provider_account_id: str
    email: str | None
    email_verified: bool


@dataclass(frozen=True)
class _ProviderConfig:
    authorize_url: str
    token_url: str
    scope: str
    client_id_field: str
    client_secret_field: str


_PROVIDERS: dict[str, _ProviderConfig] = {
    "github": _ProviderConfig(
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scope="read:user user:email",
        client_id_field="github_oauth_client_id",
        client_secret_field="github_oauth_client_secret",
    ),
    "google": _ProviderConfig(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scope="openid email profile",
        client_id_field="google_oauth_client_id",
        client_secret_field="google_oauth_client_secret",
    ),
}


def _provider_credentials(provider: str, settings: Settings) -> tuple[str, str] | None:
    config = _PROVIDERS[provider]
    client_id = getattr(settings, config.client_id_field)
    client_secret = getattr(settings, config.client_secret_field)
    if not (client_id and client_secret):
        return None
    return client_id, client_secret


async def _fetch_identity(
    provider: str, client: httpx.AsyncClient, access_token: str, timeout_seconds: float
) -> _Identity:
    """Fetch userinfo and normalise it to ``(provider_account_id, email, email_verified)``.

    **Known asymmetry:** GitHub's primary/verified email may not be present
    on the main ``/user`` response (it requires the ``user:email`` scope and
    a SECOND API call, ``GET /user/emails``); Google's single OIDC-claims
    response always carries ``email``/``email_verified`` together. This
    function does not fake symmetry between the two.
    """
    auth_header = {"Authorization": f"Bearer {access_token}"}
    if provider == "github":
        user = await get_json(
            client=client,
            url="https://api.github.com/user",
            headers={**auth_header, "Accept": "application/vnd.github+json"},
            timeout_seconds=timeout_seconds,
            provider="GitHub",
            error_event="github_userinfo_error",
        )
        raw_id = user.get("id")
        if raw_id is None:
            raise OAuthProviderError("GitHub userinfo response missing id")
        provider_account_id = str(raw_id)
        email = user.get("email")
        email_verified = bool(email)
        if not email:
            emails = await get_json(
                client=client,
                url="https://api.github.com/user/emails",
                headers={**auth_header, "Accept": "application/vnd.github+json"},
                timeout_seconds=timeout_seconds,
                provider="GitHub",
                error_event="github_emails_error",
            )
            # The endpoint returns a JSON array, not an object; get_json's
            # dict[str, Any] contract only describes the OBJECT case, so the
            # array is cast back here rather than widening that shared helper.
            entries = cast("list[dict[str, Any]]", emails)
            primary: dict[str, Any] | None = next(
                (e for e in entries if e.get("primary") and e.get("verified")),
                None,
            )
            if primary is not None:
                email = primary.get("email")
                email_verified = True
        return _Identity(
            provider_account_id=provider_account_id, email=email, email_verified=email_verified
        )

    # google
    userinfo = await get_json(
        client=client,
        url="https://openidconnect.googleapis.com/v1/userinfo",
        headers=auth_header,
        timeout_seconds=timeout_seconds,
        provider="Google",
        error_event="google_userinfo_error",
    )
    raw_sub = userinfo.get("sub")
    if raw_sub is None:
        raise OAuthProviderError("Google userinfo response missing sub")
    return _Identity(
        provider_account_id=str(raw_sub),
        email=userinfo.get("email"),
        email_verified=bool(userinfo.get("email_verified")),
    )


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def _new_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _redirect_uri(provider: str, settings: Settings) -> str:
    # Fixed, config-derived — NEVER built from request.base_url/Host (a
    # request through a proxy/CDN could present a spoofed Host; deriving the
    # redirect URI from client-influenced input is exactly the open-redirect
    # risk the provider's exact-match requirement exists to close).
    base = (settings.oauth_redirect_base_url or "http://localhost:8000").rstrip("/")
    return f"{base}/v1/auth/oauth/{provider}/callback"


def _build_http_client() -> httpx.AsyncClient:
    """Construct the client used for the token-exchange + userinfo calls.

    A thin seam so tests can monkeypatch this to an ``httpx.AsyncClient``
    backed by ``httpx.MockTransport`` (this codebase's established pattern
    for the Gemini/OpenRouter clients) instead of hitting the network.
    """
    return httpx.AsyncClient()


def _now() -> datetime:
    return datetime.now(UTC)


def _to_naive_utc(value: datetime) -> datetime:
    """Mirror ``app.core.auth_sessions._to_naive_utc`` — see that docstring."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# GET /v1/auth/oauth/{provider}/start
# ---------------------------------------------------------------------------


@router.get(
    "/oauth/{provider}/start",
    summary="Begin an OAuth login (GitHub or Google).",
    description="Redirects to the provider's consent screen. Not an API call — a real navigation.",
)
async def oauth_start(
    request: Request,
    provider: str,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _rate_limit: Annotated[None, Depends(rate_limited_oauth_navigation)],
) -> RedirectResponse:
    if provider not in _PROVIDERS:
        raise _not_found(request)
    credentials = _provider_credentials(provider, settings)
    if credentials is None:
        raise _not_found(request)
    client_id, _client_secret = credentials

    # Constructed with a placeholder URL and mutated below, so the Set-Cookie
    # header ensure_auth_session() writes lands directly on the response
    # actually returned — FastAPI does NOT merge headers from the
    # dependency-injected `response` parameter onto a Response subclass a
    # handler returns explicitly (only the returned object's own headers are
    # used), so those two must be the same object rather than two that need
    # reconciling after the fact.
    redirect = RedirectResponse("about:blank", status_code=status.HTTP_302_FOUND)
    auth_session_id = await ensure_auth_session(request, redirect, db, settings)

    code_verifier = _new_code_verifier()
    now = _now()
    nonce = OAuthNonce(
        nonce_id=uuid.uuid4(),
        provider=provider,
        code_verifier=code_verifier,
        auth_session_id=auth_session_id,
        return_intent="login",
        created_at=now,
        expires_at=now + timedelta(seconds=_NONCE_TTL_SECONDS),
    )
    db.add(nonce)
    # Durable before the redirect — nothing will retry this write.
    await db.commit()

    config = _PROVIDERS[provider]
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(provider, settings),
        "scope": config.scope,
        "state": str(nonce.nonce_id),
        "code_challenge": _code_challenge(code_verifier),
        "code_challenge_method": "S256",
        "response_type": "code",
    }
    authorize_url = httpx.URL(config.authorize_url, params=params)
    redirect.headers["location"] = str(authorize_url)
    return redirect


# ---------------------------------------------------------------------------
# GET /v1/auth/oauth/{provider}/callback
# ---------------------------------------------------------------------------


@router.get(
    "/oauth/{provider}/callback",
    summary="Complete an OAuth login (GitHub or Google).",
    description="Always redirects (/?auth=ok or /?auth=error) — never a JSON response.",
)
async def oauth_callback(
    request: Request,
    provider: str,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _rate_limit: Annotated[None, Depends(rate_limited_oauth_navigation)],
) -> RedirectResponse:
    async def _fail(event: str) -> RedirectResponse:
        await record_audit_event(
            db,
            action=AuditAction.auth_failed,
            user_id=None,
            role=None,
            metadata={"event": event, "provider": provider},
        )
        await db.commit()
        return RedirectResponse("/?auth=error", status_code=status.HTTP_302_FOUND)

    # 1. Provider declined consent — audited (the "oauth_denied" event this
    #    codebase's own audit-metadata contract names) but otherwise no
    #    state/nonce/identity work is touched.
    if request.query_params.get("error"):
        return await _fail("oauth_denied")

    # 2. Defense in depth — the callback URL is guessable/bookmarkable even
    #    if `start` never ran for this provider.
    if provider not in _PROVIDERS:
        raise _not_found(request)
    credentials = _provider_credentials(provider, settings)
    if credentials is None:
        raise _not_found(request)
    client_id, client_secret = credentials

    state = request.query_params.get("state")
    code = request.query_params.get("code")

    if not state or not code:
        return await _fail("oauth_state_invalid")

    try:
        nonce_id = uuid.UUID(hex=state)
    except ValueError:
        return await _fail("oauth_state_invalid")

    # Atomic claim-by-delete: this is what makes "single-use" airtight under
    # concurrency (two near-simultaneous callbacks racing the same state) --
    # only ONE of two concurrent DELETE...RETURNING statements against the
    # same primary key can ever get a non-None row back; the loser's claim
    # finds nothing and fails closed below, rather than both racing a
    # separate SELECT-then-delete and both passing validation before either
    # commit lands.
    claimed = await db.execute(
        delete(OAuthNonce).where(OAuthNonce.nonce_id == nonce_id).returning(OAuthNonce)
    )
    nonce = claimed.scalar_one_or_none()
    await db.commit()

    if nonce is None:
        return await _fail("oauth_state_invalid")
    if _to_naive_utc(nonce.expires_at) <= _to_naive_utc(_now()):
        return await _fail("oauth_expired")
    if nonce.provider != provider:
        return await _fail("oauth_state_invalid")

    current_auth_session_id = await try_resolve_auth_session_id(request, db, settings)
    if nonce.auth_session_id is None or nonce.auth_session_id != current_auth_session_id:
        return await _fail("oauth_state_invalid")

    config = _PROVIDERS[provider]
    try:
        async with _build_http_client() as client:
            token_headers = {"Accept": "application/json"} if provider == "github" else {}
            token_response = await post_form(
                client=client,
                url=config.token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": _redirect_uri(provider, settings),
                    "grant_type": "authorization_code",
                    "code_verifier": nonce.code_verifier,
                },
                headers=token_headers,
                timeout_seconds=15.0,
                provider=provider,
                error_event=f"{provider}_token_error",
            )
            access_token = token_response.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise OAuthProviderError(f"{provider} token response missing access_token")
            identity = await _fetch_identity(provider, client, access_token, timeout_seconds=15.0)
    except (OAuthProviderError, AttributeError, TypeError, KeyError):
        # Covers both a clean OAuthProviderError (timeout, non-2xx, non-JSON
        # -- see app.core.oauth_http) and a malformed-but-200 payload (a
        # non-dict body, an expected field missing) that would otherwise
        # surface as an unhandled 500 instead of the documented "always
        # redirects, never JSON" contract and leave no audit trail.
        return await _fail("oauth_provider_error")

    # 8. Identity resolution — the core security logic, strict order.
    existing = (
        await db.execute(
            select(UserIdentity).where(
                UserIdentity.provider == provider,
                UserIdentity.provider_account_id == identity.provider_account_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        resolved_user_id = existing.user_id
    else:
        # Not found: do NOT search by email TO RESOLVE who logs in — a new
        # User + UserIdentity is created unconditionally, no "link to
        # existing account" branch in this PR (the account-takeover guard).
        #
        # ``users.email`` is separately UNIQUE (migration 0008's
        # ix_users_email) for an unrelated reason (login-by-email lookup),
        # and that constraint doesn't know about OAuth's "never link"
        # policy — it would reject this INSERT outright if the provider's
        # email already belongs to a different (e.g. password) account. The
        # lookup below exists ONLY to avoid crashing on that constraint; it
        # never changes WHICH user_id this request logs in as, so it does
        # not reopen the account-takeover hole the identity lookup above
        # already closed.
        email_for_new_user = identity.email if identity.email_verified else None
        if email_for_new_user is not None:
            email_taken = (
                await db.execute(select(User.user_id).where(User.email == email_for_new_user))
            ).scalar_one_or_none()
            if email_taken is not None:
                email_for_new_user = None
        new_user = User(
            user_id=f"usr_{uuid.uuid4().hex}",
            role=UserRole.demo_user,
            created_at=_now(),
            email=email_for_new_user,
            password_hash=None,
        )
        db.add(new_user)
        try:
            await db.flush()  # UserIdentity's FK target must exist first
            db.add(
                UserIdentity(
                    identity_id=uuid.uuid4(),
                    provider=provider,
                    provider_account_id=identity.provider_account_id,
                    user_id=new_user.user_id,
                    created_at=_now(),
                )
            )
            await db.flush()
        except IntegrityError:
            # Two concurrent first-time logins for the SAME external
            # identity both pass the "not found" check above before either
            # commits -- the loser hits uq_user_identities_provider_account
            # here instead. Roll back this attempt and resolve to the
            # WINNING identity rather than surfacing a raw 500 (mirrors
            # register()'s own handling of the analogous concurrent-
            # duplicate-email race in app.api.routes.auth).
            await db.rollback()
            winner = (
                await db.execute(
                    select(UserIdentity).where(
                        UserIdentity.provider == provider,
                        UserIdentity.provider_account_id == identity.provider_account_id,
                    )
                )
            ).scalar_one_or_none()
            if winner is None:
                # Unreachable in practice (the IntegrityError means some row
                # now satisfies this key) -- fail closed rather than guess.
                return await _fail("oauth_provider_error")
            resolved_user_id = winner.user_id
        else:
            resolved_user_id = new_user.user_id

    # Same placeholder-then-mutate approach as `start`: claim_and_login needs
    # a Response to set the login cookie on, and it must be the SAME object
    # ultimately returned (see the comment in `start`).
    redirect = RedirectResponse("about:blank", status_code=status.HTTP_302_FOUND)
    await claim_and_login(request, redirect, db, settings, user_id=resolved_user_id)
    await record_audit_event(
        db,
        action=AuditAction.login,
        user_id=resolved_user_id,
        role=UserRole.demo_user,
        metadata={"event": f"oauth_{provider}"},
    )
    await db.commit()

    redirect.headers["location"] = "/?auth=ok"
    return redirect


__all__ = ["router"]
