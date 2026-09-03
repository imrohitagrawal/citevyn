"""The CSP must permit every origin the shipped page actually loads (#306).

WHY THIS FILE EXISTS
--------------------
``font-src`` listed ``api.fontshare.com`` — where Fontshare's *stylesheet*
lives — but Fontshare serves the font *files* from ``cdn.fontshare.com``, so the
browser logged 12 CSP violations on every page load (one per ``src`` URL across
4 weights x 3 formats) and nothing looked broken. The Google Fonts pair beside it
is correct (``fonts.googleapis.com`` CSS -> ``fonts.gstatic.com`` files, both
listed), which is exactly why the identical Fontshare split was missed.

(#306 described this as the page "falling back to a system font". It does not:
no ``font-family`` in the codebase uses Satoshi, so the files are never actually
fetched and no pixel changes — see #316. The errors were real; the fallback was
not.)

WHY IT IS NOT DERIVED FROM ``index.html`` ALONE
-----------------------------------------------
The obvious guard — parse ``index.html`` for external origins and require each in
the matching directive — **would have passed while the bug was live**:
``cdn.fontshare.com`` appears nowhere in ``index.html``. It only exists inside
the stylesheet the browser fetches afterwards, so it is invisible to any static
read of our own markup.

So the font-file host cannot be discovered; it has to be *declared*. The mapping
below is the load-bearing part of this test, and adding a new webfont provider
means adding a row to it. That is deliberate: a human stating "this provider
serves its files from over there" is the only thing that closes this class of
gap.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.security_headers import _CSP

_INDEX_HTML = Path(__file__).resolve().parents[2] / "frontend" / "index.html"

#: Stylesheet origin -> the origin that provider serves its FONT FILES from.
#: Verified against the live CSS, not assumed:
#:   curl 'https://api.fontshare.com/v2/css?f[]=satoshi@400,500,700,900'
#:     -> src: url('//cdn.fontshare.com/wf/...woff2')
#:   Google Fonts CSS likewise points at fonts.gstatic.com.
_FONT_FILE_ORIGIN = {
    "https://fonts.googleapis.com": "https://fonts.gstatic.com",
    "https://api.fontshare.com": "https://cdn.fontshare.com",
}


def _directives() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for part in _CSP.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0]] = set(tokens[1:])
    return out


def _stylesheet_origins() -> set[str]:
    """Every external stylesheet origin ``frontend/index.html`` loads."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    links = re.findall(r"<link\b[^>]*>", html, flags=re.IGNORECASE | re.DOTALL)
    origins = set()
    for tag in links:
        if not re.search(r'rel\s*=\s*["\']stylesheet["\']', tag, flags=re.IGNORECASE):
            continue
        href = re.search(r'href\s*=\s*["\'](https://[^"\']+)["\']', tag, flags=re.IGNORECASE)
        if href:
            origins.add("https://" + href.group(1).split("/")[2])
    return origins


def test_the_parser_finds_exactly_the_providers_we_expect() -> None:
    """Partner assertion, pinned to the exact SET rather than just non-emptiness.

    A non-empty check only catches TOTAL parse failure. Partial loss is the real
    risk and it is silent: an unquoted attribute (``href=https://...``, valid
    HTML5) or a protocol-relative ``href="//api.fontshare.com/..."`` makes the
    regexes below skip that tag, and with the other provider still found the
    suite stays green while one provider's coverage has evaporated. Comparing the
    whole set turns that into a failure that names what went missing.
    """
    assert _stylesheet_origins() == set(_FONT_FILE_ORIGIN), (
        "the stylesheet origins parsed from frontend/index.html no longer match "
        "the declared providers. If you ADDED one, add its font-file origin to "
        "_FONT_FILE_ORIGIN (check the provider's CSS for the host in its "
        "@font-face src) and to the CSP. If you REMOVED one, drop it from both — "
        "and if you changed neither, the parser has stopped seeing a <link> it "
        "used to see (unquoted attribute? protocol-relative href?), which would "
        "otherwise silently reduce this file's coverage to nothing."
    )


@pytest.mark.parametrize("origin", sorted(_stylesheet_origins()))
def test_every_stylesheet_origin_is_allowed_by_style_src(origin: str) -> None:
    assert origin in _directives()["style-src"], (
        f"{origin} serves a stylesheet the page loads but is not in style-src"
    )


@pytest.mark.parametrize("origin", sorted(_stylesheet_origins()))
def test_every_providers_font_file_origin_is_allowed_by_font_src(origin: str) -> None:
    """The half that was missing. A provider's CSS host being allowed says nothing
    about where its @font-face rules point."""
    files_from = _FONT_FILE_ORIGIN[origin]
    assert files_from in _directives()["font-src"], (
        f"{origin} serves its font FILES from {files_from}, which is not in "
        "font-src — every glyph it provides is blocked and the page silently "
        "falls back to a system font"
    )
