"""Magic-link login: request + confirm (ADR-0004 PR 14).

A third way in, next to password (``auth.py``) and OAuth (``oauth.py``): the
user asks for a link by email, and the link logs them in. It is also the
working password-recovery path -- a forgotten password is solved by
requesting a link, then setting a new password from the account menu
(``POST /v1/auth/me/password``), so there is deliberately NO separate
reset-via-email flow with its own token type to secure.

Same one-file-one-router pattern as ``oauth.py``/``me.py``. The pure pieces
live in seams with no app coupling -- ``app.core.token_secrets`` (the
credential) and ``app.core.email_client`` (delivery) -- and this module owns
the policy: single-use, expiry, one live token per user, which branch pays
what cost.

Security invariants, written down because they transfer to any stack
-----------------------------------------------------------------------

* **``request`` never reveals whether the email exists.** Always 202, and
  the two branches do the same work before the response is decided: the
  same SELECT, the same DELETE-by-user (a sentinel id on no-match), one
  INSERT-cost statement each (the token row, or a discarded DELETE by a
  fresh random id), one audit INSERT each, one commit. Only the network
  send is deferred -- via ``BackgroundTasks``, registered on BOTH branches
  (a real send vs. an explicit no-op) so the code path carries no signal.
  This mirrors ``verify_password_or_dummy``'s precedent of actually
  equalising cost rather than "returning before the slow part"; the
  planning review rejected the latter (registering a background task only
  on one branch is itself a branch-dependent path).
* **``GET confirm`` consumes nothing.** Corporate mail scanners and link
  prefetchers GET every URL in an inbound email before the human opens it;
  a GET that redeemed the token would hand the real user a dead link. The
  GET renders a plain interstitial whose ``<form method="post">`` holds the
  token; ONLY a real click on its submit button posts. No auto-submitting
  JS, no ``<meta http-equiv="refresh">``, no ``onload`` -- any of those
  would silently reopen the scanner hole. (Also structurally enforced: the
  CSP forbids inline script.)
* **``POST confirm`` claims atomically and conditionally.** One
  ``DELETE ... WHERE token_id AND secret_hash ... RETURNING``, the same
  proven shape as ``oauth_nonces``: at most one of several concurrent
  claims can win, and a wrong secret deletes NOTHING, so a guess cannot
  burn the real user's link. Expiry is checked in Python on the claimed
  row (consuming an already-dead token is harmless).
* **``POST confirm`` refuses a cross-site origin.** The form POST carries
  no demo bearer (a browser navigation cannot), so the bearer's CSRF role
  does not apply. Without a check, an attacker could request a link for
  THEIR OWN account and auto-post its token from a hostile page, logging
  the victim's browser into the attacker's account (login CSRF) -- every
  question the victim then asks lands in the attacker's history. A present
  ``Origin`` must equal the configured ``magic_link_base_url`` origin; a
  present ``Sec-Fetch-Site`` must be ``same-origin``/``none``. A request
  with neither header (a non-browser client) is allowed: it already holds
  the credential and gains nothing.
* **No session binding on the token**, unlike ``oauth_nonces`` -- deliberately.
  A magic link is cross-device by design (request on the laptop, open on
  the phone); the token alone is sufficient, the way the session cookie's
  own secret is.
* **The token is never logged.** ``AGENTS.md`` forbids logging tokens even
  in development, and this token IS the credential. The dev outbox logs the
  file path it wrote (no secret in the name); the request route logs nothing
  about the token at all.
"""

from __future__ import annotations

import html
import logging
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.auth import normalize_email
from app.core.auth_sessions import claim_and_login
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.email_client import (
    EmailClient,
    EmailDeliveryError,
    EmailMessage,
    FileOutboxEmailClient,
    ResendEmailClient,
)
from app.core.errors import APIErrorCode, error_response
from app.core.rate_limit import (
    enforce_magic_link_rate_limit,
    rate_limited_demo,
    rate_limited_oauth_navigation,
)
from app.core.token_secrets import generate_token, hash_token, verify_token
from app.models import AuditAction, MagicLinkToken, User, UserRole
from app.services.audit import record_audit_event

router = APIRouter(prefix="/v1/auth/magic-link", tags=["auth"])

_logger = logging.getLogger("citevyn.magic_link")

# Local-dev fallback for the emailed link's origin -- same default the OAuth
# redirect URI uses (``app.api.routes.oauth._redirect_uri``).
_LOCAL_BASE_URL = "http://localhost:8000"
_CONFIRM_PATH = "/v1/auth/magic-link/confirm"

