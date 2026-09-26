"""Billing: Stripe checkout, portal, membership status and the webhook (ADR-0005 Phase 4).

**Off unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on.** While it is off, every route
here answers 404, exactly like a route that does not exist.

* ``POST /v1/billing/webhook`` — Stripe's deliveries. No bearer, no cookie: the
  Stripe signature over the RAW body is the credential, checked with a replay
  window (``app.billing.stripe_signature``). Each event id is applied once; a
  redelivery gets a 200 and changes nothing (Stripe retries until it sees a 2xx).
* ``POST /v1/billing/checkout`` — sends a signed-in account to Stripe Checkout for
  Pro, monthly or yearly. The account id is written into the subscription's
  metadata so every later webhook says whose it is.
* ``POST /v1/billing/portal`` — Stripe's customer portal: card, invoices, cancel
  at period end.
* ``GET /v1/billing/membership`` — the caller's tier and plan, for the UI.

Entitlement is never decided here or by the browser: ``resolve_tier`` reads the
``memberships`` table on every protected request.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import requires
from app.access.policy import Capability, Tier
from app.access.quota import usage_for
from app.billing.memberships import account_is_pro, apply_event, memberships_for, primary
from app.billing.stripe_client import (
    StripeError,
    create_checkout_session,
    create_portal_session,
    retrieve_subscription,
)
from app.billing.stripe_signature import SignatureError, verify_signature
from app.core.auth_sessions import resolve_principal
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import APIErrorCode, error_response
from app.models import User
from app.services.notifications import site_base_url

router = APIRouter(prefix="/v1/billing", tags=["billing"])
_logger = logging.getLogger("citevyn.billing")

# Stripe event payloads are a few KB; anything far larger is not one.
_MAX_WEBHOOK_BYTES = 1_000_000


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def billing_enabled(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> Settings:
    """404 unless the access model is on: 'off' must look like 'not there'."""
    if not settings.access_model_enabled:
        raise error_response(
            request_id=_request_id(request), code=APIErrorCode.not_found, message="Not found."
        )
    return settings


async def get_stripe_http() -> AsyncIterator[httpx.AsyncClient]:
    """The HTTP client Stripe calls go through. Tests override it with a mock."""
    async with httpx.AsyncClient() as client:
        yield client


class CheckoutRequest(BaseModel):
    interval: Literal["month", "year"]


def _stripe_unavailable(request: Request) -> Exception:
    # The same 503 whatever Stripe said: its error body can echo request fields.
    return error_response(
        request_id=_request_id(request),
        code=APIErrorCode.billing_unavailable,
        message="Billing is not available right now. Please try again later.",
    )


@router.post("/webhook", dependencies=[Depends(requires(Capability.public))])
async def stripe_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(billing_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
    http: Annotated[httpx.AsyncClient, Depends(get_stripe_http)],
) -> dict[str, Any]:
    request_id = _request_id(request)
    payload = await request.body()
    bad = error_response(
        request_id=request_id, code=APIErrorCode.validation_error, message="Invalid signature."
    )
    if len(payload) > _MAX_WEBHOOK_BYTES:
        raise bad
    try:
        verify_signature(
            payload,
            request.headers.get("stripe-signature", ""),
            settings.stripe_webhook_secret or "",
            tolerance_s=settings.stripe_webhook_tolerance_seconds,
            now=int(time.time()),
        )
        event: Any = json.loads(payload)
    except (SignatureError, ValueError) as exc:
        raise bad from exc
    if not isinstance(event, dict):
        raise bad
    secret_key = settings.stripe_secret_key
    if not secret_key:
        raise _stripe_unavailable(request)

    async def fetch(subscription_id: str) -> dict[str, Any]:
        # The event is a signal; the live subscription is what gets stored.
        return await retrieve_subscription(
            http,
            api_base=settings.stripe_api_base,
            secret_key=secret_key,
            subscription_id=subscription_id,
        )

    try:
        outcome = await apply_event(
            db,
            cast(dict[str, Any], event),
            fetch_subscription=fetch,
            grace_days=settings.membership_grace_days,
            now=datetime.now(UTC),
        )
    except StripeError as exc:
        # 5xx: Stripe retries, and nothing was logged, so the retry applies. Logged
        # here, because a PERMANENT failure (a revoked key, a key without
        # Subscriptions: read, a test/live mismatch) would otherwise fail every
        # event silently until Stripe gives up, leaving cancellations unapplied.
        _logger.warning(
            "billing_webhook_stripe_read_failed",
            extra={
                "event_type": str(cast(dict[str, Any], event).get("type"))[:64],
                "error": str(exc)[:120],
            },
        )
        raise _stripe_unavailable(request) from exc
    await db.commit()
    return {"received": True, "outcome": outcome}


@router.post("/checkout", dependencies=[Depends(requires(Capability.billing))])
async def start_checkout(
    request: Request,
    body: Annotated[CheckoutRequest, Body()],
    # The credential is checked BEFORE the setting, like every other route (the
    # route inventory's executed 401 checks); "off" is then a 404 to a caller who
    # authenticated.
    principal_id: Annotated[str, Depends(resolve_principal)],
    settings: Annotated[Settings, Depends(billing_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
    http: Annotated[httpx.AsyncClient, Depends(get_stripe_http)],
) -> dict[str, Any]:
    request_id = _request_id(request)
    if account_is_pro(await memberships_for(db, principal_id), datetime.now(UTC)):
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.already_subscribed,
            message="You already have Pro. Manage it from the billing portal.",
        )
    price = (
        settings.stripe_price_pro_monthly
        if body.interval == "month"
        else settings.stripe_price_pro_yearly
    )
    user = await db.get(User, principal_id)
    if not settings.stripe_secret_key or not price or user is None:
        raise _stripe_unavailable(request)
    email = user.email
    # Release the database connection before the Stripe call (up to 15 s): a slow
    # Stripe must not hold pooled connections.
    await db.close()
    site = site_base_url(settings)
    try:
        url = await create_checkout_session(
            http,
            api_base=settings.stripe_api_base,
            secret_key=settings.stripe_secret_key,
            price_id=price,
            account_id=principal_id,
            customer_email=email,
            success_url=f"{site}/?billing=success",
            cancel_url=f"{site}/?billing=cancel",
        )
    except StripeError as exc:
        raise _stripe_unavailable(request) from exc
    return {"request_id": request_id, "url": url}


@router.post("/portal", dependencies=[Depends(requires(Capability.billing))])
async def open_portal(
    request: Request,
    principal_id: Annotated[str, Depends(resolve_principal)],
    settings: Annotated[Settings, Depends(billing_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
    http: Annotated[httpx.AsyncClient, Depends(get_stripe_http)],
) -> dict[str, Any]:
    request_id = _request_id(request)
    rows = await memberships_for(db, principal_id)
    # The subscription that keeps billing them first (two checkout tabs can make
    # two Stripe customers), then any row that has a customer.
    lead = primary(rows, datetime.now(UTC))
    customer_id = (lead.stripe_customer_id if lead else None) or next(
        (m.stripe_customer_id for m in rows if m.stripe_customer_id), None
    )
    if customer_id is None:
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.not_found,
            message="No billing account yet.",
        )
    if not settings.stripe_secret_key:
        raise _stripe_unavailable(request)
    await db.close()  # not held across the Stripe call
    try:
        url = await create_portal_session(
            http,
            api_base=settings.stripe_api_base,
            secret_key=settings.stripe_secret_key,
            customer_id=customer_id,
            return_url=f"{site_base_url(settings)}/",
        )
    except StripeError as exc:
        raise _stripe_unavailable(request) from exc
    return {"request_id": request_id, "url": url}


@router.get("/membership", dependencies=[Depends(requires(Capability.billing))])
async def get_membership(
    request: Request,
    principal_id: Annotated[str, Depends(resolve_principal)],
    settings: Annotated[Settings, Depends(billing_enabled)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    now = datetime.now(UTC)
    rows = await memberships_for(db, principal_id)
    m = primary(rows, now)
    tier = Tier.pro if account_is_pro(rows, now) else Tier.free
    usage = await usage_for(db, principal_id, tier, settings, now)

    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "request_id": _request_id(request),
        "tier": tier.value,
        # The subscription that decides the tier (or the latest one).
        "status": m.status if m else None,
        "interval": m.billing_interval if m else None,
        "current_period_end": iso(m.current_period_end) if m else None,
        "cancel_at_period_end": m.cancel_at_period_end if m else False,
        "grace_until": iso(m.grace_until) if m else None,
        # Every subscription, so paying twice (two checkout tabs) is visible.
        "subscriptions": [
            {
                "status": r.status,
                "interval": r.billing_interval,
                "current_period_end": iso(r.current_period_end),
            }
            for r in rows
        ],
        "allowance": {
            "free_trial_answers": settings.access_free_trial_answers,
            "pro_monthly_answers": settings.access_pro_monthly_answers,
        },
        # Where this account stands: answered questions used and left.
        "usage": usage.as_dict(),
    }


__all__ = ["billing_enabled", "get_stripe_http", "router"]
