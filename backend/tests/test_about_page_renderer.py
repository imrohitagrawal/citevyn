"""The Markdown-subset renderer behind ``/about`` (#84 item 6).

``app.services.about_page`` handles exactly four constructs — ``#``/``##``
headings, hard-wrapped paragraphs and ``-`` bullet lists — because that is
everything the two corpus docs that cite ``/about`` use, and taking a Markdown
dependency to render four constructs would be a larger change than the page.

The risk that buys is drift: a later corpus edit adding a link, a table or bold
text would render as literal text, and nobody would notice because the page
still returns 200. ``test_shipped_about_sources_stay_inside_the_subset`` is the
tripwire for that, and it is the reason the renderer is allowed to stay small.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest

from app.api.routes.about import ABOUT_PATH
from app.services.about_page import (
    STYLESHEET_URL,
    THEME_SCRIPT_URL,
    document_anchor,
    render_about_page,
    render_markdown,
    slugify,
)
from app.worker.allowlist import MVP_SOURCES
from app.worker.fetchers import build_fetcher

# ---------------------------------------------------------------------------
# The subset it does render
# ---------------------------------------------------------------------------


def test_headings_become_headings_with_anchors() -> None:
    """``tabindex="-1"`` is part of the contract, not decoration: without it a
    table-of-contents link scrolls without moving focus, and where the keyboard
    resumes is left to the browser."""
    assert (
        render_markdown("# About CiteVyn")
        == '<h1 id="about-citevyn" tabindex="-1">About CiteVyn</h1>'
    )
    assert render_markdown("## Coverage") == '<h2 id="coverage" tabindex="-1">Coverage</h2>'


@pytest.mark.parametrize("text", ["#no space", "#1 priority for us", "#"])
def test_hashes_without_a_following_space_are_not_headings(text: str) -> None:
    """CommonMark requires the space, and the space is load-bearing here.

    Without it ``#1 priority`` became ``<h2 id="1-priority-for-us">`` — legal
    HTML5, but ``querySelector('#1-…')`` throws and the equivalent CSS selector
    is invalid — and a bare ``#`` emitted an empty ``<h2></h2>``.
    """
    rendered = render_markdown(text, heading_offset=1)
    assert rendered.startswith("<p>"), rendered


def test_a_list_written_under_its_lead_in_still_renders_as_a_list() -> None:
    """Blocks are blank-line delimited, but Markdown lists need no blank line.

    Before this, ``Lead in:\\n- a\\n- b`` rendered as
    ``<p>Lead in: - a - b</p>`` — the whole list silently flattened into a
    paragraph of hyphens, and invisible to the subset tripwire because bullets
    ARE a supported construct. ``concepts.md`` is one deleted blank line away
    from this shape.
    """
    assert render_markdown("Lead in:\n- a\n- b") == "<p>Lead in:</p><ul><li>a</li><li>b</li></ul>"
    assert render_markdown("## T\n- a") == ('<h2 id="t" tabindex="-1">T</h2><ul><li>a</li></ul>')


def test_heading_offset_keeps_one_h1_on_the_page() -> None:
    """Two ``<h1>``s give a screen reader two competing document titles."""
    assert render_markdown("# Doc", heading_offset=1).startswith("<h2")
    assert render_markdown("## Section", heading_offset=1).startswith("<h3")


@pytest.mark.parametrize(
    ("markdown", "offset", "expected"),
    [
        ("###### Deep", 1, "<h6"),
        ("####### Deeper", 0, "<h6"),
        ("# Shallow", -3, "<h1"),
        ("## Shallow", -5, "<h1"),
    ],
)
def test_heading_level_is_clamped_at_both_ends(markdown: str, offset: int, expected: str) -> None:
    """There is no ``<h7>`` and no ``<h0>``.

    ``heading_offset`` is a public parameter, so the LOWER bound is reachable
    from outside this module. Review mutated the ``max(..., 1)`` away and the
    whole suite stayed green while the renderer emitted ``<h0>``.
    """
    assert render_markdown(markdown, heading_offset=offset).startswith(expected)


def test_hard_wrapped_paragraph_becomes_one_paragraph() -> None:
    assert render_markdown("one line\nsecond line") == "<p>one line second line</p>"


def test_blank_lines_separate_paragraphs() -> None:
    assert render_markdown("first\n\nsecond") == "<p>first</p><p>second</p>"


def test_bullet_list_becomes_a_list_and_continuations_join_their_item() -> None:
    markdown = "- first item\n- second item that\n  wraps onto a second line"
    assert render_markdown(markdown) == (
        "<ul><li>first item</li><li>second item that wraps onto a second line</li></ul>"
    )


@pytest.mark.parametrize(
    ("markdown", "where"),
    [
        ("a <script>alert(1)</script> b", "paragraph"),
        ("- <script>alert(1)</script>", "list item"),
        ("# <script>alert(1)</script>", "heading"),
        ('# x" onload="alert(1)', "heading attribute context"),
    ],
)
def test_markup_in_the_corpus_is_escaped_not_executed(markdown: str, where: str) -> None:
    """Defence in depth: the corpus is repo-controlled, but the renderer is not
    entitled to assume that about whatever calls it later.

    Parametrized over ALL THREE escape call sites. The first version fed only a
    paragraph, and review then dropped ``html.escape`` from the ``<li>`` and the
    heading independently — both mutants survived a fully green suite while the
    module docstring claimed "escaping is unconditional".
    """
    rendered = render_markdown(markdown)
    assert "<script>" not in rendered, where
    assert "&lt;script&gt;" in rendered or "&quot;" in rendered, (where, rendered)


def test_unsupported_markup_degrades_to_text_rather_than_raising() -> None:
    """A corpus edit must never be able to 500 a public page."""
    rendered = render_markdown("| a | b |\n| - | - |")
    assert rendered.startswith("<p>")
    assert "|" in rendered


def test_slugify_is_total() -> None:
    assert slugify("About CiteVyn") == "about-citevyn"
    assert slugify("CiteVyn Pro and membership") == "citevyn-pro-and-membership"
    assert slugify("!!!") == ""


def test_a_heading_with_no_slug_emits_no_empty_id() -> None:
    assert render_markdown("# !!!") == "<h1>!!!</h1>"


# ---------------------------------------------------------------------------
# The page shell
# ---------------------------------------------------------------------------


def test_page_has_exactly_one_h1() -> None:
    page = render_about_page([("About CiteVyn", "# About CiteVyn\n\nBody.")])
    assert len(re.findall(r"<h1[ >]", page)) == 1


def test_page_links_its_external_stylesheet_and_script() -> None:
    """Both must be external: the CSP allows no inline style or script."""
    page = render_about_page([("About CiteVyn", "# About CiteVyn\n\nBody.")])
    assert f'<link rel="stylesheet" href="{STYLESHEET_URL}">' in page
    assert f'<script src="{THEME_SCRIPT_URL}"></script>' in page


def test_page_actually_requests_the_typeface_its_css_asks_for() -> None:
    """``about.css`` names "Geist" first; something has to deliver it.

    #316's defect exactly: CSS naming a face nobody fetches falls silently
    through to ``system-ui`` and no error appears anywhere. Dropping the font
    ``<link>`` from this page is invisible to the CSP guard (which only cares
    that loaded origins are permitted), so it is asserted here instead.

    Asserted on the PARSED ORIGIN of the stylesheet link, not on a substring of
    the page. CodeQL flagged the substring form
    (``py/incomplete-url-substring-sanitization``, high): a host name can sit at
    an arbitrary position in a URL, so ``https://evil.example/?x=fonts.
    googleapis.com`` satisfied it. Same "guards that check strings" family as
    the two bypasses review already found in this change.
    """
    page = render_about_page([("About CiteVyn", "# About CiteVyn\n\nBody.")])
    stylesheets = re.findall(r'<link rel="stylesheet" href="([^"]+)"', page)
    assert stylesheets, "no stylesheet links rendered — this guard would be vacuous"
    origins = {f"{u.scheme}://{u.netloc}" for u in map(urlsplit, stylesheets) if u.scheme}
    assert origins == {"https://fonts.googleapis.com"}, (
        f"unexpected external stylesheet origins on /about: {sorted(origins)}"
    )
    families = {
        family for url in stylesheets for family in parse_qs(urlsplit(url).query).get("family", [])
    }
    assert any(f.startswith("Geist") for f in families), (
        f"the page requests no Geist family, so --font-sans falls to system-ui: {families}"
    )
    preconnects = re.findall(r'<link rel="preconnect" href="([^"]+)"', page)
    assert {urlsplit(u).netloc for u in preconnects} >= {"fonts.googleapis.com"}


def test_table_of_contents_links_resolve_to_real_anchors() -> None:
    """A jump link to an id that is not on the page is worse than no link."""
    page = render_about_page(
        [("About CiteVyn", "# About CiteVyn\n\nA."), ("AI Concepts", "# AI Concepts\n\nB.")]
    )
    targets = set(re.findall(r'href="#([^"]+)"', page))
    ids = set(re.findall(r'id="([^"]+)"', page))
    assert targets, "no table-of-contents links were rendered"
    assert targets <= ids, f"dangling anchors: {sorted(targets - ids)}"


def test_the_page_never_repeats_an_id() -> None:
    """A duplicate ``id`` is invalid HTML and ambiguous for anything resolving one.

    THE defect this pins, found by mutation-testing the anchor logic and then
    reproduced on the real page: the wrapping ``<section>`` and the document's
    own ``<h2>`` both carried ``id="about-citevyn"``. The jump link still
    worked, which is why nothing else noticed — and why the anchor mutant
    survived until this existed.
    """
    page = render_about_page(
        [("About CiteVyn", "# About CiteVyn\n\nA."), ("AI Concepts", "# AI Concepts\n\nB.")]
    )
    ids = re.findall(r'id="([^"]+)"', page)
    assert ids, "no ids rendered — this guard would be vacuous"
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"the page repeats these ids: {duplicates}"


def test_document_anchor_prefers_the_documents_own_heading() -> None:
    """The anchor must be the heading's id, not an independently slugged title.

    Slugging the title separately is what produced the duplicate above: it
    happened to equal the heading's slug, so the page shipped both.
    """
    assert document_anchor("Source Title", "# Real Heading\n\nBody.") == "real-heading"
    assert document_anchor("Source Title", "No heading here.") == "source-title"


def test_empty_selection_says_so_instead_of_shipping_a_bare_heading() -> None:
    page = render_about_page([])
    assert "not available" in page


def test_documents_that_render_nothing_also_trigger_the_fallback() -> None:
    """The fallback tests the RENDERED CONTENT, not the concatenated markup.

    An earlier version checked ``if not body``, which could never fire once a
    document was present but empty: the wrapping ``<section>`` kept ``body``
    non-empty, so the page shipped an empty section plus a table-of-contents
    link pointing at an id that was never rendered.
    """
    page = render_about_page([("About CiteVyn", "")])
    assert "not available" in page
    targets = set(re.findall(r'href="#([^"]+)"', page))
    assert not targets, f"dangling table-of-contents anchors on an empty page: {targets}"


def test_one_empty_document_does_not_leave_a_dangling_jump_link() -> None:
    """The fallback is all-or-nothing; the table of contents is per-document.

    Reachable in production when ONE corpus file ships truncated-but-present:
    the other document renders, so the whole-page fallback stays quiet and the
    reader gets a 200 with a jump link to an id that is nowhere on the page.
    The first fix for this only covered the case where EVERY document was
    empty, and review reproduced the gap; mutating the per-link render check
    away must turn this red.
    """
    page = render_about_page(
        [("AI concepts glossary", ""), ("About CiteVyn", "# About CiteVyn\n\nBody.")]
    )
    targets = set(re.findall(r'href="#([^"]+)"', page))
    ids = set(re.findall(r'id="([^"]+)"', page))
    assert targets, "no jump links at all — this guard would be vacuous"
    assert targets <= ids, f"dangling anchors: {sorted(targets - ids)}"
    assert "about-citevyn" in targets, "the document that DID render lost its link"


def test_the_real_served_page_never_repeats_an_id() -> None:
    """The id guard above uses a two-document literal; this one uses the CORPUS.

    Review reproduced the gap: give ``concepts.md`` a ``## Coverage`` heading —
    ``citevyn.md`` already has one — and the served page ships ``id="coverage"``
    twice while every hand-written test stays green. ``slugify`` does not
    de-duplicate across documents, so the only honest check is against the real
    documents that will actually be rendered.
    """
    documents = [
        (spec.title, build_fetcher(spec).fetch(spec))
        for spec in MVP_SOURCES
        if spec.source_url == ABOUT_PATH
    ]
    assert len(documents) >= 2, "fewer than two /about sources — this guard would be weak"
    ids = re.findall(r'id="([^"]+)"', render_about_page(documents))
    assert len(ids) > 10, f"only {len(ids)} ids rendered — the corpus or the parser is wrong"
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, (
        "two headings across the /about corpus slug to the same id: "
        f"{duplicates}. Rename one heading, or namespace the anchors per document."
    )


# ---------------------------------------------------------------------------
# The tripwire that lets the renderer stay small
# ---------------------------------------------------------------------------

# Block-level constructs the renderer does NOT handle. A corpus doc that grows
# one of these renders it as literal text, so this fails and the choice —
# extend the renderer, or reword the doc — is made in review.
_UNSUPPORTED = {
    "ordered list": re.compile(r"^\s*\d+\.\s", re.MULTILINE),
    "blockquote": re.compile(r"^\s*>\s", re.MULTILINE),
    "fenced code block": re.compile(r"^\s*```", re.MULTILINE),
    "table row": re.compile(r"^\s*\|", re.MULTILINE),
    "link": re.compile(r"\[[^\]]+\]\([^)]+\)"),
    "image": re.compile(r"!\[[^\]]*\]"),
    "bold": re.compile(r"\*\*\S"),
    # Bullets written with * or +, and nested bullets, are ORDINARY Markdown
    # that this renderer does not accept: `* one` renders as a paragraph of
    # asterisks and an indented sub-bullet flattens into a sibling <li>.
    # Because bullets are a *supported* construct, nothing else notices.
    "asterisk or plus bullet": re.compile(r"^[ \t]*[*+][ \t]+\S", re.MULTILINE),
    "nested bullet": re.compile(r"^[ \t]+[-*+][ \t]+\S", re.MULTILINE),
    "setext heading": re.compile(r"^[^\n]+\n[ \t]*=+[ \t]*$", re.MULTILINE),
    "raw html": re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\s*/?>"),
    "closed atx heading": re.compile(r"^#{1,6} .*[^#]#+[ \t]*$", re.MULTILINE),
    "italic or emphasis": re.compile(r"(?<!\w)[*_]\w"),
    "inline code": re.compile(r"`"),
    "horizontal rule": re.compile(r"^\s*(?:---+|\*\*\*+|___+)\s*$", re.MULTILINE),
    # An HTML comment is the likeliest raw-HTML construct in a prose Markdown
    # file, and the `raw html` pattern above does not match `<!--`. It renders
    # VISIBLY to the reader as escaped text rather than disappearing.
    "html comment": re.compile(r"<!--"),
    "task list": re.compile(r"^\s*-\s+\[[ xX]\]", re.MULTILINE),
    "indented code block": re.compile(r"^ {4,}\S", re.MULTILINE),
}


def test_shipped_about_sources_stay_inside_the_subset() -> None:
    found: list[str] = []
    # Ground truth, not the route's own selection: a guard that asks the code
    # under test which documents to check cannot notice the code dropping one.
    specs = [spec for spec in MVP_SOURCES if spec.source_url == ABOUT_PATH]
    assert specs, "no source cites /about — this guard would be vacuous"
    for spec in specs:
        text = build_fetcher(spec).fetch(spec)
        for name, pattern in _UNSUPPORTED.items():
            match = pattern.search(text)
            if match:
                found.append(f"{spec.location}: {name} at {match.group(0)!r}")
    assert not found, (
        "a /about source now uses Markdown the page renderer cannot render, so it "
        "would appear as literal text on the page. Either extend "
        "app/services/about_page.py or reword the doc:\n  " + "\n  ".join(found)
    )


def test_the_subset_detector_actually_detects() -> None:
    """Vacuous-pass partner: patterns that match nothing would pass the guard above.

    Without this, deleting the body of every pattern leaves the guard green.
    """
    samples = {
        "ordered list": "1. one",
        "blockquote": "> quoted",
        "fenced code block": "```py",
        "table row": "| a | b |",
        "link": "see [docs](https://x)",
        "image": "![alt](x.png)",
        "bold": "**bold**",
        "italic or emphasis": "*em*",
        "inline code": "`code`",
        "horizontal rule": "---",
        "asterisk or plus bullet": "* one",
        "nested bullet": "  - nested",
        "setext heading": "Title\n=====",
        "raw html": "<b>bold</b>",
        "closed atx heading": "## Title ##",
        "html comment": "<!-- note -->",
        "task list": "- [ ] todo",
        "indented code block": "    code()",
    }
    assert samples.keys() == _UNSUPPORTED.keys()
    for name, sample in samples.items():
        assert _UNSUPPORTED[name].search(sample), f"pattern for {name!r} matches nothing"
