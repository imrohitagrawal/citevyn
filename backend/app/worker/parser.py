"""Parse raw source text into a :class:`ParsedDocument`.

The MVP supports Markdown only. The parser is intentionally
naive — no CommonMark, no HTML, no front-matter. The MVP
fixtures are flat markdown and the production source feed
will pre-render to the same shape.

A :class:`ParsedDocument` is a title (the first ``#``
heading) followed by a list of :class:`Block` records, one
per ``##`` section. The chunker downstream groups blocks
by ``## `` heading; the title is preserved separately so the
chunker can prepend it to each chunk (the "contextual" in
"contextual chunking").
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    """One section under a ``## `` heading.

    ``heading`` is the ``## `` line text (no leading
    ``## ``). ``text`` is the body lines joined by
    single spaces — paragraph boundaries are not preserved
    because the retriever sees the chunk as a single string.
    """

    heading: str
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    """The result of parsing one source.

    ``title`` is the document's ``# H1`` heading. ``blocks``
    is the list of ``## H2`` sections, in source order.
    """

    title: str
    blocks: list[Block]


class ParseError(Exception):
    """Raised when the raw text cannot be parsed into anything indexable.

    :func:`parse_markdown` itself never raises it: empty input is a valid
    empty :class:`ParsedDocument`. The runner raises it when a source yields
    zero sections (#461), catches it, and writes ``"ParseError"`` to
    the :class:`IngestionJob.error_type` column. The message
    is preserved in ``error_message`` for the SRE to inspect.
    """


def parse_markdown(raw: str) -> ParsedDocument:
    """Parse a flat markdown string into a :class:`ParsedDocument`.

    Rules
    -----
    * The first ``# H1`` line is the document title. If no
      ``# `` line is present, the document title is empty
      (the runner will fall back to :class:`SourceSpec.title`).
    * ``## H2`` lines start a new :class:`Block`. The
      heading is everything after ``## ``.
    * Body lines between headings are collected verbatim,
      then joined with single spaces and stripped.
    * ``# H1`` lines after the first are treated as body
      text (rare; preserves the source's intent).
    * Empty input returns an empty :class:`ParsedDocument`.
    """
    title, blocks = _parse(_iter_lines(raw))
    return ParsedDocument(title=title, blocks=list(blocks))


def _iter_lines(raw: str) -> Iterator[str]:
    """Yield non-empty stripped lines (preserving order)."""
    for line in raw.splitlines():
        # Don't strip — headings need their leading
        # whitespace-free ``#`` markers. But trailing
        # whitespace is irrelevant.
        line = line.rstrip()
        if not line:
            continue
        yield line


def _parse(lines: Iterator[str]) -> tuple[str, list[Block]]:
    """Walk ``lines`` once, returning ``(title, blocks)``."""
    title = ""
    blocks: list[Block] = []
    current_heading: str | None = None
    current_text: list[str] = []

    def _flush() -> None:
        if current_heading is None:
            return
        text = " ".join(current_text).strip()
        if text:
            blocks.append(Block(heading=current_heading, text=text))
        current_text.clear()

    for line in lines:
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            _flush()
            current_heading = line[3:].strip()
            current_text = []
            continue
        if current_heading is None:
            # Pre-heading body lines are discarded and never
            # indexed; test_worker_sources_content.py keeps the
            # shipped corpus free of them (#461). The runner
            # falls back to ``SourceSpec.title`` if ``title``
            # is empty.
            continue
        current_text.append(line)
    _flush()
    return title, blocks


__all__ = [
    "Block",
    "ParseError",
    "ParsedDocument",
    "parse_markdown",
]
