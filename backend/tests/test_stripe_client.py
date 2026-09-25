"""The Stripe REST client (ADR-0005 Phase 4): every failure is a StripeError that
never carries the key or Stripe's error body. Each test says which change turns it red."""

from __future__ import annotations

import httpx
import pytest

from app.billing.stripe_client import StripeError, create_checkout_session, create_portal_session

KW = {"api_base": "https://api.stripe.com", "secret_key": "sk_test_secret"}


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _checkout(client: httpx.AsyncClient) -> str:
    return await create_checkout_session(
        client,
        **KW,
        price_id="price_1",
        account_id="usr_1",
        customer_email=None,
        success_url="https://s/ok",
        cancel_url="https://s/no",
    )


@pytest.mark.parametrize(
    ("handler", "why"),
    [
        (lambda r: httpx.Response(200, json=["not", "an", "object"]), "non-object body"),
        (lambda r: httpx.Response(200, json={"id": "cs_1"}), "no url"),
        (lambda r: httpx.Response(200, json={"url": "http://insecure"}), "not https"),
        (lambda r: httpx.Response(400, json={"error": {"message": "sk_test_secret bad"}}), "4xx"),
    ],
)
async def test_every_bad_answer_is_a_stripe_error(handler, why: str) -> None:  # type: ignore[no-untyped-def]
    """Turns red if: any of these returns a URL or raises something else."""
    with pytest.raises(StripeError) as exc:
        await _checkout(_client(handler))
    assert "sk_test_secret" not in str(exc.value)


async def test_an_unreachable_stripe_is_a_stripe_error() -> None:
    """Turns red if: a transport error escapes as httpx's own exception."""

    def down(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(StripeError):
        await _checkout(_client(down))


async def test_the_portal_needs_an_https_url_back() -> None:
    """Turns red if: a portal response without a usable URL is accepted."""
    with pytest.raises(StripeError):
        await create_portal_session(
            _client(lambda r: httpx.Response(200, json={"id": "bps_1"})),
            **KW,
            customer_id="cus_1",
            return_url="https://s/",
        )


async def test_no_customer_email_is_sent_when_there_is_none() -> None:
    """An OAuth account may have no email. Turns red if: an empty email is sent."""
    seen: list[bytes] = []

    def ok(r: httpx.Request) -> httpx.Response:
        seen.append(r.content)
        return httpx.Response(200, json={"url": "https://checkout.stripe.com/x"})

    await _checkout(_client(ok))
    assert b"customer_email" not in seen[0]
