"""OAuth login routes: GitHub + Google (ADR-0004 PR 12).

Separate from ``auth.py`` (which frames itself as "register, login, logout,
me" against a JSON request body) because this is a meaningfully different
shape: external HTTP calls, redirects, no JSON body — same one-file-one-
router pattern ``me.py`` (PR 10) established.

Both routes are provider-parameterized, not duplicated per provider. The
``provider`` path segment is validated against :data:`_PROVIDERS` and any
other value 404s — this codebase's existing "unknown → quiet 404, not
informative" convention (the ownership-mismatch-is-404 rule from PR 1).
**One deliberate exception:** ``callback``'s ``error=`` branch (the provider
declined consent) is checked and redirected on BEFORE the provider is
validated, per the plan's own explicit step ordering — an unknown/bogus
provider combined with ``error=access_denied`` gets the same ``/?auth=error``
redirect an unknown provider without ``error=`` would 404 on. This leaks
nothing an attacker doesn't already have (the response is byte-for-byte
identical regardless of whether the provider is real, unconfigured, or
nonexistent, and the two configured provider names are already public in
the frontend bundle) — confirmed by adversarial review, not assumed.

State / PKCE mechanism
-----------------------

DB-backed (``oauth_nonces``), not Redis. Redis is documented optional infra
in this codebase (the in-process rate limiter is the fallback when it's
absent) — making login hard-depend on Redis would block *all* login,
including brand-new signups with no fallback path, on a Redis outage. A DB
row also gets transactional consistency with the ``AuthSession``/``User``
writes at callback time for free.

Validation at callback: the row is claimed via a single ATOMIC, CONDITIONAL
``DELETE ... WHERE nonce_id = ? AND provider = ? AND auth_session_id = ? ...
RETURNING`` — not a plain ``SELECT`` followed by a separate, unconditional
delete, and not an unconditional delete-by-id validated afterward either.
Both of those alternatives are broken in their own way:

* An unconditional ``SELECT`` then delete lets two near-simultaneous
  callbacks racing the same ``state`` both read a live row and both pass
  validation before either commit lands (double-consumption).
* An unconditional ``DELETE ... WHERE nonce_id = ?`` (claim first, validate
  the returned fields after) closes that race, but consumes the nonce even
  when the CLAIMANT fails validation — so an attacker who merely observed a
  real victim's ``state`` value (no session compromise needed) can submit it
  and permanently burn the victim's nonce before the victim's own browser
  completes the flow. This is a denial-of-login griefing attack, not a
  benign race, and was caught by adversarial review of an earlier version
  of this fix.

Baking ``provider`` and the CURRENT request's cookie-resolved
``auth_session_id`` directly into the ``WHERE`` clause closes both: only a
claim whose predicates ALL match can ever delete anything, so (a) at most
one of several concurrent matching claims succeeds, and (b) a non-matching
claim (wrong provider, wrong/no session) deletes nothing and leaves the
nonce intact for whoever DOES hold the matching session to retry — the
concrete meaning of "state bound to session": the browser that started the
flow must be the one completing it, not merely someone who observed the
``state`` value. Expiry is checked separately, in Python, on the claimed
row (using the same naive/aware normalization every other timestamp
comparison in this codebase uses) — deliberately not a ``WHERE`` predicate,
since consuming an already-dead nonce on a failed claim is harmless.

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

Account linking ("connect") — ADR-0004 PR 13
---------------------------------------------

The same two providers, but attached to an EXISTING signed-in account
instead of resolving who logs in. This is the working form of the
password-recovery mitigation the ADR promised ("GitHub OAuth is the
recovery path"): a password account that has connected GitHub/Google can
still get in after forgetting its password.

``GET .../{provider}/connect/start`` is a second start route, not a query
param on ``start``: it has a different precondition (a signed-in, real
``usr_`` account whose session is FRESH — see
``Settings.oauth_connect_max_session_age_seconds``) and never mints an
anonymous session. Both start routes share :func:`_start_oauth_flow`; the
only difference between the two nonce rows they write is
``return_intent`` (``"login"`` / ``"connect"``) — a plain string column,
which is exactly why this feature needed no migration.

``callback`` runs the SAME claim/expiry/token-exchange/userinfo prefix for
both intents, then dispatches on the claimed nonce's ``return_intent`` to
:func:`_handle_login_intent` (a pure extraction of the PR 12 tail) or
:func:`_handle_connect_intent`. The security invariants of the connect
half, written down here because they transfer to any stack:

* **Never reassign.** An external identity already linked to a DIFFERENT
  account is rejected (:attr:`LinkResult.LINKED_ELSEWHERE`), never moved —
  including under a concurrent-insert race, where the loser must compare
  the winning row's ``user_id`` against ITS OWN intended target rather than
  report success just because a row now exists.
* **Never create a User, never touch users.email.** Linking writes exactly
  one ``UserIdentity`` row; a target account's email is not overwritten by
  the provider's.
* **Never rotate the caller's session.** The signed-in cookie must stay
  byte-identical across the round trip — ``claim_and_login`` is not called
  on this path.
* **The target is the claimed nonce's own session**, resolved via
  :func:`resolve_principal_by_auth_session_id`, not the request cookie read
  a second time — one source of truth for "which session started this".
* **Fresh session required at start.** A stolen cookie must not be able to
  plant a permanent backdoor identity at any point in a 180-day session.
* **Disconnect invariant (for the future "disconnect" feature, not built
  here):** an account must always retain >= 1 access method —
  ``password_hash`` set OR >= 1 remaining ``UserIdentity`` row. Do not allow
  removing the last one.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Callable
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
    resolve_principal_by_auth_session_id,
    try_resolve_auth_session,
    try_resolve_auth_session_id,
)
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.core.oauth_http import OAuthProviderError, get_json, post_form
from app.core.rate_limit import rate_limited_oauth_navigation
from app.models import AuditAction, AuthSession, OAuthNonce, User, UserIdentity, UserRole
from app.services.audit import record_audit_event

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Short — the whole round trip (redirect to provider, consent, redirect
# back) normally takes seconds; 5 minutes covers a slow consent screen
# without leaving a meaningfully long forgery window.
_NONCE_TTL_SECONDS = 300

# ``OAuthNonce.return_intent`` values. Plain strings (the column is an
# unchecked String(16)), compared exactly at callback -- a nonce minted for
# one intent must never complete as the other.
_INTENT_LOGIN = "login"
_INTENT_CONNECT = "connect"

_REGISTERED_PREFIX = "usr_"


class LinkResult(enum.StrEnum):
    """Outcome of :func:`_link_identity` -- explicit, not string/exception signalling.

    ``LINKED`` and ``ALREADY_LINKED_SAME`` are both success from the user's
    point of view (the identity now points at their account); only
    ``LINKED_ELSEWHERE`` is a rejection. Kept as three values rather than a
    bool so the audit trail can tell "newly linked" from "was already
    linked" without the route re-deriving it.
    """

    LINKED = "linked"
    ALREADY_LINKED_SAME = "already_linked_same"
    LINKED_ELSEWHERE = "linked_elsewhere"


_FailFn = Callable[..., Awaitable[RedirectResponse]]


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
# GET /v1/auth/oauth/{provider}/start  and  .../{provider}/connect/start
# ---------------------------------------------------------------------------


def _require_provider(request: Request, provider: str, settings: Settings) -> tuple[str, str]:
    """Validate the path's provider and return its ``(client_id, client_secret)``.

    The ONE place both start routes (and the callback) check "is this a
    known, configured provider" -- an unknown or unconfigured provider 404s
    quietly, per this codebase's convention. Shared so the two start routes
    cannot drift on this check.
    """
    if provider not in _PROVIDERS:
        raise _not_found(request)
    credentials = _provider_credentials(provider, settings)
    if credentials is None:
        raise _not_found(request)
    return credentials


async def _start_oauth_flow(
    redirect: RedirectResponse,
    provider: str,
    settings: Settings,
    db: AsyncSession,
    *,
    client_id: str,
    return_intent: str,
    auth_session_id: uuid.UUID,
) -> RedirectResponse:
    """Persist a nonce bound to ``auth_session_id`` and point ``redirect`` at the provider.

    Shared by :func:`oauth_start` (``return_intent="login"``) and
    :func:`oauth_connect_start` (``"connect"``). ``redirect`` is passed in
    (constructed by the caller with a placeholder URL) rather than created
    here so a caller that has already set a cookie on it -- ``oauth_start``
    via ``ensure_auth_session`` -- returns that same object; FastAPI does
    NOT merge headers from a dependency-injected ``response`` onto a
    Response subclass a handler returns explicitly.
    """
    code_verifier = _new_code_verifier()
    now = _now()
    nonce = OAuthNonce(
        nonce_id=uuid.uuid4(),
        provider=provider,
        code_verifier=code_verifier,
        auth_session_id=auth_session_id,
        return_intent=return_intent,
        created_at=now,
        expires_at=now + timedelta(seconds=_NONCE_TTL_SECONDS),
    )
    db.add(nonce)
    # Durable before the redirect -- nothing will retry this write.
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
    client_id, _client_secret = _require_provider(request, provider, settings)

    # Constructed with a placeholder URL and mutated by _start_oauth_flow, so
    # the Set-Cookie header ensure_auth_session() writes lands directly on
    # the response actually returned (see _start_oauth_flow's docstring).
    redirect = RedirectResponse("about:blank", status_code=status.HTTP_302_FOUND)
    auth_session_id = await ensure_auth_session(request, redirect, db, settings)
    return await _start_oauth_flow(
        redirect,
        provider,
        settings,
        db,
        client_id=client_id,
        return_intent=_INTENT_LOGIN,
        auth_session_id=auth_session_id,
    )


def _connect_error_location(provider: str, reason: str) -> str:
    # ``provider`` is only ever a validated _PROVIDERS key by the time this
    # is called, never raw path input -- so it is safe to echo into the URL.
    return f"/?connect=error&reason={reason}&provider={provider}"


def _session_is_fresh(session: AuthSession, settings: Settings) -> bool:
    """The stolen-cookie gate: was this session CREATED recently enough to link from?

    ``created_at`` is set once at login/register/OAuth login and never
    refreshed, so "fresh" means "a real credential check happened within
    the window", not "recently active". See the setting's own comment in
    ``app.core.config`` for the threat this bounds.
    """
    age = _to_naive_utc(_now()) - _to_naive_utc(session.created_at)
    return age <= timedelta(seconds=settings.oauth_connect_max_session_age_seconds)


@router.get(
    "/oauth/{provider}/connect/start",
    summary="Connect a GitHub or Google identity to the signed-in account (ADR-0004 PR 13).",
    description=(
        "Redirects to the provider's consent screen. Requires a signed-in, "
        "registered account whose session is fresh (created within "
        "oauth_connect_max_session_age_seconds); otherwise redirects to "
        "/?connect=error&reason=session. Not an API call — a real navigation."
    ),
)
async def oauth_connect_start(
    request: Request,
    provider: str,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _rate_limit: Annotated[None, Depends(rate_limited_oauth_navigation)],
) -> RedirectResponse:
    client_id, _client_secret = _require_provider(request, provider, settings)

    # Never mints: a request that is about to be rejected must not leave an
    # anonymous session behind, and the nonce must bind to the caller's
    # ACTUAL existing session (the one whose owner is the link target).
    # Anonymous / no session / stale session all collapse to one redirect:
    # the browser already knows whether it is signed in, so there is
    # nothing to leak, and a bare 404 JSON body would be a dead end for a
    # real user on a top-level navigation (the redirect lets the frontend
    # say "sign in again, then retry").
    session = await try_resolve_auth_session(request, db, settings)
    if (
        session is None
        or not session.user_id.startswith(_REGISTERED_PREFIX)
        or not _session_is_fresh(session, settings)
    ):
        return RedirectResponse(
            _connect_error_location(provider, "session"), status_code=status.HTTP_302_FOUND
        )

    redirect = RedirectResponse("about:blank", status_code=status.HTTP_302_FOUND)
    return await _start_oauth_flow(
        redirect,
        provider,
        settings,
        db,
        client_id=client_id,
        return_intent=_INTENT_CONNECT,
        auth_session_id=session.auth_session_id,
    )


async def _resolve_or_create_identity(
    db: AsyncSession, provider: str, identity: _Identity
) -> str | None:
    """Resolve ``identity`` to a ``user_id``, creating a new account if needed.

    The core security logic, strict order: look up ``UserIdentity`` by
    ``(provider, provider_account_id)`` ONLY. Found → that row's ``user_id``
    logs in. Not found → **do not search by email TO RESOLVE who logs in**
    — a new ``User``/``UserIdentity`` pair is created unconditionally, no
    "link to an existing account" branch in this PR (the account-takeover
    guard: matching the provider's email against ``users.email`` would let
    anyone who controls a matching email on GitHub/Google take over an
    existing password account without ever proving they know its password).

    Returns ``None`` only in the practically-unreachable case described
    below, where the caller should fail closed rather than guess.

    A separate function (not inlined in :func:`oauth_callback`) so a test
    can exercise the concurrent-identity-creation race below by injecting a
    competing insert between the "not found" lookup and this function's own
    insert, without needing real thread/process-level concurrency.
    """
    existing = (
        await db.execute(
            select(UserIdentity).where(
                UserIdentity.provider == provider,
                UserIdentity.provider_account_id == identity.provider_account_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.user_id

    # ``users.email`` is separately UNIQUE (migration 0008's
    # ix_users_email) for an unrelated reason (login-by-email lookup), and
    # that constraint doesn't know about OAuth's "never link" policy — it
    # would reject this INSERT outright if the provider's email already
    # belongs to a different (e.g. password) account. The lookup below
    # exists ONLY to avoid crashing on that constraint; it never changes
    # WHICH user_id this request logs in as, so it does not reopen the
    # account-takeover hole the identity lookup above already closed.
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
        # Two concurrent first-time logins for the SAME external identity
        # both pass the "not found" check above before either commits --
        # the loser hits uq_user_identities_provider_account here instead.
        # Roll back this attempt and resolve to the WINNING identity rather
        # than surfacing a raw 500 (mirrors register()'s own handling of
        # the analogous concurrent-duplicate-email race in
        # app.api.routes.auth).
        await db.rollback()
        winner = (
            await db.execute(
                select(UserIdentity).where(
                    UserIdentity.provider == provider,
                    UserIdentity.provider_account_id == identity.provider_account_id,
                )
            )
        ).scalar_one_or_none()
        # winner is None is unreachable in practice (the IntegrityError
        # means some row now satisfies this key) -- fail closed rather
        # than guess.
        return winner.user_id if winner is not None else None
    else:
        return new_user.user_id


async def _link_identity(
    db: AsyncSession, provider: str, identity: _Identity, target_user_id: str
) -> LinkResult:
    """Attach ``identity`` to ``target_user_id``; never reassign, never create a User.

    Writes at most ONE ``UserIdentity`` row. Never inserts/updates ``User``
    (the target already exists) and never touches ``users.email`` -- the
    provider's email is irrelevant to linking, so the email-uniqueness
    concern the login path has to dodge simply does not arise here.

    The race branch is the security-relevant part: two concurrent connects
    for the SAME external identity toward DIFFERENT accounts both pass the
    "not found" lookup, one insert wins ``uq_user_identities_provider_account``
    and the other raises ``IntegrityError``. The loser must NOT be told it
    succeeded merely because a row now exists (that is what the login
    path's analogous branch does, correctly for login, where "some account
    now owns this identity, log it in" is the right answer). It re-reads the
    winner and compares ``winner.user_id`` against its OWN target -- same
    account -> idempotent success, different account -> rejected.

    Future "disconnect" invariant (documented, not built): removing an
    identity must leave the account with >= 1 access method (``password_hash``
    set OR another ``UserIdentity`` row). Do not silently design this away.
    """
    existing = (
        await db.execute(
            select(UserIdentity).where(
                UserIdentity.provider == provider,
                UserIdentity.provider_account_id == identity.provider_account_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.user_id == target_user_id:
            return LinkResult.ALREADY_LINKED_SAME
        return LinkResult.LINKED_ELSEWHERE

    db.add(
        UserIdentity(
            identity_id=uuid.uuid4(),
            provider=provider,
            provider_account_id=identity.provider_account_id,
            user_id=target_user_id,
            created_at=_now(),
        )
    )
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        winner = (
            await db.execute(
                select(UserIdentity).where(
                    UserIdentity.provider == provider,
                    UserIdentity.provider_account_id == identity.provider_account_id,
                )
            )
        ).scalar_one_or_none()
        # winner is None is unreachable in practice (the IntegrityError means
        # some row now satisfies this key) -- fail CLOSED (rejected), never
        # open, if it somehow happens.
        if winner is not None and winner.user_id == target_user_id:
            return LinkResult.ALREADY_LINKED_SAME
        return LinkResult.LINKED_ELSEWHERE
    return LinkResult.LINKED


async def _resolve_connect_target(db: AsyncSession, nonce: OAuthNonce) -> str | None:
    """The account a claimed CONNECT nonce links into, or ``None`` to fail closed.

    Resolved from the claimed nonce's own ``auth_session_id`` (captured in
    Python before the row was deleted), NOT by re-reading the request cookie:
    the claim's WHERE clause already proved cookie == nonce binding, so this
    is the single source of truth. Re-verified as a real ``usr_`` account
    because the session could have been revoked/rotated between ``start``
    and ``callback`` (on Postgres the nonce would CASCADE away with it, but
    that is a schema property, not something this code should lean on).
    """
    if nonce.auth_session_id is None:
        return None
    principal_id = await resolve_principal_by_auth_session_id(db, nonce.auth_session_id)
    if principal_id is None or not principal_id.startswith(_REGISTERED_PREFIX):
        return None
    return principal_id


async def _handle_login_intent(
    request: Request,
    db: AsyncSession,
    settings: Settings,
    provider: str,
    identity: _Identity,
    fail: _FailFn,
) -> RedirectResponse:
    """The PR 12 login tail, extracted verbatim: resolve/create, claim+login, audit."""
    resolved_user_id = await _resolve_or_create_identity(db, provider, identity)
    if resolved_user_id is None:
        return await fail("oauth_provider_error")

    # Same placeholder-then-mutate approach as `start`: claim_and_login needs
    # a Response to set the login cookie on, and it must be the SAME object
    # ultimately returned (see _start_oauth_flow's docstring).
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


async def _handle_connect_intent(
    db: AsyncSession,
    provider: str,
    identity: _Identity,
    nonce: OAuthNonce,
    fail: _FailFn,
) -> RedirectResponse:
    """Link ``identity`` to the account that started this flow. No cookie changes."""
    target_user_id = await _resolve_connect_target(db, nonce)
    if target_user_id is None:
        return await fail(
            "oauth_connect_no_session", location=_connect_error_location(provider, "session")
        )

    result = await _link_identity(db, provider, identity, target_user_id)
    if result is LinkResult.LINKED_ELSEWHERE:
        # Deliberately does NOT record the other account's user_id -- the
        # audit row is attributed to the acting user, and naming the other
        # account would leak cross-account information into the trail.
        return await fail(
            "oauth_connect_conflict",
            user_id=target_user_id,
            location=_connect_error_location(provider, "already_linked"),
        )

    # Deliberately NOT claim_and_login: the caller is already signed in as
    # the target, their cookie must not rotate, and claim-on-login's
    # "fold in a prior anon_ principal" logic can never apply here.
    await record_audit_event(
        db,
        action=AuditAction.login,
        user_id=target_user_id,
        role=UserRole.demo_user,
        metadata={
            "event": f"oauth_connect_{provider}",
            "provider": provider,
            "result": result.value,
        },
    )
    await db.commit()
    return RedirectResponse(f"/?connect=ok&provider={provider}", status_code=status.HTTP_302_FOUND)


async def _claim_nonce(
    db: AsyncSession, state: str | None, provider: str, auth_session_id: uuid.UUID | None
) -> OAuthNonce | None:
    """Atomically consume the nonce ``state`` names -- only if every predicate matches.

    The single-use + session-binding mechanism the module docstring
    describes: one ``DELETE ... WHERE nonce_id AND provider AND
    auth_session_id ... RETURNING``. A malformed ``state``, or no current
    session, matches nothing and deletes nothing. Returns the claimed row
    (already deleted, captured in Python) or ``None``. Commits.
    """
    if not state or auth_session_id is None:
        return None
    try:
        nonce_id = uuid.UUID(hex=state)
    except ValueError:
        return None
    claimed = await db.execute(
        delete(OAuthNonce)
        .where(
            OAuthNonce.nonce_id == nonce_id,
            OAuthNonce.provider == provider,
            OAuthNonce.auth_session_id == auth_session_id,
        )
        .returning(OAuthNonce)
    )
    nonce = claimed.scalar_one_or_none()
    await db.commit()
    return nonce


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
    # Where a failure sends the browser. "/?auth=error" until the nonce is
    # claimed (nobody knows the intent before that); a claimed CONNECT nonce
    # switches it so a failed link is not reported as a failed sign-in.
    failure_location = "/?auth=error"

    async def _fail(
        event: str, *, user_id: str | None = None, location: str | None = None
    ) -> RedirectResponse:
        # ``user_id`` is None for login failures (nobody's identity is known
        # yet) but SET for connect failures, where the acting account is
        # known and losing that attribution would weaken the audit trail.
        await record_audit_event(
            db,
            action=AuditAction.auth_failed,
            user_id=user_id,
            role=None,
            metadata={"event": event, "provider": provider},
        )
        await db.commit()
        return RedirectResponse(location or failure_location, status_code=status.HTTP_302_FOUND)

    # Session binding is resolved up front and baked directly into every
    # claim's WHERE clause (see below) -- not checked afterward on whatever
    # got returned. A caller with no valid session at all can never be the
    # browser that started the flow, so the claims below fail closed without
    # even being attempted (also sidesteps a SQL NULL-comparison footgun:
    # comparing a bind of Python None to auth_session_id would compile to
    # "IS NULL", which could wrongly match a nonce whose own
    # auth_session_id happens to be NULL).
    current_auth_session_id = await try_resolve_auth_session_id(request, db, settings)

    # 1. Provider declined consent — audited (the "oauth_denied" event this
    #    codebase's own audit-metadata contract names). No identity work is
    #    touched, but the nonce IS consumed if -- and only if -- it is ours
    #    (same session-bound conditional claim as the success path, so an
    #    attacker replaying a victim's state with error=access_denied still
    #    deletes nothing): a declined state can never complete, and the
    #    claimed row's intent is what tells a CONNECT attempt apart from a
    #    LOGIN one. Without this, cancelling "Connect GitHub" reported
    #    "Sign-in failed" to a user who is still signed in (found live).
    #    Still checked BEFORE provider validation, per PR 12's ordering: an
    #    unknown provider + error= gets the same /?auth=error as before.
    if request.query_params.get("error"):
        denied = await _claim_nonce(
            db, request.query_params.get("state"), provider, current_auth_session_id
        )
        if denied is not None and denied.return_intent == _INTENT_CONNECT:
            return await _fail(
                "oauth_denied",
                user_id=await _resolve_connect_target(db, denied),
                location=_connect_error_location(provider, "denied"),
            )
        return await _fail("oauth_denied")

    # 2. Defense in depth — the callback URL is guessable/bookmarkable even
    #    if `start` never ran for this provider.
    client_id, client_secret = _require_provider(request, provider, settings)

    state = request.query_params.get("state")
    code = request.query_params.get("code")

    if not state or not code:
        return await _fail("oauth_state_invalid")

    if current_auth_session_id is None:
        return await _fail("oauth_state_invalid")

    # Atomic, CONDITIONAL claim-by-delete: nonce_id, provider, AND session
    # binding are all baked into one DELETE...RETURNING's WHERE clause,
    # which is what makes single-use airtight under concurrency without
    # also being griefable. Two hazards, closed together:
    #   1. Two near-simultaneous callbacks with the SAME state (and the
    #      SAME session) racing each other -- only one concurrent DELETE
    #      matching every predicate can ever return a row; the loser's
    #      claim matches nothing and fails closed.
    #   2. An attacker who has observed/leaked a real victim's `state`
    #      value but is not the browser that started the flow -- their
    #      claim's WHERE clause does not match (wrong auth_session_id), so
    #      it deletes NOTHING, leaving the nonce intact for the legitimate
    #      browser to still complete the flow. An earlier version of this
    #      fix deleted unconditionally on `nonce_id` alone and validated
    #      the fields afterward, which closed hazard 1 but reopened this
    #      as a denial-of-login griefing attack -- caught by adversarial
    #      review. Expiry is deliberately NOT one of the WHERE predicates
    #      (see below): consuming an already-dead nonce on a failed claim
    #      is harmless, and a SQL-side comparison against a bind time
    #      would risk the same naive/aware datetime mismatch
    #      `_to_naive_utc` exists to paper over on the Python side.
    nonce = await _claim_nonce(db, state, provider, current_auth_session_id)

    if nonce is None:
        return await _fail("oauth_state_invalid")
    if nonce.return_intent == _INTENT_CONNECT:
        failure_location = _connect_error_location(provider, "provider")
    if _to_naive_utc(nonce.expires_at) <= _to_naive_utc(_now()):
        return await _fail("oauth_expired")

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

    # 8. Dispatch on the CLAIMED nonce's intent -- an exact string compare,
    #    so a nonce minted for one intent can never complete as the other.
    #    Everything above this line is byte-for-byte shared by both.
    if nonce.return_intent == _INTENT_CONNECT:
        return await _handle_connect_intent(db, provider, identity, nonce, _fail)
    if nonce.return_intent == _INTENT_LOGIN:
        return await _handle_login_intent(request, db, settings, provider, identity, _fail)
    # An unrecognised intent (a future value this code does not know) fails
    # closed rather than defaulting to login.
    return await _fail("oauth_state_invalid")


__all__ = ["LinkResult", "router"]
