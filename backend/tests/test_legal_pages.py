"""Legal pages (ADR-0005 Phase 5C; published 2026-10-28).

``GET /legal/{slug}`` serves Terms, Privacy, Refunds and No-training.

* All four are owner-approved and PUBLISHED (Terms, Privacy and Refunds
  2026-10-28; No-training 2026-10-30): served whatever
  ``CITEVYN_ACCESS_MODEL_ENABLED`` says, with no DRAFT notice and no noindex,
  each with its own "Last updated" date. A payment provider's site review
  needs them public.
* A page that is not published is a DRAFT: 404 while the access model is off;
  a visible "DRAFT, not in effect" notice and ``X-Robots-Tag: noindex`` while
  it is on. No page is a draft today, so that gate is tested on a probe slug.
* A published page may not contain an ``[OWNER: ...]`` note.

Each test says which change turns it red.
"""

from __future__ import annotations

import html
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.api.routes import legal
from app.api.routes.legal import LAST_UPDATED, LEGAL_PAGES, PUBLISHED, has_owner_note
from app.core.config import get_settings
from app.main import create_app

SLUGS = ["terms", "privacy", "refunds", "no-training"]
PUBLIC = ["terms", "privacy", "refunds", "no-training"]
#: The pages a payment provider's review reads for the seller's name and contact.
SELLER_PAGES = ["terms", "privacy", "refunds"]
#: No page is a draft today. The draft gate is still tested, on this slug: it is
#: added to LEGAL_PAGES (not to PUBLISHED) for the test only.
DRAFT_PROBE = "draft-probe"
_LEGAL_DIR = Path(__file__).resolve().parents[1] / "app" / "legal"


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true")
    get_settings.cache_clear()


def test_the_four_pages_are_the_ones_the_owner_must_approve() -> None:
    """Turns red if: a page is added or dropped without updating this list."""
    assert sorted(LEGAL_PAGES) == sorted(SLUGS)


def test_exactly_the_owner_approved_pages_are_published() -> None:
    """Turns red if: a page is published (or unpublished) without the owner."""
    assert sorted(PUBLISHED) == sorted(PUBLIC)


@pytest.fixture
def draft(monkeypatch: pytest.MonkeyPatch) -> str:
    """A page that exists but is not published, made from a real page's file."""
    monkeypatch.setitem(legal.LEGAL_PAGES, DRAFT_PROBE, ("Draft Probe", "no-training.md"))
    assert DRAFT_PROBE not in PUBLISHED
    return DRAFT_PROBE


def test_with_the_setting_off_a_draft_does_not_exist(draft: str) -> None:
    """The live site must not show unapproved legal text. Turns red if: a draft
    is served while the access model is off."""
    with TestClient(create_app()) as client:
        assert client.get(f"/legal/{draft}").status_code == 404


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
    assert f"Last updated {LAST_UPDATED[slug]}." in res.text
    title = LEGAL_PAGES[slug][0]
    assert f"<h1>{html.escape(title)}</h1>" in res.text
    assert "<h2" in res.text


@pytest.mark.parametrize("slug", sorted(PUBLISHED))
def test_a_published_page_has_no_owner_note_left(slug: str) -> None:
    """Turns red if: a page goes public with an unanswered owner note in it."""
    text = (_LEGAL_DIR / LEGAL_PAGES[slug][1]).read_text(encoding="utf-8")
    assert not has_owner_note(text)


def test_the_owner_note_check_finds_a_note() -> None:
    """Partner of the check above: it must actually find notes. Turns red if:
    has_owner_note stops matching (then the check above proves nothing)."""
    for note in [
        "Contact: [OWNER: address].",
        "x [owner:  y]",
        "[ OWNER : x]",
        "[  OWNER: x]",
        "[Owner note: x]",
        "[OWNER]",
        "[OWNER - x]",
        "\uff3bOWNER\uff1ax\uff3d",
        "[\u200bOWNER: x]",
    ]:
        assert has_owner_note(note), note
    assert not has_owner_note("Contact: rohit@stackclimb.com.")
    assert not has_owner_note("the owner of the account")


#: Each published page's own words, so a page can't quietly serve another
#: page's file (the <h1> comes from the title table, not the file).
OWN_WORDS = {
    "terms": "CiteVyn is provided &quot;as is&quot;",
    "privacy": "Argon2id",
    "refunds": "refunded in full if you ask within 14 days",
    "no-training": "to find wrong or missing answers",
}

#: Each published page's "Last updated" date and the SHA-256 of its file on that
#: date. Change a published page's text -> update its date and hash here AND its
#: date in ``LAST_UPDATED`` in legal.py, together.
TEXT_AS_OF_DATE = {
    "terms": (
        "28 October 2026",
        "62d0e71baf9a97bb1df2c64218d294a6e4f87e65ee1efb14a19fbf2806bcb2f2",
    ),
    "privacy": (
        "28 October 2026",
        "45f939bc79ff12313927ac9e1ef2f0564ca0f07d073f14b5e78e6cfda7b70125",
    ),
    "refunds": (
        "28 October 2026",
        "c5578a53fea801a06cd7c6ce82b11a140485cf019a487022729eaeb4f71a763e",
    ),
    "no-training": (
        "30 October 2026",
        "361af95ce1dfaa75a1b41860301d543811278b1f07cf531255cea10702f256a1",
    ),
}


@pytest.mark.parametrize("slug", sorted(PUBLISHED))
def test_each_published_page_serves_its_own_file(slug: str) -> None:
    """Turns red if: LEGAL_PAGES points a slug at another page's file."""
    with TestClient(create_app()) as client:
        res = client.get(f"/legal/{slug}")
    assert OWN_WORDS[slug] in res.text
    for other, words in OWN_WORDS.items():
        if other != slug:
            assert words not in res.text


@pytest.mark.parametrize("slug", sorted(PUBLISHED))
def test_the_last_updated_date_moves_with_the_text(slug: str) -> None:
    """Turns red if: a published page's text changes but its fingerprint here
    does not. When it does, update "Last updated" in legal.py too."""
    import hashlib

    text = (_LEGAL_DIR / LEGAL_PAGES[slug][1]).read_bytes()
    date, digest = TEXT_AS_OF_DATE[slug]
    assert hashlib.sha256(text).hexdigest() == digest, (
        f"{slug} changed: set its date here and in legal.py's LAST_UPDATED to today, "
        "and update this hash"
    )
    # The date the page shows is the date recorded beside the hash.
    assert LAST_UPDATED[slug] == date


def test_every_published_page_has_its_own_date() -> None:
    """Turns red if: a page is published without a "Last updated" date, or a
    date is kept for a page that is not published."""
    assert sorted(LAST_UPDATED) == sorted(PUBLISHED)


@pytest.mark.parametrize("slug", SELLER_PAGES)
def test_a_published_page_names_who_runs_the_service(slug: str) -> None:
    """A payment provider's review wants the seller's legal name and a contact.
    Turns red if: the operator line or contact address is dropped."""
    with TestClient(create_app()) as client:
        res = client.get(f"/legal/{slug}")
    assert "rohit@stackclimb.com" in res.text
    if slug != "refunds":
        assert "Rohit Agrawal" in res.text


def test_a_draft_is_marked_draft_and_not_indexed(on: None, draft: str) -> None:
    """Turns red if: the DRAFT notice or the noindex header is dropped, or the
    page renders without its own text."""
    slug = draft
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
