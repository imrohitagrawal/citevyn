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


def test_first_time_login_stores_the_provider_email_lower_cased(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """Pre-existing bug surfaced by ADR-0004 PR 14's review: the provider's
    email was stored verbatim, while every typed-address route (register,
    login, magic-link request) lower-cases before an exact, case-sensitive
    match -- so a GitHub/Google account with any uppercase in its email could
    never receive a magic link or log in with a later-set password. RED if
    the normalisation at account creation is removed."""
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="Jane.Doe@Example.com "
    )
    state = _state_from_start_response(_start(oauth_client, "github"))
    assert _callback(oauth_client, "github", state=state).headers["location"] == "/?auth=ok"
    registered = [u for u in _query_all(User) if u.user_id.startswith("usr_")]
    assert len(registered) == 1
    assert registered[0].email == "jane.doe@example.com"


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


def test_an_oauth_account_with_no_verified_email_is_not_reported_anonymous(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """#288: `/v1/auth/me` used to compute `anonymous` as `user.email is None`.

    PR 12 deliberately stores `email=None` when the provider says the address is
    unverified (the test above pins that). So a real `usr_` account with a live
    session came back as `anonymous: true`, `authStore.stateFor()` mapped that to
    status "anonymous", and `AccountMenu` rendered the "Sign in" button -- the
    user could not reach History or Connected accounts despite being signed in.

    RED before the fix: `anonymous` is `True` here.
    """
    _patch_provider(
        monkeypatch,
        "google",
        account_id=_GOOGLE_SUB,
        email="unverified@example.com",
        email_verified=False,
    )
    start = _start(oauth_client, "google")
    callback = _callback(oauth_client, "google", state=_state_from_start_response(start))
    assert callback.headers["location"] == "/?auth=ok"

    me = oauth_client.get("/v1/auth/me", headers={"Authorization": DEMO_BEARER})
    assert me.status_code == 200
    body = me.json()
    # Partner: this really is the unverified-email account, so `anonymous: false`
    # cannot be passing because some OTHER, email-bearing user was resolved.
    assert body["email"] is None
    assert body["user_id"].startswith("usr_")
    assert body["anonymous"] is False


def test_an_anonymous_principal_is_still_reported_anonymous(
    oauth_client: TestClient,
) -> None:
    """Partner to the test above: the fix must not make EVERYONE non-anonymous.

    Without this, hardcoding `anonymous` to `False` would pass.

    `/v1/auth/me` deliberately does NOT mint a session (a missing cookie is a
    401, not a silent new identity), so an anonymous principal has to be created
    the way a real visitor creates one -- by touching a session route first.
    """
    created = oauth_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    assert created.status_code in (200, 201), created.text

    me = oauth_client.get("/v1/auth/me", headers={"Authorization": DEMO_BEARER})
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["user_id"].startswith("anon_")
    assert body["anonymous"] is True


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


# ===========================================================================
# Account linking: GET .../{provider}/connect/start + callback dispatch
# (ADR-0004 PR 13). Each test names the exact change that turns it red.
# ===========================================================================

_COOKIE = "citevyn_session"


def _register(client: TestClient, email: str) -> str:
    response = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER},
    )
    assert response.status_code == 201, response.text
    assert _COOKIE in client.cookies
    return response.json()["user_id"]


def _connect_start(client: TestClient, provider: str) -> httpx.Response:
    # A real top-level navigation: no Authorization header, ever.
    return client.get(f"/v1/auth/oauth/{provider}/connect/start", follow_redirects=False)


def _me(client: TestClient) -> httpx.Response:
    return client.get("/v1/auth/me", headers={"Authorization": DEMO_BEARER})


def _age_current_session(client: TestClient, *, seconds: int) -> None:
    """Back-date the client's CURRENT AuthSession.created_at by ``seconds``."""
    from app.models import AuthSession

    auth_session_id = uuid.UUID(hex=client.cookies[_COOKIE].partition(".")[0])

    async def _run() -> None:
        factory = get_sessionmaker()
        async with factory() as session:
            row = await session.get(AuthSession, auth_session_id)
            assert row is not None
            row.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=seconds)
            await session.commit()

    asyncio.run(_run())


