"""``frontend/public/about.css`` must not drift from the design tokens (#84 item 6).

The ``/about`` page is served by the API, so it links a hand-written stylesheet
at a stable URL. Vite content-hashes everything under ``src/``, so that
stylesheet cannot import ``src/styles/tokens.css`` — it has to redeclare the
custom properties it uses. That is a copy, and this repo already knows what an
unguarded copy of the design system costs.

So the copy is enforced rather than trusted: every ``--token: value`` declared
in ``about.css`` must match ``tokens.css`` exactly, in both themes. A palette
change in ``tokens.css`` now fails here until ``about.css`` is updated, instead
of leaving one page on last season's colours.

Lives in ``backend/tests`` (not vitest) for the same reason
``test_frontend_assets.py`` does: it is a cross-tree consistency check on files,
not a behaviour test of the React app, and vitest's ``include`` globs only
``frontend/src``.
"""

from __future__ import annotations

import hashlib
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TOKENS_CSS = REPO_ROOT / "frontend" / "src" / "styles" / "tokens.css"
ABOUT_CSS = REPO_ROOT / "frontend" / "public" / "about.css"
ABOUT_THEME_JS = REPO_ROOT / "frontend" / "public" / "about-theme.js"
APP_TSX = REPO_ROOT / "frontend" / "src" / "App.tsx"

_DECL_RE = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# ``about.css`` block  ->  the ``tokens.css`` selector it copies from.
# ``tokens.css`` states the dark values three times (the manual override, and
# the OS-preference block); the two here are the ones ``about.css`` mirrors.
_BLOCK_SOURCES = {
    ":root": ':root,\n[data-theme="light"]',
    '[data-theme="dark"]': '[data-theme="dark"]',
    ':root:not([data-theme="light"])': '[data-theme="dark"]',
}


def _strip_comments(css: str) -> str:
    return _COMMENT_RE.sub("", css)


def _declarations(css: str, selector: str) -> dict[str, str]:
    """Custom properties declared in the rule opened by ``selector``.

    Deliberately a small hand parser rather than a CSS dependency: it only has
    to read two files this repo controls, and a mis-parse shows up as a missing
    token, which the vacuity guards below catch.
    """
    css = _strip_comments(css)
    start = css.index(selector + " {") + len(selector) + 2
    depth = 1
    end = start
    while depth:
        char = css[end]
        depth += char == "{"
        depth -= char == "}"
        end += 1
    return {name: " ".join(value.split()) for name, value in _DECL_RE.findall(css[start : end - 1])}


@pytest.fixture(scope="module")
def tokens_css() -> str:
    return TOKENS_CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def about_css() -> str:
    return ABOUT_CSS.read_text(encoding="utf-8")


def test_the_parser_actually_found_declarations(tokens_css: str, about_css: str) -> None:
    """Vacuous-pass guard: a parser returning ``{}`` makes every check below pass.

    Delete this and a broken ``_DECL_RE`` would turn the drift guard into a
    no-op that still reports green.
    """
    assert len(_declarations(tokens_css, _BLOCK_SOURCES[":root"])) > 50
    assert len(_declarations(about_css, ":root")) > 20
    assert len(_declarations(about_css, '[data-theme="dark"]')) >= 5


@pytest.mark.parametrize(("about_selector", "tokens_selector"), sorted(_BLOCK_SOURCES.items()))
def test_about_css_tokens_match_the_design_system(
    tokens_css: str, about_css: str, about_selector: str, tokens_selector: str
) -> None:
    ours = _declarations(about_css, about_selector)
    theirs = _declarations(tokens_css, tokens_selector)

    unknown = sorted(set(ours) - set(theirs))
    assert not unknown, (
        f"{ABOUT_CSS.name} block {about_selector!r} declares tokens that do not exist in "
        f"tokens.css {tokens_selector!r}: {unknown}"
    )
    drifted = {name: (value, theirs[name]) for name, value in ours.items() if value != theirs[name]}
    assert not drifted, (
        f"{ABOUT_CSS.name} block {about_selector!r} has drifted from tokens.css "
        f"{tokens_selector!r} (about.css value, tokens.css value): {drifted}"
    )


