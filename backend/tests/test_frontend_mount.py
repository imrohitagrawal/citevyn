"""The browser bundle is served at ``/`` without shadowing the API.

The Fly image serves the React bundle from the same origin as the API (one
hostname, no CORS, and Cloudflare's free Universal SSL only covers one
subdomain level). That is done with a ``StaticFiles`` mount at ``/``, which is
a CATCH-ALL — so the ordering between it and the routers is load-bearing, and
these tests exist to pin it.

The failure this guards against is not a crash. It is ``/v1/...`` quietly
returning ``index.html`` with a 200, which reads as a broken frontend rather
than a broken route, and would sail past any check that only asserts 200.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import create_app


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app at a throwaway directory that looks like a built bundle."""
    dist = tmp_path / "frontend_dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>CiteVyn</title>")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log('bundle')")
    monkeypatch.setattr(main_module, "FRONTEND_DIST", dist)
    return dist


def test_root_serves_the_bundle_when_it_exists(bundle: Path) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "CiteVyn" in response.text


def test_bundle_assets_are_served(bundle: Path) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/assets/app.js")
    assert response.status_code == 200
    assert "bundle" in response.text


def test_api_routes_are_not_shadowed_by_the_mount(bundle: Path) -> None:
    """The whole point of mounting last: /health must stay JSON, not HTML."""
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["status"] == "healthy"


def test_unknown_api_path_does_not_return_the_spa_shell(bundle: Path) -> None:
    """A missing /v1 route must 404, never fall through to index.html.

    ``html=True`` makes StaticFiles serve index.html for unknown paths, which
    is right for client-side routes and WRONG for the API surface — a caller
    that gets a 200 of HTML for a typo'd endpoint has no way to tell it made a
    mistake.
    """
    with TestClient(create_app()) as client:
        response = client.get("/v1/definitely-not-a-route")
    assert response.status_code == 404
    assert "<!doctype html" not in response.text.lower()


def test_missing_bundle_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No bundle (tests, local uvicorn, CI) must not break app construction.

    ``frontend/dist`` is gitignored and only built inside the image, so the
    common case for everything except the deployed container is "absent".
    """
    monkeypatch.setattr(main_module, "FRONTEND_DIST", tmp_path / "does_not_exist")
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 404
