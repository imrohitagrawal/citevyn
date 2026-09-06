"""Every markdown table row in docs/BACKLOG.md must actually RENDER.

WHY THIS EXISTS. `docs/BACKLOG.md` is the in-repo index of tracked follow-ups
and AGENTS.md makes reading it a start-of-session step, so a row that silently
loses its content is worse than a missing row: it looks present and complete.

GitHub-flavoured markdown discards every cell past the header's column count.
An unescaped ``|`` inside a code span still splits the row -- backticks do NOT
protect it -- so one stray pipe turns a 5-column row into a 7-column one and
GFM throws the last two cells away. Measured on the #355 row before this guard:
the rendered Origin cell stopped mid-word at ``[data-style="softly`` and
**2,701 characters of evidence rendered as nothing**, including every measured
contrast ratio and mutation result the row existed to carry.

It is not hypothetical and it is not once-off: the same defect was reintroduced
by the very edit that fixed it, four commits later, in a sentence about a
different bug. Fifteen pre-existing rows still carry it -- three in the open
follow-ups table and twelve in the 2-column table of resolved items -- and they
are enumerated below rather than silently fixed here or silently tolerated.

This lives in pytest rather than the frontend suite because `docs/` is
repo-level and this job already runs on every PR.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKLOG = Path(__file__).resolve().parents[2] / "docs" / "BACKLOG.md"

# A pipe that is NOT preceded by a backslash. This is the split GFM performs;
# `re.split` on a bare "|" would disagree with the renderer and report false
# failures on correctly-escaped rows.
CELL_SPLIT = re.compile(r"(?<!\\)\|")

# Rows that predate this guard and still lose content, enumerated rather than
# waved at. Fixing one means deleting it from here; adding a NEW broken row
# FAILS rather than quietly joining the list, which is the whole point.
#
# Twelve of these sit in the 2-column "Issue | Why it matters" table of closed
# items, where a single ``|`` in prose is enough. Repairing them is a docs sweep
# with no bearing on this change, so it is tracked as its own item rather than
# folded in here -- see docs/BACKLOG.md.
KNOWN_BROKEN = {
    # 5-column "Open follow-ups" table
    "#300",
    "#316",
    "#308",
    # 2-column table of resolved items
    "#208",
    "#221",
    "#231",
    "#229",
    "#226",
    "#215",
    "#216",
    "#217",
    "#210",
    "#237",
    "#236",
    "#234",
}


def _cell_count(line: str) -> int:
    """Cells GFM would render for a table row, excluding the leading/trailing."""
    return len(CELL_SPLIT.split(line.rstrip())) - 2


def _tables(text: str) -> list[tuple[int, int, list[tuple[int, str]]]]:
    """Every markdown table: (header line no, column count, [(line no, row)])."""
    lines = text.splitlines()
    tables: list[tuple[int, int, list[tuple[int, str]]]] = []
    i = 0
    while i < len(lines) - 1:
        header, sep = lines[i], lines[i + 1]
        # A separator row is the only unambiguous marker of a GFM table.
        if header.startswith("|") and re.fullmatch(r"\|[\s:|-]+\|", sep.strip()):
            cols = _cell_count(header)
            rows: list[tuple[int, str]] = []
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                rows.append((j + 1, lines[j]))
                j += 1
            tables.append((i + 1, cols, rows))
            i = j
            continue
        i += 1
    return tables


def test_backlog_has_tables_to_check() -> None:
    """Partner. Without it, a parser that matched nothing would pass everything."""
    tables = _tables(BACKLOG.read_text(encoding="utf-8"))
    assert len(tables) >= 3, f"only {len(tables)} tables parsed out of BACKLOG.md"
    assert sum(len(rows) for _, _, rows in tables) >= 40


def test_no_backlog_row_loses_cells_to_an_unescaped_pipe() -> None:
    offenders: list[str] = []
    for header_no, cols, rows in _tables(BACKLOG.read_text(encoding="utf-8")):
        for line_no, row in rows:
            got = _cell_count(row)
            if got <= cols:
                # Fewer cells renders as empty trailing cells -- ugly, not lossy.
                continue
            issue = re.search(r"\[(#\d+)\]", row)
            tag = issue.group(1) if issue else f"line {line_no}"
            if tag in KNOWN_BROKEN:
                continue
            lost = len(row) - len(CELL_SPLIT.split(row.rstrip())[cols].rstrip())
            offenders.append(
                f"BACKLOG.md:{line_no} ({tag}) renders {got} cells under a "
                f"{cols}-column header (from line {header_no}); GFM DISCARDS the "
                f"last {got - cols}, roughly {lost} characters. Escape the pipe "
                f"as \\| -- backticks do not protect it."
            )
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("tag", sorted(KNOWN_BROKEN))
def test_known_broken_rows_are_still_broken(tag: str) -> None:
    """The partner for the exemption list.

    Without this, fixing one of those rows would leave a stale entry in
    ``KNOWN_BROKEN`` that silently exempts the next row to reuse that issue
    number -- and an exemption nobody can see is how a guard stops guarding.
    Fix a row, delete it from the set, and this turns green again.
    """
    text = BACKLOG.read_text(encoding="utf-8")
    for _, cols, rows in _tables(text):
        for _, row in rows:
            if f"[{tag}]" in row and _cell_count(row) > cols:
                return
    pytest.fail(
        f"{tag} no longer over-runs its header -- remove it from KNOWN_BROKEN "
        f"in this file so the exemption cannot shelter a future row."
    )
