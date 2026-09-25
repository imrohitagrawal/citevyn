"""``requires(capability)``: the one per-route authorisation check (ADR-0005 §2).

Every route declares the capability it needs::

    @router.post("", dependencies=[Depends(requires(Capability.chat))])

The route says WHAT it needs; :data:`app.access.policy.POLICY` says WHO has it.
No route checks a tier itself.

With ``CITEVYN_ACCESS_MODEL_ENABLED`` false (the default) the check returns at
once, before reading anything, so every route behaves exactly as it did before
ADR-0005 and no request pays for an extra database read. The label is still
there, and ``tests/test_route_auth_inventory.py`` fails for a route without
exactly one.

With the setting on:

* a capability anonymous callers have (``public``, ``sign_in``) passes without
  resolving anyone — those are the doors a visitor needs to BECOME signed in;
* ``operate`` passes too: it is granted by the admin API key, which the route's
  own ``rate_limited_admin`` dependency checks, never by a tier;
* anything else resolves the caller's tier from our own tables (never from the
  browser, never from Stripe) and refuses a tier without the capability —
  ``auth_required`` (401) for an anonymous caller, since signing in is what
  helps them, and ``plan_required`` (403) for a signed-in one.

Tier resolution today knows two tiers: no registered session is ``anonymous``,
a registered account is ``free``. ``pro`` arrives with the memberships table
(ADR-0005 Phase 4); until then no caller resolves to it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, Request

from app.access.policy import (
    CREDENTIAL_ONLY,
    Capability,
    Tier,
    has_capability,
    lowest_tier_with,
)
from app.core.auth_sessions import is_registered_principal, try_resolve_principal
from app.core.config import Settings, get_settings
from app.core.db import get_sessionmaker
from app.core.errors import APIErrorCode, error_response
from app.core.middleware import get_current_request_id

# The attribute the route inventory reads. A plain attribute on the dependency
# function, so the label is visible without calling anything.
_MARKER = "__citevyn_capability__"


async def resolve_tier(request: Request, settings: Settings) -> Tier:
    """The caller's tier, from OUR tables. Never mints an anonymous principal."""
    async with get_sessionmaker()() as db:
        principal = await try_resolve_principal(request, db, settings)
    if principal is not None and is_registered_principal(principal):
        return Tier.free
    return Tier.anonymous


def requires(capability: Capability) -> Callable[..., Awaitable[None]]:
    """Build the dependency that enforces ``capability`` (when the setting is on)."""

    async def check_capability(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> None:
        if not settings.access_model_enabled:
            return
        if capability in CREDENTIAL_ONLY or has_capability(Tier.anonymous, capability):
            return
        tier = await resolve_tier(request, settings)
        if has_capability(tier, capability):
            return
        needed = lowest_tier_with(capability)
        details = {
            "capability": capability.value,
            "required_tier": needed.value if needed else None,
        }
        request_id = str(getattr(request.state, "request_id", "") or get_current_request_id())
        if tier is Tier.anonymous:
            raise error_response(
                request_id=request_id,
                code=APIErrorCode.auth_required,
                message="Sign in to use this.",
                details=details,
            )
        raise error_response(
            request_id=request_id,
            code=APIErrorCode.plan_required,
            message="Your plan does not include this.",
            details=details,
        )

    setattr(check_capability, _MARKER, capability)
    return check_capability


def capability_of(dependency: Any) -> Capability | None:
    """The capability a dependency built by :func:`requires` enforces, else None."""
    value = getattr(dependency, _MARKER, None)
    return value if isinstance(value, Capability) else None


__all__ = ["capability_of", "requires", "resolve_tier"]
