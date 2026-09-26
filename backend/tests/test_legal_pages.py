"""Draft legal pages (ADR-0005 Phase 5C).

``GET /legal/{slug}`` serves the DRAFT Terms, Privacy, Refunds and No-training
pages for the owner to review. The owner approves legal text (handoff: "final
legal text ... the owner approves them"), so until then every page:

* answers 404 while ``CITEVYN_ACCESS_MODEL_ENABLED`` is off (the live site is
  unchanged);
* carries a visible "DRAFT, not in effect" notice;
* tells search engines not to index it (``X-Robots-Tag: noindex``).

Each test says which change turns it red.
"""

from __future__ import annotations

import html
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.api.routes.legal import LEGAL_PAGES
from app.core.config import get_settings
from app.main import create_app

SLUGS = ["terms", "privacy", "refunds", "no-training"]


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true")
    get_settings.cache_clear()


def test_the_four_pages_are_the_ones_the_owner_must_approve() -> None:
    """Turns red if: a page is added or dropped without updating this list."""
    assert sorted(LEGAL_PAGES) == sorted(SLUGS)


@pytest.mark.parametrize("slug", SLUGS)
def test_with_the_setting_off_the_drafts_do_not_exist(slug: str) -> None:
    """The live site must not show unapproved legal text. Turns red if: a page
    is served while the access model is off."""
    with TestClient(create_app()) as client:
        assert client.get(f"/legal/{slug}").status_code == 404


@pytest.mark.parametrize("slug", SLUGS)
def test_each_draft_is_marked_draft_and_not_indexed(on: None, slug: str) -> None:
    """Turns red if: the DRAFT notice or the noindex header is dropped, or the
    page renders without its own text."""
    with TestClient(create_app()) as client:
        res = client.get(f"/legal/{slug}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert res.headers["x-robots-tag"] == "noindex"
    assert "DRAFT" in res.text and "not in effect" in res.text
    title = LEGAL_PAGES[slug][0]
    assert f"<h1>{html.escape(title)}</h1>" in res.text
    # The page's own source text reached the page.
    assert "<h2" in res.text


def test_an_unknown_page_is_a_404(on: None) -> None:
    """Turns red if: any slug is accepted (e.g. a path read from the request)."""
    with TestClient(create_app()) as client:
        for bad in ["cookies", "..%2Fconfig", "terms.md"]:
            assert client.get(f"/legal/{bad}").status_code == 404


def test_the_legal_route_wins_against_the_frontend_mount(
    on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like /about, the router must be included before the catch-all static
    mount. Turns red if: the mount answers /legal/... instead."""
    dist = tmp_path / "frontend_dist"
    (dist / "legal").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>CiteVyn</title>")
    (dist / "legal" / "terms.html").write_text("<!doctype html><h1>STATIC</h1>")
    monkeypatch.setattr(main_module, "FRONTEND_DIST", dist)
    with TestClient(create_app(), follow_redirects=False) as client:
        res = client.get("/legal/terms")
    assert res.status_code == 200
    assert "STATIC" not in res.text
