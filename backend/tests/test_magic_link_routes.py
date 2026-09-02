"""Tests for ``/v1/auth/magic-link/{request,confirm}`` (ADR-0004 PR 14).

Every docstring names the change that turns the test red. Delivery goes
through the dev file outbox (``CITEVYN_EMAIL_OUTBOX_DIR``), so the link a
test redeems is the one a real user would have received -- the route is
never bypassed to mint a token directly, except where a test needs to
tamper with a stored row (expiry).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update

from app.api.routes import magic_link as magic_link_module
from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import AuditEvent, Base, MagicLinkToken, Session, User

DEMO_BEARER = "Bearer local-demo-key"
_TOKEN_RE = re.compile(r"/v1/auth/magic-link/confirm\?token=([0-9a-f]{32}\.[0-9a-f]{64})")

_ENV_KEYS = (
    "CITEVYN_DATABASE_URL",
    "CITEVYN_EMAIL_OUTBOX_DIR",
    "CITEVYN_RESEND_API_KEY",
    "CITEVYN_EMAIL_FROM",
    "CITEVYN_MAGIC_LINK_BASE_URL",
    "CITEVYN_RATE_LIMIT_ENABLED",
    "CITEVYN_RATE_LIMIT_DEMO_USER_PER_HOUR",
    "CITEVYN_RATE_LIMIT_GLOBAL_PER_HOUR",
    "CITEVYN_RATE_LIMIT_AUTH_LOGIN_PER_HOUR",
    "CITEVYN_RATE_LIMIT_MAGIC_LINK_PER_HOUR",
    "CITEVYN_RATE_LIMIT_KEY_SALT",
)


@pytest.fixture
def magic_app_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Generator[Callable[..., Path], None, None]:
    """Build the app against a temp SQLite DB + a temp outbox; returns the outbox dir.

    Env overrides are set with ``setenv`` (never ``delenv``): a local
    ``backend/.env`` would otherwise feed real values into ``Settings()``.
    """
    import app.core.rate_limit as rate_limit

    def _build(**env: str) -> Path:
        db_module.reset_engine()
        get_settings.cache_clear()
        outbox = tmp_path / "outbox"
        monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'magic.db'}")
        monkeypatch.setenv("CITEVYN_EMAIL_OUTBOX_DIR", str(outbox))
        monkeypatch.setenv("CITEVYN_RESEND_API_KEY", "")
        monkeypatch.setenv("CITEVYN_EMAIL_FROM", "")
        monkeypatch.setenv("CITEVYN_MAGIC_LINK_BASE_URL", "")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        rate_limit.reset_limiter()
        engine = db_module.get_engine()

        async def _init_schema() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(_init_schema())
        return outbox

    try:
        yield _build
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()
        rate_limit.reset_limiter()
        for key in _ENV_KEYS:
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def magic_app(magic_app_factory: Callable[..., Path]) -> Path:
    return magic_app_factory()


def _client() -> TestClient:
    """A fresh client with its OWN cookie jar -- a distinct browser."""
    return TestClient(create_app())


def _register(client: TestClient, email: str, password: str = "correct horse battery"):
    response = client.post(
        "/v1/auth/register",
        json={"email": email, "password": password},
        headers={"Authorization": DEMO_BEARER},
    )
    assert response.status_code == 201, response.text
    return response


def _login(client: TestClient, email: str, password: str) -> httpx.Response:
    return client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
        headers={"Authorization": DEMO_BEARER},
    )


def _request_link(client: TestClient, email: str, **headers: str) -> httpx.Response:
    return client.post(
        "/v1/auth/magic-link/request",
        json={"email": email},
        headers={"Authorization": DEMO_BEARER, **headers},
    )


def _outbox_tokens(outbox: Path) -> list[str]:
    """Tokens from every outbox file, oldest first."""
    if not outbox.exists():
        return []
    tokens: list[str] = []
    for path in sorted(outbox.iterdir()):
        match = _TOKEN_RE.search(path.read_text(encoding="utf-8"))
        assert match is not None, path.read_text(encoding="utf-8")
        tokens.append(match.group(1))
    return tokens


def _latest_token(outbox: Path) -> str:
    tokens = _outbox_tokens(outbox)
    assert tokens, "expected at least one email in the outbox"
    return tokens[-1]


def _confirm_get(client: TestClient, token: str) -> httpx.Response:
    return client.get(
        "/v1/auth/magic-link/confirm", params={"token": token}, follow_redirects=False
    )


def _confirm_post(client: TestClient, token: str, **headers: str) -> httpx.Response:
    return client.post(
        "/v1/auth/magic-link/confirm",
        content=f"token={token}",
        headers={"Content-Type": "application/x-www-form-urlencoded", **headers},
        follow_redirects=False,
    )


def _me(client: TestClient) -> httpx.Response:
    return client.get("/v1/auth/me", headers={"Authorization": DEMO_BEARER})


def _run(coro):
    return asyncio.run(coro)


def _query_all(model: type) -> list:
    async def _go() -> list:
        factory = get_sessionmaker()
        async with factory() as session:
            return list((await session.execute(select(model))).scalars().all())

    return _run(_go())


def _audit_events() -> list[tuple[str, str]]:
    return [(str(row.action), row.metadata_.get("event", "")) for row in _query_all(AuditEvent)]


def _password_hash(email: str) -> str | None:
    users = [u for u in _query_all(User) if u.email == email]
    assert len(users) == 1
    return users[0].password_hash


# ---------------------------------------------------------------------------
# POST /request
# ---------------------------------------------------------------------------


def test_request_always_returns_202_regardless_of_match(magic_app: Path) -> None:
    """Plan test 1. RED if the no-match branch returns anything but the same
    202 + body shape (a 404/422 there is an enumeration oracle)."""
    _register(_client(), "real@example.com")
    client = _client()
    hit = _request_link(client, "real@example.com")
    miss = _request_link(client, "nobody@example.com")
    assert hit.status_code == miss.status_code == 202
    assert set(hit.json()) == set(miss.json()) == {"request_id", "status"}
    assert hit.json()["status"] == miss.json()["status"] == "accepted"


def test_request_runs_the_same_statement_count_whether_or_not_the_email_exists(
    magic_app: Path,
) -> None:
    """Plan test 2, the timing-oracle regression, as a white-box check that the
    symmetric work actually happens rather than a flaky wall-clock comparison.
    RED if the no-match branch's discarded DELETE or its audit INSERT is removed
    (statement counts diverge), or if the match branch gains a query the
    no-match branch does not run.
    """
    _register(_client(), "real@example.com")
    engine = db_module.get_engine()
    verbs: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        verbs.append(statement.strip().split()[0].upper())

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        client = _client()
        verbs.clear()
        assert _request_link(client, "real@example.com").status_code == 202
        match_verbs = list(verbs)
        verbs.clear()
        assert _request_link(client, "nobody@example.com").status_code == 202
        miss_verbs = list(verbs)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert len(match_verbs) == len(miss_verbs), (match_verbs, miss_verbs)
    # Both branches really do write: one DELETE-by-user each, and the match
    # branch's token INSERT is mirrored by the no-match branch's discarded
    # DELETE; both write one audit row.
    assert match_verbs.count("INSERT") == 2 and match_verbs.count("DELETE") == 1
    assert miss_verbs.count("INSERT") == 1 and miss_verbs.count("DELETE") == 2

    # And the audit trail shows both branches did their (different) write.
    events = _audit_events()
    assert ("login", "magic_link_requested") in events
    assert ("auth_failed", "magic_link_unknown_email") in events


def test_request_never_emails_an_unregistered_address(magic_app: Path) -> None:
    """RED if the no-match branch registers the real send task."""
    _register(_client(), "real@example.com")
    _request_link(_client(), "nobody@example.com")
    assert _outbox_tokens(magic_app) == []
    _request_link(_client(), "real@example.com")
    assert len(_outbox_tokens(magic_app)) == 1


def test_request_uses_its_own_rate_limit_bucket_not_auth_login(
    magic_app_factory: Callable[..., Path],
) -> None:
    """Plan test 3. RED if ``enforce_magic_link_rate_limit`` is replaced by
    ``enforce_auth_login_rate_limit``: flooding link requests would then lock
    the victim out of password login, and vice versa."""
    magic_app_factory(
        CITEVYN_RATE_LIMIT_ENABLED="true",
        CITEVYN_RATE_LIMIT_DEMO_USER_PER_HOUR="1000",
        CITEVYN_RATE_LIMIT_GLOBAL_PER_HOUR="1000",
        # register consumes one auth_login hit itself, so 4 leaves 3 logins.
        CITEVYN_RATE_LIMIT_AUTH_LOGIN_PER_HOUR="4",
        CITEVYN_RATE_LIMIT_MAGIC_LINK_PER_HOUR="2",
        CITEVYN_RATE_LIMIT_KEY_SALT="magic-link-test-salt",
    )
    _register(_client(), "victim@example.com")
    client = _client()

    # Exhaust the magic-link bucket for the victim...
    responses = [_request_link(client, "victim@example.com") for _ in range(3)]
    assert [r.status_code for r in responses] == [202, 202, 429]
    # ...with a message about sign-in links, not "queries" (it is shown in the dialog).
    assert "sign-in links" in responses[2].json()["error"]["message"]
    # ...and password login for that same email is untouched: its own bucket
    # still starts empty (3 allowed, the 4th is the limiter, not the password).
    assert [_login(client, "victim@example.com", "wrong").status_code for _ in range(4)] == [
        401,
        401,
        401,
        429,
    ]

    # The reverse direction, on a second address: a login flood does not
    # consume the magic-link allowance.
    _register(_client(), "other@example.com")
    for _ in range(4):
        _login(client, "other@example.com", "wrong")
    assert _request_link(client, "other@example.com").status_code == 202


def test_request_rate_limit_applies_to_unknown_emails_too(
    magic_app_factory: Callable[..., Path],
) -> None:
    """RED if the limiter is called only on the match branch -- rate-limiting
    real accounts alone is itself an enumeration signal."""
    magic_app_factory(
        CITEVYN_RATE_LIMIT_ENABLED="true",
        CITEVYN_RATE_LIMIT_DEMO_USER_PER_HOUR="1000",
        CITEVYN_RATE_LIMIT_GLOBAL_PER_HOUR="1000",
        CITEVYN_RATE_LIMIT_MAGIC_LINK_PER_HOUR="2",
    )
    client = _client()
    assert [_request_link(client, "ghost@example.com").status_code for _ in range(3)] == [
        202,
        202,
        429,
    ]


def test_issuing_a_new_token_invalidates_the_users_prior_live_token(magic_app: Path) -> None:
    """Plan test 4. RED if the delete-prior-tokens statement is removed: the
    older, still-unread email would remain redeemable."""
    _register(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    first, second = _outbox_tokens(magic_app)
    assert len(_query_all(MagicLinkToken)) == 1

    assert _confirm_post(_client(), first).headers["location"] == "/?auth=error"
    assert _confirm_post(_client(), second).headers["location"] == "/?auth=ok"


def test_request_sends_to_the_stored_canonical_address_and_uses_the_configured_base_url(
    magic_app_factory: Callable[..., Path],
) -> None:
    """RED if the link is built from ``request.base_url`` (host-header
    poisoning) or the recipient is the raw, un-normalised input."""
    outbox = magic_app_factory(CITEVYN_MAGIC_LINK_BASE_URL="https://citevyn.example/")
    _register(_client(), "Mixed.Case@Example.com")
    assert _request_link(_client(), "  MIXED.case@example.COM ").status_code == 202
    content = next(outbox.iterdir()).read_text(encoding="utf-8")
    assert "To: mixed.case@example.com" in content
    assert "https://citevyn.example/v1/auth/magic-link/confirm?token=" in content
    assert "testserver" not in content


def test_request_is_404_when_no_email_provider_is_available(
    magic_app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production-without-a-provider behaviour, at the route: RED if the route
    proceeds (and 500s, or silently drops the email) with no client."""
    monkeypatch.setattr(magic_link_module, "_build_email_client", lambda settings: None)
    _register(_client(), "real@example.com")
    assert _request_link(_client(), "real@example.com").status_code == 404
    assert _query_all(MagicLinkToken) == []


