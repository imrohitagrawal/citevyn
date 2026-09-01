"""Route-level proof of the signed-in rate tier (ADR-0004 PR 11).

Same rationale as ``test_rate_limit_per_visitor_routes.py``: these tests
drive the REAL ``rate_limited_demo`` dependency over the REAL routes, not
the limiter in isolation, because that is the only place the wiring
(cookie -> registered user_id -> a DIFFERENT bucket, at a DIFFERENT limit)
is actually observable. A limiter unit test would not catch a regression
that, say, keyed the "registered" bucket on the IP again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.core import db as db_module
from app.core.config import get_settings
from app.main import create_app
from app.models import Base

DEMO_BEARER = "Bearer local-demo-key"
ANON_LIMIT = 2
REGISTERED_LIMIT = 4


@pytest.fixture
def limited_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Generator[TestClient, None, None]:
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "registered_tier.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_DEMO_USER_PER_HOUR", str(ANON_LIMIT))
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_DEMO_USER_REGISTERED_PER_HOUR", str(REGISTERED_LIMIT))
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_CLIENT_IP_HEADER", "Fly-Client-IP")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_KEY_SALT", "registered-tier-test-salt")
    # Keep the shared backstop far away so it cannot be what trips these tests.
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_GLOBAL_PER_HOUR", "10000")
    get_settings.cache_clear()
    rate_limit.reset_limiter()

    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())

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
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER, "Fly-Client-IP": ip},
    )


def test_an_anonymous_visitor_still_gets_the_lower_ip_keyed_limit(
    limited_client: TestClient,
) -> None:
    for _ in range(ANON_LIMIT):
        assert _create_session(limited_client, "203.0.113.1").status_code == 201
    over = _create_session(limited_client, "203.0.113.1")
    assert over.status_code == 429
    assert "Sign in for a higher limit" in over.json()["error"]["message"]


def test_a_registered_caller_gets_the_higher_limit_and_no_upsell_once_exhausted(
    limited_client: TestClient,
) -> None:
    limited_client.post(
        "/v1/auth/register",
        json={"email": "tier@example.com", "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER, "Fly-Client-IP": "203.0.113.2"},
    )

    for _ in range(REGISTERED_LIMIT):
        assert _create_session(limited_client, "203.0.113.2").status_code == 201
    over = _create_session(limited_client, "203.0.113.2")
    assert over.status_code == 429
    # Already on the higher tier -- nothing to upsell.
    assert "Sign in for a higher limit" not in over.json()["error"]["message"]


def test_the_registered_bucket_is_keyed_per_account_not_per_ip(
    limited_client: TestClient,
) -> None:
    """The whole point of PR 11: a signed-in caller's limit follows the
    ACCOUNT across IPs/devices, unlike the anonymous IP-keyed bucket."""
    limited_client.post(
        "/v1/auth/register",
        json={"email": "roaming@example.com", "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER, "Fly-Client-IP": "203.0.113.3"},
    )

    # Half the allowance from IP #1, half from IP #2 -- one shared account
    # bucket must still hit the SAME registered limit, not double it.
    for _ in range(REGISTERED_LIMIT // 2):
        assert _create_session(limited_client, "203.0.113.3").status_code == 201
    for _ in range(REGISTERED_LIMIT // 2):
        assert _create_session(limited_client, "198.51.100.9").status_code == 201
    over = _create_session(limited_client, "198.51.100.9")
    assert over.status_code == 429


def test_two_different_registered_accounts_do_not_share_a_bucket(
    limited_client: TestClient,
) -> None:
    limited_client.post(
        "/v1/auth/register",
        json={"email": "alice-tier@example.com", "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER, "Fly-Client-IP": "203.0.113.4"},
    )
    for _ in range(REGISTERED_LIMIT):
        assert _create_session(limited_client, "203.0.113.4").status_code == 201
    assert _create_session(limited_client, "203.0.113.4").status_code == 429

    other_client = TestClient(limited_client.app)
    other_client.post(
        "/v1/auth/register",
        json={"email": "bob-tier@example.com", "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER, "Fly-Client-IP": "203.0.113.5"},
    )
    response = other_client.post(
        "/v1/sessions",
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER, "Fly-Client-IP": "203.0.113.5"},
    )
    assert response.status_code == 201
