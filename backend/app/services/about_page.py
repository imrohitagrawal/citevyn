"""Render the corpus markdown that CiteVyn cites about itself into an HTML page.

This module is pure mechanism: a bounded Markdown-subset renderer plus the page
shell. It imports nothing from ``app.*`` on purpose — per AGENTS.md, protocol /
wire-format logic stays free of app-specific imports so it can move to a shared
package later without dragging the application with it. Check the seam holds
with ``grep "^from app\\." backend/app/services/about_page.py`` (nothing).

Why render the corpus rather than write a page
----------------------------------------------
The citation URL ``/about`` is stamped onto every self-referential answer from
``app.worker.allowlist``. If the page were hand-authored it would become the
seventh paraphrase of the same copy (#84 item 4 counts six today), and the
citation would point at a page that merely *resembles* the source the claim was
drawn from. Rendering the source doc itself makes the link truthful by
construction and adds no copy to keep in sync.

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


def _render_list(block: list[str]) -> str:
    """``- item`` lines into ``<ul>``; continuation lines join their item.

    A continuation line is any line in a list block that does not itself start a
    new ``- `` item — the corpus hard-wraps bullets across several lines.
    """
    items: list[str] = []
    for line in block:
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
        elif items:
            items[-1] = f"{items[-1]} {stripped}"
        else:
            # A block that only *starts* looking like a list. Treat the stray
            # leading line as its own item rather than dropping it.
            items.append(stripped)
    rendered = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return f"<ul>{rendered}</ul>"


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
        first = block[0].strip()
        if first.startswith("#"):
            hashes = len(first) - len(first.lstrip("#"))
            text = first[hashes:].strip()
            # Clamp to h6: HTML has no h7, and a deeper corpus heading would
            # otherwise emit an element browsers do not recognise.
            level = min(hashes + heading_offset, 6)
            anchor = slugify(text)
            attrs = f' id="{html.escape(anchor, quote=True)}"' if anchor else ""
            parts.append(f"<h{level}{attrs}>{html.escape(text)}</h{level}>")
            # A heading block may carry following lines when the source has no
            # blank line after it; render them as a paragraph rather than lose them.
            if len(block) > 1:
                parts.append(f"<p>{html.escape(' '.join(x.strip() for x in block[1:]))}</p>")
        elif first.startswith("- "):
            parts.append(_render_list(block))
        else:
            parts.append(f"<p>{html.escape(' '.join(line.strip() for line in block))}</p>")
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
        if stripped.startswith("#"):
            return slugify(stripped.lstrip("#").strip())
    return slugify(title)


def _table_of_contents(documents: Sequence[tuple[str, str]]) -> str:
    """Jump links to each document section.

    Present because the page carries more than one source: an answer cited to
    ``/about`` may have come from either the product description or the
    glossary, and dropping the reader at the top of a two-document page with no
    signpost is the citation equivalent of "see the manual".
    """
    links: list[str] = []
    for title, markdown in documents:
        anchor = document_anchor(title, markdown)
        if anchor:
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
    for _title, markdown in documents:
        # heading_offset=1: the source doc's `#` becomes an <h2> beneath the
        # page's single <h1>, and carries the section's only id.
        rendered = render_markdown(markdown, heading_offset=1)
        sections.append(f'<section class="about-doc">{rendered}</section>')
    body = "".join(sections)
    if not body:
        # Defensive, and honest about it: an empty corpus selection would
        # otherwise ship a page with a heading and nothing under it, which reads
        # as a broken deploy rather than a broken configuration.
        body = "<p>The source documents for this page are not available.</p>"
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
        '<main class="about-page">\n'
        '<header class="about-header">\n'
        '<a class="about-back" href="/">CiteVyn</a>\n'
        f"<h1>{html.escape(PAGE_TITLE)}</h1>\n"
        '<p class="about-lead">These are the pages CiteVyn cites when it answers '
        "questions about itself. They are the source text, not a summary of it.</p>\n"
        "</header>\n"
        f"{_table_of_contents(documents)}\n"
        f"{body}\n"
        '<footer class="about-footer"><a href="/">Back to CiteVyn</a></footer>\n'
        "</main>\n"
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