def _set_nonce_intent(state: str, intent: str) -> None:
    async def _run() -> None:
        factory = get_sessionmaker()
        async with factory() as session:
            row = await session.get(OAuthNonce, uuid.UUID(hex=state))
            assert row is not None
            row.return_intent = intent
            await session.commit()

    asyncio.run(_run())


def _auth_failed_metadatas() -> list[dict]:
    return [e.metadata_ for e in _query_all(AuditEvent) if e.action == AuditAction.auth_failed]


def _link_identity_to(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    *,
    account_id: str,
    email: str,
) -> httpx.Response:
    """Run a full connect round trip for the signed-in ``client``."""
    _patch_provider(monkeypatch, provider, account_id=account_id, email=email)
    start = _connect_start(client, provider)
    assert start.status_code == 302, start.text
    assert start.headers["location"].startswith("https://"), start.headers["location"]
    return _callback(client, provider, state=_state_from_start_response(start))


# --- connect/start preconditions ------------------------------------------


def test_connect_start_requires_a_real_signed_in_account(oauth_client: TestClient) -> None:
    """RED if the `usr_` prefix check in oauth_connect_start is removed: an
    anonymous visitor would be handed a connect nonce."""
    oauth_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    assert _COOKIE in oauth_client.cookies  # a genuine (anonymous) session exists
    response = _connect_start(oauth_client, "github")
    assert response.status_code == 302
    assert response.headers["location"] == "/?connect=error&reason=session&provider=github"
    assert _query_all(OAuthNonce) == [], "no nonce may be minted for a rejected connect"


def test_connect_start_with_no_session_at_all_fails_closed(oauth_client: TestClient) -> None:
    """RED if oauth_connect_start switches to ensure_auth_session (which
    mints): a rejected request must leave ZERO rows behind and set no cookie."""
    from app.models import AuthSession

    response = _connect_start(oauth_client, "github")
    assert response.status_code == 302
    assert response.headers["location"] == "/?connect=error&reason=session&provider=github"
    assert "set-cookie" not in response.headers
    assert _query_all(AuthSession) == []
    assert _query_all(User) == []
    assert _query_all(OAuthNonce) == []


