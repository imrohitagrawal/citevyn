"""``GET /about`` must serve the page every CiteVyn self-citation points at (#84 item 6).

The defect this pins, reproduced against production on 2026-09-06::

    $ curl -s https://citevyn.stackclimb.com/about
    {"request_id":"req_79074f0a…","status":"error",
     "error":{"code":"not_found","message":"Not Found","details":null}}

Every answer CiteVyn gives about *itself* cites ``/about`` — the URL is stamped
onto ``Document.source_url`` at ingest time from
``app.worker.allowlist.MVP_SOURCES`` — and the frontend renders it as a real
``<a href="/about">``. Clicking a citation therefore landed the reader on a raw
JSON error envelope. Changing the corpus URL would have needed a production
re-ingest; serving the URL fixes every citation already in the database with a
deploy alone, which is why the page is served rather than the URL changed.

Two things make the ordering here load-bearing, both measured rather than
assumed (see ``test_about_route_wins_against_a_bundle_that_also_claims_the_path``):

* ``StaticFiles(html=True)`` does **not** fall back to ``index.html`` for an
  unknown path — it 404s. So the SPA shell never covered ``/about``.
* the mount at ``/`` is a catch-all, so a route registered *after* it is dead
  code that silently 307s instead of serving. The failure mode is a redirect,
  not an error.
"""

from __future__ import annotations

