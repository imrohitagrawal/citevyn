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
different bug. Fifteen pre-existing rows carried it when this guard was written
-- three in the open follow-ups table and twelve in the 2-column table of
resolved items. **All fifteen were repaired in #360**, so ``KNOWN_BROKEN`` below
is now empty; see the note on it for what "repaired" meant in each shape.

This lives in pytest rather than the frontend suite because `docs/` is
repo-level and this job already runs on every PR.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKLOG = Path(__file__).resolve().parents[2] / "docs" / "BACKLOG.md"

# A pipe that is NOT preceded by a backslash. This is the split GFM performs;
# `re.split` on a bare "|" would disagree with the renderer and report false
# failures on correctly-escaped rows.
CELL_SPLIT = re.compile(r"(?<!\\)\|")

# Rows that predate this guard and still lose content, enumerated rather than
# waved at. Fixing one means deleting it from here; adding a NEW broken row
# FAILS rather than quietly joining the list, which is the whole point.
KNOWN_BROKEN: set[str] = set()
# EMPTY, and that is the finished state, not an oversight. All fifteen rows the
# docstring above enumerates were repaired in the #360 sweep: three in the
# 5-column table had literal pipes inside their Origin cell (escaped to \| ),
# and twelve in the 2-column archive table were carrying the 5-column schema
# (Issue | Title | Area | Priority | Origin) after being pasted across when they
# closed -- GFM was discarding Area, Priority and Origin from every one of them.
# Those three columns are now folded into the "Why it matters" cell, so nothing
# was dropped to make this set empty.
#
# An empty exemption set makes the old parametrized staleness partner VACUOUS --
# pytest reports "got empty parameter set" and the check asserts nothing, which
# is the failure mode tracked as #381 elsewhere in this repo. It is replaced
# below by test_the_detector_actually_detects, which proves the parser still
# finds a broken row by feeding it one, and therefore keeps meaning something no
# matter how many rows are exempted.


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


def _offenders(text: str) -> list[str]:
    """Rows whose cells GFM would discard, as operator-readable messages.

    A FUNCTION, not a loop inlined into the test, so the detector partner can
    call the same code path the real file check uses. Review of PR #383 proved
    why that matters: with the loop inlined, four mutations that completely
    disable the guard -- ``if got <= cols or True``, ``offenders.append`` ->
    ``[].append``, ``got = cols``, ``if tag in KNOWN_BROKEN`` -> ``if True`` --
    all left every test in this file GREEN, because nothing executed the loop
    with input it should have flagged. Those four die.

    HOW FAR THAT GOES, stated as a measured list rather than a count. An earlier
    version of this docstring claimed "one mutation still survives"; a skeptic
    ran 35 and found nine, so the count was wrong by eight. Counts invite that
    error, so there is no count here.

    Killed, each re-verified with the mutant text grep-confirmed present and the
    file restored byte-identically afterwards: ``if got <= cols or True``;
    ``offenders.append`` -> ``[].append``; ``got = cols``;
    ``if tag in KNOWN_BROKEN`` -> ``if True``; ``_cell_count`` -> ``return 0``;
    ``continue`` -> ``break`` in the skip below (which stopped the sweep at the
    first compliant row and reported ZERO offenders on a file that had one);
    ``return offenders[:1]``; ``lost = 0``; ``{got - cols}`` -> ``0``;
    ``tag = "#"``; and zeroing either ``line_no`` or ``header_no`` in the
    message.

    Still surviving, verified rather than assumed, and NOT claimed to be the
    complete set -- all three are assertion deletions inside the tests
    themselves, which no test can catch from inside itself:

    * ``assert not offenders`` -> ``assert True`` in the file check;
    * ``len(found) == 1`` -> ``>= 1`` in the detector test;
    * short-circuiting ``assert _offenders(escaped) == []``.

    Everything a message *says* -- file, line, tag, cell counts, header line,
    characters lost -- is asserted field by field below, so mutating any of them
    dies. What is deliberately NOT pinned is the message's wording: rephrasing
    the remedy sentence is not a test failure.
    """
    offenders: list[str] = []
    for header_no, cols, rows in _tables(text):
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
    return offenders


