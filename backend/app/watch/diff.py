"""Compare two versions of a Markdown documentation page, section by section.

Pure mechanism: no ``app.*`` imports (AGENTS.md), no I/O. A section is the text
under a Markdown heading, named by its heading path ("Limits > Tier 1"). Lines
inside fenced code blocks are never headings (a ``# install`` shell comment is
not a section). Whitespace noise (trailing spaces, CRLF, runs of blank lines)
is normalised away first, so it never counts as a change.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TypedDict

# Linear-time patterns only: a lazy group followed by optional runs (the first
# version) backtracked for tens of seconds on one heading with a long run of
# spaces. The trailing ``#``s and spaces are stripped in code instead.
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")
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
    seen: Counter[str] = Counter()
    path: list[tuple[int, str]] = []
    current = ""
    fence: str | None = None  # the open fence marker; only a matching one closes it
    for line in normalize(text).split("\n"):
        opener = _FENCE.match(line)
        if opener:
            mark = opener.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip()[len(mark) :]:
                fence = None
        heading = None if fence is not None or opener else _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).rstrip(" \t").rstrip("#").rstrip(" \t") or "(untitled)"
            path = [(lvl, t) for lvl, t in path if lvl < level] + [(level, title)]
            name = " > ".join(t for _, t in path)
            seen[name] += 1  # a repeated heading path: counted, not searched for
            key = name if seen[name] == 1 else f"{name} ({seen[name]})"
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
    # Lines added and removed as a multiset difference: linear time on any page
    # (difflib took minutes on a 130 KB page of short repeated lines), and a
    # content line such as "---" is never mistaken for a diff header. A moved
    # line counts as neither added nor removed.
    before, after = Counter(normalize(old).split("\n")), Counter(normalize(new).split("\n"))
    plus = sum((after - before).values())
    minus = sum((before - after).values())
    return Summary(
        changed=changed[:MAX_LISTED],
        added=added[:MAX_LISTED],
        removed=removed[:MAX_LISTED],
        more=more,
        lines_added=plus,
        lines_removed=minus,
    )


#: The most text kept per change, added and removed lines together (for watch
#: alerts, Phase 7C). Whole lines are kept, never cut: vendor Markdown puts a
#: paragraph on one line, and a term past a per-line cut would never match.
MAX_CHANGED_CHARS = 200_000


class ChangedLines(TypedDict):
    added: list[str]
    removed: list[str]
    truncated: bool  # True when the budget ran out: later lines were not kept


def changed_lines(old: str, new: str) -> ChangedLines:
    """The lines added and removed, in page order, as a multiset difference
    (linear time; a moved line is neither). Blank lines are left out. Whole
    lines are kept until :data:`MAX_CHANGED_CHARS` (both lists together) runs
    out; then ``truncated`` is True."""
    budget = MAX_CHANGED_CHARS
    truncated = False

    def extra(these: list[str], those: list[str]) -> list[str]:
        nonlocal budget, truncated
        left = Counter(those)
        out: list[str] = []
        for line in these:
            if left[line] > 0:
                left[line] -= 1
            elif line.strip():
                if len(line) > budget:
                    truncated = True
                    break
                budget -= len(line)
                out.append(line)
        return out

    a, b = normalize(old).split("\n"), normalize(new).split("\n")
    added = extra(b, a)
    removed = extra(a, b)
    return ChangedLines(added=added, removed=removed, truncated=truncated)


__all__ = [
    "MAX_CHANGED_CHARS",
    "MAX_LISTED",
    "ChangedLines",
    "Summary",
    "changed_lines",
    "normalize",
    "section_changes",
]