def test_the_dark_blocks_cover_the_same_tokens(about_css: str) -> None:
    """A token overridden in one dark path but not the other is a half-theme.

    ``about.css`` has two dark paths — the explicit ``data-theme="dark"`` the
    SPA writes, and the OS-preference fallback. A token present in only one of
    them renders correctly for some readers and wrong for others.
    """
    explicit = set(_declarations(about_css, '[data-theme="dark"]'))
    preferred = set(_declarations(about_css, ':root:not([data-theme="light"])'))
    assert explicit == preferred, (
        "the two dark blocks in about.css override different tokens: "
        f"only-explicit={sorted(explicit - preferred)}, only-media={sorted(preferred - explicit)}"
    )


def test_every_var_the_page_uses_is_declared(about_css: str) -> None:
    """An undeclared ``var(--x)`` silently resolves to nothing — no error, no colour."""
    declared = set(_declarations(about_css, ":root"))
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", _strip_comments(about_css)))
    assert used, "no var() references found — the parser or the stylesheet is wrong"
    assert used <= declared, f"about.css uses undeclared tokens: {sorted(used - declared)}"


def test_the_page_uses_tokens_rather_than_raw_hex(about_css: str) -> None:
    """Declarations may hold hex; USAGE must go through ``var(--…)``.

    The drift guard above compares declared values, so review pointed out that
    replacing every ``var(--ink)`` with the literal ``#1c1b19`` left it green —
    the tokens it compares would simply have become unused, and the page would
    silently stop following a palette change. ``stylelint``'s ``color-no-hex``
    enforces this for ``src/styles/``; this file is outside its glob, so the
    same rule is asserted here.
    """
    body = _strip_comments(about_css)
    # Everything outside the three token-declaration blocks.
    for selector in _BLOCK_SOURCES:
        start = body.index(selector + " {")
        end = body.index("}", start) + 1
        body = body[:start] + body[end:]
    stray = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
    stray += re.findall(r"\b(?:rgba?|hsla?)\([^)]*\)", body)
    assert not stray, f"about.css uses raw colours outside its token blocks: {stray}"


def test_the_theme_key_matches_the_app(about_css: str) -> None:
    """``about-theme.js`` copies the SPA's localStorage key; nothing else binds them.

    Rename ``THEME_STORAGE_KEY`` in ``App.tsx`` and ``/about`` silently stops
    honouring a manual dark choice — no error, no failing test. Same drift
    shape the CSS tokens above are guarded against, and the same shape the
    repo already guards for ``citevynAliases.ts``.
    """
    del about_css
    app_tsx = APP_TSX.read_text(encoding="utf-8")
    match = re.search(r'THEME_STORAGE_KEY\s*=\s*"([^"]+)"', app_tsx)
    assert match, "could not find THEME_STORAGE_KEY in App.tsx — this guard would be vacuous"
    key = match.group(1)
    assert key == "citevyn:theme", f"unexpected key {key!r}; update the /about page with it"
    # Matched in an EXECUTABLE position, with comments stripped first. The
    # first version of this guard asserted only that the key string appeared
    # somewhere in the file -- and `about-theme.js` names it in a docstring on
    # line 4 as well as in the `getItem` call. Review pointed the real
    # `getItem` at a wrong key and this test stayed GREEN. A guard satisfied by
    # a comment is not a guard.
    theme_js = _strip_comments(ABOUT_THEME_JS.read_text(encoding="utf-8"))
    theme_js = re.sub(r"(?m)^\s*//.*$", "", theme_js)
    assert f'getItem("{key}")' in theme_js, (
        f"App.tsx stores the theme under {key!r} but about-theme.js does not READ "
        "that key (checked outside comments)"
    )
    # ...and the value has to reach the DOM, or the page never changes theme.
    assert 'setAttribute("data-theme"' in theme_js, (
        "about-theme.js reads the stored theme but never applies it"
    )


# A hex escape is terminated by ONE optional whitespace character, and CSS
# counts form feed (\x0c) and CRLF as whitespace — not just space/tab/LF.
# A backslash before a newline is a LINE CONTINUATION and is removed entirely.
# Both gaps were live bypasses: `@\\69<FF>mport "/\\<LF>/host/x.css"` reached the
# network in real Chromium while every backend and frontend test stayed green.
_CSS_ESCAPE_RE = re.compile(
    r"\\([0-9a-fA-F]{1,6})(?:\r\n|[ \t\n\r\f])?|\\(\r\n|[\n\r\f])|\\([\s\S])"
)


