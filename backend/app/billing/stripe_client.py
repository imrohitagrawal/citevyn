"""The two Stripe API calls CiteVyn makes. Pure mechanism: no app imports (AGENTS.md).

Called directly over HTTPS with ``httpx`` (already a dependency) rather than
through Stripe's SDK: two form-encoded POSTs do not justify a new dependency, and
an injectable ``httpx`` client lets every test run against ``MockTransport`` with
no network and no Stripe account.

These calls happen only when a signed-in user CHOOSES to upgrade or manage
billing. No answer, and no entitlement check, ever waits on Stripe (ADR-0005 §2):
entitlement is read from our own ``memberships`` table, which Stripe's signed
webhooks keep current.
"""

from __future__ import annotations

import re
from typing import Any

import httpx


class StripeError(Exception):
    """Stripe refused the call or could not be reached. Never carries the key."""


async def _post(
    client: httpx.AsyncClient, *, api_base: str, secret_key: str, path: str, form: dict[str, str]
) -> dict[str, Any]:
    try:
        res = await client.post(
            f"{api_base.rstrip('/')}{path}",
            data=form,
            headers={"Authorization": f"Bearer {secret_key}"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise StripeError(f"Stripe unreachable: {type(exc).__name__}") from exc
    return _body(res)


def _body(res: httpx.Response) -> dict[str, Any]:
    if res.status_code >= 400:
        # The status only: Stripe's error body can echo request fields.
        raise StripeError(f"Stripe returned {res.status_code}")
    try:
        body: Any = res.json()
    except ValueError as exc:
        raise StripeError("Stripe returned a body that is not JSON") from exc
    if not isinstance(body, dict):
        raise StripeError("Stripe returned a non-object body")
    return body  # type: ignore[return-value]


# A subscription id goes into a URL path, so only the shape Stripe issues.
_SUBSCRIPTION_ID = re.compile(r"sub_[A-Za-z0-9]{1,255}")


async def retrieve_subscription(
    client: httpx.AsyncClient, *, api_base: str, secret_key: str, subscription_id: str
) -> dict[str, Any]:
    """The live Subscription object.

    Webhooks tell us THAT something changed; this tells us what is true now.
    Stripe's docs: "Don't use ``created`` to determine event order ... use the API
    to retrieve any missing objects." Called only on the webhook path; no user
    request ever waits on it.
    """
    if not _SUBSCRIPTION_ID.fullmatch(subscription_id):
        raise StripeError("not a subscription id")
    try:
        res = await client.get(
            f"{api_base.rstrip('/')}/v1/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {secret_key}"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise StripeError(f"Stripe unreachable: {type(exc).__name__}") from exc
    return _body(res)


async def create_checkout_session(
    client: httpx.AsyncClient,
    *,
    api_base: str,
    secret_key: str,
    price_id: str,
    account_id: str,
    customer_email: str | None,
    success_url: str,
    cancel_url: str,
) -> str:
    """Start a subscription checkout; return the URL to send the user to.

    ``account_id`` is written to BOTH the session's ``client_reference_id`` and the
    SUBSCRIPTION's metadata, so every later ``customer.subscription.*`` event says
    whose it is, without depending on the order Stripe delivers events in.
    """
    form = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "client_reference_id": account_id,
        "subscription_data[metadata][account_id]": account_id,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    if customer_email:
        form["customer_email"] = customer_email
    body = await _post(
        client, api_base=api_base, secret_key=secret_key, path="/v1/checkout/sessions", form=form
    )
    url = body.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise StripeError("Stripe returned no checkout URL")
    return url


async def create_portal_session(
    client: httpx.AsyncClient,
    *,
    api_base: str,
    secret_key: str,
    customer_id: str,
    return_url: str,
) -> str:
    """Open Stripe's customer portal (change card, cancel at period end)."""
    body = await _post(
        client,
        api_base=api_base,
        secret_key=secret_key,
        path="/v1/billing_portal/sessions",
        form={"customer": customer_id, "return_url": return_url},
    )
    url = body.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise StripeError("Stripe returned no portal URL")
    return url


__all__ = [
    "StripeError",
    "create_checkout_session",
    "create_portal_session",
    "retrieve_subscription",
]
