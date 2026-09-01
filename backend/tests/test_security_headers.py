"""Tests for :mod:`app.core.security_headers` (ADR-0004 PR 2).

Asserts headers on **two** distinct response paths on purpose: a router
response (``GET /health`` via the real app) and a ``StaticFiles`` mount
response (``GET /`` — the browser-bundle path). ``BaseHTTPMiddleware`` wraps
the whole ASGI stack so both should carry the headers, but the two code
paths are different enough (one is FastAPI's JSON response machinery, the
other is Starlette's static-file response) that asserting only one would
leave the other unverified.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.routing import Route

from app.core import db as db_module
from app.core.config import Settings, get_settings
from app.core.security_headers import configure_security_headers
from app.main import create_app
from app.models import Base

_EXPECTED_STATIC_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
}


def _production_settings(**overrides: object) -> Settings:
    """A minimally-valid ``environment='production'`` Settings.

    Production rejects the stub LLM provider at parse time (a real guard,
    unrelated to this PR) — ``anthropic`` + a placeholder key is the
    smallest config that satisfies it without touching the embedding
    provider guards (which only fire for ``gemini``/``openrouter``, so the
    default ``embedding_provider="stub"`` needs no key here).
    """
    return Settings(
        environment="production",
        llm_provider="anthropic",
        anthropic_api_key="test-anthropic-key",
        demo_api_key="a-strong-demo-key-not-the-default-1234",
        admin_api_key="a-strong-admin-key-not-the-default-1234",
        _env_file=None,
        **overrides,  # type: ignore[arg-type]
    )


def _parse_csp(csp: str) -> dict[str, set[str]]:
    """Split a ``Content-Security-Policy`` header into ``{directive: {tokens}}``.

    Parsed into exact tokens rather than checked with `substring in csp` —
    CodeQL's ``py/incomplete-url-substring-sanitization`` flags the latter
    pattern (an origin string could appear as a substring at an arbitrary,
    unintended position), and parsing into directive tokens is also a
    strictly stronger assertion: it proves the origin is actually a value of
    the *specific* directive, not merely present somewhere in the header.
    """
    directives: dict[str, set[str]] = {}
    for part in csp.split(";"):
        tokens = part.strip().split()
        if not tokens:
            continue
        directives[tokens[0]] = set(tokens[1:])
    return directives


def _assert_common_headers(headers: dict[str, str]) -> None:
    for name, value in _EXPECTED_STATIC_HEADERS.items():
        assert headers[name] == value, f"{name}: expected {value!r}, got {headers.get(name)!r}"
    directives = _parse_csp(headers["content-security-policy"])
    assert directives["default-src"] == {"'self'"}
    assert directives["script-src"] == {"'self'"}
    assert directives["frame-ancestors"] == {"'none'"}
    # The two third-party origins the shipped frontend actually loads
    # (frontend/index.html): Google Fonts + Fontshare stylesheets, and the
    # font files those stylesheets reference.
    assert directives["style-src"] == {
        "'self'",
        "https://fonts.googleapis.com",
        "https://api.fontshare.com",
    }
    assert directives["font-src"] == {
        "'self'",
        "https://fonts.gstatic.com",
        "https://api.fontshare.com",
    }


# ---------------------------------------------------------------------------
# StaticFiles mount path — a minimal app, not the real frontend_dist bundle
# ---------------------------------------------------------------------------


def test_headers_present_on_a_staticfiles_response(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<html><body>ok</body></html>")

    app = FastAPI()
    configure_security_headers(app, Settings(environment="local", _env_file=None))
    app.mount("/", StaticFiles(directory=tmp_path, html=True), name="static")

    response = TestClient(app).get("/")

    assert response.status_code == 200
    _assert_common_headers(dict(response.headers))
    # local (non-production) settings must NOT set HSTS — see the docstring
    # in security_headers.py: HSTS over a plain-HTTP local server is a lie
    # the browser would act on.
    assert "strict-transport-security" not in {k.lower() for k in response.headers}


def test_hsts_only_set_in_production(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<html><body>ok</body></html>")

    app = FastAPI()
    configure_security_headers(app, _production_settings())
    app.mount("/", StaticFiles(directory=tmp_path, html=True), name="static")

    response = TestClient(app).get("/")

    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


# ---------------------------------------------------------------------------
# Real app — a router response
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> Generator[TestClient, None, None]:
    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "security_headers.db"
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


def test_headers_present_on_the_real_app_health_route(in_memory_client: TestClient) -> None:
    response = in_memory_client.get("/health")
    assert response.status_code == 200
    _assert_common_headers(dict(response.headers))


# ---------------------------------------------------------------------------
# Headers on a BARE (non-HTTPException) unhandled exception — a real gap a
# security review of this PR caught and this test pins. Starlette wires a
# bare ``Exception`` handler into ``ServerErrorMiddleware``, the outermost
# ASGI layer added around every ``add_middleware`` registration, so its
# response is sent WITHOUT ever passing back through
# ``SecurityHeadersMiddleware``. Fixed by stamping the headers directly onto
# the handler's response (``app.core.security_headers
# .apply_security_headers``) rather than relying on the middleware alone.
# Reverting that call in ``app.main._unhandled_exception_handler`` turns
# this test red.
# ---------------------------------------------------------------------------


def test_headers_present_on_a_bare_unhandled_exception_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "bare_exception.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())

    async def _boom(request) -> None:  # noqa: ANN001 - a raw Starlette Route endpoint
        # A ValueError is NOT one of the three types registered with
        # ExceptionMiddleware (StarletteHTTPException / OrchestratorError /
        # RequestValidationError in app.main.create_app), so it reaches
        # ServerErrorMiddleware and _unhandled_exception_handler.
        raise ValueError("surprise")

    try:
        app = create_app()
        app.router.routes.insert(0, Route("/__boom", _boom))
        response = TestClient(app, raise_server_exceptions=False).get("/__boom")
        assert response.status_code == 500
        _assert_common_headers(dict(response.headers))
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()
        monkeypatch.delenv("CITEVYN_DATABASE_URL", raising=False)


# ---------------------------------------------------------------------------
# Docs/redoc/openapi disabled in production
# ---------------------------------------------------------------------------


def test_docs_endpoints_are_disabled_in_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "prod_docs.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("CITEVYN_ENVIRONMENT", "production")
    monkeypatch.setenv("CITEVYN_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CITEVYN_ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("CITEVYN_EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("CITEVYN_DEMO_API_KEY", "a-strong-demo-key-not-the-default-1234")
    monkeypatch.setenv("CITEVYN_ADMIN_API_KEY", "a-strong-admin-key-not-the-default-1234")
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()


def test_docs_endpoints_stay_enabled_outside_production(in_memory_client: TestClient) -> None:
    assert in_memory_client.get("/docs").status_code == 200
    assert in_memory_client.get("/openapi.json").status_code == 200