def _unescape_css(text: str) -> str:
    r"""Resolve CSS escape sequences, so a guard reads what the PARSER reads.

    This is load-bearing, not tidiness. ``@import`` was checked as a plain
    substring, and review defeated it with one character class CSS explicitly
    allows in an at-keyword::

        @\69mport "\/\/x.example/a.css";

    ``@\69mport`` is not the substring ``@import``; ``"\/\/x..."`` contains no
    ``url()`` and does not start with ``http``. Every backend and frontend test
    stayed green (1946 pytest, 523 vitest) — and served in real Chromium the
    page issued a render-blocking request to ``http://x.example/a.css``.
    ``frontend/public/`` is copied verbatim into ``dist/``, so it would have
    shipped.

    The lesson is the one already in this repo's notes: resolve what the SYSTEM
    resolved. A guard that matches source bytes is guarding a spelling.
    """

    def _resolve(m: re.Match[str]) -> str:
        if m.group(1) is not None:
            return chr(int(m.group(1), 16))
        if m.group(2) is not None:
            return ""  # line continuation: the backslash AND the newline go
        return m.group(3)

    return _CSS_ESCAPE_RE.sub(_resolve, text)


def test_the_css_escape_reader_resolves_what_the_browser_resolves(about_css: str) -> None:
    """PARTNER for the two absence checks below, which a broken reader satisfies.

    Every line here is a form review actually used to smuggle a third-party
    request past the substring check.
    """
    ff, lf, cr = "\x0c", "\n", "\r"
    assert _unescape_css(r"@\69mport") == "@import"
    assert _unescape_css(r"@\49 mport") == "@Import"  # hex is case-insensitive, one trailing space
    assert _unescape_css(r'"\/\/x.example/a.css"') == '"//x.example/a.css"'
    assert _unescape_css(r"url(\68 ttps://x)") == "url(https://x)"
    # FORM FEED as the hex-escape terminator, and a backslash LINE CONTINUATION.
    # Both were demonstrated bypasses of the first version of this reader, with
    # the resulting @import verified to hit the network in real Chromium.
    assert _unescape_css("@\\69" + ff + "mport") == "@import"
    assert _unescape_css("@\\69" + cr + lf + "mport") == "@import"
    assert _unescape_css('"/\\' + lf + '/host/x.css"') == '"//host/x.css"'
    assert _unescape_css('"/\\' + ff + '/host/x.css"') == '"//host/x.css"'
    assert _unescape_css('"/\\' + cr + lf + '/host/x.css"') == '"//host/x.css"'
    # And it leaves ordinary CSS alone, so the checks below are not reading
    # mangled input.
    assert _unescape_css("@font-face { src: url(/fonts/a.woff2); }") == (
        "@font-face { src: url(/fonts/a.woff2); }"
    )
    # Partner for THAT: the real file survives the transform intact.
    assert "@font-face" in _unescape_css(about_css)


def test_the_stylesheet_pulls_in_no_further_origins(about_css: str) -> None:
    """``@import`` and ``url()`` inside about.css are invisible to page parsing.

    The CSP guard on ``/about`` walks the HTML the browser receives; a font or
    stylesheet pulled in from INSIDE the linked CSS never appears there. That
    is the #306 shape exactly -- the host that broke it existed only inside a
    fetched stylesheet. This page's CSS is expected to reference nothing
    external at all, so the honest guard is to assert that.

    Read through ``_unescape_css`` because the browser does; see its docstring
    for the bypass that made that necessary.
    """
    body = _unescape_css(_strip_comments(about_css))
    assert "@import" not in body.lower(), "about.css @imports a stylesheet the CSP guard cannot see"
    urls = re.findall(r"url\(([^)]*)\)", body)
    quoted = re.findall(r"""["']([^"']*)["']""", body)
    remote = [
        u
        for u in urls + quoted
        if "//" in u or re.match(r"^\s*[a-z][a-z0-9+.-]*:", u.strip().strip("'\""), re.IGNORECASE)
    ]
    assert not remote, f"about.css loads remote resources the CSP guard cannot see: {remote}"