# A user_id that can never exist (``usr_``/``anon_`` prefixes are the only
# shapes minted), used as the DELETE target on the no-match branch so that
# branch runs the same statement as the match branch.
_NO_SUCH_USER_ID = "none_00000000000000000000000000000000"

# Upper bound on the emailed token value -- 32 hex + "." + 64 hex = 97 chars.
_TOKEN_MAX_LENGTH = 128


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def _now() -> datetime:
    return datetime.now(UTC)


def _to_naive_utc(value: datetime) -> datetime:
    """Mirror ``app.core.auth_sessions._to_naive_utc`` -- see that docstring."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _build_email_client(settings: Settings) -> EmailClient | None:
    """Pick the delivery backend, or ``None`` when the feature is unavailable.

    A configured ``resend_api_key`` wins everywhere. Otherwise the file
    outbox is used outside production only (a ``Settings`` validator refuses
    it IN production), and production without a provider returns ``None``
    -- the request route then 404s, the same "not configured -> quiet 404"
    convention an unconfigured OAuth provider follows.
    """
    if settings.resend_api_key:
        return ResendEmailClient(
            api_key=settings.resend_api_key, from_addr=settings.email_from or ""
        )
    if settings.environment != "production":
        directory = (
            Path(settings.email_outbox_dir)
            if settings.email_outbox_dir
            else Path(tempfile.gettempdir()) / "citevyn_email_outbox"
        )
        return FileOutboxEmailClient(directory)
    return None


def _link_base_url(settings: Settings) -> str:
    return (settings.magic_link_base_url or _LOCAL_BASE_URL).rstrip("/")


def _build_message(*, to_addr: str, link: str, ttl_seconds: int) -> EmailMessage:
    minutes = max(1, ttl_seconds // 60)
    subject = "Your CiteVyn sign-in link"
    text = (
        "Sign in to CiteVyn\n"
        "\n"
        f"Open this link to sign in: {link}\n"
        "\n"
        f"It works once and expires in {minutes} minutes. If you didn't ask for it, "
        "ignore this email -- nothing happens unless the link is used.\n"
    )
    safe_link = html.escape(link, quote=True)
    body_html = (
        "<p>Sign in to CiteVyn</p>"
        f'<p><a href="{safe_link}">Open this link to sign in</a></p>'
        f"<p>It works once and expires in {minutes} minutes. If you didn't ask for it, "
        "ignore this email &mdash; nothing happens unless the link is used.</p>"
    )
    return EmailMessage(to_addr=to_addr, subject=subject, text=text, html=body_html)


async def _deliver(client: EmailClient, message: EmailMessage, request_id: str) -> None:
    """Background send. Failures are logged (no body, no address), never raised."""
    try:
        await client.send(message)
    except EmailDeliveryError as exc:
        _logger.warning(
            "magic_link_email_failed",
            extra={"request_id": request_id, "reason": str(exc)},
        )


async def _deliver_nothing() -> None:
    """The no-match branch's background task -- explicit, so both branches register one."""
    return None


# ---------------------------------------------------------------------------
# POST /v1/auth/magic-link/request
# ---------------------------------------------------------------------------


class MagicLinkRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)