def test_no_backlog_row_loses_cells_to_an_unescaped_pipe() -> None:
    offenders = _offenders(BACKLOG.read_text(encoding="utf-8"))
    assert not offenders, "\n".join(offenders)


def test_no_exemption_is_stale() -> None:
    """Staleness partner for ``KNOWN_BROKEN``.

    A tag left here after its row is fixed silently shelters the next row to
    reuse that issue number.

    HONEST SCOPE, because the first version of this docstring overstated it and
    review caught that: while ``KNOWN_BROKEN`` is EMPTY this test asserts
    nothing -- the body reduces to ``assert not []`` and no mutation of the
    parser or of BACKLOG.md can redden it. Iterating rather than parametrizing
    buys one narrow thing: it stays EXECUTED instead of collapsing to pytest's
    "got empty parameter set" (the vacuous-parametrize shape tracked as #381).
    It is held in place for when the set refills, at which point it becomes a
    real check again. The non-vacuity of this FILE is carried by
    ``test_the_detector_actually_detects``, not by this test.
    """
    text = BACKLOG.read_text(encoding="utf-8")
    still_broken = {
        tag
        for tag in KNOWN_BROKEN
        for _, cols, rows in _tables(text)
        for _, row in rows
        if f"[{tag}]" in row and _cell_count(row) > cols
    }
    stale = sorted(KNOWN_BROKEN - still_broken)
    assert not stale, (
        f"{stale} no longer over-run their header -- remove them from "
        f"KNOWN_BROKEN in this file so the exemption cannot shelter a future row."
    )


