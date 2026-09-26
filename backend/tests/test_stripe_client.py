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


async def test_retrieve_subscription_gets_the_live_object() -> None:
    """Webhooks are only a signal; the stored state comes from the live object
    (Stripe: do not use event order). Turns red if: the wrong URL or key is used."""
    from app.billing.stripe_client import retrieve_subscription

    seen: list[httpx.Request] = []

    def ok(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"id": "sub_ABC123", "status": "active"})

    sub = await retrieve_subscription(_client(ok), **KW, subscription_id="sub_ABC123")
    assert sub["status"] == "active"
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "https://api.stripe.com/v1/subscriptions/sub_ABC123"
    assert seen[0].headers["authorization"] == "Bearer sk_test_secret"


@pytest.mark.parametrize("bad", ["../v1/customers", "sub_1/../../x", "", "cus_1", "sub_1?expand=x"])
async def test_retrieve_subscription_refuses_a_non_subscription_id(bad: str) -> None:
    """The id goes into a URL path. Turns red if: a crafted id can reach another
    Stripe endpoint."""
    from app.billing.stripe_client import retrieve_subscription

    with pytest.raises(StripeError):
        await retrieve_subscription(
            _client(lambda r: httpx.Response(200, json={})), **KW, subscription_id=bad
        )


async def test_a_non_json_success_body_is_a_stripe_error() -> None:
    """Turns red if: a 2xx that is not JSON escapes as ValueError (a 500)."""
    with pytest.raises(StripeError):
        await _checkout(_client(lambda r: httpx.Response(200, content=b"<html>")))


async def test_retrieve_subscription_unreachable_is_a_stripe_error() -> None:
    """Turns red if: a transport error on the live read escapes as httpx's own."""
    from app.billing.stripe_client import retrieve_subscription

    def down(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(StripeError):
        await retrieve_subscription(_client(down), **KW, subscription_id="sub_1")


async def test_the_default_stripe_client_dependency_yields_a_real_client() -> None:
    """The production dependency (tests replace it). No request is sent.
    Turns red if: it yields something other than an httpx client, or leaks it."""
    from app.api.routes.billing import get_stripe_http

    gen = get_stripe_http()
    client = await gen.__anext__()
    assert isinstance(client, httpx.AsyncClient) and not client.is_closed
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert client.is_closed