@router.post(
    "/request",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email the caller a one-time sign-in link.",
    description=(
        "Always 202, whether or not the email is registered -- the two cases "
        "are made to cost the same (docs/API_SPEC.md §4c). 404 when no email "
        "provider is configured."
    ),
)
async def magic_link_request(
    request: Request,
    body: MagicLinkRequest,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _demo_user_id: Annotated[str, Depends(rate_limited_demo)],
) -> dict[str, Any]:
    request_id = _request_id(request)
    client = _build_email_client(settings)
    if client is None:
        raise error_response(
            request_id=request_id, code=APIErrorCode.not_found, message="Not found."
        )
    email = normalize_email(request_id, body.email)
    # Dedicated bucket, applied BEFORE the lookup and on both branches.
    await enforce_magic_link_rate_limit(email, settings)

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    # Equal-cost prefix: generate + hash on both branches (cheap, but
    # identical), then ONE delete-by-user statement on both branches.
    secret = generate_token()
    secret_hash = hash_token(secret)
    token_id = uuid.uuid4()
    now = _now()
    target_user_id = user.user_id if user is not None else _NO_SUCH_USER_ID
    await db.execute(delete(MagicLinkToken).where(MagicLinkToken.user_id == target_user_id))

    deliver: Callable[[], Awaitable[None]]
    if user is not None:
        db.add(
            MagicLinkToken(
                token_id=token_id,
                secret_hash=secret_hash,
                user_id=user.user_id,
                created_at=now,
                expires_at=now + timedelta(seconds=settings.magic_link_ttl_seconds),
            )
        )
        # record_audit_event flushes: the token INSERT and the audit INSERT
        # go out together here. No email address in the metadata, ever.
        await record_audit_event(
            db,
            action=AuditAction.login,
            user_id=user.user_id,
            role=UserRole.demo_user,
            metadata={"event": "magic_link_requested"},
        )
        link = f"{_link_base_url(settings)}{_CONFIRM_PATH}?token={token_id.hex}.{secret}"
        # Send to the STORED address, not the raw input -- the stored one is
        # the canonical form the account was registered with.
        message = _build_message(
            to_addr=user.email or email, link=link, ttl_seconds=settings.magic_link_ttl_seconds
        )

        async def _send() -> None:
            await _deliver(client, message, request_id)

        deliver = _send
    else:
        # The discarded write: one more statement, the same round trip the
        # match branch spends on its token INSERT, deleting a row that can
        # never exist (a fresh random id). Then the same audit INSERT.
        await db.execute(delete(MagicLinkToken).where(MagicLinkToken.token_id == token_id))
        await record_audit_event(
            db,
            action=AuditAction.auth_failed,
            user_id=None,
            role=None,
            metadata={"event": "magic_link_unknown_email"},
        )
        deliver = _deliver_nothing

    await db.commit()
    background_tasks.add_task(deliver)
    return {"request_id": request_id, "status": "accepted"}


# ---------------------------------------------------------------------------
# GET /v1/auth/magic-link/confirm  -- the interstitial (read-only)
# ---------------------------------------------------------------------------


def _parse_token(value: str) -> tuple[uuid.UUID, str] | None:
    """Split ``<token_id hex>.<secret>``; ``None`` for anything malformed."""
    if not value or len(value) > _TOKEN_MAX_LENGTH:
        return None
    token_id_part, _, secret = value.partition(".")
    if not secret:
        return None
    try:
        token_id = uuid.UUID(hex=token_id_part)
    except ValueError:
        return None
    return token_id, secret


def _render_interstitial(token: str | None) -> str:
    """The plain-HTML confirm page. ``token`` is ``None`` for an invalid/expired link.

    Unstyled on purpose: the app-wide CSP (``app.core.security_headers``)
    allows no inline styles, and a stylesheet mount just for this page is
    not worth having. The ``<meta name="referrer">`` keeps the URL (which
    carries the credential) out of the Referer header of any link followed
    from this page; the response also carries ``Cache-Control: no-store``.
    """
    head = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        "<title>Sign in to CiteVyn</title></head><body><main>"
    )
    tail = "</main></body></html>\n"
    if token is None:
        body = (
            "<h1>This sign-in link is invalid or has expired</h1>"
            "<p>Links work once and expire 10 minutes after they are sent. "
            "Request a new one from the sign-in dialog.</p>"
            '<p><a href="/">Back to CiteVyn</a></p>'
        )
    else:
        body = (
            "<h1>Sign in to CiteVyn</h1>"
            "<p>Press the button to finish signing in. This link works once.</p>"
            f'<form method="post" action="{_CONFIRM_PATH}">'
            f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">'
            '<button type="submit">Continue to CiteVyn</button>'
            "</form>"
            "<p>If you didn't ask for this email, close this page and nothing happens.</p>"
        )
    return head + body + tail