def test_the_page_s_own_script_pulls_in_no_further_origins() -> None:
    """``/about``'s theme script can inject a stylesheet at runtime.

    Review demonstrated it: three lines appended to ``about-theme.js`` creating
    a ``<link rel="stylesheet">`` for fonts.googleapis.com and appending it to
    ``<head>``. Nothing looked — this file was read only for its
    ``localStorage`` key and its ``setAttribute`` call, and the HTML-scanning
    guards see the DOM the server sends, not the DOM the script builds. 1946
    pytest and 523 vitest tests stayed green.

    ``/about`` is the surface with no browser-level coverage at all: no
    Playwright spec navigates to it (it needs the real backend, and the demo
    config runs only the Vite dev server). So this static scan stands in for
    one.

    WHAT IT CANNOT SEE, stated rather than implied — because an earlier version
    of this docstring said it "says so rather than implying otherwise" while
    saying no such thing, which is the overstatement it was warning about:

    * a URL built from PARTS. ``["ht", "tps:", "//", host].join("")`` contains
      no ``//host`` in any single string literal, and this scan reads string
      literals. Demonstrated in review: five such lines create the link and
      Chromium issues the request with this file green. It is kept as a
      deliberate KNOWN SURVIVOR in ``frontend/scripts/mutate-font-guards.sh``,
      so the limit is exercised on every run rather than merely written down —
      and if a future change DOES catch it, that case reports
      ``UNEXPECTEDLY-KILLED`` and asks for this note to be updated.
    * ``String.fromCharCode``, ``atob``, or any other runtime construction.
    * anything a third-party script does, if one is ever added.

    Closing that class properly needs a browser test of ``/about`` against the
    real backend, which is tracked rather than faked here. What makes the gap
    tolerable meanwhile is that the CSP fails CLOSED: ``style-src 'self'``
    blocks such a stylesheet, so the visible result is a console violation and
    a fallback face (the #306 shape), not #365's blank page.
    """
    js = ABOUT_THEME_JS.read_text(encoding="utf-8")
    # Strip comments so a URL merely discussed in one is not a false red.
    body = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    body = re.sub(r"^\s*//.*$", "", body, flags=re.MULTILINE)
    remote = re.findall(r"""["'`]\s*(?:[a-z][a-z0-9+.-]*:)?//[^"'`]+["'`]""", body, re.IGNORECASE)
    assert not remote, (
        f"about-theme.js references a third-party host: {remote}. A script-injected "
        "stylesheet is render-blocking for everything after it and is invisible to "
        "every HTML-scanning guard here — that is #365's mechanism, one layer down."
    )
    # PARTNER: the scan really is looking at the script, and really would find
    # such a URL. Without this, an empty or mis-read file passes for free.
    assert len(body) > 200, "about-theme.js is implausibly short — the scan is vacuous"
    assert re.findall(
        r"""["'`]\s*(?:[a-z][a-z0-9+.-]*:)?//[^"'`]+["'`]""",
        'var l="https://fonts.googleapis.com/css2";',
        re.IGNORECASE,
    ), "the scan cannot see the very shape it exists to catch"


# ---------------------------------------------------------------------------
# The @font-face copy (#365)
# ---------------------------------------------------------------------------

FONTS_CSS = REPO_ROOT / "frontend" / "src" / "styles" / "fonts.css"
PUBLIC_DIR = REPO_ROOT / "frontend" / "public"

_FONT_FACE_RE = re.compile(r"@font-face\s*\{([^}]*)\}", re.DOTALL)


def _font_faces(css: str) -> list[dict[str, str]]:
    """Every ``@font-face`` block, as a normalised ``property -> value`` dict.

    Whitespace inside a value is collapsed so a re-wrapped ``unicode-range``
    (which prettier will do the moment either file is touched) is not read as a
    drift. Everything else is compared exactly.
    """
    faces = []
    for body in _FONT_FACE_RE.findall(_strip_comments(css)):
        decls = {}
        for part in body.split(";"):
            if ":" not in part:
                continue
            name, _, value = part.partition(":")
            decls[name.strip().lower()] = " ".join(value.split())
        faces.append(decls)
    return faces


@pytest.fixture
def fonts_css() -> str:
    return FONTS_CSS.read_text(encoding="utf-8")


def test_the_font_face_parser_found_real_rules(fonts_css: str, about_css: str) -> None:
    """Partner for the comparison below, which would pass on two empty lists.

    Both files declare the same three rules: Geist as one variable face
    covering 400-700, and JetBrains Mono at 400 and 500 pointing at the SAME
    file — which is how Google declares it, one binary, two weights, no
    synthesis.
    """
    for label, css in (("fonts.css", fonts_css), ("about.css", about_css)):
        faces = _font_faces(css)
        assert len(faces) == 3, f"{label} declares {len(faces)} @font-face rules, expected 3"
        families = {f["font-family"].strip("\"'") for f in faces}
        assert families == {"Geist", "JetBrains Mono"}, f"{label}: {families}"
        for face in faces:
            assert face["font-display"] == "swap", (
                f"{label}: a face without font-display: swap blanks its text for up to "
                "3 s while the file is in flight"
            )
            assert face["unicode-range"], f"{label}: a face with no unicode-range"


