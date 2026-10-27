"""Legal pages (ADR-0005 Phase 5C; published 2026-10-28).

``GET /legal/{slug}`` serves Terms, Privacy, Refunds and No-training.

* Terms, Privacy and Refunds are owner-approved (2026-10-28) and PUBLISHED:
  served whatever ``CITEVYN_ACCESS_MODEL_ENABLED`` says, with no DRAFT notice
  and no noindex. A payment provider's site review needs them public.
* No-training is still a DRAFT (two owner checks, issue #492): 404 while the
  access model is off; a visible "DRAFT, not in effect" notice and
  ``X-Robots-Tag: noindex`` while it is on.
* A published page may not contain an ``[OWNER: ...]`` note.

Each test says which change turns it red.
"""

from __future__ import annotations

import html
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.api.routes.legal import LEGAL_PAGES, PUBLISHED, has_owner_note
from app.core.config import get_settings
from app.main import create_app

SLUGS = ["terms", "privacy", "refunds", "no-training"]
PUBLIC = ["terms", "privacy", "refunds"]
DRAFTS = ["no-training"]
_LEGAL_DIR = Path(__file__).resolve().parents[1] / "app" / "legal"


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true")
    get_settings.cache_clear()


def test_the_four_pages_are_the_ones_the_owner_must_approve() -> None:
    """Turns red if: a page is added or dropped without updating this list."""
    assert sorted(LEGAL_PAGES) == sorted(SLUGS)


def test_exactly_terms_privacy_and_refunds_are_published() -> None:
    """Turns red if: a page is published (or unpublished) without the owner."""
    assert sorted(PUBLISHED) == sorted(PUBLIC)


@pytest.mark.parametrize("slug", DRAFTS)
def test_with_the_setting_off_the_drafts_do_not_exist(slug: str) -> None:
    """The live site must not show unapproved legal text. Turns red if: a draft
    is served while the access model is off."""
    with TestClient(create_app()) as client:
        assert client.get(f"/legal/{slug}").status_code == 404


@pytest.mark.parametrize("model_on", [False, True])
@pytest.mark.parametrize("slug", PUBLIC)
def test_a_published_page_is_served_as_final(
    monkeypatch: pytest.MonkeyPatch, slug: str, model_on: bool
) -> None:
    """Turns red if: a published page 404s with the access model off, or still
    says DRAFT, or still tells search engines not to index it."""
    monkeypatch.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true" if model_on else "false")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        res = client.get(f"/legal/{slug}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "x-robots-tag" not in res.headers
    assert "DRAFT" not in res.text and "not in effect" not in res.text
    assert "Last updated 28 October 2026." in res.text
    title = LEGAL_PAGES[slug][0]
    assert f"<h1>{html.escape(title)}</h1>" in res.text
    assert "<h2" in res.text


@pytest.mark.parametrize("slug", PUBLIC)
def test_a_published_page_has_no_owner_note_left(slug: str) -> None:
    """Turns red if: a page goes public with an unanswered owner note in it."""
    text = (_LEGAL_DIR / LEGAL_PAGES[slug][1]).read_text(encoding="utf-8")
    assert not has_owner_note(text)


def test_the_owner_note_check_finds_a_note() -> None:
    """Partner of the check above: it must actually find notes. Turns red if:
    has_owner_note stops matching (then the check above proves nothing)."""
    assert has_owner_note("Contact: [OWNER: address].")
    assert has_owner_note("x [owner:  y]")
    assert not has_owner_note("Contact: rohit@stackclimb.com.")


@pytest.mark.parametrize("slug", PUBLIC)
def test_a_published_page_names_who_runs_the_service(slug: str) -> None:
    """A payment provider's review wants the seller's legal name and a contact.
    Turns red if: the operator line or contact address is dropped."""
    with TestClient(create_app()) as client:
        res = client.get(f"/legal/{slug}")
    assert "rohit@stackclimb.com" in res.text
    if slug != "refunds":
        assert "Rohit Agrawal" in res.text


@pytest.mark.parametrize("slug", DRAFTS)
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
