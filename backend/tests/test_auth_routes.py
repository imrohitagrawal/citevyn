"""Tests for ``/v1/auth/{register,login,logout,me}`` (ADR-0004 PR 6).

This is the first PR in the sequence that can mint a REAL second principal,
so it carries the real two-account IDOR test the plan calls for (see
``test_session_ownership.py``'s docstring, which seeded a fake second
principal directly against the DB and said a real one "will look like this
once ADR-0004 PR 6 ships") — this file is that promise kept.
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


@pytest.fixture
def in_memory_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Generator[None, None, None]:
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "auth_routes.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    # Several tests below issue >30 requests from the same TestClient host
    # against the process-wide (not per-test) demo_user limiter; a previous
    # test file's leftover bucket state would make this one flaky.
    rate_limit.reset_limiter()
    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())
    try:
        yield
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()
        rate_limit.reset_limiter()
        monkeypatch.delenv("CITEVYN_DATABASE_URL", raising=False)


def _client() -> TestClient:
    """A fresh client with its OWN cookie jar — simulates a distinct browser."""
    return TestClient(create_app())


def _register(client: TestClient, email: str, password: str = "correct horse battery"):
    return client.post(
        "/v1/auth/register",
        json={"email": email, "password": password},
        headers={"Authorization": DEMO_BEARER},
    )


def _login(client: TestClient, email: str, password: str):
    return client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
        headers={"Authorization": DEMO_BEARER},
    )


def _me(client: TestClient):
    return client.get("/v1/auth/me", headers={"Authorization": DEMO_BEARER})


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_creates_account_and_sets_cookie(in_memory_app: None) -> None:
    client = _client()
    response = _register(client, "alice@example.com", "correct horse battery")
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert body["anonymous"] is False
    assert body["user_id"].startswith("usr_")
    assert "citevyn_session" in client.cookies


def test_register_normalizes_email_case_and_whitespace(in_memory_app: None) -> None:
    client = _client()
    response = _register(client, "  Alice@Example.com ", "correct horse battery")
    assert response.json()["email"] == "alice@example.com"


@pytest.mark.parametrize(
    "malformed",
    [
        "not-an-email",
        "two@@example.com",
        "no-domain@",
        "@no-local.com",
        "trailing-dot@example.",
        "leading-dot@.example.com",
        "has space@example.com",
    ],
)
def test_register_rejects_malformed_email(in_memory_app: None, malformed: str) -> None:
    response = _register(_client(), malformed, "correct horse battery")
    assert response.status_code == 422


def test_register_rejects_short_password(in_memory_app: None) -> None:
    response = _client().post(
        "/v1/auth/register",
        json={"email": "bob@example.com", "password": "short"},
        headers={"Authorization": DEMO_BEARER},
    )
    assert response.status_code == 422


def test_register_duplicate_email_is_rejected(in_memory_app: None) -> None:
    """Deliberate email-existence leak per ADR-0004 (no email provider for always-202)."""
    _register(_client(), "carol@example.com", "correct horse battery")
    second = _register(_client(), "carol@example.com", "a totally different password")
    assert second.status_code == 422


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_with_correct_password_succeeds(in_memory_app: None) -> None:
    _register(_client(), "dave@example.com", "correct horse battery")
    client = _client()
    response = _login(client, "dave@example.com", "correct horse battery")
    assert response.status_code == 200
    assert response.json()["email"] == "dave@example.com"
    assert "citevyn_session" in client.cookies


def test_login_with_wrong_password_is_401(in_memory_app: None) -> None:
    _register(_client(), "erin@example.com", "correct horse battery")
    response = _login(_client(), "erin@example.com", "wrong password entirely")
    assert response.status_code == 401


def test_login_with_unknown_email_is_401_not_404(in_memory_app: None) -> None:
    """Unknown email takes the SAME code path as wrong password (dummy-verify, ADR-0004
    PR 4) — a distinct status code here would be an account-enumeration oracle."""
    response = _login(_client(), "nobody@example.com", "whatever password")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# me / logout / cookie rotation
# ---------------------------------------------------------------------------


def test_me_without_cookie_is_401(in_memory_app: None) -> None:
    assert _me(_client()).status_code == 401


def test_me_with_valid_session_returns_identity(in_memory_app: None) -> None:
    client = _client()
    _register(client, "frank@example.com", "correct horse battery")
    response = _me(client)
    assert response.status_code == 200
    assert response.json()["email"] == "frank@example.com"
    assert response.json()["anonymous"] is False


def test_login_rotates_the_cookie_and_the_old_value_401s(in_memory_app: None) -> None:
    """The plan's own verify line: "login rotates the cookie *and* the old value now
    401s". Mint an anonymous cookie, capture it, then log in — the pre-login cookie
    value must stop authenticating anything (its ``AuthSession`` row was deleted, not
    superseded)."""
    client = _client()
    client.post("/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER})
    old_cookie_value = client.cookies["citevyn_session"]
    _register(client, "grace@example.com", "correct horse battery")
    new_cookie_value = client.cookies["citevyn_session"]
    assert new_cookie_value != old_cookie_value

    stale_client = _client()
    stale_client.cookies.set("citevyn_session", old_cookie_value)
    assert _me(stale_client).status_code == 401


def test_logout_clears_the_cookie_and_subsequent_me_is_401(in_memory_app: None) -> None:
    client = _client()
    _register(client, "heidi@example.com", "correct horse battery")
    assert _me(client).status_code == 200

    logout_response = client.post("/v1/auth/logout", headers={"Authorization": DEMO_BEARER})
    assert logout_response.status_code == 204
    assert _me(client).status_code == 401


def test_logout_with_no_session_is_idempotent(in_memory_app: None) -> None:
    response = _client().post("/v1/auth/logout", headers={"Authorization": DEMO_BEARER})
    assert response.status_code == 204


# ---------------------------------------------------------------------------
# claim-on-login
# ---------------------------------------------------------------------------


def test_register_claims_the_anonymous_sessions_history(in_memory_app: None) -> None:
    """The reason PR 6 exists: chat started before signup must survive signup."""
    client = _client()
    created = client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    session_id = created.json()["session_id"]

    _register(client, "ivan@example.com", "correct horse battery")

    fetched = client.get(f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER})
    assert fetched.status_code == 200, "the pre-signup session must still be reachable"


def test_login_claims_the_anonymous_sessions_history(in_memory_app: None) -> None:
    _register(_client(), "judy@example.com", "correct horse battery")

    client = _client()
    created = client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    session_id = created.json()["session_id"]

    _login(client, "judy@example.com", "correct horse battery")

    fetched = client.get(f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER})
    assert fetched.status_code == 200, "the pre-login session must still be reachable"


def test_logging_into_a_second_account_does_not_claim_the_first_accounts_sessions(
    in_memory_app: None,
) -> None:
    """CRITICAL regression (found by adversarial review of this PR): claim-on-login
    must claim ONLY an anonymous prior principal's history. Without that check, a
    browser that already holds a valid cookie for a real registered account (forgot
    to log out; a shared/kiosk machine) would have ITS sessions silently reassigned
    the instant a second account logs in on the same browser — cross-account data
    disclosure with no credential theft or URL-guessing required at all.
    """
    client = _client()
    _register(client, "alice@example.com", "correct horse battery")
    session_a = client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    ).json()["session_id"]

    # Still holding Alice's cookie — no logout — log into a second, unrelated account.
    _register(_client(), "bob@example.com", "correct horse battery")  # bob exists
    login_as_bob = _login(client, "bob@example.com", "correct horse battery")
    assert login_as_bob.status_code == 200
    assert login_as_bob.json()["email"] == "bob@example.com"

    # Bob must NOT be able to read Alice's pre-existing session.
    as_bob = client.get(f"/v1/sessions/{session_a}", headers={"Authorization": DEMO_BEARER})
    assert as_bob.status_code == 404

    # And Alice must still own it herself.
    alice_client = _client()
    _login(alice_client, "alice@example.com", "correct horse battery")
    as_alice = alice_client.get(f"/v1/sessions/{session_a}", headers={"Authorization": DEMO_BEARER})
    assert as_alice.status_code == 200


# ---------------------------------------------------------------------------
# the real two-account IDOR test
# ---------------------------------------------------------------------------


def test_two_real_accounts_cannot_read_each_others_sessions(in_memory_app: None) -> None:
    """Account B cannot read account A's session — with two GENUINE registered
    principals, not the seeded stand-in ``test_session_ownership.py`` used before
    this PR existed. Never a 403 either direction: a mismatch must be
    indistinguishable from a genuine miss (ADR-0004 PR 1)."""
    client_a = _client()
    _register(client_a, "kim@example.com", "correct horse battery")
    session_a = client_a.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    ).json()["session_id"]

    client_b = _client()
    _register(client_b, "leo@example.com", "correct horse battery")
    session_b = client_b.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    ).json()["session_id"]

    assert (
        client_b.get(
            f"/v1/sessions/{session_a}", headers={"Authorization": DEMO_BEARER}
        ).status_code
        == 404
    )
    assert (
        client_a.get(
            f"/v1/sessions/{session_b}", headers={"Authorization": DEMO_BEARER}
        ).status_code
        == 404
    )
    # Each account still reads its own session fine — the predicate excludes, it
    # does not break, ownership.
    assert (
        client_a.get(
            f"/v1/sessions/{session_a}", headers={"Authorization": DEMO_BEARER}
        ).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# credential-stuffing guard: keyed per email, not per client
# ---------------------------------------------------------------------------


def test_login_rate_limit_is_keyed_per_email_not_per_ip(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The plan's own verify line: "429 after N from 200 distinct IPs". An IP-keyed
    limiter would let a distributed attacker walk straight through; this proves the
    SAME target email is capped even though every attempt claims a different
    client address.
    """
    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "auth_rate_limit.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_DEMO_USER_PER_HOUR", "1000")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_GLOBAL_PER_HOUR", "1000")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_AUTH_LOGIN_PER_HOUR", "5")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_CLIENT_IP_HEADER", "Fly-Client-IP")
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_KEY_SALT", "auth-rate-limit-test-salt")
    get_settings.cache_clear()
    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())
    try:
        target_email = "mallory@example.com"
        statuses: list[int] = []
        for i in range(6):
            response = TestClient(create_app()).post(
                "/v1/auth/login",
                json={"email": target_email, "password": "guess-number-" + str(i)},
                headers={"Authorization": DEMO_BEARER, "Fly-Client-IP": f"203.0.113.{i}"},
            )
            statuses.append(response.status_code)
        # First 5 (the configured auth-login limit) fail authentication normally;
        # the 6th — still a distinct source IP — is rejected by the limiter, not
        # by password verification.
        assert statuses[:5] == [401] * 5
        assert statuses[5] == 429
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()
        monkeypatch.delenv("CITEVYN_DATABASE_URL", raising=False)