def test_the_detector_actually_detects() -> None:
    """The non-vacuity partner, and the reason an empty KNOWN_BROKEN is safe.

    ``test_no_backlog_row_loses_cells_to_an_unescaped_pipe`` now passes with an
    empty exemption list. That is indistinguishable, from the outside, from a
    parser that has stopped finding rows at all -- so this feeds the parser a
    table it MUST flag, in both shapes the #360 sweep repaired.
    """
    five_col = (
        "| Issue | Title | Area | Priority | Origin |\n"
        "|---|---|---|---|---|\n"
        "| [#1](u) | t | a | p | prose with a | stray pipe |\n"
    )
    two_col = (
        "| Issue | Why it matters |\n|---|---|\n| [#2](u) | title | area | priority | origin |\n"
    )
    # Expected cell counts are LITERALS, counted by hand from the fixtures
    # above, not computed from the code under test -- deriving them from
    # `_cell_count` would make the assertion agree with any bug in it.
    #   five_col row: | [#1](u) | t | a | p | prose with a | stray pipe |  -> 6 cells, 1 discarded
    #   two_col  row: | [#2](u) | title | area | priority | origin |       -> 5 cells, 3 discarded
    cases = (
        ("5-column", five_col, 5, "#1", 6, 1),
        ("2-column", two_col, 2, "#2", 5, 3),
    )
    for name, doc, cols, tag, want_cells, want_lost in cases:
        tables = _tables(doc)
        assert len(tables) == 1, f"{name}: parser found {len(tables)} tables, want 1"
        _, got_cols, rows = tables[0]
        assert got_cols == cols, f"{name}: header read as {got_cols} columns, want {cols}"
        assert len(rows) == 1, f"{name}: parser found {len(rows)} rows, want 1"
        assert _cell_count(rows[0][1]) == want_cells, (
            f"{name}: the deliberately-broken row counted as "
            f"{_cell_count(rows[0][1])} cells, want {want_cells} -- the detector "
            f"has stopped detecting."
        )
        # Through `_offenders` too, which is what the file check actually calls.
        # Asserting only on the helpers above left the collection loop -- the
        # `got <= cols` skip, the KNOWN_BROKEN skip, the append itself -- with no
        # guard at all; four mutations disabling it survived every test here.
        found = _offenders(doc)
        assert len(found) == 1, (
            f"{name}: _offenders returned {len(found)} messages for a table with "
            f"exactly one over-running row -- the check's own loop is not working."
        )
        # The message's FIELDS, not a substring of its wording. Asserting
        # `"#" in msg and "DISCARDS" in msg` passed for the constant string
        # "#DISCARDS" -- no file, no line, no tag, no counts -- while also
        # pinning one word's casing, so a reworded remedy went red. Wrong in both
        # directions; a skeptic demonstrated each.
        for field, label in (
            ("BACKLOG.md:3", "the file and line to open"),
            (f"({tag})", "which row"),
            (f"renders {want_cells} cells", "how many cells GFM sees"),
            (f"{cols}-column header", "what it should have been"),
            ("from line 1", "which header the row is measured against"),
            (f"last {want_lost}", "how many cells are discarded"),
        ):
            assert field in found[0], (
                f"{name}: the offender message is missing {label} ({field!r}), so "
                f"an operator cannot act on it: {found[0]!r}"
            )
        # The character estimate, asserted as an INVARIANT rather than a literal:
        # a row that over-runs its header must discard something, so zero is
        # always wrong. Pinning the exact figure would just restate the formula
        # under test; this still kills `lost = 0`, which review found surviving.
        assert "roughly 0 characters" not in found[0], (
            f"{name}: the message claims zero characters are lost by a row that "
            f"over-runs its header, which cannot be true: {found[0]!r}"
        )
        lost_figure = int(re.search(r"roughly (\d+) characters", found[0]).group(1))  # type: ignore[union-attr]
        assert lost_figure > 0, (
            f"{name}: discarded-character estimate is {lost_figure}; a row GFM "
            f"truncates always loses something: {found[0]!r}"
        )

    # And the inverse, so the detector is not simply flagging everything: a
    # correctly-escaped row must NOT be flagged.
    escaped = (
        "| Issue | Title | Area | Priority | Origin |\n"
        "|---|---|---|---|---|\n"
        "| [#3](u) | t | a | p | prose with an escaped \\| pipe |\n"
    )
    _, cols, rows = _tables(escaped)[0]
    assert _cell_count(rows[0][1]) == cols, (
        "an escaped pipe was counted as a cell split -- the guard would report "
        "false failures on correctly-written rows."
    )
    assert _offenders(escaped) == [], (
        "a correctly-escaped row was reported as an offender -- the guard would "
        "go red on rows that render perfectly well."
    )

    # MULTI-ROW, and the reason these exist: every fixture above is a ONE-row
    # table, which cannot tell `continue` from `break`. A skeptic mutated the
    # skip below to `break` and the whole file stayed green while the sweep
    # reported ZERO offenders on a BACKLOG.md that really had one -- the same
    # "completely disables the guard" class the extraction was meant to close.
    # `return offenders[:1]` hid in the same blind spot.
    HDR = "| Issue | Title | Area | Priority | Origin |\n|---|---|---|---|---|\n"
    good = "| [#4](u) | t | a | p | fine |\n"
    broken_a = "| [#5](u) | t | a | p | prose with a | stray pipe |\n"
    broken_b = "| [#6](u) | t | a | p | another | stray pipe |\n"

    # A compliant row FIRST, so a `break` at the skip never reaches the bad one.
    after_good = _offenders(HDR + good + broken_a)
    assert len(after_good) == 1, (
        f"a broken row that sits AFTER a compliant one was not reported "
        f"({len(after_good)} offenders) -- the sweep is stopping early instead "
        f"of skipping, so any file whose first row is fine looks clean."
    )
    assert "(#5)" in after_good[0], f"wrong row reported: {after_good[0]!r}"

    # TWO broken rows, so returning only the first is visible.
    both = _offenders(HDR + broken_a + broken_b)
    assert len(both) == 2, (
        f"a table with two over-running rows produced {len(both)} offender(s) -- "
        f"a sweep would fix one row and believe the file was clean."
    )
    named = {tag for tag in ("#5", "#6") if any(f"({tag})" in m for m in both)}
    assert named == {"#5", "#6"}, (
        f"both broken rows must be named; the report identifies {named or 'neither'}: {both!r}"
    )
