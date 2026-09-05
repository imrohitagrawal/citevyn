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
    assert render_markdown("# About CiteVyn") == '<h1 id="about-citevyn">About CiteVyn</h1>'
    assert render_markdown("## Coverage") == '<h2 id="coverage">Coverage</h2>'


def test_heading_offset_keeps_one_h1_on_the_page() -> None:
    """Two ``<h1>``s give a screen reader two competing document titles."""
    assert render_markdown("# Doc", heading_offset=1).startswith("<h2")
    assert render_markdown("## Section", heading_offset=1).startswith("<h3")


def test_heading_level_is_clamped_to_h6() -> None:
    """There is no ``<h7>``; a deep heading must not emit an unknown element."""
    assert render_markdown("###### Deep", heading_offset=1).startswith("<h6")


def test_hard_wrapped_paragraph_becomes_one_paragraph() -> None:
    assert render_markdown("one line\nsecond line") == "<p>one line second line</p>"


def test_blank_lines_separate_paragraphs() -> None:
    assert render_markdown("first\n\nsecond") == "<p>first</p><p>second</p>"


def test_bullet_list_becomes_a_list_and_continuations_join_their_item() -> None:
    markdown = "- first item\n- second item that\n  wraps onto a second line"
    assert render_markdown(markdown) == (
        "<ul><li>first item</li><li>second item that wraps onto a second line</li></ul>"
    )


def test_markup_in_the_corpus_is_escaped_not_executed() -> None:
    """Defence in depth: the corpus is repo-controlled, but the renderer is not
    entitled to assume that about whatever calls it later."""
    rendered = render_markdown("a <script>alert(1)</script> b")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


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
    "italic or emphasis": re.compile(r"(?<!\w)[*_]\w"),
    "inline code": re.compile(r"`"),
    "horizontal rule": re.compile(r"^\s*(?:---+|\*\*\*+|___+)\s*$", re.MULTILINE),
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
    }
    assert samples.keys() == _UNSUPPORTED.keys()
    for name, sample in samples.items():
        assert _UNSUPPORTED[name].search(sample), f"pattern for {name!r} matches nothing"
