"""The CSP must permit every origin the shipped pages actually load (#306, #365).

WHY THIS FILE EXISTS
--------------------
``font-src`` listed ``api.fontshare.com`` — where Fontshare's *stylesheet*
lives — but Fontshare serves the font *files* from ``cdn.fontshare.com``, so the
browser logged 12 CSP violations on every page load (one per ``src`` URL across
4 weights x 3 formats) and nothing looked broken. The Google Fonts pair beside it
was correct (``fonts.googleapis.com`` CSS -> ``fonts.gstatic.com`` files, both
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
gap. ``/about`` is a second input for the same reason — it is not the SPA shell,
it declares its own ``<link>`` tags, and review once repointed its font host at
an origin absent from the CSP while this file stayed green.

WHAT #365 CHANGED, AND WHY THIS FILE GOT LONGER RATHER THAN SHORTER
-------------------------------------------------------------------
Both pages now self-host their two faces, so there are NO third-party stylesheet
origins left and ``_FONT_FILE_ORIGIN`` is empty. That turns the original central
assertion into ``parsed == set()`` — a check that counts nothing, which this
repo's house rule says needs a partner proving the thing counted would have been
found. A broken parser and a clean page look identical to it.

So the parser is now (a) exercised against the exact shapes that would slip past
it, in ``test_the_parser_catches_the_forms_that_would_slip_past_it``, (b)
partnered by a check that it is reading real markup with real stylesheet links
in it, and (c) joined by ``test_the_permission_check_really_rejects_an_unlisted_origin``,
which feeds the same permission logic a synthetic third-party origin and
requires it to complain — because the two "every origin is permitted" tests
below now loop over an empty set and would otherwise pass with the logic
deleted.

The parser was also FIXED as part of this. It used to match only
``href="https://..."`` with a quote. That was tolerable while it was looking for
one known host; it is not tolerable now that its emptiness is the finding. An
unquoted ``href=https://...`` and a protocol-relative ``href="//host/..."`` are
both valid HTML that load a third-party stylesheet, and both were invisible to
it. ``rel`` is a space-separated token list, so ``rel="alternate stylesheet"``
was invisible too.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from app.core.security_headers import _CSP

_INDEX_HTML = Path(__file__).resolve().parents[2] / "frontend" / "index.html"

#: Stylesheet origin -> the origin that provider serves its FONT FILES from.
#:
#: EMPTY since #365: both pages self-host Geist and JetBrains Mono from
#: ``frontend/public/fonts/``, so no third party is in either critical path and
#: the CSP's ``style-src``/``font-src`` are ``'self'`` only.
#:
#: Adding a provider means adding its row here AND its two hosts to
#: ``app.core.security_headers._CSP`` — and note what that costs: a
#: render-blocking third-party stylesheet also blocks ``<script type="module">``
#: from executing, which is the #365 defect (a blank page, not an unstyled one).
#: Verified against the live CSS when the rows existed, not assumed:
#:   curl 'https://api.fontshare.com/v2/css?f[]=satoshi@400,500,700,900'
#:     -> src: url('//cdn.fontshare.com/wf/...woff2')
#:   Google Fonts CSS likewise points at fonts.gstatic.com.
_FONT_FILE_ORIGIN: dict[str, str] = {}


def _directives() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for part in _CSP.split(";"):
        tokens = part.split()
        if tokens:
            # LAST-WINS here, FIRST-WINS in every browser (CSP Level 3). A policy
            # with a directive repeated therefore enforces something this function
            # never reports -- see #322, where prepending ``script-src
            # 'unsafe-inline'`` left the whole suite green while inline script ran.
            # ``test_security_headers.py`` pins the policy byte-exact; this refuses
            # the ambiguous input rather than quietly resolving it the wrong way.
            assert tokens[0] not in out, (
                f"duplicate directive {tokens[0]!r}: browsers honour the FIRST "
                f"occurrence, this parser would take the last"
            )
            out[tokens[0]] = set(tokens[1:])
    return out


def _rendered_about_page() -> str:
    """The SECOND page this app serves, rendered exactly as ``GET /about`` does.

    ``/about`` (#84 item 6) is not the SPA shell: it is built by
    ``app.services.about_page`` and declares its own ``<link>`` tags,
    independently of ``index.html``. Parsing only ``index.html`` left it
    unguarded — review repointed the About page's font host at an origin absent
    from the CSP and this file stayed GREEN while the page loaded it. That is
    the #306 defect class on a new page, so the page is a second input here.
    """
    from app.api.routes.about import _load_documents
    from app.services.about_page import render_about_page

    return render_about_page(_load_documents())


def _pages_html() -> str:
    """Both surfaces this app serves, concatenated.

    Comments are NOT stripped, deliberately. A parser that honours comments can
    be defeated by a real tag disguised as one, and counting a commented-out tag
    is a false POSITIVE — the safe direction. ``frontend/index.html`` carries a
    note telling the next editor to keep third-party URLs out of its comments
    for exactly this reason.
    """
    return _INDEX_HTML.read_text(encoding="utf-8") + _rendered_about_page()


#: Tags whose ``href``/``src``/``data``/``action`` can name a third-party host.
#: Wider than ``link|script|img`` because the name of the last test in this file
#: is a claim about the whole page. All of these are blocked by the current
#: policy (everything unlisted inherits ``default-src 'self'``, and
#: ``form-action`` is ``'self'``), so this is guard coverage rather than live
#: exposure — but a guard whose name overstates what it looks at is the exact
#: shape this repo keeps getting caught by.
_REFERENCING_TAGS = frozenset(
    [
        "link",
        "script",
        "img",
        "iframe",
        "video",
        "audio",
        "source",
        "object",
        "embed",
        "use",
        "form",
    ]
)
_URL_ATTRS = ("href", "src", "data", "action", "xlink:href")


class _TagCollector(HTMLParser):
    """Collect ``(tag, attrs)`` for every referencing tag, with a REAL parser.

    This replaces two generations of hand-rolled regex, both of which review
    broke:

    * ``<link\b[^>]*>`` stopped at a ``>`` inside a quoted attribute, so
      ``title="Brand > Fonts"`` hid the ``href`` after it.
    * ``<link\b(?:"[^"]*"|'[^']*'|[^>])*>`` fixed that and introduced a worse
      one — a single stray quote in an UNQUOTED value
      (``<link rel=stylesheet a=x" b="c>d" href="http://evil/x.css">``) shifts
      the quote parity for the REST OF THE DOCUMENT, so every later tag
      boundary is wrong. Chromium parses that tag fine and issues the request.

    ``html.parser`` is the stdlib, is what the rest of this repo's HTML tests
    already use, and settles tag boundaries, attribute quoting, character
    references, comments and case-insensitivity in one move. It also decodes
    ``&#x2F;`` in attribute values, which the regexes never did — and which is
    how a URL can hide from a matcher while a browser resolves it normally.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _REFERENCING_TAGS:
            self.tags.append((tag, {k.lower(): (v or "") for k, v in attrs}))

    # A void element written as ``<link ... />`` arrives here instead.
    handle_startendtag = handle_starttag


def _parsed_tags(html: str) -> list[tuple[str, dict[str, str]]]:
    collector = _TagCollector()
    collector.feed(html)
    collector.close()
    return collector.tags


def _rel_tokens(attrs: dict[str, str]) -> set[str]:
    """``rel`` is a space-separated TOKEN LIST, matched ASCII-case-insensitively."""
    return {t.lower() for t in attrs.get("rel", "").split()}


#: ASCII tab, LF and CR are removed from ANYWHERE in a URL by the WHATWG parser,
#: and leading/trailing C0-or-space is stripped before parsing. Both were used
#: to walk a third-party stylesheet straight past the previous matcher while
#: Chromium fetched it — measured, with the request in the server log.
#: The scheme the shipped pages are served over. Only relevant to the
#: single-slash case; see ``_origin_of``.
_PAGE_SCHEME = "https"

_URL_STRIP_ANYWHERE = str.maketrans("", "", "\t\n\r")


def _origin_of(url: str) -> str | None:
    r"""The origin a URL names, or ``None`` if it names none (same-origin).

    Normalised the way the WHATWG URL parser normalises, because that is what
    decides where the browser actually sends the request: leading/trailing C0
    controls and spaces are stripped, ASCII tab/LF/CR are removed from anywhere
    in the string, and a backslash is equivalent to ``/`` after a special
    scheme.

    WHY THE "ONE SLASH" RULE IS NOT UNCONDITIONAL — this is the part two
    successive revisions got wrong in opposite directions. ``https:/host`` goes
    to relative state and keeps the BASE's host **only when the scheme matches
    the document's**. When the scheme DIFFERS, WHATWG goes to
    special-authority-slashes and the host IS reached: measured,
    ``new URL("http:/evil.example/x", "https://citevyn.example/")`` is
    ``http://evil.example``. So a single slash is reported off-origin whenever
    the scheme is not the page's, and ``None`` only for the page's own scheme.

    The page's scheme is HTTPS in production, which is what ``_PAGE_SCHEME``
    encodes. Getting it wrong in the other direction only ever ADDS an origin
    the policy must list, which fails closed.

    Non-HTTP schemes (``data:``, ``blob:``, ``about:``, ``javascript:``) name no
    host and are reported as ``None``, matching the JS side — an earlier version
    returned nonsense like ``data://text``.
    """
    url = url.translate(_URL_STRIP_ANYWHERE)
    url = url.strip("".join(chr(c) for c in range(0x21)))
    m = re.match(r"^(?:([a-z][a-z0-9+.-]*):)?([/\\]*)([^/\\?#]*)", url, flags=re.IGNORECASE)
    if not m:
        return None
    scheme, slashes, host = m.group(1), m.group(2), m.group(3)
    if not host:
        return None
    if scheme is None:
        # No scheme: only ``//host`` (two or more slashes) names a host.
        return f"{_PAGE_SCHEME}://{host.lower()}" if len(slashes) >= 2 else None
    scheme = scheme.lower()
    # Only http(s) can name a host worth listing in a CSP source expression.
    if scheme not in ("http", "https"):
        return None
    if len(slashes) == 1 and scheme == _PAGE_SCHEME:
        # Same scheme as the page, one slash -> relative state, base's host.
        return None
    return f"{scheme}://{host.lower()}"


def _stylesheet_origins_in(html: str) -> set[str]:
    """Every off-origin stylesheet origin ``html`` loads."""
    origins = set()
    for tag, attrs in _parsed_tags(html):
        if tag != "link" or "stylesheet" not in _rel_tokens(attrs):
            continue
        origin = _origin_of(attrs.get("href", ""))
        if origin:
            origins.add(origin)
    return origins


def _stylesheet_origins() -> set[str]:
    return _stylesheet_origins_in(_pages_html())


def _unpermitted(
    origins: set[str],
    directives: dict[str, set[str]],
    mapping: dict[str, str] | None = None,
) -> list[str]:
    """Complaints: every parsed origin that the policy does not actually allow.

    Pure, and ``mapping`` is a PARAMETER rather than a read of the module
    constant, so a test can feed it a declared provider whose font-file host is
    missing — the exact #306 split — and prove that specific complaint fires.
    (It was a closed-over global in the first draft, and the partner test below
    caught that: it could reach the "no row declared" branch and no other.)
    The real caller passes ``_FONT_FILE_ORIGIN``.
    """
    mapping = _FONT_FILE_ORIGIN if mapping is None else mapping
    problems = []
    for origin in sorted(origins):
        if origin not in directives.get("style-src", set()):
            problems.append(f"{origin} serves a stylesheet the page loads but is not in style-src")
        files_from = mapping.get(origin)
        if files_from is None:
            problems.append(
                f"{origin} loads a stylesheet but has no row in _FONT_FILE_ORIGIN, so "
                "nothing checks where it serves its font FILES from"
            )
        elif files_from not in directives.get("font-src", set()):
            problems.append(
                f"{origin} serves its font FILES from {files_from}, which is not in "
                "font-src — every glyph it provides is blocked and the page silently "
                "falls back to a system font"
            )
    return problems


def test_the_parser_finds_exactly_the_providers_we_expect() -> None:
    """Pinned to the exact SET rather than just non-emptiness.

    A non-empty check only catches TOTAL parse failure. Partial loss is the real
    risk and it is silent: with one provider still found, the suite stays green
    while another's coverage has evaporated. Comparing the whole set turns that
    into a failure that names what went missing.

    Today both sides are empty (#365, everything self-hosted), so the three
    tests below it are what keep this from being a check that counts nothing.
    """
    assert _stylesheet_origins() == set(_FONT_FILE_ORIGIN), (
        "the stylesheet origins parsed from frontend/index.html AND the rendered "
        "/about page no longer match the declared providers. If you ADDED one, add "
        "its font-file origin to _FONT_FILE_ORIGIN (check the provider's CSS for the "
        "host in its @font-face src) and to the CSP — and read #365 first, because a "
        "render-blocking third-party stylesheet also blocks the module entry from "
        "executing and leaves the visitor on a blank page. If you REMOVED one, drop "
        "it from both."
    )


def test_the_parser_is_reading_real_markup_with_real_stylesheet_links() -> None:
    """PARTNER for the emptiness above: the parser had something to look at.

    Without this, an empty string, a moved ``index.html`` or an ``/about``
    renderer that raised and returned nothing would all satisfy the assertion
    above perfectly.
    """
    html = _pages_html()
    assert len(html) > 4000, "the two pages together are implausibly short"
    tags = _parsed_tags(html)
    links = [a for t, a in tags if t == "link"]
    assert len(links) > 5, f"only {len(links)} <link> tags across both pages"
    sheets = [a for a in links if "stylesheet" in _rel_tokens(a)]
    assert sheets, "neither page links a stylesheet at all — the parser is broken"
    # Every one of them is same-origin, which is the actual #365 property.
    assert [a for a in sheets if not _origin_of(a.get("href", ""))] == sheets


def test_the_parser_catches_the_forms_that_would_slip_past_it() -> None:
    r"""POSITIVE CONTROL. The emptiness above means "nothing there", not "blind".

    Every entry is a real way to load a third-party stylesheet, and the expected
    origin of each was taken from a REAL URL PARSER (``new URL(form, base)``),
    not from reasoning. That matters: a previous revision of this file asserted
    that ``https:\`` + host reaches the host. It does not — one slash or
    backslash after a special scheme keeps the base's host — and the false claim
    had been copied into two tests and four documents before a skeptic ran the
    one command that settles it.
    """
    tab, lf, cr, c0, bs = "\t", "\n", "\r", "\x01", "\\"
    evil = "fonts.googleapis.com"
    cases = {
        f'<link rel="stylesheet" href="https://{evil}/css2?family=Geist">': f"https://{evil}",
        f"<link rel=stylesheet href=https://{evil}/css2>": f"https://{evil}",
        '<link rel="stylesheet" href="//cdn.fontshare.com/a.css">': "https://cdn.fontshare.com",
        '<link rel="alternate stylesheet" href="http://example.com/a.css">': "http://example.com",
        "<link rel='STYLESHEET' href='https://example.org/a.css'>": "https://example.org",
        '<link\n  rel="stylesheet"\n  href="https://example.net/a.css"\n>': "https://example.net",
        # A `>` inside a quoted value does not close the tag.
        f'<link rel="stylesheet" title="Brand > Fonts" href="https://{evil}/css2">': (
            f"https://{evil}"
        ),
        # A stray quote in an UNQUOTED value shifted quote parity for the whole
        # rest of the document under the previous tokeniser.
        f'<link rel=stylesheet a=x" b="c>d" href="https://{evil}/css2">': f"https://{evil}",
        # Two or more slashes/backslashes DO reach a new authority.
        f'<link rel="stylesheet" href="https:{bs}{bs}{evil}/css2">': f"https://{evil}",
        f'<link rel="stylesheet" href="https:////{evil}/css2">': f"https://{evil}",
        f'<link rel="stylesheet" href="{bs}{bs}{evil}/a.css">': f"https://{evil}",
        # ASCII tab/LF/CR are removed from ANYWHERE in a URL, and leading C0 is
        # stripped, before the URL is parsed.
        f'<link rel="stylesheet" href="http:{tab}//{evil}/x.css">': f"http://{evil}",
        f'<link rel="stylesheet" href="http:{lf}//{evil}/x.css">': f"http://{evil}",
        f'<link rel="stylesheet" href="http:{cr}//{evil}/x.css">': f"http://{evil}",
        f'<link rel="stylesheet" href="ht{tab}tp://{evil}/x.css">': f"http://{evil}",
        f'<link rel="stylesheet" href="{c0}http://{evil}/x.css">': f"http://{evil}",
        f'<link rel="stylesheet" href="/{lf}/{evil}/x.css">': f"https://{evil}",
        # A scheme with no slashes still names a host when the base scheme differs.
        f'<link rel="stylesheet" href="http:{evil}/x.css">': f"http://{evil}",
        # The HTML parser decodes character references before the URL parser
        # ever sees the value — invisible to any matcher that reads raw source.
        f'<link rel="stylesheet" href="http:&#x2F;&#x2F;{evil}/css2">': f"http://{evil}",
    }
    for tag, expected in cases.items():
        assert _stylesheet_origins_in(tag) == {expected}, f"this form slips past the parser: {tag}"

    # And the shapes that must NOT be reported, so the rule is not "flag every link".
    for tag in (
        '<link rel="stylesheet" href="/about.css">',
        '<link rel="stylesheet" href="about.css">',
        '<link rel="stylesheet" href="/fonts/geist-latin.woff2">',
        '<link rel="preload" as="style" href="https://example.com/a.css">',
        '<link rel="preconnect" href="https://example.com">',
        '<link rel="icon" href="/favicon.svg">',
        # ONE backslash. In the must-NOT-report list precisely because an
        # earlier revision asserted the opposite and shipped it.
        f'<link rel="stylesheet" href="https:{bs}{evil}/css2">',
        f'<link rel="stylesheet" href="{bs}{evil}/css2">',
        # A commented-out tag is not fetched, so reporting it would be a false
        # red. This is a DELIBERATE change from the regex version, which could
        # not see comments at all.
        f'<!-- <link rel="stylesheet" href="https://{evil}/css2"> -->',
    ):
        assert _stylesheet_origins_in(tag) == set(), f"false positive on: {tag}"

    # The parser reads adjacent tags as two, not one.
    assert len(_parsed_tags('<link rel="a"><link rel="b">')) == 2


def test_the_origin_reader_agrees_with_a_real_url_parser() -> None:
    r"""DIFFERENTIAL. The JS guard uses ``new URL(...).origin``; this one cannot.

    ``backend/`` has no WHATWG URL parser, so ``_origin_of`` is a hand-rolled
    normalisation — and hand-rolled parsing of URL semantics is precisely what
    three review rounds broke. So the expectations below are not reasoning: each
    was taken from ``new URL(form, "https://citevyn.example/")`` in node, and
    ``frontend/scripts/mutate-font-guards.sh`` re-derives them.

    The three at the top FAILED OPEN in an earlier revision — this parser said
    "same-origin" while a browser reached a third-party host.
    """
    tab, lf, cr, c0, bs = "\t", "\n", "\r", "\x01", "\\"
    off_origin = {
        # Scheme DIFFERS from the page's, one slash -> the host IS reached.
        "http:/evil.example/x": "http://evil.example",
        f"http:{bs}evil.example/x": "http://evil.example",
        "HTTP:/evil.example/x": "http://evil.example",
        # Two or more slashes/backslashes always reach a new authority.
        "https://evil.example/x": "https://evil.example",
        "//evil.example/x": "https://evil.example",
        f"https:{bs}{bs}evil.example/x": "https://evil.example",
        "https:////evil.example/x": "https://evil.example",
        f"{bs}{bs}evil.example/x": "https://evil.example",
        # Removed-anywhere whitespace and stripped leading controls.
        f"http:{tab}//evil.example/x": "http://evil.example",
        f"http:{lf}//evil.example/x": "http://evil.example",
        f"http:{cr}//evil.example/x": "http://evil.example",
        f"ht{tab}tp://evil.example/x": "http://evil.example",
        f"{c0}http://evil.example/x": "http://evil.example",
        f"/{lf}/evil.example/x": "https://evil.example",
        # A scheme with no slashes still names a host when the scheme differs.
        "http:evil.example/x": "http://evil.example",
        # Host case is normalised, so it can be compared to a CSP entry.
        "https://EVIL.EXAMPLE/x": "https://evil.example",
    }
    for href, expected in off_origin.items():
        assert _origin_of(href) == expected, f"{href!r} -> {_origin_of(href)!r}"

    same_origin = [
        "/about.css",
        "about.css",
        "/fonts/geist-latin.woff2",
        "",
        "#anchor",
        "?q=1",
        # The page's OWN scheme with one slash IS relative state.
        "https:/about.css",
        f"https:{bs}about.css",
        # No host to reach.
        "data:font/woff2;base64,AAA",
        "blob:https://citevyn.example/8f2c",
        "about:blank",
        "javascript:void(0)",
    ]
    for href in same_origin:
        assert _origin_of(href) is None, f"false positive: {href!r} -> {_origin_of(href)!r}"


def test_every_stylesheet_origin_the_pages_load_is_permitted() -> None:
    assert _unpermitted(_stylesheet_origins(), _directives()) == []


def test_the_permission_check_really_rejects_an_unlisted_origin() -> None:
    """PARTNER for the test above, which loops over an empty set today.

    Feeds the same logic the same real CSP with a third-party origin the policy
    does not allow, and requires all three complaints: the stylesheet host is
    not in ``style-src``, and there is no declared font-file host to check
    against ``font-src``. Without this, deleting ``_unpermitted``'s body would
    leave the suite green.
    """
    directives = _directives()
    problems = _unpermitted({"https://fonts.googleapis.com"}, directives)
    assert any("style-src" in p for p in problems), problems
    assert any("_FONT_FILE_ORIGIN" in p for p in problems), problems

    # And with a row DECLARED but the file host missing from font-src — the
    # exact #306 split, which is the branch the other case cannot reach — it
    # complains about font-src specifically.
    problems = _unpermitted(
        {"https://fonts.googleapis.com"},
        {"style-src": {"https://fonts.googleapis.com"}, "font-src": {"'self'"}},
        {"https://fonts.googleapis.com": "https://fonts.gstatic.com"},
    )
    assert any("font-src" in p for p in problems), problems

    # And a fully-permitted provider produces NO complaint, so the checker is
    # not simply "always complain".
    assert (
        _unpermitted(
            {"https://fonts.googleapis.com"},
            {
                "style-src": {"https://fonts.googleapis.com"},
                "font-src": {"https://fonts.gstatic.com"},
            },
            {"https://fonts.googleapis.com": "https://fonts.gstatic.com"},
        )
        == []
    )


def test_neither_page_puts_a_third_party_in_front_of_first_paint() -> None:
    """#365, stated directly rather than inferred from the CSP being tight.

    Wider than stylesheets: a ``preconnect``/``dns-prefetch`` to a font CDN is
    the fingerprint of the pattern coming back, and an off-origin script or
    preload is a third-party fetch in the critical path even though it is not a
    stylesheet.
    """
    html = _pages_html()
    tags = _parsed_tags(html)
    assert len(tags) > 5, "the tag probe found almost nothing — the check below is vacuous"
    offenders = []
    for tag, attrs in tags:
        for name in _URL_ATTRS:
            origin = _origin_of(attrs.get(name, ""))
            if origin:
                offenders.append(f"{origin} via <{tag} {name}={attrs.get(name)!r}>")
    assert offenders == [], (
        "a page references a third-party host before first paint. A render-blocking "
        "off-origin stylesheet also blocks <script type=module> from EXECUTING, so "
        "the visitor gets a blank page while that host is slow — that is #365. "
        "Self-host it. If it genuinely has to be third party, widen the CSP in the "
        "same change and add its row to _FONT_FILE_ORIGIN. Note this scans HTML "
        "comments too, so a third-party URL merely mentioned in one fails here."
    )
