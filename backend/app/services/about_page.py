"""Render the corpus markdown that CiteVyn cites about itself into an HTML page.

This module is pure mechanism: a bounded Markdown-subset renderer plus the page
shell. It imports nothing from ``app.*`` on purpose — per AGENTS.md, protocol /
wire-format logic stays free of app-specific imports so it can move to a shared
package later without dragging the application with it. Check the seam holds
with ``grep "^from app\\." backend/app/services/about_page.py`` (nothing). That
makes it deliberately unlike its neighbours in ``app/services/``, which all take
a session or a repository; it lives here for discoverability, not because it is
a service.

Why render the corpus rather than write a page
----------------------------------------------
The citation URL ``/about`` is stamped onto every self-referential answer from
``app.worker.allowlist``. A hand-authored page would be one more paraphrase of
copy that issue #84 item 4 already tracks as duplicated (the issue says four
places; ``citevyn.md`` is the canonical one), and the citation would point at a
page that merely *resembles* the source the claim was drawn from. Rendering the
source doc itself makes the link truthful by construction and adds no copy to
keep in sync.

The supported subset, and why it is a subset
--------------------------------------------
The two docs that cite ``/about`` use ``#`` and ``##`` headings, hard-wrapped
paragraphs and ``-`` bullet lists. Nothing else. Rather than take a Markdown
dependency for that, the renderer handles exactly those four constructs and
renders anything else as literal escaped text — it never raises, because a
corpus edit must not be able to 500 a public page. The pressure to keep the
corpus inside the subset lives in a test
(``test_about_page_renderer.py::test_shipped_about_sources_stay_inside_the_subset``),
which goes red when a doc grows a construct this cannot render, so the choice is
made deliberately in review rather than discovered as mangled output in
production.

Escaping is unconditional. The input is repo-controlled today, so this is
defence in depth rather than a live XSS boundary — but "the input is trusted"
is a property of the caller, not of this function, and the app-wide CSP forbids
inline script anyway.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence

# Where the page's stylesheet and theme script come from. Both are plain files
# under ``frontend/public/``, which Vite copies verbatim into ``dist/`` and the
# Dockerfile copies into the image, so the existing StaticFiles mount at ``/``
# serves them at these exact URLs (verified live: ``/favicon.svg`` ships the
# same way). They are NOT hashed bundle assets, which is the point — a
# hand-written ``<link>`` cannot name a content-hashed filename stably.
#
# Both must be EXTERNAL and same-origin: the app-wide CSP
# (``app.core.security_headers``) carries no ``'unsafe-inline'`` on either
# ``style-src`` or ``script-src``. Measured in real Chromium against this app,
# an inline ``<style>``, a ``style=`` attribute and an inline ``<script>`` are
# all blocked, and nothing about the page looks broken until you open the
# console.
STYLESHEET_URL = "/about.css"
THEME_SCRIPT_URL = "/about-theme.js"

# Same Google Fonts request the SPA shell makes (``frontend/index.html``), so
# the page uses the real ``--font-sans`` rather than falling through to
# ``system-ui`` (#316). Both hosts are already in the CSP's ``style-src`` /
# ``font-src``; adding any OTHER font host here would need
# ``app.core.security_headers._CSP`` widened in the same change.
_FONT_STYLESHEET_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Geist:wght@400..700&family=JetBrains+Mono:wght@400;500&display=swap"
)

PAGE_TITLE = "About CiteVyn"
PAGE_DESCRIPTION = (
    "What CiteVyn is, what it covers, and the plain-language glossary behind its "
    "answers — the source pages CiteVyn cites when it answers about itself."
)


def slugify(text: str) -> str:
    """A URL fragment for ``text`` — lowercase, non-alphanumerics collapsed to ``-``.

    Used for the section anchors the table of contents links to. Kept simple and
    total: an empty or all-punctuation heading yields ``""``, which the caller
    treats as "no anchor" rather than emitting ``id=""``.
    """
    out: list[str] = []
    for char in text.lower():
        if char.isalnum():
            out.append(char)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def _blocks(markdown: str) -> Iterable[list[str]]:
    """Split ``markdown`` into blank-line-separated blocks of non-empty lines."""
    block: list[str] = []
    for line in markdown.splitlines():
        if line.strip():
            block.append(line)
        elif block:
            yield block
            block = []
    if block:
        yield block


def _is_heading(line: str) -> bool:
    """True for an ATX heading — ``#``\\ s followed by a space or a tab.

    Without the whitespace requirement, ``#1 priority`` and ``#hashtag`` render
    as headings whose ``id`` starts with a digit: legal HTML5, but
    ``querySelector('#1-…')`` throws and the equivalent CSS selector is invalid.
    It also stops a bare ``#`` from emitting an empty ``<h2></h2>``.

    This is DELIBERATELY narrower than CommonMark, which also accepts a ``#``
    run terminated by end-of-line (a valid empty heading). An empty heading has
    no anchor text and nothing to link to, so it is rendered as a paragraph
    instead. A tab IS accepted, because CommonMark accepts one and rejecting it
    would silently turn a real heading into a paragraph of hashes.
    """
    stripped = line.lstrip("#")
    return stripped != line and stripped[:1] in (" ", "\t")


def _is_list_item(line: str) -> bool:
    """True for a ``- `` bullet. Only ``-`` — see the subset guard for why."""
    return line.lstrip().startswith("- ")


def _render_list(lines: list[str]) -> str:
    """``- item`` lines into ``<ul>``; continuation lines join their item.

    A continuation line is any line that does not itself start a new ``- ``
    item — the corpus hard-wraps bullets across several lines. Callers only
    reach here with a first line that IS an item, so there is no stray-leading-
    line case to handle; an earlier version had one and it was unreachable.
    """
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
        else:
            items[-1] = f"{items[-1]} {stripped}"
    rendered = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return f"<ul>{rendered}</ul>"


def _split_block(block: list[str]) -> Iterable[list[str]]:
    """Split one blank-line block wherever a heading or a list starts.

    ``_blocks`` splits on blank lines only, so a list written directly under its
    lead-in — no blank line between, which is valid Markdown and which
    ``concepts.md`` is one edit away from — arrived here as a single block whose
    first line was a paragraph. The whole list then rendered as
    ``<p>Lead in: - a - b</p>``: silently mangled, and invisible to the
    subset tripwire, because bullets ARE a supported construct.
    """
    run: list[str] = []
    for line in block:
        # A heading always starts a run. A bullet starts one unless the run is
        # already a list, in which case it is the next item; anything else is a
        # continuation line and belongs to whatever run is open.
        in_list = bool(run) and _is_list_item(run[0])
        starts_run = _is_heading(line.strip()) or (_is_list_item(line) and not in_list)
        if starts_run and run:
            yield run
            run = []
        run.append(line)
    if run:
        yield run


def render_markdown(markdown: str, *, heading_offset: int = 0) -> str:
    """Render the supported Markdown subset of ``markdown`` to an HTML fragment.

    ``heading_offset`` shifts heading levels so a document's ``#`` can sit under
    the page's own ``<h1>`` without producing a second top-level heading — an
    accessibility requirement, not a cosmetic one: a page with two ``<h1>``s
    gives a screen-reader user two competing document titles.

    Never raises. Anything outside the subset is emitted as an escaped
    paragraph, so a corpus edit degrades to plain text instead of a 500.
    """
    parts: list[str] = []
    for block in _blocks(markdown):
        for run in _split_block(block):
            first = run[0].strip()
            if _is_heading(first):
                hashes = len(first) - len(first.lstrip("#"))
                text = first[hashes:].strip()
                # Clamp to 1..6 in BOTH directions: HTML has no h7 and no h0,
                # and `heading_offset` is a public parameter, so a negative one
                # would otherwise emit an element browsers do not recognise.
                level = min(max(hashes + heading_offset, 1), 6)
                anchor = slugify(text)
                # ``quote=True`` is belt-and-braces: `slugify` emits only
                # [a-z0-9-], so no quote can reach here and a mutant that
                # removes it will survive. Kept because the escape is the
                # invariant, not the current alphabet.
                #
                # ``tabindex="-1"`` makes a jump link MOVE FOCUS rather than
                # only scroll. Without it `document.activeElement` stays on
                # <body> after following a table-of-contents link, so where the
                # keyboard resumes is up to the browser rather than the page.
                # -1 keeps the heading out of the Tab order.
                attrs = f' id="{html.escape(anchor, quote=True)}" tabindex="-1"' if anchor else ""
                parts.append(f"<h{level}{attrs}>{html.escape(text)}</h{level}>")
                if len(run) > 1:
                    body = " ".join(x.strip() for x in run[1:])
                    parts.append(f"<p>{html.escape(body)}</p>")
            elif _is_list_item(first):
                parts.append(_render_list(run))
            else:
                parts.append(f"<p>{html.escape(' '.join(line.strip() for line in run))}</p>")
    return "".join(parts)


def document_anchor(title: str, markdown: str) -> str:
    """The id a document's section is reachable at — its own ``#`` heading's slug.

    Derived from the heading rather than from ``title`` because
    :func:`render_markdown` already stamps an ``id`` on that heading. Slugging
    the title *separately* and putting it on a wrapping ``<section>`` produced
    the same string twice on the real page — ``id="about-citevyn"`` on both the
    section and its ``<h2>`` — which is invalid HTML and ambiguous for anything
    that resolves ids. Found by mutation-testing the anchor logic: the mutant
    survived precisely because the duplicate id was still there to catch the
    jump link. One id, on the heading, is what a reader's focus should land on
    anyway.

    Falls back to the title's slug for a document with no ``#`` heading.
    """
    for line in markdown.splitlines():
        stripped = line.strip()
        if _is_heading(stripped):
            return slugify(stripped.lstrip("#").strip())
    return slugify(title)


def _table_of_contents(rendered_documents: Sequence[tuple[str, str, str]]) -> str:
    """Jump links to each document section.

    Present because the page carries more than one source: an answer cited to
    ``/about`` may have come from either the product description or the
    glossary, and dropping the reader at the top of a two-document page with no
    signpost is the citation equivalent of "see the manual".
    """
    links: list[str] = []
    for title, markdown, rendered in rendered_documents:
        # An anchor is only real once the section it names has actually been
        # rendered and carries that id. A previous version linked every
        # document, so a corpus file that shipped truncated-but-present gave
        # the reader a jump link to an id nowhere on the page — and because
        # only ONE document has to render for the whole-page fallback to stay
        # quiet, that shipped a 200 with a dead link in it.
        anchor = document_anchor(title, markdown)
        if anchor and f'id="{anchor}"' in rendered:
            links.append(
                f'<li><a href="#{html.escape(anchor, quote=True)}">{html.escape(title)}</a></li>'
            )
    if not links:
        return ""
    return f'<nav class="about-toc" aria-label="On this page"><ul>{"".join(links)}</ul></nav>'


def render_about_page(documents: Sequence[tuple[str, str]]) -> str:
    """The complete ``/about`` document for ``(title, markdown)`` pairs.

    ``title`` comes from the source's ``SourceSpec.title`` and is used only as
    the table-of-contents label — each document's own ``#`` heading supplies
    both the visible section heading and its anchor, so the page shows the
    corpus's words rather than a second name for the same thing, and no id is
    written twice (see :func:`document_anchor`).
    """
    sections: list[str] = []
    rendered_documents: list[tuple[str, str, str]] = []
    for title, markdown in documents:
        # heading_offset=1: the source doc's `#` becomes an <h2> beneath the
        # page's single <h1>, and carries the section's only id.
        rendered = render_markdown(markdown, heading_offset=1)
        rendered_documents.append((title, markdown, rendered))
        sections.append(f'<section class="about-doc">{rendered}</section>')
    rendered_anything = any(rendered for _, _, rendered in rendered_documents)
    body = "".join(sections)
    toc = _table_of_contents(rendered_documents)
    if not rendered_anything:
        # Tested on the RENDERED CONTENT, not on ``body``. An earlier version
        # checked ``if not body``, which could never fire once a document was
        # present but empty: the wrapping ``<section>`` made ``body`` non-empty,
        # so the page shipped an empty section plus a table-of-contents link
        # pointing at an anchor that did not exist.
        body = "<p>The source documents for this page are not available.</p>"
        toc = ""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta name="description" content="{html.escape(PAGE_DESCRIPTION, quote=True)}">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{html.escape(PAGE_TITLE)} — CiteVyn</title>\n"
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        f'<link rel="stylesheet" href="{_FONT_STYLESHEET_URL}">\n'
        f'<link rel="stylesheet" href="{STYLESHEET_URL}">\n'
        f'<script src="{THEME_SCRIPT_URL}"></script>\n'
        "</head>\n"
        "<body>\n"
        # <header> and <footer> sit OUTSIDE <main> on purpose. Per HTML-AAM
        # they only map to the banner / contentinfo landmarks when they are not
        # inside main/article/aside/nav/section; nested, they expose
        # role=generic and the page has no banner or contentinfo at all. The
        # shared width is `.about-page`, which wraps all three.
        '<div class="about-page">\n'
        '<header class="about-header">\n'
        '<a class="about-back" href="/" aria-label="Back to CiteVyn">&#8592; CiteVyn</a>\n'
        f"<h1>{html.escape(PAGE_TITLE)}</h1>\n"
        '<p class="about-lead">These are the pages CiteVyn cites when it answers '
        "questions about itself. They are the source text, not a summary of it.</p>\n"
        "</header>\n"
        "<main>\n"
        f"{toc}\n"
        f"{body}\n"
        "</main>\n"
        '<footer class="about-footer"><a href="/">Back to CiteVyn</a></footer>\n'
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


__all__ = [
    "PAGE_DESCRIPTION",
    "PAGE_TITLE",
    "STYLESHEET_URL",
    "THEME_SCRIPT_URL",
    "document_anchor",
    "render_about_page",
    "render_markdown",
    "slugify",
]
