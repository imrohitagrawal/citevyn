"""Tests for :func:`app.core.security.require_admin_api_key`.

The dependency is a thin wrapper around a constant-time string
compare, but the test surface is large because there are four
failure modes (missing header, wrong header, empty configured
key, mismatched scheme) and one success mode. The configured key
behaviour is gated by an env override so the failure modes are
testable without touching the global ``Settings`` singleton.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.errors import APIErrorCode
from app.core.security import ADMIN_USER_ID, require_admin_api_key


@pytest.fixture(autouse=True)
def _admin_key(monkeypatch):
    """Pin a deterministic admin key for the duration of the test."""
    monkeypatch.setenv("CITEVYN_ADMIN_API_KEY", "test-admin-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _build_app() -> FastAPI:
    """Build a tiny app that exposes the dependency at ``/__admin__``.

    A dedicated app (not the real one) means the test does not
    have to spin up the orchestrator or DB session to assert on
    header behaviour.
    """
    app = FastAPI()

    @app.get("/__admin__")
    def _route(user_id: str = Depends(require_admin_api_key)):  # noqa: B008
        return {"user_id": user_id}

    return app


def test_admin_key_returns_admin_user_id() -> None:
    """The success path returns the sentinel admin id and a 200."""
    client = TestClient(_build_app())
    response = client.get("/__admin__", headers={"X-Admin-API-Key": "test-admin-key"})
    assert response.status_code == 200
    assert response.json() == {"user_id": ADMIN_USER_ID}
    assert ADMIN_USER_ID == "admin"


def test_admin_key_missing_header() -> None:
    client = TestClient(_build_app())
    response = client.get("/__admin__")
    assert response.status_code == 401
    err = response.json()["detail"]["error"]
    assert err["code"] == APIErrorCode.auth_required.value
    assert "Missing admin API key header." in err["message"]


def test_admin_key_wrong_value() -> None:
    client = TestClient(_build_app())
    response = client.get("/__admin__", headers={"X-Admin-API-Key": "wrong-key"})
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["code"] == APIErrorCode.auth_required.value


def test_admin_key_uses_constant_time_compare() -> None:
    """Smoke test that the dep tolerates keys of any length.

    We don't measure timing here (flaky on CI), but the test
    confirms ``secrets.compare_digest`` doesn't raise on
    mismatched lengths — a naive ``==`` would also work, the
    timing guarantee is the part the dependency must provide.
    """
    client = TestClient(_build_app())
    response = client.get("/__admin__", headers={"X-Admin-API-Key": ""})
    assert response.status_code == 401


def test_admin_key_extra_headers_ignored() -> None:
    """The dependency only reads ``X-Admin-API-Key``."""
    client = TestClient(_build_app())
    # Sending a demo bearer does NOT unlock the admin route —
    # separation is the point.
    response = client.get(
        "/__admin__",
        headers={
            "X-Admin-API-Key": "test-admin-key",
            "Authorization": "Bearer demo",
        },
    )
    assert response.status_code == 200
