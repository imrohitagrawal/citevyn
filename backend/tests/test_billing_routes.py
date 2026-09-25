"""Billing routes and Pro entitlement end to end (ADR-0005 Phase 4).

* ``POST /v1/billing/webhook`` — Stripe's deliveries. No credential; the
  signature is the credential. Forged, replayed and stale deliveries are refused.
* ``POST /v1/billing/checkout`` / ``/portal`` — send a signed-in account to
  Stripe (test mode here: every Stripe call is a MockTransport, no network).
* ``GET /v1/billing/membership`` — what the UI shows.

Everything is OFF with ``CITEVYN_ACCESS_MODEL_ENABLED`` false: every billing route
answers 404, exactly like a route that does not exist.
Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Generator
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import billing
from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import Base, Membership, StripeEvent

DEMO = {"Authorization": "Bearer local-demo-key"}
SECRET = "whsec_test"
ON = {
    "CITEVYN_ACCESS_MODEL_ENABLED": "true",
    "CITEVYN_STRIPE_SECRET_KEY": "sk_test_x",
    "CITEVYN_STRIPE_WEBHOOK_SECRET": SECRET,
    "CITEVYN_STRIPE_PRICE_PRO_MONTHLY": "price_month",
    "CITEVYN_STRIPE_PRICE_PRO_YEARLY": "price_year",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Generator[pytest.MonkeyPatch, None, None]:
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    get_settings.cache_clear()
    rate_limit.reset_limiter()

    async def _init() -> None:
        async with db_module.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield monkeypatch
    get_settings.cache_clear()
    db_module.reset_engine()
    rate_limit.reset_limiter()


def _on(mp: pytest.MonkeyPatch) -> None:
    for k, v in ON.items():
        mp.setenv(k, v)
    get_settings.cache_clear()


def _signed(event: dict[str, Any], t: int | None = None, secret: str = SECRET) -> tuple[bytes, str]:
    body = json.dumps(event).encode()
    t = int(time.time()) if t is None else t
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={t},v1={sig}"


def _post_event(client: TestClient, event: dict[str, Any], **kw: Any) -> Any:
    body, header = _signed(event, **kw)
    return client.post(
        "/v1/billing/webhook",
        content=body,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )


def _register(client: TestClient, email: str = "a@example.com") -> str:
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct horse battery"},
        headers=DEMO,
    )
    assert res.status_code == 201
    return res.json()["user_id"]


def _sub_event(event_id: str, account: str, status: str = "active", **extra: Any) -> dict[str, Any]:
    """A subscription event, and the matching LIVE subscription in the fake Stripe."""
    obj = {
        "id": "sub_1",
        "customer": "cus_1",
        "status": status,
        "cancel_at_period_end": False,
        "current_period_end": int(time.time()) + 30 * 86400,
        "metadata": {"account_id": account},
        "items": {"data": [{"price": {"recurring": {"interval": "month"}}}]},
    }
    obj.update(extra)
    LIVE[str(obj["id"])] = dict(obj)
    return {
        "id": event_id,
        "type": "customer.subscription.updated",
        "created": int(time.time()),
        "data": {"object": obj},
    }


def _rows(model: Any) -> list[Any]:
    async def _run() -> list[Any]:
        async with get_sessionmaker()() as s:
            return list((await s.execute(select(model))).scalars().all())

    return asyncio.run(_run())


# A fake Stripe: the live state of each subscription (what GET /v1/subscriptions
# returns), and every request made. The webhook stores what THIS says, not what
# the event payload says.
LIVE: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def _clear_live() -> None:
    LIVE.clear()


def _stripe_mock(calls: list[httpx.Request], url: str = "https://checkout.stripe.com/c/pay/cs_1"):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if request.method == "GET" and path.startswith("/v1/subscriptions/"):
            sub = LIVE.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=sub) if sub else httpx.Response(404, json={})
        return httpx.Response(200, json={"id": "x", "url": url})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _app(calls: list[httpx.Request] | None = None, **mock_kw: Any) -> Any:
    """The real app, with Stripe replaced by the fake."""
    app = create_app()
    app.dependency_overrides[billing.get_stripe_http] = lambda: _stripe_mock(
        calls if calls is not None else [], **mock_kw
    )
    return app


# ---------------------------------------------------------------------------
# Off means off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/billing/webhook"),
        ("POST", "/v1/billing/checkout"),
        ("POST", "/v1/billing/portal"),
        ("GET", "/v1/billing/membership"),
    ],
)
def test_with_the_setting_off_every_billing_route_is_a_404(
    env: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """Turns red if: any billing route acts while the access model is off."""
    with TestClient(_app()) as client:
        res = client.request(method, path, headers=DEMO, json={"interval": "month"})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------


def test_a_signed_subscription_event_makes_the_account_pro(env: pytest.MonkeyPatch) -> None:
    """The whole entitlement path: webhook -> table -> tier. Turns red if: the
    event is not applied, or the tier is not read from the table."""
    _on(env)
    with TestClient(_app()) as client:
        account = _register(client)
        before = client.get("/v1/billing/membership", headers=DEMO).json()
        res = _post_event(client, _sub_event("evt_1", account))
        after = client.get("/v1/billing/membership", headers=DEMO).json()
    assert res.status_code == 200 and res.json()["outcome"] == "applied"
    assert before["tier"] == "free"
    assert after["tier"] == "pro"
    assert after["status"] == "active" and after["interval"] == "month"


def test_pro_unlocks_a_pro_capability_through_the_real_check(env: pytest.MonkeyPatch) -> None:
    """Reaching a Pro feature without a membership fails; with one it passes.
    Turns red if: resolve_tier never returns pro."""
    from fastapi import Depends

    from app.access.deps import requires
    from app.access.policy import Capability

    _on(env)
    app = _app()

    @app.get("/v1/_probe_pro", dependencies=[Depends(requires(Capability.mcp_ask))])
    async def _probe() -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as client:
        account = _register(client)
        refused = client.get("/v1/_probe_pro", headers=DEMO)
        _post_event(client, _sub_event("evt_1", account))
        allowed = client.get("/v1/_probe_pro", headers=DEMO)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "plan_required"
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    "tamper",
    ["wrong_secret", "edited_body", "old_timestamp", "no_header"],
)
def test_a_forged_or_replayed_delivery_is_refused_and_changes_nothing(
    env: pytest.MonkeyPatch, tamper: str
) -> None:
    """Turns red if: the route applies an event without a valid, fresh signature."""
    _on(env)
    with TestClient(_app()) as client:
        account = _register(client)
        event = _sub_event("evt_1", account)
        if tamper == "wrong_secret":
            body, header = _signed(event, secret="whsec_attacker")
        elif tamper == "old_timestamp":
            body, header = _signed(event, t=int(time.time()) - 3600)
        else:
            body, header = _signed(event)
        if tamper == "edited_body":
            body = body.replace(b'"active"', b'"trialing"')
        headers = {"Content-Type": "application/json"}
        if tamper != "no_header":
            headers["Stripe-Signature"] = header
        res = client.post("/v1/billing/webhook", content=body, headers=headers)
        tier = client.get("/v1/billing/membership", headers=DEMO).json()["tier"]
    # 422 validation_error: Stripe treats any non-2xx as "not delivered".
    assert res.status_code == 422
    assert res.json()["error"]["message"] == "Invalid signature."
    assert tier == "free"
    assert _rows(Membership) == [] and _rows(StripeEvent) == []


def test_a_redelivered_event_is_acknowledged_but_not_reapplied(env: pytest.MonkeyPatch) -> None:
    """Stripe retries until it sees a 2xx, so a duplicate must succeed AND do
    nothing. Turns red if: the duplicate is refused (Stripe would keep retrying)
    or re-applied."""
    _on(env)
    with TestClient(_app()) as client:
        account = _register(client)
        first = _post_event(client, _sub_event("evt_1", account))
        again = _post_event(client, _sub_event("evt_1", account, status="canceled"))
        tier = client.get("/v1/billing/membership", headers=DEMO).json()["tier"]
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json()["outcome"] == "duplicate"
    assert tier == "pro"


def test_the_webhook_needs_no_bearer_or_cookie(env: pytest.MonkeyPatch) -> None:
    """Stripe calls it directly. Turns red if: the route demands the web client's
    credentials (every real delivery would fail)."""
    _on(env)
    with TestClient(_app()) as client:
        body, header = _signed(
            {"id": "evt_z", "type": "customer.created", "created": 1, "data": {"object": {}}}
        )
        res = client.post("/v1/billing/webhook", content=body, headers={"Stripe-Signature": header})
    assert res.status_code == 200
    assert res.json()["outcome"] == "ignored"


# ---------------------------------------------------------------------------
# Checkout and portal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("interval", "price"), [("month", "price_month"), ("year", "price_year")])
def test_checkout_sends_the_account_to_stripe_with_the_right_price(
    env: pytest.MonkeyPatch, interval: str, price: str
) -> None:
    """Turns red if: the wrong price is used, the account id is not written to
    the subscription metadata (the webhook could not match it), or the secret key
    is not the credential used."""
    _on(env)
    calls: list[httpx.Request] = []
    app = create_app()
    app.dependency_overrides[billing.get_stripe_http] = lambda: _stripe_mock(calls)
    with TestClient(app) as client:
        account = _register(client)
        res = client.post("/v1/billing/checkout", json={"interval": interval}, headers=DEMO)
    assert res.status_code == 200
    assert res.json()["url"] == "https://checkout.stripe.com/c/pay/cs_1"
    [call] = calls
    assert str(call.url) == "https://api.stripe.com/v1/checkout/sessions"
    assert call.headers["authorization"] == "Bearer sk_test_x"
    form = {k: v[0] for k, v in parse_qs(call.content.decode()).items()}
    assert form["line_items[0][price]"] == price
    assert form["subscription_data[metadata][account_id]"] == account
    assert form["client_reference_id"] == account
    assert form["customer_email"] == "a@example.com"


def test_checkout_is_for_accounts_only(env: pytest.MonkeyPatch) -> None:
    """Turns red if: an anonymous visitor can start a subscription with no
    account to attach it to."""
    _on(env)
    with TestClient(_app()) as client:
        client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO)
        res = client.post("/v1/billing/checkout", json={"interval": "month"}, headers=DEMO)
    assert res.status_code == 401


def test_an_existing_pro_account_is_sent_to_the_portal_not_a_second_checkout(
    env: pytest.MonkeyPatch,
) -> None:
    """Turns red if: a Pro account can start a second, double-billed subscription."""
    _on(env)
    calls: list[httpx.Request] = []
    app = create_app()
    app.dependency_overrides[billing.get_stripe_http] = lambda: _stripe_mock(calls)
    with TestClient(app) as client:
        account = _register(client)
        _post_event(client, _sub_event("evt_1", account))
        res = client.post("/v1/billing/checkout", json={"interval": "month"}, headers=DEMO)
    assert res.status_code == 409
    assert [c for c in calls if c.method == "POST"] == []  # no checkout session made


def test_the_portal_opens_for_a_customer_and_is_a_404_before_there_is_one(
    env: pytest.MonkeyPatch,
) -> None:
    """Turns red if: the portal is opened for the wrong customer, or for an
    account that never paid."""
    _on(env)
    calls: list[httpx.Request] = []
    app = create_app()
    app.dependency_overrides[billing.get_stripe_http] = lambda: _stripe_mock(
        calls, "https://billing.stripe.com/p/session/x"
    )
    with TestClient(app) as client:
        account = _register(client)
        before = client.post("/v1/billing/portal", headers=DEMO)
        _post_event(client, _sub_event("evt_1", account))
        res = client.post("/v1/billing/portal", headers=DEMO)
    assert before.status_code == 404
    assert res.status_code == 200 and res.json()["url"].startswith("https://billing.stripe.com/")
    [portal] = [c for c in calls if c.method == "POST"]
    form = {k: v[0] for k, v in parse_qs(portal.content.decode()).items()}
    assert form["customer"] == "cus_1"


def test_a_stripe_failure_is_a_503_and_never_leaks_the_key(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a Stripe error becomes a 500, or its detail reaches the client."""
    _on(env)

    def boom(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "card declined sk_test_x"}})

    app = create_app()
    app.dependency_overrides[billing.get_stripe_http] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(boom)
    )
    with TestClient(app) as client:
        _register(client)
        res = client.post("/v1/billing/checkout", json={"interval": "month"}, headers=DEMO)
    assert res.status_code == 503
    assert "sk_test" not in res.text


