"""Retiring the public token, step 1 (ADR-0005 §3): the fixed header path.

The browser now proves a request came from our own page with a fixed custom
header, ``X-CiteVyn-Client: web``, instead of a secret-looking token. A
cross-site page cannot set a custom header without a CORS preflight, and CORS
grants that only to our own origins. Where the browser tells us where the
request came from (``Origin``, ``Sec-Fetch-Site``, both of which a page cannot
forge), that must be our own site too.

Step 1 accepts EITHER the old bearer token OR the new header, so bundles already
open in someone's browser keep working. Step 2 (owner-deployed, later) removes
the token.

Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.request_origin import is_same_site_request
from app.core.security import CLIENT_HEADER, CLIENT_HEADER_VALUE, require_public_client_token

OURS = "http://localhost:3000"  # the default CORS allowlist entry
EVIL = "https://evil.example"
WEB = {CLIENT_HEADER: CLIENT_HEADER_VALUE}


def _protected_app() -> FastAPI:
    app = FastAPI()

    @app.post("/protected")
    def protected(user_id: Annotated[str, Depends(require_public_client_token)]) -> dict[str, str]:
        return {"user_id": user_id}

    return app


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    get_settings.cache_clear()
    yield TestClient(_protected_app())
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The pure origin rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fetch_site", "origin", "expected"),
    [
        # No browser signal at all: a same-origin GET (browsers omit Origin) or a
        # non-browser client. The custom header alone is the guard.
        (None, None, True),
        ("same-origin", None, True),
        ("none", None, True),
        ("same-origin", OURS, True),
        ("same-site", OURS, True),  # dev: Vite on :3000 calling the API on :8000
        # An allowlisted origin is trusted whatever Sec-Fetch-Site says: the CORS
        # allowlist is the authority on which other origins may call us.
        ("cross-site", OURS, True),
        # A foreign origin is refused.
        (None, EVIL, False),
        ("cross-site", EVIL, False),
        ("same-origin", EVIL, False),
        # A cross-site request that hides its Origin is refused.
        ("cross-site", None, False),
        ("same-site", None, False),
        # Origin "null" (a sandboxed frame, or Chromium under no-referrer) only
        # when Sec-Fetch-Site vouches that it is same-origin.
        ("same-origin", "null", True),
        (None, "null", False),
        ("cross-site", "null", False),
        # Trailing slash and case do not matter.
        (None, OURS + "/", True),
        (None, OURS.upper(), True),
    ],
)
def test_is_same_site_request(fetch_site: str | None, origin: str | None, expected: bool) -> None:
    """Turns red if: any branch of the rule changes."""
    assert is_same_site_request(fetch_site=fetch_site, origin=origin, allowed={OURS}) is expected


def test_an_empty_allowlist_trusts_no_origin() -> None:
    """Turns red if: an empty allowlist is read as "allow any"."""
    assert is_same_site_request(fetch_site=None, origin=OURS, allowed=set()) is False


# ---------------------------------------------------------------------------
# The dependency: either path
# ---------------------------------------------------------------------------


def test_the_header_alone_is_accepted(client: TestClient) -> None:
    """Turns red if: the new path is not accepted (step 1's whole point)."""
    res = client.post("/protected", headers=WEB)
    assert res.status_code == 200
    assert res.json() == {"user_id": "demo_user"}


def test_the_header_from_our_origin_is_accepted(client: TestClient) -> None:
    res = client.post("/protected", headers={**WEB, "Origin": OURS, "Sec-Fetch-Site": "same-site"})
    assert res.status_code == 200


def test_the_header_from_a_foreign_origin_is_refused(client: TestClient) -> None:
    """THE forgery test. A foreign page that somehow got the header onto a
    request (e.g. a CORS misconfiguration) is still refused by its Origin.
    Turns red if: the origin check is skipped on the header path."""
    res = client.post("/protected", headers={**WEB, "Origin": EVIL})
    assert res.status_code == 401
    assert res.json()["detail"]["error"]["code"] == "auth_required"
    assert res.json()["detail"]["error"]["message"] == "Cross-site request refused."


def test_a_cross_site_request_hiding_its_origin_is_refused(client: TestClient) -> None:
    """Turns red if: a missing Origin is trusted even when Sec-Fetch-Site says cross-site."""
    res = client.post("/protected", headers={**WEB, "Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 401


@pytest.mark.parametrize("value", ["", "Web", "WEB", "mobile", "local-demo-key"])
def test_a_wrong_header_value_is_not_the_header(client: TestClient, value: str) -> None:
    """The value is fixed and exact. Turns red if: any value, or a
    case-insensitive match, is accepted."""
    res = client.post("/protected", headers={CLIENT_HEADER: value})
    assert res.status_code == 401
    assert res.json()["detail"]["error"]["message"] == "Missing bearer token."


def test_the_old_bearer_token_is_still_accepted(client: TestClient) -> None:
    """Step 1 keeps the old path for bundles already open in browsers.
    Turns red if: step 2's removal lands early."""
    res = client.post("/protected", headers={"Authorization": "Bearer local-demo-key"})
    assert res.status_code == 200


def test_a_wrong_bearer_token_is_refused_even_with_the_header(client: TestClient) -> None:
    """A present-but-wrong token is an error, not something the header papers over:
    the token path must never be weakened by the new one.
    Turns red if: a wrong bearer falls through to the header path."""
    res = client.post("/protected", headers={**WEB, "Authorization": "Bearer wrong"})
    assert res.status_code == 401
    assert res.json()["detail"]["error"]["message"] == "Invalid bearer token."


def test_neither_path_is_refused(client: TestClient) -> None:
    """Partner for every "accepted" test: without either credential, refused.
    Turns red if: the dependency accepts a bare request."""
    res = client.post("/protected")
    assert res.status_code == 401
    assert res.json()["detail"]["error"]["message"] == "Missing bearer token."


# ---------------------------------------------------------------------------
# Through the real app
# ---------------------------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Generator[None, None, None]:
    import app.core.rate_limit as rate_limit
    from app.core import db as db_module
    from app.models import Base

    db_module.reset_engine()
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'h.db'}")
    get_settings.cache_clear()
    rate_limit.reset_limiter()

    async def _init() -> None:
        async with db_module.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield
    get_settings.cache_clear()
    db_module.reset_engine()
    rate_limit.reset_limiter()


def test_the_real_app_creates_a_session_with_the_header_only(app_db: None) -> None:
    """End to end: a real route, header only, no token. Turns red if: any
    dependency on the chat path still demands the bearer token."""
    from app.main import create_app

    with TestClient(create_app()) as app_client:
        ok = app_client.post("/v1/sessions", json={"channel": "chat"}, headers=WEB)
        forged = app_client.post(
            "/v1/sessions", json={"channel": "chat"}, headers={**WEB, "Origin": EVIL}
        )
    assert ok.status_code == 201
    assert forged.status_code == 401
    assert forged.json()["error"]["code"] == "auth_required"


def test_production_refuses_a_wildcard_cors_origin() -> None:
    """A ``*`` would grant every site the preflight, and with the token public
    that opens the bearer path to any page. cors.py always said wildcards are
    forbidden; nothing enforced it. Turns red if: production accepts ``*``."""
    from pydantic import ValidationError

    from app.core.config import Settings

    prod = {
        "environment": "production",
        "llm_provider": "gemini",
        "embedding_provider": "gemini",
        "gemini_api_key": "gk",
        "admin_api_key": "a-strong-admin-secret",
        "public_client_token": "a-strong-demo-secret",
        "_env_file": None,
    }
    with pytest.raises(ValidationError, match="wildcard"):
        Settings(**prod, cors_allowed_origins=["https://citevyn.example.com", "*"])  # type: ignore[arg-type]
    # Partner: a real origin list starts.
    ok = Settings(**prod, cors_allowed_origins=["https://citevyn.example.com"])  # type: ignore[arg-type]
    assert ok.cors_allowed_origins == ["https://citevyn.example.com"]


def test_cors_lets_our_origin_send_the_header_and_nobody_else() -> None:
    """The preflight is what stops a foreign page from setting the header at all.
    Turns red if: the header is missing from CORS allow_headers (our own
    cross-origin dev frontend would break), or a foreign origin is allowed."""
    from app.main import create_app

    with TestClient(create_app()) as app_client:
        ours = app_client.options(
            "/v1/sessions",
            headers={
                "Origin": OURS,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": CLIENT_HEADER,
            },
        )
        evil = app_client.options(
            "/v1/sessions",
            headers={
                "Origin": EVIL,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": CLIENT_HEADER,
            },
        )
    assert ours.status_code == 200
    assert ours.headers["access-control-allow-origin"] == OURS
    assert CLIENT_HEADER.lower() in ours.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-origin" not in evil.headers


def test_our_own_site_origin_is_trusted_without_being_in_the_cors_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production serves the frontend from the API's own origin, which is the
    configured site URL; it must be trusted even if the CORS list omits it.
    Turns red if: the site origin is not added to the allowed set."""
    monkeypatch.setenv("CITEVYN_MAGIC_LINK_BASE_URL", "https://citevyn.example.com/")
    monkeypatch.setenv("CITEVYN_CORS_ALLOWED_ORIGINS", "http://localhost:3000")
    get_settings.cache_clear()
    try:
        res = TestClient(_protected_app()).post(
            "/protected", headers={**WEB, "Origin": "https://citevyn.example.com"}
        )
    finally:
        get_settings.cache_clear()
    assert res.status_code == 200


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://citevyn.example.com/some/path", {"https://citevyn.example.com"}),
        ("https://user@citevyn.example.com", {"https://citevyn.example.com"}),
        ("https://CITEVYN.example.com:8443/", {"https://citevyn.example.com:8443"}),
        # No scheme: not a URL we can derive an origin from. Nothing is added,
        # rather than the meaningless "://" the first version produced.
        ("citevyn.example.com", set()),
    ],
)
def test_the_site_origin_is_scheme_host_and_port_only(base_url: str, expected: set[str]) -> None:
    """Turns red if: userinfo or a path leaks into the origin, or a scheme-less
    URL adds a junk entry."""
    from app.core.config import Settings
    from app.core.security import _allowed_origins

    s = Settings(magic_link_base_url=base_url, cors_allowed_origins=[], _env_file=None)  # type: ignore[call-arg]
    assert _allowed_origins(s) == expected


def test_a_wildcard_cors_origin_is_still_allowed_outside_production() -> None:
    """Local tooling may use ``*``; only production is refused.
    Turns red if: the wildcard guard fires in every environment."""
    from app.core.config import Settings

    s = Settings(environment="local", cors_allowed_origins=["*"], _env_file=None)  # type: ignore[call-arg]
    assert s.cors_allowed_origins == ["*"]
