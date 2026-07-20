"""Route-level proof that the demo limit is PER VISITOR (#203).

Why this file exists, separately from ``test_client_rate_key.py``:

Those tests exercise ``client_rate_key`` and the limiter directly. That is useful,
but it left a hole big enough to drive the original bug through — reverting
``rate_limited_demo`` to key on the constant ``user_id`` again (i.e. restoring
#203 exactly) did NOT fail a single one of them, because none of them went through
the dependency. Mutation-tested and confirmed: the guard was passing while the
production wiring was reverted.

So these tests drive the REAL FastAPI dependency over the REAL route with real
headers, which is the only place the wiring is observable. If someone re-keys the
limiter on a constant, the first test here goes red.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.core import db as db_module
from app.core.config import get_settings
from app.main import create_app
from app.models import Base

DEMO_BEARER = "Bearer local-demo-key"
# Low enough that a test can exhaust it without a slow loop.
LIMIT = 3


@pytest.fixture
def limited_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Generator[TestClient, None, None]:
    """A TestClient with rate limiting ON and a small per-visitor cap.

    The default suite runs with the limiter effectively out of the way; here it is
    the subject, so it is switched on deliberately.
    """
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "per_visitor.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_DEMO_USER_PER_HOUR", str(LIMIT))
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_CLIENT_IP_HEADER", "Fly-Client-IP")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_KEY_SALT", "route-test-salt")
    # Keep the shared backstop far away so it cannot be what trips these tests.
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_GLOBAL_PER_HOUR", "10000")
    get_settings.cache_clear()
    rate_limit.reset_limiter()

    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    import asyncio

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_init_schema())

    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()
        rate_limit.reset_limiter()


def _create_session(client: TestClient, ip: str):
    return client.post(
        "/v1/sessions",
        json={"user_id": "demo_user", "channel": "chat"},
        headers={"Authorization": DEMO_BEARER, "Fly-Client-IP": ip},
    )


def test_one_visitor_exhausting_the_quota_does_not_lock_out_another(
    limited_client: TestClient,
) -> None:
    """THE #203 regression, at the level where the bug actually lived.

    Visitor A burns the whole allowance; visitor B must still be served. Before the
    fix both keyed on the constant ``demo_user`` and B was locked out.
    """
    for _ in range(LIMIT):
        assert _create_session(limited_client, "203.0.113.7").status_code == 201

    # A is now over the limit.
    assert _create_session(limited_client, "203.0.113.7").status_code == 429

    # B is a different visitor and is unaffected.
    assert _create_session(limited_client, "198.51.100.4").status_code == 201


def test_a_single_visitor_is_still_rate_limited(limited_client: TestClient) -> None:
    """Non-vacuity: the separation must not have simply disabled the limit."""
    for _ in range(LIMIT):
        assert _create_session(limited_client, "203.0.113.9").status_code == 201

    response = _create_session(limited_client, "203.0.113.9")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"


def test_one_ipv6_slash_64_is_treated_as_a_single_visitor(
    limited_client: TestClient,
) -> None:
    """A customer gets a whole /64, so per-address limiting would be free to evade."""
    for i in range(LIMIT):
        addr = f"2001:db8:abcd:1234::{i + 1}"
        assert _create_session(limited_client, addr).status_code == 201

    # A different address in the SAME /64 is the same visitor, and is now limited.
    over = _create_session(limited_client, "2001:db8:abcd:1234:ffff::9")
    assert over.status_code == 429

    # A different /64 is a different visitor.
    assert _create_session(limited_client, "2001:db8:abcd:5678::1").status_code == 201


def test_requests_without_the_header_share_one_fallback_bucket(
    limited_client: TestClient,
) -> None:
    """Fail CLOSED: an absent address must not mint a fresh allowance per request.

    In the TestClient the socket peer is constant, so these all land in one bucket —
    which is the safe direction. The dangerous alternative would be each request
    getting its own key and never being limited at all.
    """
    codes = [
        limited_client.post(
            "/v1/sessions",
            json={"user_id": "demo_user", "channel": "chat"},
            headers={"Authorization": DEMO_BEARER},
        ).status_code
        for _ in range(LIMIT + 1)
    ]
    assert 429 in codes, (
        "requests with no client address were never limited — the fallback is "
        "handing out a fresh bucket per request"
    )


def test_global_backstop_binds_across_distinct_visitors(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The shared backstop must actually be enforced on the request path.

    Found by adversarial review: deleting the backstop call from
    ``rate_limited_demo`` outright left the WHOLE suite green (1274 passed). The
    only test that named it built a limiter by hand and called ``check`` itself,
    which proves the limiter CAN hold a global bucket, never that any request
    uses one — and the route tests deliberately set the backstop out of reach.

    So: set the global limit BELOW the per-visitor limit and drive requests from
    DISTINCT addresses. A 429 then cannot come from per-visitor keying, which
    makes it observable proof that the backstop is wired.
    """
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "global_backstop.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_DEMO_USER_PER_HOUR", "10")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_GLOBAL_PER_HOUR", "3")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_CLIENT_IP_HEADER", "Fly-Client-IP")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_KEY_SALT", "backstop-test-salt")
    get_settings.cache_clear()
    rate_limit.reset_limiter()

    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    import asyncio

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_init_schema())

    try:
        with TestClient(create_app()) as c:
            codes = [_create_session(c, f"203.0.113.{i + 20}").status_code for i in range(5)]
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()
        rate_limit.reset_limiter()

    assert codes[:3] == [201, 201, 201], codes
    assert 429 in codes[3:], (
        f"five DIFFERENT visitors, global cap 3, per-visitor cap 10 — expected the "
        f"backstop to bind but got {codes}. The shared bucket is not being enforced "
        f"on the request path."
    )


def test_a_spoofed_client_ip_header_cannot_mint_unlimited_buckets(
    limited_client: TestClient,
) -> None:
    """Trusting a header is only safe behind a proxy that OVERWRITES it.

    This is the abuse case: if the app is reachable other than through the
    trusted proxy, a visitor supplies their own header value and gets a brand
    new quota every request. The deployment answer is the proxy (compose/Caddy
    now uses X-Real-IP, which Caddy overwrites, and strips inbound Fly-Client-IP;
    Fly's edge sets Fly-Client-IP itself). This test pins the CONSEQUENCE so the
    coupling is visible: with a spoofable header configured, the per-visitor
    limit does not bind, and only the global backstop stands between an attacker
    and the corpus.

    It is deliberately an assertion about REALITY, not a wish — if someone later
    adds a trusted-proxy check, this test should be updated to assert the limit
    now binds, and that edit is the signal the behaviour changed.
    """
    codes = [
        _create_session(limited_client, f"198.51.100.{i + 1}").status_code for i in range(LIMIT + 3)
    ]
    assert codes.count(201) > LIMIT, (
        "expected distinct spoofed values to each get their own bucket — if this "
        "now fails, a trusted-proxy check was added and this test should assert "
        "the limit binds instead"
    )