@router.get(
    "/confirm",
    response_class=HTMLResponse,
    summary="Render the sign-in confirmation page for an emailed link.",
    description=(
        "Read-only: renders a form whose submit button POSTs the token. Never "
        "consumes the token, never sets a cookie -- so an email scanner's GET "
        "cannot burn the link (docs/API_SPEC.md §4c)."
    ),
)
async def magic_link_confirm_page(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _rate_limit: Annotated[None, Depends(rate_limited_oauth_navigation)],
    token: Annotated[str, Query(max_length=_TOKEN_MAX_LENGTH)] = "",
) -> HTMLResponse:
    del request, settings  # the per-visitor limiter already consumed both; nothing else is read
    parsed = _parse_token(token)
    valid = False
    if parsed is not None:
        token_id, secret = parsed
        row = await db.get(MagicLinkToken, token_id)
        valid = (
            row is not None
            and verify_token(secret, row.secret_hash)
            and _to_naive_utc(row.expires_at) > _to_naive_utc(_now())
        )
    return HTMLResponse(
        _render_interstitial(token if valid else None),
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# POST /v1/auth/magic-link/confirm  -- the claim
# ---------------------------------------------------------------------------


def _expected_origin(settings: Settings) -> str:
    parts = urlsplit(_link_base_url(settings))
    return f"{parts.scheme}://{parts.netloc}".lower()


def _origin_allowed(request: Request, settings: Settings) -> bool:
    """The login-CSRF guard described in the module docstring."""
    origin = request.headers.get("origin")
    if origin is not None:
        return origin.rstrip("/").lower() == _expected_origin(settings)
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site.lower() in ("same-origin", "none")
    return True


async def _form_token(request: Request) -> str:
    """Read ``token`` from an ``application/x-www-form-urlencoded`` body.

    Parsed with the stdlib rather than FastAPI's ``Form()`` because the
    latter needs ``python-multipart``, a dependency this one field does not
    justify. Anything unparseable collapses to ``""`` -> invalid.
    """
    raw = await request.body()
    values = parse_qs(raw[: _TOKEN_MAX_LENGTH * 4].decode("utf-8", "replace"))
    return values.get("token", [""])[0][:_TOKEN_MAX_LENGTH]


async def _claim_token(db: AsyncSession, token_id: uuid.UUID, secret: str) -> MagicLinkToken | None:
    """Atomically consume the token -- only if id AND secret both match.

    Baking the secret's digest into the ``WHERE`` clause is what makes a
    wrong guess delete nothing (no griefing of the real user's link), the
    same predicate-baking ``oauth.py``'s ``_claim_nonce`` does with the
    session binding. Commits, so the claim is durable before any login work
    happens. The digest is re-checked in constant time on the returned row
    as belt-and-braces.
    """
    claimed = await db.execute(
        delete(MagicLinkToken)
        .where(
            MagicLinkToken.token_id == token_id,
            MagicLinkToken.secret_hash == hash_token(secret),
        )
        .returning(MagicLinkToken)
    )
    row = claimed.scalar_one_or_none()
    await db.commit()
    if row is not None and not verify_token(secret, row.secret_hash):
        return None
    return row


@router.post(
    "/confirm",
    summary="Redeem an emailed sign-in link (form POST from the confirm page).",
    description=(
        "Always redirects, never JSON: /?auth=ok on success, /?auth=error "
        "otherwise. Single-use; 10-minute expiry (docs/API_SPEC.md §4c)."
    ),
)
async def magic_link_confirm(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_session)],
    _rate_limit: Annotated[None, Depends(rate_limited_oauth_navigation)],
) -> RedirectResponse:
    async def _fail(event: str) -> RedirectResponse:
        # user_id is None: nobody's identity is proven on any failure path.
        await record_audit_event(
            db,
            action=AuditAction.auth_failed,
            user_id=None,
            role=None,
            metadata={"event": event},
        )
        await db.commit()
        return RedirectResponse("/?auth=error", status_code=status.HTTP_302_FOUND)

    # 1. Origin check FIRST -- before anything is consumed.
    if not _origin_allowed(request, settings):
        return await _fail("magic_link_origin_rejected")

    # 2. Parse; garbage matches nothing and consumes nothing.
    parsed = _parse_token(await _form_token(request))
    if parsed is None:
        return await _fail("magic_link_invalid")
    token_id, secret = parsed

    # 3. Atomic conditional claim.
    row = await _claim_token(db, token_id, secret)
    if row is None:
        return await _fail("magic_link_invalid")
    if _to_naive_utc(row.expires_at) <= _to_naive_utc(_now()):
        return await _fail("magic_link_expired")

    # 4. Defense in depth: the FK cascade means a deleted user takes its
    #    tokens with it, but a race between that delete and this claim
    #    must fail closed rather than 500 inside claim_and_login.
    user = await db.get(User, row.user_id)
    if user is None:
        return await _fail("magic_link_invalid")

    # 5. The SAME login tail every other path uses (placeholder-then-mutate,
    #    as oauth.py's _handle_login_intent explains).
    redirect = RedirectResponse("about:blank", status_code=status.HTTP_302_FOUND)
    await claim_and_login(request, redirect, db, settings, user_id=user.user_id)
    await record_audit_event(
        db,
        action=AuditAction.login,
        user_id=user.user_id,
        role=UserRole.demo_user,
        metadata={"event": "magic_link"},
    )
    await db.commit()
    redirect.headers["location"] = "/?auth=ok"
    return redirect


__all__ = ["router"]