def test_the_about_page_font_faces_do_not_drift_from_the_app(
    fonts_css: str, about_css: str
) -> None:
    """``/about`` cannot import the hashed bundle, so it re-declares the faces.

    Same reason the tokens above are copied, and the same treatment: enforced
    identical rather than hoped identical. A weight, a ``src`` or a
    ``unicode-range`` diverging here means the two surfaces render in different
    faces, which nothing else would notice.
    """
    app = sorted(_font_faces(fonts_css), key=lambda f: (f["font-family"], f["font-weight"]))
    about = sorted(_font_faces(about_css), key=lambda f: (f["font-family"], f["font-weight"]))
    assert app == about, (
        "the @font-face rules in frontend/public/about.css have drifted from "
        "frontend/src/styles/fonts.css — /about would render in a different face "
        "from the app"
    )


def test_every_self_hosted_font_file_actually_ships(fonts_css: str, about_css: str) -> None:
    """The ``url()`` targets resolve to real files under ``frontend/public/``.

    Nothing else covers this. ``test_frontend_assets.py`` parses ``href``/``src``
    attributes in ``index.html`` and says in its own header that a CSS ``url()``
    is out of scope, so before #365 a renamed woff2 would have been a silent
    404 and a silent fallback face.
    """
    srcs = set()
    for css in (fonts_css, about_css):
        for face in _font_faces(css):
            srcs.update(re.findall(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", face["src"]))
    assert len(srcs) == 2, f"expected two distinct font files, got {sorted(srcs)}"
    for src in sorted(srcs):
        assert src.startswith("/"), f"{src} is not a root-relative same-origin path"
        assert (PUBLIC_DIR / src.lstrip("/")).is_file(), (
            f"{src} is declared by an @font-face rule but does not exist under "
            "frontend/public/ — the browser would 404 and fall back silently"
        )


#: sha256 of the two shipped faces, verified byte-for-byte against what
#: fonts.gstatic.com serves for the `latin` subsets of the exact css2 query
#: `frontend/index.html` used to make (2026-09-07).
_FONT_DIGESTS = {
    "geist-latin.woff2": "9b6f5ff45b278c744b5f379a2c4ecbaf858a842b8eaf82ac8d21b699ca16c608",
    "jetbrains-mono-latin.woff2": (
        "2c32b9b3ee358c119e210f6f5195f9bd34894d78a785ff2e95d60e718e400af4"
    ),
}


def test_the_shipped_font_binaries_are_the_bytes_we_vetted() -> None:
    """Pin the CONTENT, not just the presence, of two redistributed binaries.

    Everything else about these files is checked by existence or by pixels, and
    neither is enough. A .woff2 renders as ``Bin`` in a diff, so a replacement
    is unreviewable by eye; and a substituted face designed to render similarly
    would not move the 22 visual baselines either. These are third-party
    binaries we now redistribute from our own origin under our own TLS — the
    one property worth asserting is that they are the exact bytes that were
    licence-checked and compared against upstream.

    Turns red by changing ONE byte of either file. If it goes red after a
    deliberate re-download, re-verify the licence and update the digest in the
    same commit — do not just refresh the constant.
    """
    for name, expected in _FONT_DIGESTS.items():
        path = PUBLIC_DIR / "fonts" / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == expected, (
            f"{name} is not the vetted binary: expected {expected}, got {digest}"
        )
    # PARTNER: the loop above is empty-passing if the mapping is ever emptied,
    # and a digest of nothing is still a digest.
    assert len(_FONT_DIGESTS) == 2
    for name in _FONT_DIGESTS:
        assert (PUBLIC_DIR / "fonts" / name).stat().st_size > 20_000, f"{name} looks truncated"


def test_the_redistributed_fonts_carry_their_licence() -> None:
    """SIL OFL 1.1 requires the licence and copyright notice to travel with the files.

    These are redistributed binaries in a public artifact, not a build-time
    dependency, so the obligation is ours and it is discharged by shipping
    ``/fonts/OFL.txt`` beside them. Both copyright lines are quoted from the
    upstream LICENSE files verbatim.
    """
    ofl = (PUBLIC_DIR / "fonts" / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in ofl
    assert "Copyright (c) 2023 Vercel, in collaboration with basement.studio" in ofl
    assert "Copyright 2020 The JetBrains Mono Project Authors" in ofl
    # Partner: the full licence body, not just a header naming it.
    assert "PERMISSION & CONDITIONS" in ofl
    assert len(ofl) > 4000, "OFL.txt looks truncated"
