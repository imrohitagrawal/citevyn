"""Render a shared answer as a public page (ADR-0005 §4, Phase 6C).

Pure mechanism, like :mod:`app.services.about_page`: no ``app.*`` imports beyond
that page shell, no database. Check with
``grep "^from app\\." backend/app/services/shared_answer_page.py``.

THIS IS A LIVE SECURITY BOUNDARY
--------------------------------
Unlike ``/about``, the input here is not ours. The question is text a user
typed; the answer is text the model wrote, and a user can steer it. The page is
public and served from our domain. So:

* every piece of text is escaped with :func:`html.escape` before it is placed;
  nothing is ever inserted raw;
* nothing in the question or the answer becomes a link (no autolinking), so a
  shared page cannot point readers at a phishing site in our name;
* a citation links only when its URL is plain ``http``/``https`` with a host;
  anything else shows as text;
* the ``<title>`` and meta description are fixed text: link previews never show
  user text.

The Markdown subset is the chat's (``frontend/src/lib/answerFormat.ts``, #303):
``**bold**``, inline ``code``, ``-``/``*`` bullets and ``[n]`` citation markers.
Anything else is literal text. An unclosed ``**`` or backtick is literal too.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from app.services.about_page import render_page

PAGE_TITLE = "A shared answer"
PAGE_DESCRIPTION = "An answer from CiteVyn, with its sources, shared by a CiteVyn user."

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_BULLET = re.compile(r"^\s*[-*] (.*)$")
# One inline token: `code`, **bold**, or a [n] marker. Tried left to right.
_INLINE = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*|\[([0-9]{1,3})\]")
_MARKER = re.compile(r"\[([0-9]{1,3})\]")


def format_day(iso: str) -> str:
    """``2026-09-01`` -> ``1 Sep 2026``; anything else is returned as it is."""
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso)
    if not match or not 1 <= int(match.group(2)) <= 12:
        return iso
    year, month, day = match.groups()
    return f"{int(day)} {_MONTHS[int(month) - 1]} {year}"


def _marker_link(number: str, markers: set[int]) -> str:
    if int(number) in markers:
        return f'<a class="share-marker" href="#source-{int(number)}">[{int(number)}]</a>'
    return html.escape(f"[{number}]")


def _inline(text: str, markers: set[int]) -> str:
    """Escape ``text`` and render the inline subset. Never inserts raw text."""
    out: list[str] = []
    pos = 0
    for match in _INLINE.finditer(text):
        out.append(html.escape(text[pos : match.start()]))
        code, bold, marker = match.groups()
        if code is not None:
            out.append(f"<code>{html.escape(code)}</code>")
        elif bold is not None:
            # A citation inside bold is still a citation (as in the chat).
            parts: list[str] = []
            inner_pos = 0
            for inner in _MARKER.finditer(bold):
                parts.append(html.escape(bold[inner_pos : inner.start()]))
                parts.append(_marker_link(inner.group(1), markers))
                inner_pos = inner.end()
            parts.append(html.escape(bold[inner_pos:]))
            out.append(f"<strong>{''.join(parts)}</strong>")
        else:
            out.append(_marker_link(marker, markers))
        pos = match.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def render_answer_html(text: str, *, markers: set[int]) -> str:
    """The answer as an HTML fragment: paragraphs and bullet lists.

    ``markers`` are the citation numbers that exist; any other ``[n]`` stays text.
    """
    parts: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            parts.append(f"<p>{_inline(' '.join(para), markers)}</p>")
            para.clear()
        if items:
            parts.append(
                "<ul>" + "".join(f"<li>{_inline(i, markers)}</li>" for i in items) + "</ul>"
            )
            items.clear()

    for line in text.splitlines():
        bullet = _BULLET.match(line)
        if not line.strip():
            flush()
        elif bullet:
            if para:
                flush()
            items.append(bullet.group(1).strip())
        else:
            if items:
                flush()
            para.append(line.strip())
    flush()
    return "".join(parts)


def _safe_href(url: object) -> str | None:
    """``url`` if it is a plain http(s) URL with a host and no credentials, else None."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return url


def _marker_of(citation: Mapping[str, object]) -> int | None:
    value = citation.get("marker")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    # ASCII digits only: "²".isdigit() is True, yet int("²") raises.
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}", value):
        return int(value)
    return None


def _sources(citations: Iterable[Mapping[str, object]]) -> str:
    rows: list[str] = []
    seen: set[int] = set()
    for c in citations:
        marker = _marker_of(c)
        title = html.escape(str(c.get("title") or "Source"))
        href = _safe_href(c.get("url"))
        label = (
            f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer">{title}</a>'
            if href
            else title
        )
        as_of = c.get("docs_as_of")
        date = f" — docs as of {html.escape(format_day(as_of))}" if isinstance(as_of, str) else ""
        if marker is not None and marker not in seen:
            seen.add(marker)
            rows.append(f'<li id="source-{marker}">[{marker}] {label}{date}</li>')
        else:
            rows.append(f"<li>{label}{date}</li>")
    return f'<ol class="share-sources">{"".join(rows)}</ol>' if rows else ""


def render_shared_answer_page(
    *,
    question: str,
    answer: str,
    citations: Sequence[Mapping[str, object]],
    docs_as_of: str | None,
    shared_on: str,
) -> str:
    """The whole public page for one shared answer."""
    markers = {m for c in citations if (m := _marker_of(c)) is not None}
    as_of = f" Docs as of {html.escape(format_day(docs_as_of))}." if docs_as_of else ""
    main = (
        '<section class="about-doc">'
        "<h2>Question</h2>"
        f"<p>{html.escape(' '.join(question.split()))}</p>"
        "<h2>Answer</h2>"
        f"{render_answer_html(answer, markers=markers)}"
        "<h2>Sources</h2>"
        f"{_sources(citations)}"
        f"{f'<p>{as_of.strip()}</p>' if as_of else ''}"
        "</section>\n"
    )
    lead = (
        f"A copy of a CiteVyn answer, frozen when a CiteVyn user shared it on "
        f"{format_day(shared_on)}. That user wrote the question; CiteVyn wrote the answer."
    )
    return render_page(
        page_title=PAGE_TITLE,
        description=PAGE_DESCRIPTION,
        lead=lead,
        lead_class="about-lead",
        main_html=main,
    )


__all__ = ["PAGE_TITLE", "format_day", "render_answer_html", "render_shared_answer_page"]