def test_an_unknown_interval_is_refused(env: pytest.MonkeyPatch) -> None:
    _on(env)
    with TestClient(_app()) as client:
        _register(client)
        res = client.post("/v1/billing/checkout", json={"interval": "week"}, headers=DEMO)
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Production configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", list(ON)[1:])
def test_production_with_the_access_model_on_needs_every_stripe_setting(missing: str) -> None:
    """Turning the model on in production without Stripe configured must fail at
    startup, not at the first checkout. Turns red if: a setting is not required."""
    from pydantic import ValidationError

    from app.core.config import Settings

    field = missing.removeprefix("CITEVYN_").lower()
    kw: dict[str, Any] = {
        "environment": "production",
        "llm_provider": "gemini",
        "gemini_api_key": "gk",
        "admin_api_key": "a-strong-admin-secret",
        "public_client_token": "a-strong-demo-secret",
        "access_model_enabled": True,
        "_env_file": None,
    }
    for k in list(ON)[1:]:
        kw[k.removeprefix("CITEVYN_").lower()] = ON[k]
    kw[field] = None
    with pytest.raises(ValidationError, match="Stripe"):
        Settings(**kw)
    kw[field] = ON[missing]
    assert Settings(**kw).access_model_enabled is True  # partner: complete config starts


def test_an_oversize_or_non_object_webhook_body_is_refused(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a body over the size cap, or a signed JSON value that is not
    an object, is processed."""
    _on(env)
    with TestClient(_app()) as client:
        big = b"{" + b" " * 1_000_001 + b"}"
        t = int(time.time())
        sig = hmac.new(SECRET.encode(), f"{t}.".encode() + big, hashlib.sha256).hexdigest()
        oversize = client.post(
            "/v1/billing/webhook", content=big, headers={"Stripe-Signature": f"t={t},v1={sig}"}
        )
        body = b"[1, 2]"
        sig = hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
        array = client.post(
            "/v1/billing/webhook", content=body, headers={"Stripe-Signature": f"t={t},v1={sig}"}
        )
    assert (oversize.status_code, array.status_code) == (422, 422)


@pytest.mark.parametrize("unset", ["CITEVYN_STRIPE_SECRET_KEY", "CITEVYN_STRIPE_PRICE_PRO_MONTHLY"])
def test_checkout_without_stripe_configured_is_a_503(env: pytest.MonkeyPatch, unset: str) -> None:
    """Outside production the model can be on without Stripe (production refuses
    to start). Turns red if: a missing key or price reaches Stripe or 500s."""
    _on(env)
    env.delenv(unset)
    get_settings.cache_clear()
    calls: list[httpx.Request] = []
    app = create_app()
    app.dependency_overrides[billing.get_stripe_http] = lambda: _stripe_mock(calls)
    with TestClient(app) as client:
        _register(client)
        res = client.post("/v1/billing/checkout", json={"interval": "month"}, headers=DEMO)
    assert res.status_code == 503
    assert calls == []


@pytest.mark.parametrize("failure", ["no_key", "stripe_error"])
def test_the_portal_fails_closed(env: pytest.MonkeyPatch, failure: str) -> None:
    """Turns red if: the portal 500s or calls Stripe without a key."""
    _on(env)
    calls: list[httpx.Request] = []

    def boom(r: httpx.Request) -> httpx.Response:
        # Stripe answers the webhook's subscription read, then fails the portal call.
        if r.method == "GET":
            return httpx.Response(200, json=LIVE[r.url.path.rsplit("/", 1)[1]])
        calls.append(r)
        return httpx.Response(500)

    app = create_app()
    app.dependency_overrides[billing.get_stripe_http] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(boom)
    )
    with TestClient(app) as client:
        account = _register(client)
        _post_event(client, _sub_event("evt_1", account))
        if failure == "no_key":
            env.delenv("CITEVYN_STRIPE_SECRET_KEY")
            get_settings.cache_clear()
        res = client.post("/v1/billing/portal", headers=DEMO)
    assert res.status_code == 503
    assert (calls == []) is (failure == "no_key")


def test_the_webhook_stores_the_live_subscription_not_the_payload(env: pytest.MonkeyPatch) -> None:
    """Stripe: 'Don't use created to determine event order ... use the API to
    retrieve any missing objects.' Turns red if: the route applies the payload
    instead of reading the subscription."""
    _on(env)
    calls: list[httpx.Request] = []
    with TestClient(_app(calls)) as client:
        account = _register(client)
        event = _sub_event("evt_1", account, status="incomplete")
        LIVE["sub_1"]["status"] = "active"  # what is true now
        res = _post_event(client, event)
        tier = client.get("/v1/billing/membership", headers=DEMO).json()["tier"]
    assert res.json()["outcome"] == "applied"
    assert tier == "pro"
    assert [c.url.path for c in calls if c.method == "GET"] == ["/v1/subscriptions/sub_1"]


def test_stripe_unreachable_during_a_webhook_is_a_503_and_logs_nothing(
    env: pytest.MonkeyPatch,
) -> None:
    """A 5xx makes Stripe retry; logging the event anyway would turn that retry
    into an ignored duplicate. Turns red if: a failed read returns 2xx, or logs."""
    _on(env)
    with TestClient(_app()) as client:
        account = _register(client)
        event = _sub_event("evt_1", account)
        LIVE.clear()  # Stripe has no answer (404) for this read
        res = _post_event(client, event)
    assert res.status_code == 503
    assert _rows(StripeEvent) == [] and _rows(Membership) == []


def test_a_webhook_without_the_secret_key_configured_is_a_503(env: pytest.MonkeyPatch) -> None:
    """The live read needs the key. Outside production the model can be on
    without it (production refuses to start). Turns red if: the route proceeds
    without a key, or returns 2xx (Stripe would stop retrying)."""
    _on(env)
    env.delenv("CITEVYN_STRIPE_SECRET_KEY")
    get_settings.cache_clear()
    with TestClient(_app()) as client:
        account = _register(client)
        res = _post_event(client, _sub_event("evt_1", account))
    assert res.status_code == 503
    assert _rows(StripeEvent) == []
