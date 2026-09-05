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
    assert not stray, f"about.css uses raw hex outside its token blocks: {stray}"


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
    theme_js = ABOUT_THEME_JS.read_text(encoding="utf-8")
    assert f'"{key}"' in theme_js, (
        f"App.tsx stores the theme under {key!r} but about-theme.js does not read that key"
    )