def test_request_rejects_a_malformed_email_with_422(magic_app: Path) -> None:
    assert _request_link(_client(), "not-an-email").status_code == 422


# ---------------------------------------------------------------------------
# GET /confirm  (the interstitial)
# ---------------------------------------------------------------------------


def test_confirm_get_does_not_consume_the_token(magic_app: Path) -> None:
    """Plan test 5, the scanner-safety regression. RED if the GET deletes the
    row, sets a cookie, or auto-submits (script / meta-refresh / onload)."""
    _register(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    token = _latest_token(magic_app)

    scanner = _client()
    for _ in range(3):  # a scanner may fetch more than once
        page = _confirm_get(scanner, token)
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "set-cookie" not in page.headers
    assert len(_query_all(MagicLinkToken)) == 1
    assert _me(scanner).status_code == 401

    body = page.text
    assert '<form method="post" action="/v1/auth/magic-link/confirm">' in body
    assert f'name="token" value="{token}"' in body
    assert "<script" not in body.lower()
    assert "http-equiv" not in body.lower()
    assert "onload" not in body.lower()
    assert page.headers["cache-control"] == "no-store"
    # same-origin, NOT no-referrer: under no-referrer Chromium nulls the
    # form POST's Origin header (found live) -- see the module docstring.
    assert '<meta name="referrer" content="same-origin">' in body
    assert "no-referrer" not in body

    # The real user's click still works afterwards.
    assert _confirm_post(_client(), token).headers["location"] == "/?auth=ok"


def test_confirm_get_renders_the_error_page_for_a_bad_or_expired_link(magic_app: Path) -> None:
    """RED if the invalid branch still renders a form (it would POST garbage),
    500s on a malformed token, answers a JSON 422 for an over-long token (a
    ``Query(max_length=...)`` would), or lets a wrong secret / an expired row
    through the read-only check."""
    _register(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    real = _latest_token(magic_app)
    token_id, _, _secret = real.partition(".")
    wrong_secret = f"{token_id}.{'0' * 64}"
    over_long = f"{token_id}.{'f' * 300}"
    for token in (
        "",
        "garbage",
        "not-a-uuid.abc",
        f"{'0' * 32}.{'f' * 64}",
        wrong_secret,
        over_long,
    ):
        page = _confirm_get(_client(), token)
        assert page.status_code == 200, token
        assert page.headers["content-type"].startswith("text/html"), token
        assert "invalid or has expired" in page.text, token
        assert "<form" not in page.text, token

    async def _expire() -> None:
        factory = get_sessionmaker()
        async with factory() as session:
            await session.execute(
                update(MagicLinkToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

    _run(_expire())
    page = _confirm_get(_client(), real)
    assert "invalid or has expired" in page.text
    assert "<form" not in page.text
    assert len(_query_all(MagicLinkToken)) == 1, "the read-only GET never consumes, even expired"


def test_confirm_page_and_email_quote_the_configured_ttl(
    magic_app_factory: Callable[..., Path],
) -> None:
    """RED if the interstitial or the email hard-code "10 minutes" while
    ``CITEVYN_MAGIC_LINK_TTL_SECONDS`` says otherwise (review finding)."""
    outbox = magic_app_factory(CITEVYN_MAGIC_LINK_TTL_SECONDS="120")
    _register(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    assert "expires in 2 minutes" in next(outbox.iterdir()).read_text(encoding="utf-8")
    assert "expire 2 minutes" in _confirm_get(_client(), "garbage").text


# ---------------------------------------------------------------------------
# POST /confirm  (the claim)
# ---------------------------------------------------------------------------


def test_confirm_post_atomically_claims_and_logs_in(magic_app: Path) -> None:
    """Plan test 6. RED if the claim does not delete the row, does not call
    ``claim_and_login`` (no cookie / 401 on /me), or touches the password."""
    _register(_client(), "real@example.com")
    hash_before = _password_hash("real@example.com")
    _request_link(_client(), "real@example.com")
    token = _latest_token(magic_app)

    browser = _client()
    response = _confirm_post(browser, token)
    assert response.status_code == 302
    assert response.headers["location"] == "/?auth=ok"
    assert "set-cookie" in response.headers

    me = _me(browser)
    assert me.status_code == 200
    assert me.json()["email"] == "real@example.com"
    assert me.json()["has_password"] is True
    assert _query_all(MagicLinkToken) == []
    assert _password_hash("real@example.com") == hash_before
    assert ("login", "magic_link") in _audit_events()


def test_confirm_post_reused_token_fails_closed(magic_app: Path) -> None:
    """Plan test 7. RED if a consumed token still redeems (the row is not
    deleted on success) or if failure sets a cookie. A SEQUENTIAL replay
    cannot tell an atomic claim from a SELECT-then-DELETE -- that shape is
    pinned by ``test_confirm_post_claims_with_one_conditional_delete`` below."""
    _register(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    token = _latest_token(magic_app)
    assert _confirm_post(_client(), token).headers["location"] == "/?auth=ok"

    replay = _client()
    response = _confirm_post(replay, token)
    assert response.status_code == 302
    assert response.headers["location"] == "/?auth=error"
    assert "set-cookie" not in response.headers
    assert _me(replay).status_code == 401
    assert ("auth_failed", "magic_link_invalid") in _audit_events()


def test_confirm_post_claims_with_one_conditional_delete(magic_app: Path) -> None:
    """The atomic-claim SHAPE, white-box (a sequential test cannot see a race):
    during a successful POST exactly one statement touches
    ``magic_link_tokens`` -- a DELETE ... RETURNING whose WHERE names both
    ``token_id`` and ``secret_hash`` -- and no SELECT on the table precedes
    it. RED if the claim becomes SELECT-then-DELETE (two statements) or drops
    a predicate."""
    _register(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    token = _latest_token(magic_app)
    engine = db_module.get_engine()
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        assert _confirm_post(_client(), token).headers["location"] == "/?auth=ok"
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    touching = [s for s in statements if "magic_link_tokens" in s]
    assert len(touching) == 1, touching
    claim = touching[0].upper()
    assert claim.startswith("DELETE FROM MAGIC_LINK_TOKENS")
    assert "RETURNING" in claim
    assert "TOKEN_ID" in claim and "SECRET_HASH" in claim


def test_confirm_post_fails_closed_when_the_user_row_is_gone(magic_app: Path) -> None:
    """Defense in depth (step 4 of the claim): the FK cascade normally takes
    the token with the user, but SQLite's FK enforcement is off here (#286),
    which conveniently models the delete/claim race. RED if the missing-user
    guard is removed: on Postgres ``claim_and_login`` would 500 on the FK; on
    this SQLite harness it would mint a ghost session for a deleted user
    (302 ``/?auth=ok``) -- either way not the ``/?auth=error`` asserted here."""
    _register(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    token = _latest_token(magic_app)

    async def _delete_user() -> None:
        factory = get_sessionmaker()
        async with factory() as session:
            user = (
                await session.execute(select(User).where(User.email == "real@example.com"))
            ).scalar_one()
            await session.delete(user)
            await session.commit()

    _run(_delete_user())
    assert len(_query_all(MagicLinkToken)) == 1, "precondition: the token row outlived the user"
    response = _confirm_post(_client(), token)
    assert response.status_code == 302
    assert response.headers["location"] == "/?auth=error"


def test_confirm_post_accepts_each_same_origin_signal_on_its_own(magic_app: Path) -> None:
    """The accepting half of the origin matrix: a matching ``Origin`` alone (an
    older browser with no Sec-Fetch-Site) and ``Sec-Fetch-Site: none`` alone
    (a typed/bookmarked navigation) both pass. RED if the guard demands both
    headers or treats ``none`` as cross-site."""
    _register(_client(), "real@example.com")
    for headers in ({"Origin": "http://localhost:8000"}, {"Sec-Fetch-Site": "none"}):
        _request_link(_client(), "real@example.com")
        browser = _client()
        ok = _confirm_post(browser, _latest_token(magic_app), **headers)
        assert ok.headers["location"] == "/?auth=ok", headers
        assert _me(browser).status_code == 200, headers


def test_confirm_post_expired_token_fails_closed(magic_app: Path) -> None:
    """Plan test 8. RED if the expiry check on the claimed row is dropped."""
    _register(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    token = _latest_token(magic_app)

    async def _expire() -> None:
        factory = get_sessionmaker()
        async with factory() as session:
            await session.execute(
                update(MagicLinkToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

    _run(_expire())

    browser = _client()
    response = _confirm_post(browser, token)
    assert response.headers["location"] == "/?auth=error"
    assert _me(browser).status_code == 401
    assert ("auth_failed", "magic_link_expired") in _audit_events()
    # Consumed on the way out -- a dead token has no reason to linger.
    assert _query_all(MagicLinkToken) == []


@pytest.mark.parametrize(
    "token",
    ["", "garbage", "not-a-uuid.secret", "0" * 32, f"{'0' * 32}.", "x" * 500],
)
def test_confirm_post_malformed_token_fails_closed_not_500(magic_app: Path, token: str) -> None:
    """Plan test 9, mirroring ``oauth_callback``'s ``_fail`` for garbage input."""
    response = _confirm_post(_client(), token)
    assert response.status_code == 302
    assert response.headers["location"] == "/?auth=error"


def test_confirm_post_with_a_non_form_body_fails_closed(magic_app: Path) -> None:
    response = _client().post(
        "/v1/auth/magic-link/confirm",
        json={"token": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/?auth=error"


def test_confirm_post_with_the_wrong_secret_consumes_nothing(magic_app: Path) -> None:
    """The griefing guard: a guess at the secret must not burn the real user's
    link. RED if the DELETE's WHERE clause drops the ``secret_hash`` predicate."""
    _register(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    token = _latest_token(magic_app)
    token_id, _, _secret = token.partition(".")

    attacker = _client()
    response = _confirm_post(attacker, f"{token_id}.{'0' * 64}")
    assert response.headers["location"] == "/?auth=error"
    assert _me(attacker).status_code == 401
    assert len(_query_all(MagicLinkToken)) == 1

    assert _confirm_post(_client(), token).headers["location"] == "/?auth=ok"


def test_confirm_post_from_a_cross_site_origin_is_rejected_without_consuming(
    magic_app: Path,
) -> None:
    """The login-CSRF guard. RED if ``_origin_allowed`` is removed or is checked
    AFTER the claim: a hostile page could log a victim's browser into the
    attacker's account, and the check must not burn the link either."""
    _register(_client(), "attacker@example.com")
    _request_link(_client(), "attacker@example.com")
    token = _latest_token(magic_app)

    victim = _client()
    for headers in (
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ):
        response = _confirm_post(victim, token, **headers)
        assert response.headers["location"] == "/?auth=error", headers
        assert "set-cookie" not in response.headers, headers
    assert _me(victim).status_code == 401
    assert len(_query_all(MagicLinkToken)) == 1
    assert ("auth_failed", "magic_link_origin_rejected") in _audit_events()

    # A same-origin submission (what the interstitial's form actually sends)
    # is accepted; the configured base URL is the expected origin.
    ok = _confirm_post(
        _client(), token, Origin="http://localhost:8000", **{"Sec-Fetch-Site": "same-origin"}
    )
    assert ok.headers["location"] == "/?auth=ok"


def test_confirm_post_accepts_chromiums_null_origin_when_sec_fetch_site_vouches(
    magic_app: Path,
) -> None:
    """The live-found regression: a real Chromium form POST from the interstitial
    arrived with ``Origin: null`` + ``Sec-Fetch-Site: same-origin`` and the first
    version of the guard (Origin checked first) rejected every genuine click.
    RED if ``_origin_allowed`` reads ``Origin`` before ``Sec-Fetch-Site`` or
    refuses the literal ``null`` when Sec-Fetch-Site has vouched. The two
    headers must still AGREE: a mismatched real Origin next to a same-origin
    Sec-Fetch-Site is refused.
    """
    _register(_client(), "real@example.com")
    _request_link(_client(), "real@example.com")
    token = _latest_token(magic_app)

    contradictory = _confirm_post(
        _client(), token, Origin="https://evil.example", **{"Sec-Fetch-Site": "same-origin"}
    )
    assert contradictory.headers["location"] == "/?auth=error"
    assert len(_query_all(MagicLinkToken)) == 1

    browser = _client()
    ok = _confirm_post(browser, token, Origin="null", **{"Sec-Fetch-Site": "same-origin"})
    assert ok.headers["location"] == "/?auth=ok"
    assert _me(browser).status_code == 200


def test_confirm_post_claims_the_anonymous_sessions_history(magic_app: Path) -> None:
    """The shared login tail: chat started before the link was clicked must
    survive it, exactly as password/OAuth login do. RED if the route bypasses
    ``claim_and_login``."""
    _register(_client(), "real@example.com")
    browser = _client()
    created = browser.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    session_id = created.json()["session_id"]

    _request_link(browser, "real@example.com")
    assert _confirm_post(browser, _latest_token(magic_app)).headers["location"] == "/?auth=ok"

    fetched = browser.get(f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER})
    assert fetched.status_code == 200
    owners = {str(row.user_id) for row in _query_all(Session)}
    assert owners == {_me(browser).json()["user_id"]}