def test_connect_start_rejects_a_stale_session(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """The stolen-cookie gate. RED if `_session_is_fresh` is dropped from
    oauth_connect_start, or if its comparison flips direction."""
    _register(oauth_client, "stale@example.com")
    _age_current_session(oauth_client, seconds=21 * 60)

    response = _connect_start(oauth_client, "github")
    assert response.headers["location"] == "/?connect=error&reason=session&provider=github"
    assert _query_all(OAuthNonce) == []

    # Still signed in -- the gate rejects linking, it does not log anyone out.
    assert _me(oauth_client).status_code == 200

    # Just inside the default window is accepted.
    _age_current_session(oauth_client, seconds=19 * 60)
    fresh = _connect_start(oauth_client, "github")
    assert fresh.headers["location"].startswith("https://github.com/login/oauth/authorize")

    # And the window is the setting, not a hardcoded number.
    monkeypatch.setenv("CITEVYN_OAUTH_CONNECT_MAX_SESSION_AGE_SECONDS", "60")
    get_settings.cache_clear()
    narrowed = _connect_start(oauth_client, "github")
    assert narrowed.headers["location"] == "/?connect=error&reason=session&provider=github"
    monkeypatch.delenv("CITEVYN_OAUTH_CONNECT_MAX_SESSION_AGE_SECONDS", raising=False)
    get_settings.cache_clear()


def test_connect_start_404s_for_unknown_or_unconfigured_provider(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """RED if oauth_connect_start stops calling _require_provider.

    Empty-string overrides rather than delenv: a developer's local
    ``backend/.env`` with real Google keys would otherwise silently
    re-configure the provider the moment the env var is removed
    (pydantic-settings falls back to the env file) and turn this into a 302.
    """
    _register(oauth_client, "prov@example.com")
    assert _connect_start(oauth_client, "facebook").status_code == 404
    monkeypatch.setenv("CITEVYN_GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("CITEVYN_GOOGLE_OAUTH_CLIENT_SECRET", "")
    get_settings.cache_clear()
    assert _connect_start(oauth_client, "google").status_code == 404


def test_connect_start_mints_a_connect_intent_nonce_bound_to_the_callers_session(
    oauth_client: TestClient,
) -> None:
    """RED if _start_oauth_flow is called with return_intent="login" from the
    connect route, or bound to anything but the caller's existing session."""
    _register(oauth_client, "intent@example.com")
    my_session_id = uuid.UUID(hex=oauth_client.cookies[_COOKIE].partition(".")[0])
    response = _connect_start(oauth_client, "google")
    assert response.status_code == 302
    rows = _query_all(OAuthNonce)
    assert len(rows) == 1
    assert rows[0].return_intent == "connect"
    assert rows[0].auth_session_id == my_session_id
    assert "set-cookie" not in response.headers, "connect/start must never mint or rotate"


# --- callback: connect intent ----------------------------------------------


def test_connect_callback_links_new_identity_without_rotating_the_session(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """Happy path. RED if _handle_connect_intent calls claim_and_login (cookie
    would rotate) or if _link_identity fails to insert the row."""
    user_id = _register(oauth_client, "link@example.com")
    cookie_before = oauth_client.cookies[_COOKIE]

    response = _link_identity_to(
        oauth_client,
        monkeypatch,
        "github",
        account_id=str(_GITHUB_ACCOUNT_ID),
        email="gh@example.com",
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/?connect=ok&provider=github"
    assert "set-cookie" not in response.headers
    assert oauth_client.cookies[_COOKIE] == cookie_before, (
        "the session cookie must be byte-identical"
    )

    identities = _query_all(UserIdentity)
    assert len(identities) == 1
    assert identities[0].provider == "github"
    assert identities[0].provider_account_id == str(_GITHUB_ACCOUNT_ID)
    assert identities[0].user_id == user_id

    me = _me(oauth_client)
    assert me.status_code == 200
    assert me.json()["user_id"] == user_id
    assert me.json()["providers"] == ["github"]
    # users.email is untouched by the provider's (different) email.
    assert me.json()["email"] == "link@example.com"

    success = [
        e for e in _query_all(AuditEvent) if e.metadata_.get("event") == "oauth_connect_github"
    ]
    assert len(success) == 1
    assert success[0].action == AuditAction.login
    assert success[0].user_id == user_id
    assert success[0].metadata_ == {
        "event": "oauth_connect_github",
        "provider": "github",
        "result": "linked",
    }


def test_connect_callback_is_idempotent_for_the_same_account(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """RED if _link_identity's "found + same target" branch returns
    LINKED_ELSEWHERE, or tries a second insert (unique-constraint 500)."""
    user_id = _register(oauth_client, "twice@example.com")
    first = _link_identity_to(
        oauth_client,
        monkeypatch,
        "github",
        account_id=str(_GITHUB_ACCOUNT_ID),
        email="gh@example.com",
    )
    assert first.headers["location"] == "/?connect=ok&provider=github"
    second = _link_identity_to(
        oauth_client,
        monkeypatch,
        "github",
        account_id=str(_GITHUB_ACCOUNT_ID),
        email="gh@example.com",
    )
    assert second.headers["location"] == "/?connect=ok&provider=github"

    identities = _query_all(UserIdentity)
    assert len(identities) == 1
    assert identities[0].user_id == user_id
    results = sorted(
        e.metadata_["result"]
        for e in _query_all(AuditEvent)
        if e.metadata_.get("event") == "oauth_connect_github"
    )
    assert results == ["already_linked_same", "linked"]  # order-independent


def test_connect_callback_rejects_identity_already_linked_to_a_different_account(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """THE core security case. RED if _link_identity's "found + different
    target" branch reassigns the row (UPDATE user_id) or returns success."""
    # Account A: created by a plain OAuth LOGIN in its own browser.
    browser_a = TestClient(create_app())
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="a@example.com"
    )
    start_a = _start(browser_a, "github")
    assert (
        _callback(browser_a, "github", state=_state_from_start_response(start_a)).headers[
            "location"
        ]
        == "/?auth=ok"
    )
    account_a = _query_all(UserIdentity)[0].user_id

    # Account B: a password account in a different browser, tries to connect
    # the SAME GitHub identity.
    account_b = _register(oauth_client, "b@example.com")
    cookie_b = oauth_client.cookies[_COOKIE]
    response = _link_identity_to(
        oauth_client,
        monkeypatch,
        "github",
        account_id=str(_GITHUB_ACCOUNT_ID),
        email="a@example.com",
    )
    assert response.headers["location"] == "/?connect=error&reason=already_linked&provider=github"

    identities = _query_all(UserIdentity)
    assert len(identities) == 1
    assert identities[0].user_id == account_a, "the identity must NOT move to account B"
    assert _me(oauth_client).json()["providers"] == []

    # B stays signed in, cookie untouched -- a rejected link is not a logout.
    assert oauth_client.cookies[_COOKIE] == cookie_b
    assert _me(oauth_client).json()["user_id"] == account_b

    # Audited against the ACTING user (B), without naming A anywhere.
    conflicts = [
        e for e in _query_all(AuditEvent) if e.metadata_.get("event") == "oauth_connect_conflict"
    ]
    assert len(conflicts) == 1
    assert conflicts[0].action == AuditAction.auth_failed
    assert conflicts[0].user_id == account_b
    assert conflicts[0].metadata_ == {"event": "oauth_connect_conflict", "provider": "github"}
    assert account_a not in repr(conflicts[0].metadata_)


def test_connect_callback_rejects_if_signed_out_between_start_and_callback(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """The session is revoked server-side (e.g. from another device) while
    the provider consent screen is open; the browser still holds the now-dead
    cookie. The callback cannot even claim the nonce (its session-bound
    WHERE clause no longer matches), so the intent is unknowable and the
    redirect is the login-shaped /?auth=error -- documented in API_SPEC §4b.
    RED if the callback's session binding is removed. (The post-claim
    re-verification in _resolve_connect_target is covered by the unit test
    below and by test_connect_nonce_cannot_complete_as_login_and_vice_versa,
    not by this route test -- a review skeptic proved this test stays GREEN
    with that check deleted.)"""
    from app.models import AuthSession

    _register(oauth_client, "revoked@example.com")
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="gh@example.com"
    )
    start = _connect_start(oauth_client, "github")
    state = _state_from_start_response(start)

    async def _revoke() -> None:
        factory = get_sessionmaker()
        async with factory() as session:
            for row in (await session.execute(select(AuthSession))).scalars().all():
                await session.delete(row)
            await session.commit()

    asyncio.run(_revoke())

    response = _callback(oauth_client, "github", state=state)
    assert response.status_code == 302
    assert response.headers["location"] == "/?auth=error"
    assert _query_all(UserIdentity) == []
    assert "set-cookie" not in response.headers


async def test_resolve_connect_target_fails_closed_for_dead_or_anonymous_sessions(tmp_path) -> None:
    """Unit-level proof of the re-verification the route test above can only
    reach indirectly. RED if the `usr_` prefix check or the expiry check in
    resolve_principal_by_auth_session_id / _resolve_connect_target is removed."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.api.routes.oauth as oauth_module
    from app.models import AuthSession

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)

    def _nonce(session_id: uuid.UUID | None) -> OAuthNonce:
        return OAuthNonce(
            nonce_id=uuid.uuid4(),
            provider="github",
            code_verifier="v",
            auth_session_id=session_id,
            return_intent="connect",
            created_at=now,
            expires_at=now,
        )

    async with factory() as db:
        registered = User(user_id="usr_live", role=UserRole.demo_user, created_at=now)
        anonymous = User(user_id="anon_x", role=UserRole.demo_user, created_at=now)
        db.add_all([registered, anonymous])
        await db.flush()
        live = AuthSession(
            auth_session_id=uuid.uuid4(),
            secret_hash="h",
            user_id="usr_live",
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
        expired = AuthSession(
            auth_session_id=uuid.uuid4(),
            secret_hash="h",
            user_id="usr_live",
            created_at=now,
            expires_at=now - timedelta(seconds=1),
        )
        anon = AuthSession(
            auth_session_id=uuid.uuid4(),
            secret_hash="h",
            user_id="anon_x",
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
        db.add_all([live, expired, anon])
        await db.commit()

        assert (
            await oauth_module._resolve_connect_target(db, _nonce(live.auth_session_id))
            == "usr_live"
        )
        assert (
            await oauth_module._resolve_connect_target(db, _nonce(expired.auth_session_id)) is None
        )
        assert await oauth_module._resolve_connect_target(db, _nonce(anon.auth_session_id)) is None
        assert await oauth_module._resolve_connect_target(db, _nonce(uuid.uuid4())) is None
        assert await oauth_module._resolve_connect_target(db, _nonce(None)) is None
    await engine.dispose()


def test_connect_callback_never_creates_a_new_user(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """RED if _handle_connect_intent ever falls into _resolve_or_create_identity
    (which creates a User) -- checked on the happy path AND both rejections."""
    browser_a = TestClient(create_app())
    _patch_provider(monkeypatch, "google", account_id=_GOOGLE_SUB, email="a@example.com")
    start_a = _start(browser_a, "google")
    _callback(browser_a, "google", state=_state_from_start_response(start_a))

    _register(oauth_client, "counter@example.com")
    users_before = len(_query_all(User))

    # 1. happy path (github, unlinked)
    ok = _link_identity_to(
        oauth_client,
        monkeypatch,
        "github",
        account_id=str(_GITHUB_ACCOUNT_ID),
        email="x@example.com",
    )
    assert ok.headers["location"] == "/?connect=ok&provider=github"
    # 2. conflict (google id already owned by browser_a's account)
    conflict = _link_identity_to(
        oauth_client, monkeypatch, "google", account_id=_GOOGLE_SUB, email="a@example.com"
    )
    assert conflict.headers["location"] == "/?connect=error&reason=already_linked&provider=google"
    # 3. no-session at callback (nonce re-labelled connect from an anonymous browser)
    anon = TestClient(create_app())
    _patch_provider(monkeypatch, "github", account_id="999", email="anon@example.com")
    start_anon = _start(anon, "github")
    state_anon = _state_from_start_response(start_anon)
    _set_nonce_intent(state_anon, "connect")
    no_session = _callback(anon, "github", state=state_anon)
    assert no_session.headers["location"] == "/?connect=error&reason=session&provider=github"

    assert len(_query_all(User)) == users_before + 1, (
        "exactly one extra row is allowed: the anonymous principal `start` "
        "minted for the third browser -- never a usr_ account from a connect"
    )
    assert [u for u in _query_all(User) if u.user_id.startswith("usr_")].__len__() == 2


def test_connect_start_and_callback_use_navigation_rate_limiting_not_the_demo_bearer(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """Regression for PR 12's CRITICAL_BLOCKER class, applied to the new route:
    a real navigation carries no Authorization header. RED if
    oauth_connect_start's dependency is switched to rate_limited_demo."""
    _register(oauth_client, "nav@example.com")
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="gh@example.com"
    )
    start = oauth_client.get("/v1/auth/oauth/github/connect/start", follow_redirects=False)
    assert start.status_code == 302
    assert start.headers["location"].startswith("https://github.com/login/oauth/authorize")
    callback = oauth_client.get(
        "/v1/auth/oauth/github/callback",
        params={"code": "auth-code-1", "state": _state_from_start_response(start)},
        follow_redirects=False,
    )
    assert callback.headers["location"] == "/?connect=ok&provider=github"

    # And the per-visitor limiter DOES apply: the route is not unlimited.
    import app.core.rate_limit as rate_limit

    monkeypatch.setenv("CITEVYN_RATE_LIMIT_DEMO_USER_REGISTERED_PER_HOUR", "1")
    get_settings.cache_clear()
    rate_limit.reset_limiter()
    assert _connect_start(oauth_client, "github").status_code == 302
    assert _connect_start(oauth_client, "github").status_code == 429
    monkeypatch.delenv("CITEVYN_RATE_LIMIT_DEMO_USER_REGISTERED_PER_HOUR", raising=False)
    get_settings.cache_clear()
    rate_limit.reset_limiter()


async def test_connect_concurrent_race_toward_different_target_accounts(tmp_path) -> None:
    """The round-1/round-2 security fix's own regression test. Two connects
    for the same external identity race toward DIFFERENT accounts; the loser
    hits the unique constraint. RED if _link_identity's IntegrityError branch
    returns success without comparing winner.user_id to its own target
    (the way the LOGIN path's analogous branch legitimately does)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.api.routes.oauth as oauth_module

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'link_race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    now = datetime.now(UTC)

    async with factory() as seed:
        seed.add_all(
            [
                User(user_id="usr_target", role=UserRole.demo_user, created_at=now),
                User(user_id="usr_other", role=UserRole.demo_user, created_at=now),
            ]
        )
        await seed.commit()

    identity = oauth_module._Identity(
        provider_account_id="race-777", email=None, email_verified=False
    )

    async def _run_with_competitor(competitor_user_id: str) -> oauth_module.LinkResult:
        fired: list[str] = []

        async def _competing_insert() -> None:
            async with factory() as competitor:
                competitor.add(
                    UserIdentity(
                        identity_id=uuid.uuid4(),
                        provider="github",
                        provider_account_id="race-777",
                        user_id=competitor_user_id,
                        created_at=now,
                    )
                )
                await competitor.commit()
            fired.append(competitor_user_id)

        async with factory() as session:
            original_execute = session.execute

            async def _execute_with_injected_race(stmt, *args, **kwargs):
                result = await original_execute(stmt, *args, **kwargs)
                if not fired and "user_identities" in str(stmt).lower():
                    await _competing_insert()  # between "not found" and our insert
                return result

            session.execute = _execute_with_injected_race  # type: ignore[method-assign]
            result = await oauth_module._link_identity(session, "github", identity, "usr_target")
        assert fired, "the injected race never fired -- this test would be vacuous"
        return result

    # Loser vs a DIFFERENT account: must be rejected, never a false LINKED.
    assert await _run_with_competitor("usr_other") is oauth_module.LinkResult.LINKED_ELSEWHERE
    async with factory() as verify:
        rows = (await verify.execute(select(UserIdentity))).scalars().all()
        assert len(rows) == 1 and rows[0].user_id == "usr_other"
        for row in rows:
            await verify.delete(row)
        await verify.commit()

    # Loser vs the SAME account (a double-click): idempotent success.
    assert await _run_with_competitor("usr_target") is oauth_module.LinkResult.ALREADY_LINKED_SAME
    async with factory() as verify:
        rows = (await verify.execute(select(UserIdentity))).scalars().all()
        assert len(rows) == 1 and rows[0].user_id == "usr_target"
    await engine.dispose()


def test_login_intent_is_completely_unaffected(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """The login tail moved into _handle_login_intent must behave exactly as
    before: intent "login" nonce, cookie ROTATES, a new usr_ account + identity
    is created, /?auth=ok. RED if the dispatch routes a login nonce to the
    connect handler."""
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="login@example.com"
    )
    start = _start(oauth_client, "github")
    assert _query_all(OAuthNonce)[0].return_intent == "login"
    cookie_before = oauth_client.cookies[_COOKIE]
    response = _callback(oauth_client, "github", state=_state_from_start_response(start))
    assert response.headers["location"] == "/?auth=ok"
    assert oauth_client.cookies[_COOKIE] != cookie_before, "login must rotate the cookie"
    assert len([u for u in _query_all(User) if u.user_id.startswith("usr_")]) == 1
    assert len(_query_all(UserIdentity)) == 1
    me = _me(oauth_client)
    assert me.json()["email"] == "login@example.com"
    assert me.json()["providers"] == ["github"]


def test_connect_nonce_cannot_complete_as_login_and_vice_versa(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """Proves return_intent is ENFORCED at callback, not merely stored. RED if
    the dispatch defaults to the login path for anything but "connect"
    (an unknown intent must fail closed), or ignores the field entirely."""
    # (a) A login-minted nonce from an ANONYMOUS browser, re-labelled "connect":
    #     the connect path must refuse (no usr_ session) and must NOT create
    #     the account the login path would have.
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="x@example.com"
    )
    start = _start(oauth_client, "github")
    state = _state_from_start_response(start)
    _set_nonce_intent(state, "connect")
    response = _callback(oauth_client, "github", state=state)
    assert response.headers["location"] == "/?connect=error&reason=session&provider=github"
    assert _query_all(UserIdentity) == []
    assert [u for u in _query_all(User) if u.user_id.startswith("usr_")] == []
    assert {"event": "oauth_connect_no_session", "provider": "github"} in _auth_failed_metadatas()

    # (b) An unrecognised intent fails closed -- it does not fall through to login.
    start2 = _start(oauth_client, "github")
    state2 = _state_from_start_response(start2)
    _set_nonce_intent(state2, "bogus")
    response2 = _callback(oauth_client, "github", state=state2)
    assert response2.headers["location"] == "/?auth=error"
    assert _query_all(UserIdentity) == []
    assert [u for u in _query_all(User) if u.user_id.startswith("usr_")] == []


def test_connect_provider_failure_after_claim_redirects_to_the_connect_error(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """A provider error mid-connect must not be reported as a failed SIGN-IN.
    RED if the intent-aware failure_location switch after the claim is removed."""
    import app.api.routes.oauth as oauth_module

    _register(oauth_client, "perr@example.com")
    cookie_before = oauth_client.cookies[_COOKIE]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    monkeypatch.setattr(
        oauth_module,
        "_build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    start = _connect_start(oauth_client, "github")
    response = _callback(oauth_client, "github", state=_state_from_start_response(start))
    assert response.headers["location"] == "/?connect=error&reason=provider&provider=github"
    assert {"event": "oauth_provider_error", "provider": "github"} in _auth_failed_metadatas()
    assert oauth_client.cookies[_COOKIE] == cookie_before
    assert _me(oauth_client).status_code == 200


def test_auth_me_lists_linked_providers(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """Zero / one / two providers, and the SAME field on register + login.
    RED if `providers` is added only to `me` (register/login would omit it)
    or if the query filters on the wrong column."""
    register = oauth_client.post(
        "/v1/auth/register",
        json={"email": "list@example.com", "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER},
    )
    assert register.json()["providers"] == []
    assert _me(oauth_client).json()["providers"] == []

    _link_identity_to(
        oauth_client, monkeypatch, "google", account_id=_GOOGLE_SUB, email="g@example.com"
    )
    assert _me(oauth_client).json()["providers"] == ["google"]

    _link_identity_to(
        oauth_client,
        monkeypatch,
        "github",
        account_id=str(_GITHUB_ACCOUNT_ID),
        email="gh@example.com",
    )
    assert _me(oauth_client).json()["providers"] == ["github", "google"]  # sorted

    # A password login on a fresh browser reports the same list.
    login = TestClient(create_app()).post(
        "/v1/auth/login",
        json={"email": "list@example.com", "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER},
    )
    assert login.status_code == 200
    assert login.json()["providers"] == ["github", "google"]

    # Another account's links never bleed into this one's list.
    other = TestClient(create_app())
    _register(other, "other@example.com")
    assert _me(other).json()["providers"] == []


def test_connect_failure_audit_event_records_the_metadata_event_string(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """Asserts the exact metadata dicts, not just that SOME auth_failed row
    exists. RED if any connect failure's event string is misspelled or the
    provider key is dropped."""
    # conflict
    browser_a = TestClient(create_app())
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="a@example.com"
    )
    start_a = _start(browser_a, "github")
    _callback(browser_a, "github", state=_state_from_start_response(start_a))
    account_b = _register(oauth_client, "b@example.com")
    _link_identity_to(
        oauth_client,
        monkeypatch,
        "github",
        account_id=str(_GITHUB_ACCOUNT_ID),
        email="a@example.com",
    )
    failed = [e for e in _query_all(AuditEvent) if e.action == AuditAction.auth_failed]
    assert [(e.user_id, e.metadata_) for e in failed] == [
        (account_b, {"event": "oauth_connect_conflict", "provider": "github"})
    ]

    # no session at callback
    anon = TestClient(create_app())
    _patch_provider(monkeypatch, "google", account_id="424242", email="anon@example.com")
    start_anon = _start(anon, "google")
    state_anon = _state_from_start_response(start_anon)
    _set_nonce_intent(state_anon, "connect")
    _callback(anon, "google", state=state_anon)
    no_session = [
        e for e in _query_all(AuditEvent) if e.metadata_.get("event") == "oauth_connect_no_session"
    ]
    assert [(e.user_id, e.metadata_) for e in no_session] == [
        (None, {"event": "oauth_connect_no_session", "provider": "google"})
    ]


def test_connect_denial_redirects_to_the_connect_error_and_consumes_the_nonce(
    oauth_client: TestClient,
) -> None:
    """Found live: cancelling the provider's consent screen mid-CONNECT showed
    "Sign-in failed" to a still-signed-in user. RED if the denial branch stops
    claiming the nonce / routing by its intent."""
    user_id = _register(oauth_client, "deny@example.com")
    start = _connect_start(oauth_client, "github")
    state = _state_from_start_response(start)
    assert len(_query_all(OAuthNonce)) == 1

    response = oauth_client.get(
        "/v1/auth/oauth/github/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/?connect=error&reason=denied&provider=github"
    assert _query_all(OAuthNonce) == [], "a declined nonce can never complete -- consume it"
    assert _query_all(UserIdentity) == []
    assert _me(oauth_client).status_code == 200, "still signed in"
    denied = [e for e in _query_all(AuditEvent) if e.metadata_.get("event") == "oauth_denied"]
    assert [(e.user_id, e.metadata_) for e in denied] == [
        (user_id, {"event": "oauth_denied", "provider": "github"})
    ]


def test_login_denial_is_unchanged_and_also_consumes_its_own_nonce(
    oauth_client: TestClient,
) -> None:
    start = _start(oauth_client, "github")
    state = _state_from_start_response(start)
    response = oauth_client.get(
        "/v1/auth/oauth/github/callback",
        params={"error": "access_denied", "state": state},
        headers={"Authorization": DEMO_BEARER},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/?auth=error"
    assert _query_all(OAuthNonce) == []


def test_denial_from_a_different_browser_does_not_burn_the_victims_nonce(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """The griefing guard from PR 12, applied to the new denial-time claim:
    an attacker replaying a victim's state with error=access_denied must
    delete NOTHING. RED if the denial claim drops the auth_session_id
    predicate."""
    _register(oauth_client, "victim@example.com")
    start = _connect_start(oauth_client, "github")
    state = _state_from_start_response(start)

    attacker = TestClient(create_app())
    attacker.post("/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER})
    response = attacker.get(
        "/v1/auth/oauth/github/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/?auth=error"
    assert len(_query_all(OAuthNonce)) == 1, "the victim's nonce must survive"

    # ...and the victim can still complete their connect afterward.
    _patch_provider(
        monkeypatch, "github", account_id=str(_GITHUB_ACCOUNT_ID), email="v@example.com"
    )
    victim = _callback(oauth_client, "github", state=state)
    assert victim.headers["location"] == "/?connect=ok&provider=github"


def test_auth_me_providers_is_a_set_even_with_two_identities_of_one_provider(
    monkeypatch: pytest.MonkeyPatch, oauth_client: TestClient
) -> None:
    """Review finding A3: nothing constrains one row per (user_id, provider),
    so two different GitHub accounts can be linked to one CiteVyn account.
    The wire field is a set of providers. RED if .distinct() is dropped from
    _linked_providers."""
    _register(oauth_client, "twogh@example.com")
    _link_identity_to(oauth_client, monkeypatch, "github", account_id="111", email="a@example.com")
    _link_identity_to(oauth_client, monkeypatch, "github", account_id="222", email="b@example.com")
    assert len(_query_all(UserIdentity)) == 2
    assert _me(oauth_client).json()["providers"] == ["github"]
