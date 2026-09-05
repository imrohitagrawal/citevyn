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
import logging
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.api.routes.about import ABOUT_PATH, about_sources
from app.main import create_app
from app.services.about_page import PAGE_TITLE, THEME_SCRIPT_URL
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


def test_the_citation_url_is_literally_slash_about() -> None:
    """``ABOUT_PATH`` is not free to change — the value is already in the DB.

    Every other test follows ``ABOUT_PATH`` wherever it points, so a coordinated
    edit changing both this constant and ``allowlist.py``'s ``source_url`` to
    ``/about-us`` would leave the whole suite green while re-breaking every
    citation ALREADY PERSISTED in ``documents.source_url`` — which is the exact
    bug #84 item 6 is about, and the reason the fix serves the URL instead of
    changing it. Pinned to the literal so that edit goes red.
    """
    assert ABOUT_PATH == "/about"
    assert [spec.name for spec in MVP_SOURCES if spec.source_url == "/about"] == [
        spec.name for spec in about_sources()
    ]


def test_unreadable_source_degrades_instead_of_500ing(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A packaging fault must not turn into an outage of the page.

    This path used to be marked ``# pragma: no cover`` — a flattering 100% over
    a reachable, untested branch. ``OSError`` is included because
    ``LocalFetcher`` only converts "missing" and "not utf-8"; a permissions or
    I/O error escapes it, and before this it 500'd the page.
    """
    from app.worker import fetchers as fetchers_module

    def _explode(self: object, source: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(fetchers_module.LocalFetcher, "fetch", _explode)
    with (
        caplog.at_level(logging.WARNING),
        TestClient(create_app(), follow_redirects=False) as client,
    ):
        response = client.get(ABOUT_PATH)

    assert response.status_code == 200, response.text
    assert "not available" in response.text
    # ...and the page must not offer jump links to sections it failed to render.
    assert 'href="#' not in response.text
    # The operator has to be able to tell WHICH source failed. The app's log
    # format is "%(message)s", so an `extra=` dict is dropped entirely and the
    # line would read `about_page_source_unreadable` and nothing else -- the
    # same trap already recorded against #296. Asserted on the emitted record.
    logged = [
        r.getMessage() for r in caplog.records if "about_page_source_unreadable" in str(r.msg)
    ]
    assert logged, "the degrade path logged nothing"
    assert "citevyn" in logged[0], f"log line names no source: {logged[0]}"
    assert "Permission denied" in logged[0], f"log line carries no reason: {logged[0]}"


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


def test_page_carries_nothing_the_csp_would_block(bundle: Path) -> None:
    """The app-wide CSP has no 'unsafe-inline' on script-src OR style-src.

    Measured in real Chromium against this app: an inline ``<style>``, a
    ``style=`` attribute and an inline ``<script>`` are all blocked, silently
    as far as the page is concerned. A page that styled itself inline would
    therefore render unstyled in production while passing every status-code
    test.

    PARSED, not substring-matched. The first version of this test asserted the
    strings ``"<style"``, ``"style="`` and ``"<script"`` were absent, and review
    walked through it twice: ``onclick="alert(1)"`` is inline script under
    ``script-src 'self'`` and contains none of those strings, and
    ``style ="..."`` — one space before the ``=`` — is a live style attribute
    that the literal ``"style="`` never sees. Both left the suite fully green.
    A guard that asserts a string is absent is not a guard; this one walks the
    real elements the browser will build.
    """
    with TestClient(create_app(), follow_redirects=False) as client:
        body = client.get(ABOUT_PATH).text

    page = _parse(body)
    assert page.elements, "the parser found no elements — this guard would be vacuous"
    assert not page.inline_styles, f"inline <style> blocks: {page.inline_styles}"
    assert not page.style_attributes, f"style= attributes on: {page.style_attributes}"
    assert not page.event_handlers, f"inline event handlers (CSP-blocked): {page.event_handlers}"
    assert not page.javascript_urls, f"javascript: URLs (CSP-blocked): {page.javascript_urls}"
    assert not page.inline_scripts, f"<script> elements with a body: {page.inline_scripts}"
    # An external same-origin script IS allowed by script-src 'self'.
    assert page.script_sources == [THEME_SCRIPT_URL], page.script_sources


def test_every_origin_the_page_loads_is_permitted_by_the_csp_it_ships_with(
    bundle: Path,
) -> None:
    """Resolve each origin the page loads against the CSP on the SAME response.

    The other direction from the test above: not "is there anything inline"
    but "is every source the page names actually allowed". Both the policy and
    the body are taken from one response, so this cannot drift from what the
    browser receives — and it does not care which constant the URL came from.
    """
    with TestClient(create_app(), follow_redirects=False) as client:
        response = client.get(ABOUT_PATH)

    directives = _parse_csp(response.headers["content-security-policy"])
    page = _parse(response.text)
    checked = 0
    for url, directive in page.external_sources:
        checked += 1
        origin = urlsplit(url)
        allowed = directives.get(directive, set()) | directives.get("default-src", set())
        if origin.scheme:
            assert f"{origin.scheme}://{origin.netloc}" in allowed, (
                f"{url} is loaded under {directive} but that origin is not in the "
                f"policy this very response carries: {sorted(allowed)}"
            )
        else:
            assert "'self'" in allowed, f"{url} is same-origin but {directive} lacks 'self'"
    assert checked >= 4, (
        f"expected the stylesheet, font stylesheet, script and icon, found {checked}"
    )


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


def test_page_links_back_to_the_app_from_header_and_footer(bundle: Path) -> None:
    """A citation is a one-way trip unless the page offers a way back.

    COUNTED, because there are two such links and the first version of this
    test asserted only that one existed. Review deleted the header link and the
    whole suite stayed green — the footer link supplied the observation. Both
    are load-bearing: a reader who lands mid-page from a jump link, and one who
    reaches the bottom of a long document, need different exits.
    """
    with TestClient(create_app(), follow_redirects=False) as client:
        page = _parse(client.get(ABOUT_PATH).text)

    back = [(tag, attrs) for tag, attrs in page.elements if tag == "a" and attrs.get("href") == "/"]
    assert len(back) == 2, f"expected a header AND a footer link home, found {back}"
    # The accessible name must say where it goes: "CiteVyn" alone does not
    # (WCAG 2.4.4), so the header link carries an explicit label.
    assert any(attrs.get("aria-label") == "Back to CiteVyn" for _, attrs in back)


def test_landmarks_are_not_nested_inside_main(bundle: Path) -> None:
    """``<header>``/``<footer>`` map to banner/contentinfo only OUTSIDE ``<main>``.

    Per HTML-AAM they expose ``role=generic`` when nested inside
    main/article/aside/nav/section, so the first version of this page — which
    wrapped both in ``<main class="about-page">`` — shipped no banner and no
    contentinfo landmark at all. Measured in the served markup, not asserted
    about the template.
    """
    with TestClient(create_app(), follow_redirects=False) as client:
        body = client.get(ABOUT_PATH).text

    main_open = body.index("<main")
    main_close = body.index("</main>")
    assert body.index("<header") < main_open, "<header> is inside <main> (role=generic)"
    assert body.index("<footer") > main_close, "<footer> is inside <main> (role=generic)"


def test_page_declares_the_document_metadata_a_browser_needs(bundle: Path) -> None:
    """Title, language, charset and viewport — the page's document contract.

    None of these were covered before: review mutated each away in turn and the
    suite stayed green. The viewport one matters most in practice — a citation
    chip is overwhelmingly a phone tap, and without the meta the page renders
    at desktop width and zoomed out.
    """
    with TestClient(create_app(), follow_redirects=False) as client:
        body = client.get(ABOUT_PATH).text

    assert '<html lang="en">' in body
    assert '<meta charset="utf-8">' in body
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in body
    assert f"<title>{PAGE_TITLE} — CiteVyn</title>" in body
    description = _parse(body).meta_description
    assert description and len(description) > 40, f"thin or missing description: {description!r}"


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
    return [
        ref
        for ref, _ in _parse(html).external_sources
        if ref.startswith("/") and ref != "/" and not ref.startswith("//")
    ]


def _parse_csp(header: str) -> dict[str, set[str]]:
    """``directive -> sources`` for a Content-Security-Policy header value."""
    directives: dict[str, set[str]] = {}
    for chunk in header.split(";"):
        parts = chunk.split()
        if parts:
            directives[parts[0].lower()] = set(parts[1:])
    return directives


class _PageParser(HTMLParser):
    """What the browser will actually build out of the response body.

    Deliberately an element/attribute walk rather than a set of substring
    checks. The tests above exist to catch content the CSP would block, and
    substring checks for ``"<style"`` / ``"style="`` were demonstrably bypassed
    by ``onclick=`` and by ``style ="..."``. ``HTMLParser`` normalises
    attribute names and whitespace the way a browser does, so those bypasses
    become ordinary attributes here.
    """

    #: element -> (attribute, the CSP directive that governs loading it). Every
    #: shape here is one review demonstrated bypassing the first version of this
    #: parser while the browser would have BLOCKED it under the shipped policy.
    _SOURCE_ATTRS: dict[str, tuple[str, str]] = {
        "script": ("src", "script-src"),
        "img": ("src", "img-src"),
        "iframe": ("src", "frame-src"),
        "frame": ("src", "frame-src"),
        "embed": ("src", "object-src"),
        "object": ("data", "object-src"),
        "source": ("src", "media-src"),
        "audio": ("src", "media-src"),
        "video": ("src", "media-src"),
        "form": ("action", "form-action"),
        "base": ("href", "base-uri"),
    }

    #: ``rel`` value -> the directive that governs the resulting fetch.
    _LINK_RELS = {"stylesheet": "style-src", "icon": "img-src", "manifest": "manifest-src"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.inline_styles: list[str] = []
        self.style_attributes: list[str] = []
        self.event_handlers: list[str] = []
        self.javascript_urls: list[str] = []
        self.inline_scripts: list[str] = []
        self.script_sources: list[str] = []
        self.external_sources: list[tuple[str, str]] = []
        self.meta_description: str | None = None
        self._in_script = False
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapping = {name: (value or "") for name, value in attrs}
        self.elements.append((tag, mapping))
        for name, value in mapping.items():
            if name == "style":
                self.style_attributes.append(tag)
            elif name.startswith("on"):
                self.event_handlers.append(f"{tag}[{name}]")
            elif value.strip().lower().startswith(("javascript:", "data:text/html")):
                self.javascript_urls.append(f"{tag}[{name}]")
        if tag == "style":
            # Recorded on the ELEMENT, not only on its text: an empty
            # <style></style> is still a CSP-blocked inline style block.
            self._in_style = True
            self.inline_styles.append("<style> element")
        if tag == "script":
            self._in_script = True
            if "src" in mapping:
                self.script_sources.append(mapping["src"])
                self.external_sources.append((mapping["src"], "script-src"))
        if tag == "link":
            rel = mapping.get("rel", "").lower()
            href = mapping.get("href", "")
            if href:
                if rel in self._LINK_RELS:
                    self.external_sources.append((href, self._LINK_RELS[rel]))
                elif rel in ("preload", "prefetch", "modulepreload"):
                    # `as` decides which directive the eventual fetch lands in.
                    as_directive = {
                        "script": "script-src",
                        "style": "style-src",
                        "font": "font-src",
                        "image": "img-src",
                        "fetch": "connect-src",
                    }.get(mapping.get("as", "").lower(), "default-src")
                    self.external_sources.append((href, as_directive))
        source_attr = self._SOURCE_ATTRS.get(tag)
        if source_attr and tag != "script":
            attr, directive = source_attr
            if mapping.get(attr):
                self.external_sources.append((mapping[attr], directive))
        if mapping.get("srcset"):
            for candidate in mapping["srcset"].split(","):
                url = candidate.strip().split(" ")[0]
                if url:
                    self.external_sources.append((url, "img-src"))
        if tag == "meta" and mapping.get("name") == "description":
            self.meta_description = mapping.get("content")

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_style and data.strip():
            self.inline_styles.append(data.strip()[:60])
        if self._in_script and data.strip():
            # Also catches a <script src="..."> whose body carries code: the
            # browser ignores that body, but it is inline script text and has
            # no business on the page.
            self.inline_scripts.append(data.strip()[:60])


def _parse(html_text: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(html_text)
    parser.close()
    return parser