import html
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.api.routes.about import ABOUT_PATH, about_sources
from app.main import create_app
from app.worker.allowlist import MVP_SOURCES
from app.worker.fetchers import build_fetcher

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC = REPO_ROOT / "frontend" / "public"


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway directory that looks like a built bundle.

    Mirrors ``test_frontend_mount.py``: the mount at ``/`` only exists when
    ``FRONTEND_DIST`` is a real directory, and its presence is exactly what
    would shadow a mis-ordered ``/about`` route.
    """
    dist = tmp_path / "frontend_dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>CiteVyn</title>")
    monkeypatch.setattr(main_module, "FRONTEND_DIST", dist)
    return dist


# ---------------------------------------------------------------------------
# The regression itself
# ---------------------------------------------------------------------------


def test_about_serves_a_page_at_exactly_the_citation_url(bundle: Path) -> None:
    """THE #84-item-6 regression: /about answered a JSON 404 envelope.

    Asserted with ``follow_redirects=False`` on purpose. ``TestClient`` follows
    redirects by default, so shipping ``about/index.html`` under the static
    mount — which answers ``/about`` with a 307 to ``/about/`` — would make a
    naively written version of this test pass while the contract ("the exact
    URL in every citation serves the page") stayed broken.
    """
    with TestClient(create_app(), follow_redirects=False) as client:
        response = client.get(ABOUT_PATH)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "application/json" not in response.headers["content-type"]


def test_about_page_carries_the_prose_the_citation_promises(bundle: Path) -> None:
    """The page must be the material the cited answer was actually drawn from.

    A citation that resolves to *a* page is not enough — it has to resolve to
    the page the claim came from, or the link is decoration. Every source whose
    ``source_url`` is ``/about`` contributes; today that is the About-CiteVyn
    doc AND the AI-concepts glossary, both of which cite this one URL.
    """
    with TestClient(create_app(), follow_redirects=False) as client:
        body = client.get(ABOUT_PATH).text

    # Ground truth, NOT ``about_sources()``. Mutation testing caught this: with
    # the selection truncated to its first entry, iterating the selection made
    # this test check only what the (broken) selection already claimed, and it
    # passed. Only its partner test failed — "another guard caught it" is not
    # the same as this guard biting. Deriving the list here from MVP_SOURCES
    # makes a dropped source fail the prose check itself.
    specs = [spec for spec in MVP_SOURCES if spec.source_url == ABOUT_PATH]
    assert specs, "no MVP source cites /about — this test would be vacuous"
    for spec in specs:
        # A distinctive full sentence from each doc, read from the doc itself so
        # the assertion cannot drift away from the shipped corpus.
        source_text = build_fetcher(spec).fetch(spec)
        sentence = _first_prose_sentence(source_text)
        # Compare against the UNESCAPED body: the renderer escapes ``'`` to
        # ``&#x27;``, and what the reader sees is the unescaped text. Comparing
        # raw markup would fail on punctuation rather than on missing prose.
        assert sentence in _collapse_whitespace(html.unescape(body)), (
            f"source {spec.name!r} cites {ABOUT_PATH} but its prose is missing "
            f"from the served page; expected to find {sentence!r}"
        )


def test_every_source_that_cites_about_is_on_the_page(bundle: Path) -> None:
    """Adding a self-cited source must not silently create a dead citation.

    Partner to the test above: that one proves the prose of the *current*
    sources is present, this one proves the selection is derived from
    ``MVP_SOURCES`` rather than hard-coded, so a sixth self-citing source
    appears on the page instead of pointing at a page that omits it.
    """
    expected = {spec.name for spec in MVP_SOURCES if spec.source_url == ABOUT_PATH}
    assert expected == {spec.name for spec in about_sources()}
    assert len(expected) >= 2, (
        "both the About-CiteVyn doc and the concepts glossary cite /about; "
        "if that changed, this guard needs re-deriving rather than relaxing"
    )


# ---------------------------------------------------------------------------
# Ordering: the mount at "/" is a catch-all
# ---------------------------------------------------------------------------


def test_about_route_wins_against_a_bundle_that_also_claims_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route must beat the catch-all mount, proven adversarially.

    The bundle here is stocked with BOTH shapes that let ``StaticFiles`` claim
    this URL — ``about.html`` and ``about/index.html``. Measured against the
    installed Starlette, a mount holding ``about/index.html`` answers ``/about``
    with a 307 to ``/about/``. So if ``_mount_frontend`` ever moved ahead of
    ``include_router(about_router)``, this request would 307 to a *different*
    URL serving *different* content — a success-shaped failure that no status
    assertion elsewhere would catch. Asserted on the response, not on the route
    table, because the route table's shape is a FastAPI implementation detail
    and the redirect is what a reader would actually hit.
    """
    dist = tmp_path / "frontend_dist"
    (dist / "about").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>CiteVyn</title>")
    (dist / "about.html").write_text("<!doctype html><h1>STATIC-FLAT</h1>")
    (dist / "about" / "index.html").write_text("<!doctype html><h1>STATIC-DIR</h1>")
    monkeypatch.setattr(main_module, "FRONTEND_DIST", dist)

    with TestClient(create_app(), follow_redirects=False) as client:
        response = client.get(ABOUT_PATH)

    assert response.status_code == 200, (
        f"{ABOUT_PATH} answered {response.status_code} "
        f"(location={response.headers.get('location')!r}) — the static mount won, "
        "so about_router is registered after _mount_frontend and is dead code"
    )
    assert "STATIC-DIR" not in response.text
    assert "STATIC-FLAT" not in response.text


def test_about_works_without_a_frontend_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No bundle (pytest, local uvicorn, CI) must still serve the page.

    ``frontend/dist`` is gitignored and only built inside the image, so
    "absent" is the common case everywhere except the deployed container. The
    page is rendered from the corpus markdown that ships in the API image, so
    it does not depend on the browser bundle at all.
    """
    monkeypatch.setattr(main_module, "FRONTEND_DIST", tmp_path / "does_not_exist")
    with TestClient(create_app(), follow_redirects=False) as client:
        assert client.get("/").status_code == 404
        assert client.get(ABOUT_PATH).status_code == 200


def test_api_routes_are_still_not_shadowed(bundle: Path) -> None:
    """Adding a route before the mount must not disturb the existing ordering."""
    with TestClient(create_app(), follow_redirects=False) as client:
        health = client.get("/health")
        unknown = client.get("/v1/definitely-not-a-route")
    assert health.status_code == 200
    assert health.headers["content-type"].startswith("application/json")
    assert unknown.status_code == 404
    assert "<!doctype html" not in unknown.text.lower()


# ---------------------------------------------------------------------------
# What the browser actually receives
# ---------------------------------------------------------------------------


def test_page_has_no_inline_style_or_script(bundle: Path) -> None:
    """The app-wide CSP has no 'unsafe-inline' on script-src OR style-src.

    Measured in real Chromium against this app: an inline ``<style>``, a
    ``style=`` attribute and an inline ``<script>`` are all blocked, silently
    as far as the page is concerned. A page that styled itself inline would
    therefore render unstyled in production while passing every status-code
    test. Asserted on the response body — what the browser is handed — not on
    the template that produced it.
    """
    with TestClient(create_app(), follow_redirects=False) as client:
        body = client.get(ABOUT_PATH).text

    lowered = body.lower()
    assert "<style" not in lowered
    assert "style=" not in lowered
    assert "<script>" not in lowered
    # An external same-origin script IS allowed by script-src 'self'; an inline
    # one is not. Distinguish them rather than banning <script> outright.
    assert "<script" not in lowered.replace("<script src=", "")


def test_local_assets_the_page_references_actually_ship(bundle: Path) -> None:
    """Every local href/src in the served HTML must resolve to a real file.

    Same class of defect as #221 (``index.html`` referenced a ``/favicon.svg``
    that was never created and nothing noticed through a build, an image, CI
    and a deploy). Here the stakes are higher: the stylesheet is the only thing
    standing between this page and unstyled prose, and a 404 on it is invisible
    unless someone opens the console.
    """
    with TestClient(create_app(), follow_redirects=False) as client:
        body = client.get(ABOUT_PATH).text

    refs = _local_refs(body)
    assert len(refs) >= 2, f"expected the page to reference its stylesheet and script, got {refs}"
    missing = [ref for ref in refs if not (PUBLIC / ref.lstrip("/")).is_file()]
    assert not missing, f"/about references local assets that do not ship: {missing}"


def test_referenced_assets_are_served_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """...and the static mount actually hands them back with the right type.

    ``frontend/public`` is copied verbatim into ``dist/`` by Vite and from
    there into ``/app/frontend_dist`` by the Dockerfile, so pointing
    ``FRONTEND_DIST`` at it reproduces the production serving path without a
    build. Guards the consumer's view: a stylesheet served as ``text/plain``
    is ignored by the browser exactly like a missing one.
    """
    monkeypatch.setattr(main_module, "FRONTEND_DIST", PUBLIC)
    with TestClient(create_app(), follow_redirects=False) as client:
        page = client.get(ABOUT_PATH)
        refs = _local_refs(page.text)
        served = {ref: client.get(ref) for ref in refs}

    expected_types = {".css": "text/css", ".js": "javascript", ".svg": "image/svg"}
    assert {".css", ".js"} <= {Path(ref).suffix for ref in served}, (
        f"expected the stylesheet and theme script among the page's assets, got {list(served)}"
    )
    for ref, response in served.items():
        assert response.status_code == 200, f"{ref} -> {response.status_code}"
        suffix = Path(ref).suffix
        assert suffix in expected_types, f"unexpected asset type on /about: {ref}"
        assert expected_types[suffix] in response.headers["content-type"], (
            f"{ref} served as {response.headers['content-type']!r}"
        )


def test_security_headers_are_present_on_the_page(bundle: Path) -> None:
    """The page is a new response path; the middleware must still cover it."""
    with TestClient(create_app(), follow_redirects=False) as client:
        headers = client.get(ABOUT_PATH).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in headers


def test_page_links_back_to_the_app(bundle: Path) -> None:
    """A citation is a one-way trip unless the page offers a way back."""
    with TestClient(create_app(), follow_redirects=False) as client:
        body = client.get(ABOUT_PATH).text
    assert 'href="/"' in body


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _first_prose_sentence(markdown: str) -> str:
    """The first full sentence of the first paragraph under the first ``##``.

    Deliberately read from the doc rather than pasted here, so the assertion
    tracks the shipped corpus instead of freezing a copy of it (the #84 item-4
    failure mode).
    """
    lines = markdown.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## ")) + 1
    paragraph: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            if paragraph:
                break
            continue
        paragraph.append(line.strip())
    sentence, _, _ = _collapse_whitespace(" ".join(paragraph)).partition(". ")
    return f"{sentence}."


def _local_refs(html: str) -> list[str]:
    """Rooted local ``href``/``src`` values, minus the back-to-the-app link."""
    import re

    refs = re.findall(r"""\b(?:href|src)\s*=\s*["'](/[^"']*)["']""", html)
    return [ref for ref in refs if ref != "/" and not ref.startswith("//")]
