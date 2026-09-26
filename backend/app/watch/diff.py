"""Compare two versions of a Markdown documentation page, section by section.

Pure mechanism: no ``app.*`` imports (AGENTS.md), no I/O. A section is the text
under a Markdown heading, named by its heading path ("Limits > Tier 1"). Lines
inside fenced code blocks are never headings (a ``# install`` shell comment is
not a section). Whitespace noise (trailing spaces, CRLF, runs of blank lines)
is normalised away first, so it never counts as a change.
"""

from __future__ import annotations

import difflib
import re
from typing import TypedDict

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
#: Most section names listed per kind; the rest is counted in ``more``.
MAX_LISTED = 20


class Summary(TypedDict):
    changed: list[str]
    added: list[str]
    removed: list[str]
    more: int
    lines_added: int
    lines_removed: int


def normalize(text: str) -> str:
    """Line endings to ``\\n``, trailing whitespace off, blank-line runs to one."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _sections(text: str) -> dict[str, list[str]]:
    """Heading path -> body lines, in document order. Text before the first
    heading is the section ``""``. A repeated path gets a ``(2)`` suffix."""
    sections: dict[str, list[str]] = {"": []}
    path: list[tuple[int, str]] = []
    current = ""
    in_fence = False
    for line in normalize(text).split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2)
            path = [(lvl, t) for lvl, t in path if lvl < level] + [(level, title)]
            name = " > ".join(t for _, t in path)
            key, n = name, 2
            while key in sections:
                key, n = f"{name} ({n})", n + 1
            sections[key] = []
            current = key
        else:
            sections[current].append(line)
    if not sections[""]:
        del sections[""]
    return sections


def section_changes(old: str, new: str) -> Summary:
    """What changed from ``old`` to ``new``: sections changed, added and removed
    (each list capped at :data:`MAX_LISTED`, the overflow counted in ``more``),
    and the number of lines added and removed across the page."""
    a, b = _sections(old), _sections(new)
    changed = [k for k in b if k in a and a[k] != b[k]]
    added = [k for k in b if k not in a]
    removed = [k for k in a if k not in b]
    more = sum(max(0, len(x) - MAX_LISTED) for x in (changed, added, removed))
    plus = minus = 0
    for line in difflib.unified_diff(
        normalize(old).split("\n"), normalize(new).split("\n"), lineterm="", n=0
    ):
        if line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    return Summary(
        changed=changed[:MAX_LISTED],
        added=added[:MAX_LISTED],
        removed=removed[:MAX_LISTED],
        more=more,
        lines_added=plus,
        lines_removed=minus,
    )


__all__ = ["MAX_LISTED", "Summary", "normalize", "section_changes"]
