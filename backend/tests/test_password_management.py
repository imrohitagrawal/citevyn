"""Tests for ``POST /v1/auth/me/password`` and ``has_password`` (ADR-0004 PR 14).

Every docstring names the change that turns the test red. The
"account with no password" cases null ``password_hash`` directly in the DB
after a normal registration -- the cheapest way to get an OAuth-only /
magic-link-only shaped account without a provider round trip.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import AuditEvent, AuthSession, Base, User

DEMO_BEARER = "Bearer local-demo-key"
EMAIL = "pw@example.com"
OLD = "correct horse battery"
NEW = "new passphrase 12345"


@pytest.fixture
def password_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Generator[None, None, None]:
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    get_settings.cache_clear()
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pw.db'}")
    get_settings.cache_clear()
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
    return TestClient(create_app())


def _register(client: TestClient, email: str = EMAIL, password: str = OLD) -> httpx.Response:
    response = client.post(
        "/v1/auth/register",
        json={"email": email, "password": password},
        headers={"Authorization": DEMO_BEARER},
    )
    assert response.status_code == 201, response.text
    return response


def _login(client: TestClient, email: str = EMAIL, password: str = OLD) -> httpx.Response:
    return client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
        headers={"Authorization": DEMO_BEARER},
    )


def _me(client: TestClient) -> httpx.Response:
    return client.get("/v1/auth/me", headers={"Authorization": DEMO_BEARER})


def _update(client: TestClient, **body: str) -> httpx.Response:
    return client.post("/v1/auth/me/password", json=body, headers={"Authorization": DEMO_BEARER})


def _run(coro):
    return asyncio.run(coro)


def _password_hash(email: str = EMAIL) -> str | None:
    async def _go() -> str | None:
        factory = get_sessionmaker()
        async with factory() as session:
            user = (await session.execute(select(User).where(User.email == email))).scalar_one()
            return user.password_hash

    return _run(_go())


def _null_password(email: str = EMAIL) -> None:
    """Turn a registered account into a passwordless (OAuth/magic-link-only) one."""

    async def _go() -> None:
        factory = get_sessionmaker()
        async with factory() as session:
            await session.execute(
                update(User).where(User.email == email).values(password_hash=None)
            )
            await session.commit()

    _run(_go())


def _audit_events() -> list[tuple[str, dict]]:
    async def _go() -> list[tuple[str, dict]]:
        factory = get_sessionmaker()
        async with factory() as session:
            rows = (await session.execute(select(AuditEvent))).scalars().all()
            return [(str(row.action), dict(row.metadata_)) for row in rows]

    return _run(_go())


def _live_session_count(user_id: str) -> int:
    async def _go() -> int:
        factory = get_sessionmaker()
        async with factory() as session:
            rows = (
                await session.execute(select(AuthSession).where(AuthSession.user_id == user_id))
            ).scalars()
            return len(list(rows))

    return _run(_go())


# ---------------------------------------------------------------------------
# has_password on the wire
# ---------------------------------------------------------------------------


def test_auth_me_has_password_field_reflects_state(password_app: None) -> None:
    """Plan test 14. RED if ``has_password`` is dropped from ``_auth_user_payload``
    or derived from anything but ``password_hash``."""
    client = _client()
    registered = _register(client)
    assert registered.json()["has_password"] is True
    assert _me(client).json()["has_password"] is True
    assert _login(_client()).json()["has_password"] is True

    _null_password()
    assert _me(client).json()["has_password"] is False


# ---------------------------------------------------------------------------
# First-time set
# ---------------------------------------------------------------------------


def test_set_password_first_time_requires_no_current_password(password_app: None) -> None:
    """Plan test 10. RED if the route demands ``current_password`` for an account
    that has none (the passwordless user could never set one)."""
    client = _client()
    _register(client)
    _null_password()
    assert _me(client).json()["has_password"] is False

    response = _update(client, new_password=NEW)
    assert response.status_code == 200, response.text
    assert response.json()["has_password"] is True
    assert _password_hash() is not None

    # The new password really is the credential now.
    assert _login(_client(), password=NEW).status_code == 200
    assert _login(_client(), password=OLD).status_code == 401
    assert any(
        action == "login" and meta.get("event") == "password_set"
        for action, meta in _audit_events()
    )


def test_set_password_first_time_ignores_a_stray_current_password(password_app: None) -> None:
    """The contract's other half: with no stored hash, the field is IGNORED --
    not validated, not an error. RED if a stray value is verified against None."""
    client = _client()
    _register(client)
    _null_password()
    assert _update(client, current_password="whatever", new_password=NEW).status_code == 200
    assert _login(_client(), password=NEW).status_code == 200


# ---------------------------------------------------------------------------
# Change
# ---------------------------------------------------------------------------


def test_change_password_requires_current_password_even_if_omitted_from_body(
    password_app: None,
) -> None:
    """Plan test 11 -- the round-2 CRITICAL regression. The branch decision comes
    from the server-loaded ``password_hash``, never from body-field presence:
    an account WITH a password, sent a body WITHOUT ``current_password``, must
    be rejected -- not silently treated as a first-time set. RED if the route
    branches on ``body.current_password is None`` instead."""
    client = _client()
    _register(client)
    hash_before = _password_hash()

    response = _update(client, new_password=NEW)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    assert _password_hash() == hash_before
    assert _login(_client(), password=NEW).status_code == 401
    assert _login(_client(), password=OLD).status_code == 200


def test_change_password_rejects_wrong_current_password(password_app: None) -> None:
    """Plan test 12. RED if ``verify_password`` is skipped or its result ignored.
    Also pins the status: 422, NOT 401 -- a 401 would trip the frontend's
    global sign-out interceptor for a caller who IS authenticated."""
    client = _client()
    _register(client)
    hash_before = _password_hash()

    response = _update(client, current_password="not the password", new_password=NEW)
    assert response.status_code == 422, response.text
    assert _password_hash() == hash_before
    assert _me(client).status_code == 200, "a wrong guess must not log the caller out"
    assert any(
        action == "auth_failed" and meta.get("event") == "password_current_mismatch"
        for action, meta in _audit_events()
    )


def test_change_password_with_the_right_current_password_succeeds(password_app: None) -> None:
    client = _client()
    _register(client)
    response = _update(client, current_password=OLD, new_password=NEW)
    assert response.status_code == 200, response.text
    assert response.json()["has_password"] is True
    assert _login(_client(), password=NEW).status_code == 200
    assert _login(_client(), password=OLD).status_code == 401
    assert any(
        action == "login" and meta.get("event") == "password_changed"
        for action, meta in _audit_events()
    )


def test_password_change_revokes_other_sessions(password_app: None) -> None:
    """Plan test 13. RED if ``revoke_other_sessions`` is not called, or if it
    also deletes the caller's OWN session (the user would be logged out by
    their own action)."""
    device_a = _client()
    _register(device_a)
    device_b = _client()
    assert _login(device_b).status_code == 200
    device_c = _client()
    assert _login(device_c).status_code == 200
    user_id = _me(device_a).json()["user_id"]
    assert _live_session_count(user_id) == 3

    response = _update(device_a, current_password=OLD, new_password=NEW)
    assert response.status_code == 200, response.text

    assert _me(device_a).status_code == 200, "the acting session survives"
    assert _me(device_b).status_code == 401, "every other session is gone"
    assert _me(device_c).status_code == 401
    assert _live_session_count(user_id) == 1
    revoked = [
        meta["sessions_revoked"]
        for action, meta in _audit_events()
        if meta.get("event") == "password_changed"
    ]
    assert revoked == [2]


def test_first_time_set_also_revokes_other_sessions(password_app: None) -> None:
    """Applied uniformly, not only on change: first-time set is exactly the
    moment a hijacked session could plant a durable backdoor credential."""
    device_a = _client()
    _register(device_a)
    device_b = _client()
    assert _login(device_b).status_code == 200
    _null_password()

    assert _update(device_a, new_password=NEW).status_code == 200
    assert _me(device_a).status_code == 200
    assert _me(device_b).status_code == 401


# ---------------------------------------------------------------------------
# Who may call it
# ---------------------------------------------------------------------------


def test_update_password_without_a_session_is_401(password_app: None) -> None:
    response = _update(_client(), new_password=NEW)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"


def test_update_password_from_an_anonymous_session_is_401(password_app: None) -> None:
    """An ``anon_`` principal has no email and no account to protect. RED if
    the ``usr_`` allowlist check is dropped (an anonymous row would silently
    gain a password hash)."""
    client = _client()
    created = client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    assert created.status_code in (200, 201)
    response = _update(client, new_password=NEW)
    assert response.status_code == 401


def test_update_password_rejects_a_short_new_password(password_app: None) -> None:
    """Same floor as register (``_PASSWORD_MIN_LENGTH``)."""
    client = _client()
    _register(client)
    assert _update(client, current_password=OLD, new_password="short").status_code == 422
    assert _login(_client(), password=OLD).status_code == 200
