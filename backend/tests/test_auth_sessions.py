"""Tests for :mod:`app.core.auth_sessions` (ADR-0004 PR 3).

Covers the properties the plan's verify criteria name explicitly:
- Two anonymous visitors cannot read each other's sessions (404 both ways).
- ``Set-Cookie`` carries the right name, flags, and no ``Domain=``.
- A tampered or expired cookie is treated exactly like no cookie (mints a
  fresh principal), never as an error.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import db as db_module
from app.core.auth_sessions import _hash_secret
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import AuthSession, Base, User, UserRole

DEMO_BEARER = "Bearer local-demo-key"


@pytest.fixture
def in_memory_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> Generator[TestClient, None, None]:
    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "auth_sessions.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())
    try:
        yield TestClient(create_app())
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()
        monkeypatch.delenv("CITEVYN_DATABASE_URL", raising=False)


# ---------------------------------------------------------------------------
# Cookie issuance
# ---------------------------------------------------------------------------


def test_first_request_mints_a_cookie(in_memory_client: TestClient) -> None:
    response = in_memory_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    assert response.status_code == 201
    assert "citevyn_session" in in_memory_client.cookies
    cookie_value = in_memory_client.cookies["citevyn_session"]
    auth_session_id_part, _, secret = cookie_value.partition(".")
    assert secret, "cookie value must be <id>.<secret>"
    uuid.UUID(hex=auth_session_id_part)  # does not raise


def test_cookie_name_carries_no_host_prefix_outside_production(
    in_memory_client: TestClient,
) -> None:
    """``__Host-`` requires Secure, which a plain-http dev/test server cannot
    honestly claim — see the module docstring in ``auth_sessions.py``."""
    in_memory_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    assert "citevyn_session" in in_memory_client.cookies
    assert "__Host-citevyn_session" not in in_memory_client.cookies


def test_set_cookie_header_flags(in_memory_client: TestClient) -> None:
    response = in_memory_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.lower() or "samesite=lax" in set_cookie.lower()
    assert "Path=/" in set_cookie
    assert "Domain=" not in set_cookie
    # Not production here, so Secure must NOT be set (a Secure cookie is
    # silently dropped by some clients over plain HTTP, which would make
    # local dev/testing intermittently break).
    assert "Secure" not in set_cookie


def test_cookie_is_host_prefixed_and_secure_in_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "prod_cookie.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("CITEVYN_ENVIRONMENT", "production")
    monkeypatch.setenv("CITEVYN_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CITEVYN_ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("CITEVYN_EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("CITEVYN_DEMO_API_KEY", "a-strong-demo-key-not-the-default-1234")
    monkeypatch.setenv("CITEVYN_ADMIN_API_KEY", "a-strong-admin-key-not-the-default-1234")
    get_settings.cache_clear()
    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())
    try:
        response = TestClient(create_app()).post(
            "/v1/sessions",
            json={"channel": "chat"},
            headers={"Authorization": "Bearer a-strong-demo-key-not-the-default-1234"},
        )
        set_cookie = response.headers["set-cookie"]
        assert set_cookie.startswith("__Host-citevyn_session=")
        assert "Secure" in set_cookie
        assert "Domain=" not in set_cookie
        assert "HttpOnly" in set_cookie
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()


def test_a_second_request_reuses_the_same_principal(in_memory_client: TestClient) -> None:
    """The TestClient's cookie jar carries Set-Cookie forward automatically —
    proves the SAME principal round-trips rather than a fresh one minting on
    every request."""
    create = in_memory_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    session_id = create.json()["session_id"]

    get = in_memory_client.get(f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER})
    assert get.status_code == 200
    # If a new principal had minted, this session (owned by the FIRST
    # principal) would 404 rather than 200.


# ---------------------------------------------------------------------------
# Two anonymous visitors cannot read each other — the plan's PR 3 acceptance
# criterion, verified with two INDEPENDENT TestClients (each gets its own
# cookie jar, mirroring two separate browsers) rather than a directly-seeded
# row (test_session_ownership.py already covers that angle for PR 1).
# ---------------------------------------------------------------------------


def test_two_anonymous_visitors_cannot_read_each_others_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "two_visitors.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())
    try:
        app = create_app()
        visitor_a = TestClient(app)
        visitor_b = TestClient(app)

        created_by_a = visitor_a.post(
            "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
        )
        session_id = created_by_a.json()["session_id"]
        assert (
            visitor_a.get(
                f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER}
            ).status_code
            == 200
        )

        # B has never seen this session id, and B's cookie (freshly minted on
        # its own first request below) resolves to a DIFFERENT principal.
        read_by_b = visitor_b.get(
            f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER}
        )
        assert read_by_b.status_code == 404

        # And the reverse: B creates its own session, A cannot read it.
        created_by_b = visitor_b.post(
            "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
        )
        b_session_id = created_by_b.json()["session_id"]
        read_by_a = visitor_a.get(
            f"/v1/sessions/{b_session_id}", headers={"Authorization": DEMO_BEARER}
        )
        assert read_by_a.status_code == 404

        # The two visitors really did get different cookies/principals.
        assert visitor_a.cookies["citevyn_session"] != visitor_b.cookies["citevyn_session"]
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()


# ---------------------------------------------------------------------------
# Tampered / expired cookies degrade to "mint a fresh principal", never an
# error — a client should never see a 401/500 just because its cookie aged
# out or a proxy mangled it.
# ---------------------------------------------------------------------------


def test_a_garbage_cookie_value_is_treated_as_no_cookie(in_memory_client: TestClient) -> None:
    in_memory_client.cookies.set("citevyn_session", "not-a-valid-cookie-value")
    response = in_memory_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    assert response.status_code == 201
    # A fresh cookie was minted to replace the garbage one. Read the
    # response's own Set-Cookie header, not the client's cookie jar — the
    # jar now holds two same-name entries under different domains (one we
    # set manually with no domain, one the server just set for
    # `testserver.local`), and httpx.Cookies.__getitem__ raises
    # CookieConflict rather than picking one, which is a test-harness
    # ambiguity, not something the app needs to resolve.
    assert "not-a-valid-cookie-value" not in response.headers["set-cookie"]


def test_a_wrong_secret_for_a_real_auth_session_id_is_rejected(
    in_memory_client: TestClient,
) -> None:
    """An attacker who can guess/enumerate a real ``auth_session_id`` (the
    lookup key, not a secret) still cannot forge a session without the
    matching secret — the row's ``secret_hash`` must actually verify."""
    factory = get_sessionmaker()
    real_id = uuid.uuid4()
    now = datetime.now(UTC)

    async def _seed() -> None:
        async with factory() as db:
            db.add(User(user_id="anon_victim", role=UserRole.demo_user, created_at=now))
            db.add(
                AuthSession(
                    auth_session_id=real_id,
                    secret_hash=_hash_secret("the-real-secret"),
                    user_id="anon_victim",
                    created_at=now,
                    expires_at=now + timedelta(days=180),
                )
            )
            await db.commit()

    asyncio.run(_seed())

    in_memory_client.cookies.set("citevyn_session", f"{real_id.hex}.wrong-guessed-secret")
    create = in_memory_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    assert create.status_code == 201
    # A NEW principal was minted (the forged cookie did not resolve to
    # anon_victim) — the created session must not be owned by the victim.
    session_id = create.json()["session_id"]
    body = in_memory_client.get(
        f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER}
    ).json()
    assert body["user_id"] != "anon_victim"


def test_an_expired_auth_session_is_treated_as_no_cookie(in_memory_client: TestClient) -> None:
    factory = get_sessionmaker()
    expired_id = uuid.uuid4()
    now = datetime.now(UTC)

    async def _seed() -> None:
        async with factory() as db:
            db.add(User(user_id="anon_stale", role=UserRole.demo_user, created_at=now))
            db.add(
                AuthSession(
                    auth_session_id=expired_id,
                    secret_hash=_hash_secret("stale-secret"),
                    user_id="anon_stale",
                    created_at=now - timedelta(days=200),
                    expires_at=now - timedelta(days=1),  # already expired
                )
            )
            await db.commit()

    asyncio.run(_seed())

    in_memory_client.cookies.set("citevyn_session", f"{expired_id.hex}.stale-secret")
    create = in_memory_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    assert create.status_code == 201
    session_id = create.json()["session_id"]
    body = in_memory_client.get(
        f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER}
    ).json()
    assert body["user_id"] != "anon_stale"
