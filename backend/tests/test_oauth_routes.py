"""Tests for ``/v1/auth/oauth/{provider}/{start,callback}`` (ADR-0004 PR 12).

Every case below names the exploit a naive test would miss — see the PR's
plan doc (``~/.claude/plans/deep-discovering-hippo.md`` §8) for the full
table this file implements. No network: the provider token-exchange and
userinfo calls are faked with ``httpx.MockTransport``, monkeypatched in via
``app.api.routes.oauth._build_http_client`` — this codebase's established
pattern for the Gemini/OpenRouter LLM clients (see
``test_llm_gemini_openrouter.py``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import (
    AuditAction,
    AuditEvent,
    Base,
    OAuthNonce,
    Session,
    User,
    UserIdentity,
    UserRole,
)

DEMO_BEARER = "Bearer local-demo-key"

_GITHUB_ACCOUNT_ID = 555111
_GOOGLE_SUB = "108234567890123456789"


@pytest.fixture
def oauth_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Generator[TestClient, None, None]:
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "oauth_routes.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("CITEVYN_GITHUB_OAUTH_CLIENT_ID", "gh-client-id")
    monkeypatch.setenv("CITEVYN_GITHUB_OAUTH_CLIENT_SECRET", "gh-client-secret")
    monkeypatch.setenv("CITEVYN_GOOGLE_OAUTH_CLIENT_ID", "gg-client-id")
    monkeypatch.setenv("CITEVYN_GOOGLE_OAUTH_CLIENT_SECRET", "gg-client-secret")
    monkeypatch.setenv("CITEVYN_OAUTH_REDIRECT_BASE_URL", "https://citevyn.example")
    get_settings.cache_clear()
    rate_limit.reset_limiter()
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
        rate_limit.reset_limiter()
        for var in (
            "CITEVYN_DATABASE_URL",
            "CITEVYN_GITHUB_OAUTH_CLIENT_ID",
            "CITEVYN_GITHUB_OAUTH_CLIENT_SECRET",
            "CITEVYN_GOOGLE_OAUTH_CLIENT_ID",
            "CITEVYN_GOOGLE_OAUTH_CLIENT_SECRET",
            "CITEVYN_OAUTH_REDIRECT_BASE_URL",
        ):
            monkeypatch.delenv(var, raising=False)


def _start(client: TestClient, provider: str) -> httpx.Response:
    return client.get(
        f"/v1/auth/oauth/{provider}/start",
        headers={"Authorization": DEMO_BEARER},
        follow_redirects=False,
    )


def _callback(
    client: TestClient, provider: str, *, code: str = "auth-code-1", state: str, **extra_params: str
) -> httpx.Response:
    params = {"code": code, "state": state, **extra_params}
    return client.get(
        f"/v1/auth/oauth/{provider}/callback",
        params=params,
        headers={"Authorization": DEMO_BEARER},
        follow_redirects=False,
    )


def _state_from_start_response(response: httpx.Response) -> str:
    location = response.headers["location"]
    query = parse_qs(urlparse(location).query)
    return query["state"][0]


def _query_all(model: type) -> list:
    async def _run() -> list:
        factory = get_sessionmaker()
        async with factory() as session:
            return list((await session.execute(select(model))).scalars().all())

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Mock provider transports
# ---------------------------------------------------------------------------


def _mock_provider_client(
    provider: str,
    *,
    account_id: str,
    email: str | None,
    email_verified: bool = True,
    captured_token_bodies: list[bytes] | None = None,
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if provider == "github":
            if url.startswith("https://github.com/login/oauth/access_token"):
                if captured_token_bodies is not None:
                    captured_token_bodies.append(request.content)
                return httpx.Response(
                    200, json={"access_token": "gh-token-abc", "token_type": "bearer"}
                )
            if url.startswith("https://api.github.com/user/emails"):
                return httpx.Response(
                    200,
                    json=[{"email": email, "primary": True, "verified": email_verified}]
                    if email
                    else [],
                )
            if url.startswith("https://api.github.com/user"):
                return httpx.Response(
                    200,
                    json={
                        "id": int(account_id),
                        "login": "octocat",
                        "email": email if email_verified else None,
                    },
                )
        else:
            if url.startswith("https://oauth2.googleapis.com/token"):
                if captured_token_bodies is not None:
                    captured_token_bodies.append(request.content)
                return httpx.Response(
                    200, json={"access_token": "gg-token-abc", "token_type": "bearer"}
                )
            if url.startswith("https://openidconnect.googleapis.com/v1/userinfo"):
                return httpx.Response(
                    200,
                    json={"sub": account_id, "email": email, "email_verified": email_verified},
                )
        raise AssertionError(f"unexpected URL in OAuth mock transport: {url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _patch_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    *,
    account_id: str,
    email: str | None,
    email_verified: bool = True,
    captured_token_bodies: list[bytes] | None = None,
) -> None:
    import app.api.routes.oauth as oauth_module

    monkeypatch.setattr(
        oauth_module,
        "_build_http_client",
        lambda: _mock_provider_client(
            provider,
            account_id=account_id,
            email=email,
            email_verified=email_verified,
            captured_token_bodies=captured_token_bodies,
        ),
    )


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_redirects_to_provider_with_pkce_and_state(oauth_client: TestClient) -> None:
    response = _start(oauth_client, "github")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize")
    query = parse_qs(urlparse(location).query)
    assert query["client_id"] == ["gh-client-id"]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    assert "state" in query
    uuid.UUID(hex=query["state"][0])  # a real nonce id, not a placeholder
    assert "citevyn_session" in oauth_client.cookies


def test_start_sends_the_fixed_config_redirect_uri_regardless_of_host_headers(
    oauth_client: TestClient,
) -> None:
    """Spoofed Host/X-Forwarded-Host headers must not change the redirect_uri
    sent to the provider -- it is derived ONLY from
    CITEVYN_OAUTH_REDIRECT_BASE_URL, never from request input."""
    response = oauth_client.get(
        "/v1/auth/oauth/github/start",
        headers={
            "Authorization": DEMO_BEARER,
            "Host": "evil.example",
            "X-Forwarded-Host": "also-evil.example",
        },
        follow_redirects=False,
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["redirect_uri"] == ["https://citevyn.example/v1/auth/oauth/github/callback"]


def test_start_404s_for_unknown_provider(oauth_client: TestClient) -> None:
    response = oauth_client.get(
        "/v1/auth/oauth/facebook/start", headers={"Authorization": DEMO_BEARER}
    )
    assert response.status_code == 404


def test_start_404s_when_provider_not_configured(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    monkeypatch.delenv("CITEVYN_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("CITEVYN_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    response = oauth_client.get(
        "/v1/auth/oauth/google/start", headers={"Authorization": DEMO_BEARER}
    )
    assert response.status_code == 404


def test_start_persists_the_pkce_verifier_and_sends_only_its_challenge(
    oauth_client: TestClient,
) -> None:
    response = _start(oauth_client, "github")
    state = _state_from_start_response(response)
    rows = _query_all(OAuthNonce)
    assert len(rows) == 1
    assert str(rows[0].nonce_id) == state
    assert rows[0].code_verifier not in response.headers["location"]


# ---------------------------------------------------------------------------
# callback: state / PKCE validation
# ---------------------------------------------------------------------------


def test_callback_rejects_a_forged_state_with_no_matching_nonce(oauth_client: TestClient) -> None:
    response = _callback(oauth_client, "github", state=str(uuid.uuid4()))
    assert response.status_code == 302
    assert response.headers["location"] == "/?auth=error"
    events = _query_all(AuditEvent)
    assert any(e.action == AuditAction.auth_failed for e in events)


def test_callback_rejects_state_from_a_different_provider(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """A naive check that only asks 'does any valid nonce exist' would miss
    this -- the row exists and is unexpired, but was minted for github.

    The atomic claim's WHERE clause includes `provider`, so a mismatched-
    provider attempt matches nothing and deletes nothing -- the nonce
    survives for the correct provider to still use (an earlier version of
    this fix deleted unconditionally on nonce_id alone and validated
    provider/session afterward, which let a wrong-provider OR wrong-session
    attempt permanently burn a legitimate nonce; see oauth.py's module
    docstring)."""
    start = _start(oauth_client, "github")
    state = _state_from_start_response(start)
    _patch_provider(monkeypatch, "google", account_id=_GOOGLE_SUB, email="a@example.com")
    response = _callback(oauth_client, "google", state=state)
    assert response.headers["location"] == "/?auth=error"
    assert len(_query_all(OAuthNonce)) == 1

    # And the correct provider can still complete the flow afterward.
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="real@example.com"
    )
    retry = _callback(oauth_client, "github", state=state)
    assert retry.headers["location"] == "/?auth=ok"


def test_callback_rejects_state_bound_to_a_different_browser_session(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """Simulates a stolen/replayed state value observed by a second browser
    that never went through `start` itself -- only checking 'nonce exists
    and is unexpired' would miss the session-binding property.

    Regression for a denial-of-login griefing attack an adversarial review
    round caught in an earlier version of this fix: the atomic claim's
    WHERE clause includes the CURRENT request's own auth_session_id, so an
    attacker's non-matching claim deletes NOTHING -- it must not be able to
    burn the real victim's nonce merely by observing the state value. The
    real assertion here is not just that the attacker's own attempt fails,
    but that the LEGITIMATE browser can still complete the flow afterward.
    """
    start = _start(oauth_client, "github")
    state = _state_from_start_response(start)

    attacker = TestClient(create_app())  # a distinct cookie jar == a distinct browser
    # The attacker has their OWN genuine (but different) session -- not no
    # cookie at all, which a separate, earlier guard already rejects before
    # the atomic claim runs. Minting one first is what makes this test
    # actually exercise the claim's auth_session_id predicate specifically.
    attacker.post("/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER})
    assert "citevyn_session" in attacker.cookies

    attacker_response = attacker.get(
        "/v1/auth/oauth/github/callback",
        params={"code": "auth-code-1", "state": state},
        headers={"Authorization": DEMO_BEARER},
        follow_redirects=False,
    )
    assert attacker_response.headers["location"] == "/?auth=error"
    assert len(_query_all(OAuthNonce)) == 1, (
        "the attacker's failed claim must not consume the victim's nonce"
    )

    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="victim@example.com"
    )
    victim_response = _callback(oauth_client, "github", state=state)
    assert victim_response.headers["location"] == "/?auth=ok", (
        "the legitimate browser must still be able to complete the flow "
        "after an attacker's failed attempt on the same state"
    )


def test_callback_rejects_an_expired_nonce(oauth_client: TestClient) -> None:
    start = _start(oauth_client, "github")
    state = _state_from_start_response(start)

    async def _expire() -> None:
        factory = get_sessionmaker()
        async with factory() as session:
            row = await session.get(OAuthNonce, uuid.UUID(hex=state))
            assert row is not None
            row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(_expire())
    response = _callback(oauth_client, "github", state=state)
    assert response.headers["location"] == "/?auth=error"
    events = _query_all(AuditEvent)
    metadatas = [e.metadata_ for e in events if e.action == AuditAction.auth_failed]
    assert any(m.get("event") == "oauth_expired" for m in metadatas)


def test_callback_rejects_a_reused_state(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """Proves single-use (the row is deleted on first success), not just a
    short TTL -- a naive short-TTL-only implementation would still accept a
    fast replay."""
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="new@example.com"
    )
    start = _start(oauth_client, "github")
    state = _state_from_start_response(start)

    first = _callback(oauth_client, "github", state=state)
    assert first.headers["location"] == "/?auth=ok"

    second = _callback(oauth_client, "github", state=state)
    assert second.headers["location"] == "/?auth=error"


def test_callback_sends_the_pkce_code_verifier_in_the_token_exchange(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """Mutation-test target: deleting code_verifier from the token-exchange
    body must make this test fail."""
    captured: list[bytes] = []
    _patch_provider(
        monkeypatch,
        "github",
        account_id=str(_GITHUB_ACCOUNT_ID),
        email="verifier@example.com",
        captured_token_bodies=captured,
    )
    start = _start(oauth_client, "github")
    state = _state_from_start_response(start)
    nonce_rows = _query_all(OAuthNonce)
    sent_verifier = nonce_rows[0].code_verifier

    response = _callback(oauth_client, "github", state=state)
    assert response.headers["location"] == "/?auth=ok"
    assert len(captured) == 1
    body = captured[0].decode()
    assert f"code_verifier={sent_verifier}" in body


def test_provider_denial_redirects_and_is_audited_but_creates_no_user(
    oauth_client: TestClient,
) -> None:
    """No nonce/identity/user work happens on denial -- only the audit
    write, whose 'oauth_denied' event name this project's own audit-
    metadata contract (the plan's §6) already reserves for exactly this."""
    response = oauth_client.get(
        "/v1/auth/oauth/github/callback",
        params={"error": "access_denied"},
        headers={"Authorization": DEMO_BEARER},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/?auth=error"
    events = _query_all(AuditEvent)
    assert len(events) == 1
    assert events[0].action == AuditAction.auth_failed
    assert events[0].metadata_ == {"event": "oauth_denied", "provider": "github"}
    assert _query_all(User) == []


# ---------------------------------------------------------------------------
# identity resolution — the core security logic
# ---------------------------------------------------------------------------


def test_first_time_login_creates_a_new_user_and_identity(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="first@example.com"
    )
    start = _start(oauth_client, "github")
    state = _state_from_start_response(start)
    response = _callback(oauth_client, "github", state=state)
    assert response.headers["location"] == "/?auth=ok"

    # `start` also mints an anonymous `anon_` principal for the OAuth nonce
    # to bind to (deliberately left in place, not deleted -- see
    # `claim_and_login`'s docstring), so exactly ONE `usr_`-prefixed row is
    # the actual signal, not the total row count.
    identities = _query_all(UserIdentity)
    assert len(identities) == 1
    assert identities[0].provider == "github"
    assert identities[0].provider_account_id == str(_GITHUB_ACCOUNT_ID)

    users = _query_all(User)
    registered = [u for u in users if u.user_id.startswith("usr_")]
    assert len(registered) == 1
    assert registered[0].user_id == identities[0].user_id
    assert registered[0].email == "first@example.com"
    assert registered[0].password_hash is None


def test_returning_identity_resolves_to_the_same_user_on_second_login(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="return@example.com"
    )

    start1 = _start(oauth_client, "github")
    _callback(oauth_client, "github", state=_state_from_start_response(start1))
    first_identities = _query_all(UserIdentity)
    assert len(first_identities) == 1
    first_user_id = first_identities[0].user_id

    second_client = TestClient(create_app())  # a different browser, same GitHub account
    start2 = _start(second_client, "github")
    response2 = _callback(second_client, "github", state=_state_from_start_response(start2))
    assert response2.headers["location"] == "/?auth=ok"

    identities = _query_all(UserIdentity)
    assert len(identities) == 1, (
        "no second UserIdentity row should be created for a returning identity"
    )
    assert identities[0].user_id == first_user_id
    registered = [u for u in _query_all(User) if u.user_id.startswith("usr_")]
    assert len(registered) == 1, "no second registered User row for a returning identity"


def test_existing_password_account_matching_email_gets_a_new_separate_oauth_account(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """CRITICAL: the account-takeover scenario. A password account with a
    matching email must NOT be the account an unrelated OAuth login resolves
    to -- resolution is by (provider, provider_account_id) ONLY, never by
    email. A `select(User).where(User.email == provider_email)` lookup
    reintroduced anywhere in the resolution path must make this test fail.
    """
    register = oauth_client.post(
        "/v1/auth/register",
        json={"email": "shared@example.com", "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER},
    )
    assert register.status_code == 201
    password_user_id = register.json()["user_id"]

    # A FRESH browser (distinct cookie jar) authenticates via GitHub with the
    # SAME email -- no prior UserIdentity link exists.
    oauth_browser = TestClient(create_app())
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="shared@example.com"
    )
    start = _start(oauth_browser, "github")
    response = _callback(oauth_browser, "github", state=_state_from_start_response(start))
    assert response.headers["location"] == "/?auth=ok"

    identities = _query_all(UserIdentity)
    assert len(identities) == 1
    assert identities[0].user_id != password_user_id, (
        "the OAuth login resolved to the pre-existing password account by "
        "email -- the exact account-takeover bug this table exists to prevent"
    )

    registered = [u for u in _query_all(User) if u.user_id.startswith("usr_")]
    assert len(registered) == 2, (
        "the OAuth login must create a NEW user, not resolve to the password account"
    )
    oauth_user = next(u for u in registered if u.user_id != password_user_id)
    assert oauth_user.user_id == identities[0].user_id
    assert oauth_user.password_hash is None

    # The pre-existing password account is untouched: its own login still works.
    login = TestClient(create_app()).post(
        "/v1/auth/login",
        json={"email": "shared@example.com", "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER},
    )
    assert login.status_code == 200
    assert login.json()["user_id"] == password_user_id


def test_anonymous_session_history_is_claimed_on_first_oauth_signup(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    created = oauth_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    session_id = created.json()["session_id"]

    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="claim@example.com"
    )
    start = _start(oauth_client, "github")
    response = _callback(oauth_client, "github", state=_state_from_start_response(start))
    assert response.headers["location"] == "/?auth=ok"

    fetched = oauth_client.get(f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER})
    assert fetched.status_code == 200, "the pre-signup session must still be reachable"

    sessions = _query_all(Session)
    matching = next(s for s in sessions if str(s.session_id) == session_id)
    users = _query_all(User)
    oauth_user = next(u for u in users if u.email == "claim@example.com")
    assert matching.user_id == oauth_user.user_id


def test_google_login_creates_identity_and_logs_in(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    _patch_provider(monkeypatch, "google", account_id=_GOOGLE_SUB, email="googler@example.com")
    start = _start(oauth_client, "google")
    response = _callback(oauth_client, "google", state=_state_from_start_response(start))
    assert response.headers["location"] == "/?auth=ok"

    identities = _query_all(UserIdentity)
    assert len(identities) == 1
    assert identities[0].provider == "google"
    assert identities[0].provider_account_id == _GOOGLE_SUB

    me = oauth_client.get("/v1/auth/me", headers={"Authorization": DEMO_BEARER})
    assert me.status_code == 200
    assert me.json()["email"] == "googler@example.com"


def test_unverified_email_is_not_persisted_on_the_new_user(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    _patch_provider(
        monkeypatch,
        "google",
        account_id=_GOOGLE_SUB,
        email="unverified@example.com",
        email_verified=False,
    )
    start = _start(oauth_client, "google")
    response = _callback(oauth_client, "google", state=_state_from_start_response(start))
    assert response.headers["location"] == "/?auth=ok"
    users = _query_all(User)
    assert users[0].email is None


# ---------------------------------------------------------------------------
# callback: unknown / unconfigured provider (defense in depth)
# ---------------------------------------------------------------------------


def test_callback_404s_for_unknown_provider(oauth_client: TestClient) -> None:
    response = oauth_client.get(
        "/v1/auth/oauth/facebook/callback",
        params={"code": "x", "state": str(uuid.uuid4())},
        headers={"Authorization": DEMO_BEARER},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# a REAL browser navigation carries no Authorization header -- regression
# for the CRITICAL_BLOCKER an adversarial review round found: both routes
# depended on the same demo-bearer dependency as the JSON auth routes,
# which no top-level navigation (window.location.href, or the provider's
# own 302 back to /callback) can ever attach.
# ---------------------------------------------------------------------------


def test_start_works_with_no_authorization_header_at_all(oauth_client: TestClient) -> None:
    response = oauth_client.get("/v1/auth/oauth/github/start", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://github.com/login/oauth/authorize")


def test_callback_works_with_no_authorization_header_at_all(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="nobearer@example.com"
    )
    start = oauth_client.get("/v1/auth/oauth/github/start", follow_redirects=False)
    state = _state_from_start_response(start)
    response = oauth_client.get(
        "/v1/auth/oauth/github/callback",
        params={"code": "auth-code-1", "state": state},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/?auth=ok"


# ---------------------------------------------------------------------------
# malformed provider payload -> graceful redirect + audit, never a raw 500
# ---------------------------------------------------------------------------


def test_userinfo_missing_the_provider_account_id_field_redirects_not_500(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    import app.api.routes.oauth as oauth_module

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://github.com/login/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "gh-token-abc"})
        if url.startswith("https://api.github.com/user"):
            # Missing "id" entirely -- a malformed/unexpected provider payload.
            return httpx.Response(200, json={"login": "octocat"})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(
        oauth_module,
        "_build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    start = _start(oauth_client, "github")
    response = _callback(oauth_client, "github", state=_state_from_start_response(start))
    assert response.status_code == 302
    assert response.headers["location"] == "/?auth=error"
    events = _query_all(AuditEvent)
    assert any(e.metadata_.get("event") == "oauth_provider_error" for e in events)


def test_token_response_is_not_a_json_object_redirects_not_500(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    import app.api.routes.oauth as oauth_module

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://github.com/login/oauth/access_token"):
            # A bare JSON array instead of the expected object.
            return httpx.Response(200, json=["not", "an", "object"])
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(
        oauth_module,
        "_build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    start = _start(oauth_client, "github")
    response = _callback(oauth_client, "github", state=_state_from_start_response(start))
    assert response.status_code == 302
    assert response.headers["location"] == "/?auth=error"


# ---------------------------------------------------------------------------
# concurrent first-time logins for the SAME external identity
# ---------------------------------------------------------------------------


async def test_resolve_or_create_identity_handles_a_genuine_concurrent_insert_race(
    tmp_path,
) -> None:
    """Directly exercises ``_resolve_or_create_identity``'s ``IntegrityError``
    branch by injecting a COMPETING commit between the "not found" lookup
    and this call's own insert -- a real interleaving, not two sequential
    calls. (An earlier version of this test seeded the winner BEFORE
    calling the route at all, so the request's own "not found" SELECT
    already found it and the IntegrityError branch was never reached --
    caught by adversarial review; this version proves the actual claimed
    defect.)
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.api.routes.oauth as oauth_module

    db_file = tmp_path / "identity_race.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    identity = oauth_module._Identity(
        provider_account_id="race-123", email=None, email_verified=False
    )
    winner_holder: dict[str, str] = {}

    async def _seed_competing_winner() -> None:
        async with factory() as competitor:
            winner = User(
                user_id=f"usr_{uuid.uuid4().hex}",
                role=UserRole.demo_user,
                created_at=datetime.now(UTC),
                email=None,
                password_hash=None,
            )
            competitor.add(winner)
            await competitor.flush()
            competitor.add(
                UserIdentity(
                    identity_id=uuid.uuid4(),
                    provider="github",
                    provider_account_id="race-123",
                    user_id=winner.user_id,
                    created_at=datetime.now(UTC),
                )
            )
            await competitor.commit()
        winner_holder["user_id"] = winner.user_id

    async with factory() as session:
        original_execute = session.execute

        async def _execute_with_injected_race(stmt, *args, **kwargs):
            result = await original_execute(stmt, *args, **kwargs)
            # Fires exactly once: right after the "not found" UserIdentity
            # lookup returns empty, before this session's own insert.
            if not winner_holder and "user_identities" in str(stmt).lower():
                await _seed_competing_winner()
            return result

        session.execute = _execute_with_injected_race  # type: ignore[method-assign]

        resolved_user_id = await oauth_module._resolve_or_create_identity(
            session, "github", identity
        )

    assert winner_holder, "the injected race never fired -- this test would be vacuous"
    assert resolved_user_id == winner_holder["user_id"]

    async with factory() as verify:
        identities = (await verify.execute(select(UserIdentity))).scalars().all()
        assert len(identities) == 1, (
            "the race must not leave two identities for the same external account"
        )
        users = (await verify.execute(select(User))).scalars().all()
        assert len(users) == 1, "the loser's User row must not be left behind either"

    await engine.dispose()
