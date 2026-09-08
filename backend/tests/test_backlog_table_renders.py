"""Every markdown table row in docs/BACKLOG.md must actually RENDER.

WHY THIS EXISTS. `docs/BACKLOG.md` is the in-repo index of tracked follow-ups
and AGENTS.md makes reading it a start-of-session step, so a row that silently
loses its content is worse than a missing row: it looks present and complete.

GitHub-flavoured markdown discards every cell past the header's column count.
An unescaped ``|`` inside a code span still splits the row -- backticks do NOT
protect it -- so one stray pipe turns a 5-column row into a 7-column one and
GFM throws the last two cells away. Measured on the #355 row before this guard:
the rendered Origin cell stopped mid-word at ``[data-style="softly`` and
**2,701 characters of evidence rendered as nothing**.

A SECOND way a row can go UNCHECKED, added for #393: a checking gap rather than
a rendering one. The row collector below stops at the first line that does not
start with ``|``, so a row that loses its LEADING pipe -- or picks up an indent
-- ends the sweep, and every later row in the same table stops being examined
too. Measured at ``5e383a1``: deleting the leading pipe from the #372 row
(line 45) took the checked set from **94 rows to 47** and the file stayed green.

What GFM does with those same shapes, measured through GitHub's own renderer
(``gh api -X POST /markdown``) on the real 48-row table, subject = line 60:

===============================  =======  =======  ====================
mutation                         tables   ``<tr>``  #355 row in a table?
===============================  =======  =======  ====================
BASELINE                               6      100  yes
line 60 loses its leading pipe         6      100  yes
line 60 indented by 1, 2 or 3          6      100  yes
line 60 indented by 4 spaces           6       68  NO
===============================  =======  =======  ====================

So GFM makes the leading pipe OPTIONAL and strips up to three spaces of indent:
only the four-space indent actually breaks the table. Every message below says
"the parser stops CHECKING here", never "GFM will not render it", because the
second sentence is measurably false for three of those four shapes.
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

# Rows that predate this guard and still lose content. Adding a NEW broken row
# FAILS rather than quietly joining the list. EMPTY is the finished state: all
# fifteen were repaired in #360, and the non-vacuity of this file is carried by
# `test_the_detector_actually_detects` rather than by this set.
KNOWN_BROKEN: set[str] = set()


def _cell_count(line: str) -> int:
    """Cells GFM would render for a table row, excluding the leading/trailing."""
    return len(CELL_SPLIT.split(line.rstrip())) - 2


# Leading characters that are INVISIBLE in a rendered page but are not
# whitespace to Python: a BOM, a soft hyphen, and the zero-width / bidi
# formatting characters. A row prefixed with U+200B stops starting with `|`,
# leaves the parser's checked set, and looks unchanged in an editor.
#
# It is NOT free of false positives: `"\u200b| a | b"` counts three cells without
# the strip and two with it, so on a 2-column table the strip is what CREATES an
# orphan report on a line GFM renders as a paragraph. Kept anyway -- a zero-width
# prefix is the purest example of this file's subject.
_INVISIBLE_PREFIX = re.compile(
    "^[\\ufeff\\u00ad\\u200b-\\u200f\\u2028\\u2029\\u202a-\\u202e\\u2060-\\u2064]+"
)

# A line's LAST unescaped pipe, if the line ends on one -- trailing whitespace
# included, because trailing whitespace on a row is routine and GFM ignores it.
_ENDS_IN_A_CELL_WALL = re.compile(r"(?<!\\)\|\s*$")

# The delimiter row of a GFM table, matched against a STRIPPED line. One
# definition, used both to recognise a table (`_table_blocks`) and to notice one
# swallowed by the table above it (`_merged_tables`).
#
# At least one `-` is REQUIRED, which is GFM's rule: measured, `| a | b |` over
# `| : | : |` renders as a PARAGRAPH (zero tables), while the same over
# `| - | - |` renders as a table.
_SEPARATOR_ROW = re.compile(r"\|[\s:|-]*-[\s:|-]*\|")


# --- BEGIN SHARED CODE-BLOCK TRACKER ---------------------------------------
# Byte-identical in `test_backlog_table_renders.py` and
# `test_deploy_fly_build_args_are_documented.py`, and the equality is ASSERTED,
# not merely asked for in a comment: see
# `test_the_shared_code_block_tracker_has_not_drifted` in the deploy file, which
# compares the two blocks byte for byte. They are duplicated rather than
# imported so the two guards can still fail independently.


def _physical_lines(text: str) -> list[str]:
    """``text`` split on ``\\n`` only, with a trailing ``\\r`` removed.

    NOT ``str.splitlines()``, which also splits on U+0085, U+2028, U+2029,
    ``\\x0b`` and ``\\x0c``. Markdown does not: measured, a table cell holding any
    of those renders as one row with every cell intact, while `splitlines` tore
    the row in two and shifted every line number after it. Splitting on ``\\n``
    alone is what makes the 1:1 line numbering both guards report true.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


# A fence opener: up to three spaces of indent, then three or more backticks or
# tildes. Group 3 is the info string, which for a BACKTICK fence may not itself
# contain a backtick (CommonMark 4.5) -- that rule is what keeps a line of
# inline code from being read as a fence.
_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")

# FOUR or more spaces of indent opens an INDENTED CODE BLOCK (CommonMark 4.4),
# but only after a blank line: an indented line interrupting a paragraph is a
# lazy continuation, not code. Its content is CODE in both files, and the two do
# OPPOSITE things with it -- which is why the DETECTOR is shared and the reaction
# is not. The BACKLOG guard stops reading it as a table (measured: an indented
# row renders as `<pre>`); the deploy guard starts reading it as copy-pasteable
# (`docs/DEMO_CHECKLIST.md:51-54` is a live indented `psql` command).
#
# A TAB is not handled: CommonMark expands one to four spaces, this does not.
_INDENTED_CODE_RE = re.compile(r"^ {4,}\S")


def _opens_fence(match: re.Match[str]) -> bool:
    return not (match.group(2)[0] == "`" and "`" in match.group(3))


def _closes_fence(match: re.Match[str], char: str, length: int) -> bool:
    return (
        match.group(2)[0] == char and len(match.group(2)) >= length and not match.group(3).strip()
    )


def _code_mask(lines: list[str]) -> list[str]:
    """``""``, ``"marker"`` or ``"code"`` for each line of ``lines``.

    FLAVOUR-AWARE. A naive toggle over ``^\\s*(?:```|~~~)`` lets a ``~~~`` line
    INSIDE a ```` ```python ```` block close it early and inverts the parity of
    every fence after it. A fence closes only on the same marker character, at
    least as long as the opener, with nothing but whitespace after it.
    """
    mask: list[str] = []
    fence: tuple[str, int] | None = None
    indented = False
    prev_blank = True
    for line in lines:
        blank = not line.strip()
        if fence is None and not indented and prev_blank and _INDENTED_CODE_RE.match(line):
            indented = True
        if indented:
            if blank or _INDENTED_CODE_RE.match(line):
                mask.append("code")
                prev_blank = blank
                continue
            indented = False
        match = _FENCE_RE.match(line)
        if fence is None:
            if match is not None and _opens_fence(match):
                fence = (match.group(2)[0], len(match.group(2)))
                mask.append("marker")
            else:
                mask.append("")
        elif match is not None and _closes_fence(match, *fence):
            fence = None
            mask.append("marker")
        else:
            mask.append("code")
        prev_blank = blank
    return mask


# (name, markdown template, is the `{p}` payload inside a code block?). Both
# files parametrise their own consumer of `_code_mask` over this table with their
# own payload.
_CODE_BLOCK_SHAPES: tuple[tuple[str, str, bool], ...] = (
    ("backtick fence", "```\n{p}\n```\n", True),
    ("tilde fence", "~~~\n{p}\n~~~\n", True),
    ("fence with an info string", "```markdown\n{p}\n```\n", True),
    ("fence indented by three spaces", "   ```\n{p}\n   ```\n", True),
    ("tilde line inside a backtick fence", "```python\n~~~~\n{p}\n```\n", True),
    ("backtick line inside a tilde fence", "~~~python\n```\n{p}\n~~~\n", True),
    # A longer opener needs a closer at least as long...
    ("four-backtick fence closed by three", "````\n```\n{p}\n````\n", True),
    # ...and a LONGER closer does close a shorter opener, so this payload is
    # outside the block. Measured: GitHub renders the table that follows.
    ("three-backtick fence closed by four", "```\ncode\n````\n\n{p}\n\n", False),
    # A closing fence carries NO info string, so a same-flavour line that has one
    # does not close the block.
    ("info-string line inside a fence", "```\n```python\n{p}\n```\n", True),
    ("four-space indented code block", "text\n\n    {p}\n\ntail\n", True),
    ("fence indented by four spaces", "text\n\n    ```\n    {p}\n    ```\n\ntail\n", True),
    # An indented line that INTERRUPTS a paragraph is a lazy continuation, not
    # code, so this payload is live text.
    ("four-space indent with no blank line above", "text\n    {p}\n\ntail\n", False),
    # Not a fence at all: a backtick fence's info string may not contain a
    # backtick, so this is inline code. THREE backticks on purpose -- with two,
    # `_FENCE_RE` never matches and the rule under test is never reached.
    ("inline code, not a fence", "```a | b```\n\n{p}\n\n", False),
    ("no fence at all", "prose\n\n{p}\n\n", False),
)
# --- END SHARED CODE-BLOCK TRACKER -----------------------------------------


def _row_cell_count(line: str) -> int:
    """Cells GFM would render for ``line`` read as a table row.

    Unlike :func:`_cell_count` this does NOT assume the row still has both of
    its outer pipes: GFM makes each of them optional, so a row that lost one or
    both still has the same number of cells. Used only by the orphan check,
    whose entire subject is a row that has lost one.
    """
    stripped = _INVISIBLE_PREFIX.sub("", line.strip())
    stripped = re.sub(r"^\|", "", stripped)
    stripped = _ENDS_IN_A_CELL_WALL.sub("", stripped)
    return len(CELL_SPLIT.split(stripped))


def _scannable_lines(text: str) -> list[str]:
    """Every line of ``text`` with CODE BLOCKS BLANKED, positions kept.

    The list is the same length as :func:`_physical_lines`, so line numbers stay
    1:1. Feeding this to the TABLE parser -- not only to check (B) -- is what
    stops a fenced example table being parsed as a real one: measured, its
    closing fence was reported by check (A) on a file GFM renders perfectly.
    """
    lines = _physical_lines(text)
    return ["" if kind else line for kind, line in zip(_code_mask(lines), lines, strict=True)]


def _table_blocks(
    text: str,
) -> list[tuple[int, int, list[tuple[int, str]], str | None]]:
    """Every markdown table PLUS the line that ended its row run.

    ``(header line no, column count, [(line no, row)], terminator)``, where the
    terminator is the first line after the rows that does not start with ``|``.

    Returning it is the whole of #393: the row loop is deliberately dumb, which
    is fine for a table followed by a blank line but silent when the line that
    stopped it was MEANT to be a row.

    ``i = j`` rather than ``i = j + 1`` is EQUIVALENT and no test distinguishes
    them. ``i = j + 2`` is NOT, and
    ``test_a_table_immediately_after_a_terminator_line_is_still_parsed`` kills it.
    """
    lines = _scannable_lines(text)
    blocks: list[tuple[int, int, list[tuple[int, str]], str | None]] = []
    i = 0
    while i < len(lines) - 1:
        header, sep = lines[i], lines[i + 1]
        # A separator row is the only unambiguous marker of a GFM table.
        if header.startswith("|") and _SEPARATOR_ROW.fullmatch(sep.strip()):
            cols = _cell_count(header)
            rows: list[tuple[int, str]] = []
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                rows.append((j + 1, lines[j]))
                j += 1
            blocks.append((i + 1, cols, rows, lines[j] if j < len(lines) else None))
            i = j
            continue
        i += 1
    return blocks


def _tables(text: str) -> list[tuple[int, int, list[tuple[int, str]]]]:
    """Every markdown table: (header line no, column count, [(line no, row)]).

    A thin projection of :func:`_table_blocks` so there is ONE parser. Two
    copies of this loop would drift, and the check that watches for the loop
    bailing out early must watch the same loop the row checks use.
    """
    return [(header_no, cols, rows) for header_no, cols, rows, _ in _table_blocks(text)]


def test_backlog_has_tables_to_check() -> None:
    """Partner. Without it, a parser that matched nothing would pass everything.

    THESE FLOORS ARE NOT THE TRUNCATION GUARD, and reading them as one is the
    mistake #393 records: they prove the parser still finds SOMETHING, never that
    it finds everything. Measured at ``5e383a1``: 6 tables and 94 rows, so the
    floors carry 54 rows of slack. Raising them reproduces the defect with a
    bigger number; exact counts go red on every ordinary BACKLOG edit. The real
    protection is the four count-free checks.
    """
    tables = _tables(BACKLOG.read_text(encoding="utf-8"))
    assert len(tables) >= 3, f"only {len(tables)} tables parsed out of BACKLOG.md"
    assert sum(len(rows) for _, _, rows in tables) >= 40


def _offenders(text: str) -> list[str]:
    """Rows whose cells GFM would discard, as operator-readable messages.

    A FUNCTION, not a loop inlined into the test, so the detector partner calls
    the same code path the file check uses. Inlined, four mutations that
    completely disable the guard left every test here GREEN.

    Every FIELD a message names is asserted below; its WORDING is not pinned.
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


# A line that LOOKS like a tagged BACKLOG row. Deliberately narrow, because a
# false positive here goes red on a file that renders perfectly well. The
# OPTIONAL leading pipe is the point -- a row that still has one never reaches
# check (B), and losing it is what blinds the parser -- and the leading `\s*`
# admits the indented shape, which GFM renders and this parser does not.
_ROW_SHAPED = re.compile(r"^\s*\|?\s*\[#\d+\]")


def _unfenced_lines(text: str) -> list[tuple[int, str]]:
    """``(line no, line)`` for every line, with code blocks blanked out.

    A projection of :func:`_scannable_lines` that keeps the file's own line
    numbering, so every offender message can name a line an operator can open.
    """
    return list(enumerate(_scannable_lines(text), start=1))


# A line that begins a BLOCK-LEVEL structure. GFM ends a table at the first one
# of these, so a table followed immediately by a list item, heading, blockquote,
# HTML block or thematic break renders CORRECTLY -- measured: `| a |` + a
# `  - bullet` gives a 2-row table and a separate `<ul>`. Reporting those was a
# false positive, and the printed remedy was wrong advice for them.
#
# A plain PARAGRAPH line is deliberately NOT in this set: measured, prose placed
# directly after a table is ABSORBED as a table row (one filled cell, four empty
# ones), so it really does damage the table.
#
# Fences are absent on purpose: `_scannable_lines` has already blanked them.
_BLOCK_START = re.compile(
    r"^ {0,3}(?:"
    r"[-*+][ \t]"  # bullet list item
    r"|\d{1,9}[.)][ \t]"  # ordered list item
    r"|#{1,6}(?:[ \t]|$)"  # ATX heading
    r"|>"  # blockquote
    r"|<"  # HTML block
    r"|(?:-[ \t]*){3,}$|(?:\*[ \t]*){3,}$|(?:_[ \t]*){3,}$"  # thematic break
    r")"
)


def _is_a_block_structure(line: str) -> bool:
    """Does ``line`` open a block-level structure rather than continue a table?

    The cell-wall test comes FIRST and overrides the block-start test: a row that
    lost its leading pipe can still open with a character `_BLOCK_START` reads as
    a list bullet. ``docs/BACKLOG.md:88`` is a real untagged row whose first cell
    is a DASH (an em dash there, a hyphen in the fixtures below). A line ending
    on an unescaped ``|`` is a row that lost its front, not a list.
    """
    if _ENDS_IN_A_CELL_WALL.search(line):
        return False
    return bool(_BLOCK_START.match(line))


def _is_legitimate_terminator(line: str | None) -> bool:
    """May a table's row run legitimately END on ``line``?

    Three yeses: end of file, a blank (or whitespace-only) line, and a line that
    starts a block-level structure. Everything else means the collector bailed
    out in the middle of a table.
    """
    if line is None or not line.strip():
        return True
    return _is_a_block_structure(line)


def _truncated_tables(text: str) -> list[str]:
    """CHECK (A), #393. Tables whose row run ended somewhere it should not have.

    Parser-local and tag-free: it asks only whether the collector STOPPED for a
    legitimate reason. A header+separator yielding ZERO rows is the same failure
    landing on the first row. Verified at ``5e383a1``: all six tables terminate
    on a blank line, so this reports nothing today.

    WHAT IT CANNOT SEE: a blank line inserted mid-table (check (C)), a malformed
    HEADER or SEPARATOR (check (B)), and a row DELETED outright (no check).
    """
    offenders: list[str] = []
    for header_no, _cols, rows, terminator in _table_blocks(text):
        if not rows:
            offenders.append(
                f"BACKLOG.md:{header_no} is a table header + separator with ZERO "
                f"rows collected. Its first row is malformed -- almost always a "
                f"missing leading `|` or a leading indent -- so the parser stopped "
                f"before it and every row in that table is going unchecked."
            )
            continue
        if not _is_legitimate_terminator(terminator):
            offenders.append(
                f"BACKLOG.md:{rows[-1][0] + 1} ends the table headed at line "
                f"{header_no} with a line that is neither blank, nor a table row, nor "
                f"the start of a block-level structure, so row collection stopped "
                f"THERE and every later row in that table is going UNCHECKED. GFM may "
                f"well still render it -- a leading pipe is optional and up to three "
                f"spaces of indent are stripped -- so nothing looks wrong on the "
                f"page; only a four-space indent actually breaks the table, and a "
                f"plain prose line is absorbed as a bogus row. If this is meant to be "
                f"a row, give it a leading `|` at column 0. If it is not, put a blank "
                f"line between it and the table: {terminator!r}"
            )
    return offenders


def _orphan_run(
    lines: list[str], claimed: set[int], cols: int, order: range
) -> list[tuple[int, str]]:
    """Row-shaped lines cut off from a table, walking outward along ``order``.

    ``order`` is the sequence of 0-based indexes to visit: forwards from a
    table's terminator, or backwards from the line above its header. The walk
    skips blank lines and block-level structures (a heading, a bullet, an HTML
    comment) and then reports every consecutive line that splits into AT LEAST
    the table's column count.
    """
    positions = iter(order)
    for index in positions:
        line = lines[index]
        if not line.strip() or _is_a_block_structure(line):
            continue
        break
    else:
        return []
    found: list[tuple[int, str]] = []
    while True:
        line = lines[index]
        if index + 1 in claimed or not line.strip() or _row_cell_count(line) < cols:
            return found
        found.append((index + 1, line))
        try:
            index = next(positions)
        except StopIteration:
            return found


def _orphaned_rows(text: str) -> list[str]:
    """CHECK (C), #393 review. A row SPLIT OFF from its table by a blank line.

    Check (A) is blind whenever the row run ends at a legitimately blank line,
    and check (B) whenever the orphan carries no ``[#N]`` at the start. Those two
    blindnesses OVERLAP, and every shape in the overlap was measured LOSSY
    through GitHub's own renderer while the whole gate stayed green. So this
    check is POSITIONAL rather than shape-anchored, and runs in BOTH directions.

    THE THRESHOLD is a CELL COUNT: ``>= 2`` pipes misses the 2-column shape and
    ``>= 1`` fires on any prose carrying a pipe. The orphan must split into AT
    LEAST as many cells as the table it adjoins, both outer pipes optional the
    way GFM treats them. ``>=`` not ``==`` because an orphan of the WRONG width
    still renders as a paragraph, and an equality test declined it.

    WHAT IT CANNOT SEE, measured rather than assumed:

    * an orphan with FEWER cells than its table. It renders as a paragraph, so it
      is genuinely lossy, but at the line level it is indistinguishable from
      prose, and a gate that cries wolf gets deleted.
    * a table written inside a BLOCKQUOTE. ``_table_blocks`` needs a header
      starting with ``|`` and ``> |`` does not, so a quoted table is invisible to
      every check here -- including the pipe over-run this file exists for.
      Measured: ``> | [#1](u) | a | stray | pipe |`` under a 2-column quoted
      header renders with ``stray`` and ``pipe`` DISCARDED, unreported. Not
      closed because stripping ``> `` first would turn every legitimate
      blockquote terminator into a false positive.

    THE RESIDUAL FALSE POSITIVE: a prose PARAGRAPH line -- not a bullet, not a
    heading -- next to the 2-column table and carrying one unescaped pipe would
    be flagged. Measured: ZERO non-table lines in ``docs/BACKLOG.md`` contain a
    ``|`` at all.
    """
    lines = _scannable_lines(text)
    blocks = _table_blocks(text)
    claimed = {
        line_no
        for header_no, _cols, rows, _t in blocks
        for line_no in [header_no, header_no + 1, *(n for n, _ in rows)]
    }
    offenders: list[str] = []
    for header_no, cols, rows, _terminator in blocks:
        if not rows:
            continue
        runs = (
            ("BELOW", range(rows[-1][0], len(lines))),
            ("ABOVE", range(header_no - 2, -1, -1)),
        )
        for where, order in runs:
            for line_no, line in _orphan_run(lines, claimed, cols, order):
                offenders.append(
                    f"BACKLOG.md:{line_no} splits into at least {cols} cells -- the "
                    f"column count of the table headed at line {header_no}, whose rows "
                    f"run to line {rows[-1][0]} -- but it sits {where} that table with "
                    f"a blank line between, so GFM renders it as a PARAGRAPH outside "
                    f"the table and nothing here checks it. Delete the blank line (and "
                    f"give it a leading `|` at column 0 if it lost one): {line!r}"
                )
    return offenders


def _merged_tables(text: str) -> list[str]:
    """CHECK (D), #393 review. Two tables written with no blank line between.

    Measured: two complete tables separated by nothing are MERGED into one --
    ``<table>`` count 2 becomes 1 -- and the second table's delimiter row renders
    as a DATA ROW of the first. The parser swallows it the same way.

    A collected ROW shaped like a delimiter row is the signature, and it needs a
    RUN of dashes: a single-dash cell is ambiguous by construction, and ``| - |``
    is a real BACKLOG cell shape, so matching it reported a merged table on a
    file GitHub renders perfectly. The cost, stated: a second table whose
    delimiter is written ``| - | - |`` is NOT caught.
    """
    offenders: list[str] = []
    for header_no, _cols, rows, _terminator in _table_blocks(text):
        for line_no, row in rows:
            if _SEPARATOR_ROW.fullmatch(row.strip()) and "--" in row:
                offenders.append(
                    f"BACKLOG.md:{line_no} is a table DELIMITER row collected as a "
                    f"data row of the table headed at line {header_no}. Two tables "
                    f"are written with no blank line between them, so GFM merges them "
                    f"into one and renders this line as a row of dashes. Put a blank "
                    f"line between the two tables: {row!r}"
                )
    return offenders


def _unparsed_row_shaped_lines(text: str) -> list[str]:
    r"""CHECK (B), #393. Every row-shaped line must be a line the parser parsed.

    ONE-DIRECTIONAL, and that direction is load-bearing. "Every parsed row
    carries a `[#N]` tag" is the converse and it is FALSE here:
    ``docs/BACKLOG.md:88`` is a legitimate, correctly-rendering row carrying no
    tag at all. Four false positives were demonstrated against the naive anchor
    and are pinned by the tests below.

    THE RULE THAT REPLACED THE PIPE COUNT: a real BACKLOG row ENDS on an
    unescaped ``|``; a prose sentence ends on a full stop. Verified rather than
    assumed -- at ``5e383a1`` all 94 parsed rows do, which
    ``test_every_real_backlog_row_ends_on_an_unescaped_cell_wall`` re-checks.

    WHAT IT CANNOT SEE: an UNTAGGED row that loses its leading pipe (check (A)
    when it stopped the collector, check (C) when a blank line split it off), a
    row that lost BOTH outer pipes (check (C) again), and a row deleted outright.
    """
    parsed = {line_no for _, _, rows in _tables(text) for line_no, _ in rows}
    offenders: list[str] = []
    for line_no, line in _unfenced_lines(text):
        if line_no in parsed:
            continue
        if not _ROW_SHAPED.match(line):
            continue
        if not _ENDS_IN_A_CELL_WALL.search(line):
            # Prose that merely opens with a `[#N]` reference, pipes or not.
            continue
        offenders.append(
            f"BACKLOG.md:{line_no} is shaped like a tagged table row and ends on a "
            f"cell wall, but the parser did not collect it as one, so nothing in "
            f"this file is checking it. GFM may still render it inside the table -- "
            f"a leading pipe is optional and up to three spaces of indent are "
            f"stripped -- so the page can look fine while the row goes unchecked. "
            f"Usually a missing leading `|`, a leading indent, a blank line "
            f"splitting the table, or a malformed header/separator above it: "
            f"{line!r}"
        )
    return offenders


def _all_offenders(text: str) -> list[str]:
    """The file-level check path: every way a BACKLOG row can silently vanish.

    One entry point on purpose, so a detector cannot be orphaned -- left defined,
    imported by nothing, and quietly stop running.
    """
    return (
        _offenders(text)
        + _truncated_tables(text)
        + _orphaned_rows(text)
        + _merged_tables(text)
        + _unparsed_row_shaped_lines(text)
    )


def test_no_backlog_row_loses_cells_to_an_unescaped_pipe() -> None:
    offenders = _offenders(BACKLOG.read_text(encoding="utf-8"))
    assert not offenders, "\n".join(offenders)


def test_no_backlog_row_is_silently_dropped_from_the_check() -> None:
    """#393. The union gate: over-run rows AND rows the parser never saw.

    RED if any row in docs/BACKLOG.md loses its leading pipe, picks up an
    indent, is split off by a blank line, or sits under a malformed
    header/separator -- none of which the row-overrun check above can see,
    because all of them stop the collector rather than producing a bad row.
    """
    offenders = _all_offenders(BACKLOG.read_text(encoding="utf-8"))
    assert not offenders, "\n".join(offenders)


def test_no_exemption_is_stale() -> None:
    """Staleness partner for ``KNOWN_BROKEN``.

    A tag left here after its row is fixed silently shelters the next row to
    reuse that issue number. HONEST SCOPE: while ``KNOWN_BROKEN`` is EMPTY this
    asserts nothing. Iterating rather than parametrizing keeps it EXECUTED
    instead of collapsing to pytest's "got empty parameter set" (#381).
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

    A green `assert not offenders` on the real file is indistinguishable, from
    the outside, from a parser that has stopped finding rows at all -- so this
    feeds the parser a table it MUST flag, in both shapes #360 repaired.
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
        # Through `_offenders` too, which is what the file check calls. Asserting
        # only on the helpers left the collection loop unguarded; four mutations
        # disabling it survived every test here.
        found = _offenders(doc)
        assert len(found) == 1, (
            f"{name}: _offenders returned {len(found)} messages for a table with "
            f"exactly one over-running row -- the check's own loop is not working."
        )
        # The message's FIELDS, not a substring of its wording: `"#" in msg and
        # "DISCARDS" in msg` passed for the constant string "#DISCARDS".
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
        # The character estimate as an INVARIANT rather than a literal: pinning
        # the figure would restate the formula under test, and this still kills
        # `lost = 0`.
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

    # MULTI-ROW, because every fixture above is a ONE-row table, which cannot
    # tell `continue` from `break`. Mutating the skip to `break` left the whole
    # file green while the sweep reported ZERO offenders on a BACKLOG.md that had
    # one; `return offenders[:1]` hid in the same blind spot.
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


# ---------------------------------------------------------------------------
# #393 detector partners. `assert not offenders` on the real file passes just as
# happily when the detector has stopped detecting, so each check below is fed
# input it MUST flag and input it must NOT. Every fixture is MULTI-ROW with a
# WELL-FORMED FIRST ROW, because a single-row fixture cannot tell "stopped early"
# from "found nothing". Expected line numbers are counted by hand from the
# fixture text, never computed from the code under test.
# ---------------------------------------------------------------------------

_HDR = "| Issue | Title | Area | Priority | Origin |\n|---|---|---|---|---|\n"

# 1 | Issue | ... |
# 2 |---|---|---|---|---|
# 3 | [#1](u) | t | a | p | fine |
# 4 | [#2](u) | t | a | p | fine |
# 5 | [#3](u) | t | a | p | fine |
# 6 (blank)
# 7 prose after the table
_WELL_FORMED = _HDR + (
    "| [#1](u) | t | a | p | fine |\n"
    "| [#2](u) | t | a | p | fine |\n"
    "| [#3](u) | t | a | p | fine |\n"
    "\nprose after the table\n"
)

# The same table with line 4's LEADING PIPE removed. Row collection stops at
# line 4, so lines 4 AND 5 leave the checked set -- the #393 cascade.
_PIPE_LOST_MID = _HDR + (
    "| [#1](u) | t | a | p | fine |\n"
    "[#2](u) | t | a | p | pipe lost |\n"
    "| [#3](u) | t | a | p | fine |\n"
    "\nprose after the table\n"
)


def test_the_well_formed_fixture_is_clean_for_every_check() -> None:
    """The INVERSE, first, so no check is simply flagging everything.

    RED if `_truncated_tables`'s terminator predicate is inverted, or if
    `_ROW_SHAPED` widens to match rows the parser already collected.
    """
    assert _truncated_tables(_WELL_FORMED) == []
    assert _unparsed_row_shaped_lines(_WELL_FORMED) == []
    assert _all_offenders(_WELL_FORMED) == []
    # ...and the parser really did see all three rows, so "clean" is not
    # "found nothing". Counted by hand from the fixture above.
    assert [line_no for _, _, rows in _tables(_WELL_FORMED) for line_no, _ in rows] == [3, 4, 5]


def test_check_a_reports_a_table_whose_rows_stop_at_a_non_row_line() -> None:
    """CHECK (A) bites.

    RED if the terminator branch of `_truncated_tables` is deleted or inverted.
    """
    found = _truncated_tables(_PIPE_LOST_MID)
    assert len(found) == 1, f"want exactly one truncated table, got {found}"
    assert "BACKLOG.md:4" in found[0], (
        f"the message must name the line row collection stopped at (4): {found[0]!r}"
    )
    assert "line 1" in found[0], f"the message must name the table's header line: {found[0]!r}"
    # And the truncation really happened: the parser kept only the first row.
    assert [line_no for _, _, rows in _tables(_PIPE_LOST_MID) for line_no, _ in rows] == [3]


def test_check_a_reports_a_header_that_collected_no_rows_at_all() -> None:
    """The same failure landing on a table's FIRST row.

    Two tables, so `continue` -> `break` in `_truncated_tables` is visible: a
    `break` at the zero-row block never reaches the second.

    RED if the `if not rows:` branch is deleted, or its `continue` becomes
    `break`.
    """
    doc = (
        _HDR  # lines 1-2
        + "[#1](u) | t | a | p | first row lost its pipe |\n"  # 3
        + "\n"  # 4
        + _HDR  # 5-6
        + "| [#2](u) | t | a | p | fine |\n"  # 7
        + "[#3](u) | t | a | p | pipe lost |\n"  # 8
        + "\n"  # 9
    )
    found = _truncated_tables(doc)
    assert len(found) == 2, (
        f"a zero-row table AND a truncated one must both be reported; got "
        f"{len(found)}: {found}. One offender means the sweep stopped at the "
        f"first table instead of continuing."
    )
    assert "BACKLOG.md:1 " in found[0], f"zero-row table must name its header line: {found[0]!r}"
    assert "ZERO" in found[0], f"say what is wrong, not just where: {found[0]!r}"
    assert "BACKLOG.md:8" in found[1], f"second table truncates at line 8: {found[1]!r}"


def test_a_zero_row_table_reaches_every_check_without_crashing() -> None:
    """No check may die on `rows[-1]` when a table collected no rows.

    Nothing used to call `_orphaned_rows` on a zero-row table, so deleting its
    `if not rows: continue` raised `IndexError` and the suite stayed green -- a
    mutant that "dies" on a crash nobody runs is a mutant nobody kills.

    RED if that guard is deleted (IndexError), or if `_truncated_tables` stops
    reporting the zero-row table.
    """
    doc = _HDR + "[#1](u) | first row lost its pipe | a | p | o |\n\ntail\n"
    assert _tables(doc)[0][2] == [], "the fixture must really collect zero rows"
    assert _orphaned_rows(doc) == [], f"a zero-row table has no orphan run: {_orphaned_rows(doc)}"
    reported = _all_offenders(doc)
    assert any("ZERO" in m for m in reported), (
        f"a header+separator collecting no rows must be reported: {reported}"
    )


# 1 [#400] is mentioned in prose and carries no pipes at all.
# 2 (blank)
# 3 | Issue | ... |
# 4 |---|---|---|---|---|
# 5 | [#1](u) | t | a | p | fine |
# 6 [#2](u) | t | a | p | pipe lost |
# 7 | [#3](u) | t | a | p | fine |
# 8 (blank)
_MIXED = (
    "[#400] is mentioned in prose and carries no pipes at all.\n"
    "\n" + _HDR + "| [#1](u) | t | a | p | fine |\n"
    "[#2](u) | t | a | p | pipe lost |\n"
    "| [#3](u) | t | a | p | fine |\n"
    "\n"
)


def test_check_b_reports_every_row_shaped_line_the_parser_missed() -> None:
    """CHECK (B) bites, on BOTH rows the cascade swallowed.

    The fixture exercises all three `continue`s in order: a row-shaped PROSE line
    (the cell-wall skip), a row the parser DID collect (the `in parsed` skip),
    then the two it did not. Turning any of them into `break` reports ZERO
    offenders on a document that has two.

    RED if `_ROW_SHAPED` stops matching, if the append is neutered, if the return
    is sliced to `[:1]`, or if any `continue` becomes `break`.
    """
    found = _unparsed_row_shaped_lines(_MIXED)
    assert len(found) == 2, (
        f"lines 6 and 7 are both row-shaped and neither was collected; got {len(found)}: {found}"
    )
    assert "BACKLOG.md:6" in found[0], f"first offender must be line 6: {found[0]!r}"
    assert "BACKLOG.md:7" in found[1], f"second offender must be line 7: {found[1]!r}"
    # The prose line must NOT be reported -- a demonstrated false positive
    # against the naive regex.
    assert not any("BACKLOG.md:1" in m for m in found), (
        f"`[#400] is mentioned in prose` is not a table row and must not be flagged: {found}"
    )


def test_check_b_sees_a_de_piped_row_that_carries_TRAILING_WHITESPACE() -> None:
    r"""`_ENDS_IN_A_CELL_WALL` must tolerate whitespace after the final pipe.

    Trailing whitespace is routine and invisible in an editor, and GFM ignores it
    -- measured, the row still renders inside the table. Without the `\s*` the
    line stops looking like a row to checks (A) and (B) at once.

    RED if `_ENDS_IN_A_CELL_WALL` narrows to `(?<!\\)\|$`.
    """
    doc = _HDR + "| [#1](u) | t | a | p | fine |\n[#2](u) | t | a | p | de-piped |   \n\ntail\n"
    found = _unparsed_row_shaped_lines(doc)
    assert len(found) == 1, f"a de-piped row with trailing spaces was not reported: {found}"
    assert "BACKLOG.md:4" in found[0], f"wrong line named: {found[0]!r}"


def test_check_b_sees_the_shapes_check_a_is_blind_to() -> None:
    """Blank-line split, malformed header, malformed separator.

    All three leave a legitimate terminator (or no recognised table at all), so
    only (B) is left. This is why the gate is the UNION rather than either one.

    RED if `_unparsed_row_shaped_lines` stops being called by `_all_offenders`.
    """
    blank_split = _HDR + (
        "| [#1](u) | t | a | p | fine |\n\n| [#2](u) | t | a | p | orphaned |\n\n"
    )
    no_header_pipe = "Issue | Title | Area | Priority | Origin |\n|---|---|---|---|---|\n" + (
        "| [#1](u) | t | a | p | fine |\n| [#2](u) | t | a | p | fine |\n\n"
    )
    no_separator_pipe = "| Issue | Title | Area | Priority | Origin |\n---|---|---|---|---|\n" + (
        "| [#1](u) | t | a | p | fine |\n| [#2](u) | t | a | p | fine |\n\n"
    )
    for name, doc in (
        ("blank line mid-table", blank_split),
        ("header lost its leading pipe", no_header_pipe),
        ("separator lost its leading pipe", no_separator_pipe),
    ):
        assert _truncated_tables(doc) == [], (
            f"{name}: check (A) is documented as blind here; if it now fires, the "
            f"docstrings claiming (B) is required are wrong and must be corrected."
        )
        assert _unparsed_row_shaped_lines(doc), f"{name}: check (B) missed it"
        assert _all_offenders(doc), f"{name}: the union gate missed it"


def test_check_a_sees_the_shape_check_b_is_blind_to() -> None:
    """An UNTAGGED last row that loses its pipe -- the other half of the union.

    Nothing identifies it as a row to (B), because it carries no `[#N]`; only the
    terminator check catches it. Line 88 of the real file is exactly this shape,
    which is why (B) cannot simply demand a tag on every row.

    RED if `_truncated_tables` stops being called by `_all_offenders`.
    """
    doc = (
        _HDR
        + "| [#1](u) | t | a | p | fine |\n"
        + "- | an untagged row that lost its leading pipe | a | p | untagged |\n"
        + "\n"
    )
    assert _unparsed_row_shaped_lines(doc) == [], (
        "check (B) is documented as blind to an untagged row; if it now fires, "
        "the docstring is wrong."
    )
    assert _truncated_tables(doc), "check (A) missed an untagged row that lost its pipe"
    assert _all_offenders(doc), "the union gate missed it"


def test_row_shaped_lines_inside_a_code_fence_are_not_flagged() -> None:
    """A COMPLETE fenced example table is documentation, not a table.

    THE FIXTURE IS A COMPLETE TABLE ON PURPOSE: a bare row with no
    header/separator pair makes `_table_blocks` recognise no table at all, so the
    test could not tell working fence tracking from a parser that found nothing.

    RED if code-block tracking is removed from `_scannable_lines`, or if the
    table parser goes back to reading `_physical_lines` directly.
    """
    # 1 prose
    # 2 (blank)
    # 3 ```markdown
    # 4 | Issue | Title |
    # 5 |---|---|
    # 6 | [#998](u) | an example row |
    # 7 ```
    # 8 (blank)
    # 9 tail
    fenced = (
        "prose\n"
        "\n"
        "```markdown\n"
        "| Issue | Title |\n"
        "|---|---|\n"
        "| [#998](u) | an example row |\n"
        "```\n"
        "\n"
        "tail\n"
    )
    assert _tables(fenced) == [], (
        f"a table written INSIDE a fence was parsed as a real one: {_tables(fenced)}"
    )
    assert _truncated_tables(fenced) == [], (
        f"the closing fence was reported as an illegitimate terminator: {_truncated_tables(fenced)}"
    )
    assert _unparsed_row_shaped_lines(fenced) == []
    assert _all_offenders(fenced) == []
    # ...and the same lines OUTSIDE a fence are still caught, so the exclusion is
    # the fence and not the content.
    unfenced = "| [#998](u) | an example row that is not in a fence |\n\nprose\n"
    assert _unparsed_row_shaped_lines(unfenced), (
        "an unfenced row-shaped line under no header must still be reported"
    )


def test_a_fence_opener_directly_after_a_table_is_a_legitimate_terminator() -> None:
    """Measured: GFM renders that table correctly, so the gate must stay green.

    A fenced block opening on the line right after a table's last row ends the
    table cleanly -- 1 table, 2 `<tr>`, a separate `<pre>`.

    RED if `_code_mask` stops marking the fence OPENER line, which leaves
    "```bash" as the terminator and reports a perfectly good document.
    """
    doc = _HDR + "| [#1](u) | t | a | p | fine |\n```bash\necho hi\n```\n\ntail\n"
    assert _all_offenders(doc) == [], f"a fence directly after a table was flagged: {doc!r}"


def test_prose_bullets_that_cite_an_issue_are_not_flagged() -> None:
    """The prose `- **[#163](url)** - ...` lines in the real file.

    They begin with `- `, which is neither whitespace nor a pipe, so the anchor
    rejects them without an allowlist. Asserted directly rather than trusted:
    widening `_ROW_SHAPED` by one character would turn every one of them red.

    RED if `_ROW_SHAPED` is loosened to allow a leading `-`.
    """
    bullets = (
        "- **[#163](https://example/163)** - a prose follow-up | with a pipe | and another\n"
        "  - **[#164](https://example/164)** - an indented one | with | pipes\n"
        "\n"
    )
    assert _unparsed_row_shaped_lines(bullets) == []


# ---------------------------------------------------------------------------
# The FALSE-POSITIVE battery. Every case below was demonstrated RED against an
# earlier version of these checks and renders correctly through GitHub's own
# renderer. A guard that cries wolf gets blanket-updated or deleted.
# ---------------------------------------------------------------------------


def test_prose_that_cites_an_issue_and_carries_pipes_is_not_flagged() -> None:
    """The false positive the old TWO-PIPE rule could not close.

    Both shapes below render as ordinary paragraphs -- measured, `<table>` count
    and `<tr>` count unchanged from baseline -- and both were reported.

    RED if `_unparsed_row_shaped_lines` goes back to a pipe COUNT, or if
    `_ENDS_IN_A_CELL_WALL` is dropped from it.
    """
    paragraph = (
        "[#909](https://example/909) opens a paragraph at column 0 and mentions "
        "`a | b` and `c | d`.\n\n"
    )
    wrapped_bullet = (
        "- a bullet whose continuation wraps onto the next line and cites\n"
        "  [#909](https://example/909) while mentioning `a | b` and `c | d`.\n\n"
    )
    for name, doc in (("paragraph", paragraph), ("wrapped bullet", wrapped_bullet)):
        assert _all_offenders(doc) == [], f"{name} was flagged: {_all_offenders(doc)}"
    # ...and the SAME sentence ending on a cell wall is still caught, so the
    # discriminator is the trailing pipe and not the prose.
    row = "[#909](https://example/909) | a | b | c | d |\n\n"
    assert _unparsed_row_shaped_lines(row), (
        "a de-piped row ending on a cell wall must still be reported"
    )


def test_a_row_ending_in_an_ESCAPED_pipe_is_not_a_cell_wall() -> None:
    r"""The escape-awareness of check (B)'s rule, pinned on its own.

    ``CELL_SPLIT`` is escape-aware because GFM is; the trailing-pipe test has to
    agree with it or the two disagree about the same character. A sentence that
    ends by quoting an escaped pipe is prose.

    RED if ``(?<!\\)`` is dropped from ``_ENDS_IN_A_CELL_WALL``.
    """
    escaped = "[#909](https://example/909) ends a sentence quoting a literal \\|\n\n"
    assert _unparsed_row_shaped_lines(escaped) == [], (
        f"a line ending on an ESCAPED pipe is not a table row: "
        f"{_unparsed_row_shaped_lines(escaped)}"
    )


def test_a_block_level_structure_directly_after_a_table_is_not_flagged() -> None:
    """GFM ends a table at the first block-level structure; so does check (A).

    Measured: with no blank line between, the bullet becomes a `<ul>`, the
    heading an `<h2>`, the HTML comment disappears, and the table keeps its two
    `<tr>`s. All were reported, and the printed remedy was wrong advice for them.

    RED if `_BLOCK_START` stops matching any of these shapes, or if
    `_is_a_block_structure` stops consulting it.
    """
    one_row = _HDR + "| [#1](u) | t | a | p | fine |\n"
    for name, terminator in (
        ("nested bullet", "  - a nested bullet\n"),
        ("bullet at column 0", "- a bullet\n"),
        ("ordered list item", "1. an ordered item\n"),
        ("heading", "## a heading\n"),
        ("html comment", "<!-- a comment -->\n"),
        ("blockquote", "> quoted\n"),
        ("thematic break", "---\n"),
    ):
        doc = one_row + terminator + "\ntail\n"
        assert _all_offenders(doc) == [], f"{name}: flagged {_all_offenders(doc)}"

    # ...and a plain PARAGRAPH line is the opposite case and must STAY flagged:
    # measured, GFM absorbs it as a table row with one filled cell and four
    # empty ones, so it really does damage the table.
    prose = one_row + "Prose right after the table.\n\ntail\n"
    assert _truncated_tables(prose), (
        "a prose line directly after a table is absorbed by GFM as a bogus row "
        "and must still be reported"
    )
    # ...and so must a de-piped row whose first cell happens to start with a
    # dash, which `_BLOCK_START` would otherwise read as a list bullet.
    dash_row = one_row + "- | an untagged row that lost its pipe | a | p | o |\n\ntail\n"
    assert _truncated_tables(dash_row), (
        "the cell-wall override must beat the block-start test for a row whose first cell is a dash"
    )


def test_a_dash_only_or_colon_only_ROW_is_not_a_merged_table() -> None:
    """Measured: GFM renders all three of these as ordinary data cells.

    `| - | - |` under a 2-column header renders `<td>-</td><td>-</td>`; a
    colons-only row is not a delimiter at all -- `| a | b |` over `| : | : |`
    renders as a PARAGRAPH, zero tables. Check (D) reported every one of them.

    RED if `_merged_tables` drops its `"--" in row` requirement, or if
    `_SEPARATOR_ROW` stops requiring at least one `-`.
    """
    two = "| Issue | Why it matters |\n|---|---|\n| [#1](u) | fine |\n"
    five = _HDR + "| [#1](u) | t | a | p | fine |\n"
    for name, doc in (
        ("2-column `| - | - |` row", two + "| - | - |\n\ntail\n"),
        ("5-column dash-only row", five + "| - | - | - | - | - |\n\ntail\n"),
        ("colons-only row", five + "| : | : | : | : | : |\n\ntail\n"),
    ):
        assert _all_offenders(doc) == [], f"{name}: flagged {_all_offenders(doc)}"
    # ...and a colons-only DELIMITER really does mean "not a table", which is
    # what GFM does with it, so the rows below it are reported by check (B).
    colon_delim = "| Issue | Title |\n| : | : |\n| [#1](u) | a |\n\ntail\n"
    assert _tables(colon_delim) == [], (
        f"`| : | : |` is not a GFM delimiter row: {_tables(colon_delim)}"
    )
    assert _unparsed_row_shaped_lines(colon_delim), "the rows under it must be reported instead"


def test_an_empty_delimiter_row_does_not_start_a_table() -> None:
    """`||` is not a delimiter row -- measured, GFM renders zero tables.

    RED if `_SEPARATOR_ROW`'s dash requirement is dropped (`[\\s:|-]*` with no
    mandatory `-` accepts `||`), which invents a table and silently collects the
    rows below it as checked.
    """
    doc = "| Issue | Title |\n||\n| [#1](u) | a |\n\ntail\n"
    assert _tables(doc) == [], f"`||` started a table: {_tables(doc)}"
    assert _unparsed_row_shaped_lines(doc), "the row under it must be reported instead"


def test_line_separator_characters_inside_a_cell_do_not_split_the_row() -> None:
    r"""U+0085, U+2028, `\x0c` and `\x0b` are line breaks to Python, not to GFM.

    Measured: a cell containing any of them still renders as one row of the table
    with every cell intact -- 1 table, 3 `<tr>`, 10 `<td>`. `str.splitlines()`
    tore the row in two and shifted every line number after it.

    RED if `_physical_lines` goes back to `text.splitlines()`.
    """
    for name, ch in (
        ("U+0085", "\u0085"),
        ("U+2028", "\u2028"),
        ("form feed", "\x0c"),
        ("vertical tab", "\x0b"),
    ):
        doc = _HDR + "| [#1](u) | t | a | p | fine |\n" + f"| [#2](u) | a{ch}b | c | d | e |\n\n"
        assert _all_offenders(doc) == [], f"{name}: flagged {_all_offenders(doc)}"
        rows = [line_no for _, _, rs in _tables(doc) for line_no, _ in rs]
        assert rows == [3, 4], f"{name}: line numbering shifted -- parsed rows {rows}, want [3, 4]"


def test_an_INDENTED_CODE_BLOCK_containing_a_row_is_not_flagged() -> None:
    """Four spaces of indent after a blank line is a code block, not a row.

    Measured: each of these renders as `<pre>` and changes no table.
    `docs/BACKLOG.md` is a document ABOUT broken table rows, so quoting one as a
    code sample is the most likely future content there is, and all three shapes
    were reported.

    RED if `_code_mask` stops recognising `_INDENTED_CODE_RE`.
    """
    five = _HDR + "| [#1](u) | t | a | p | fine |\n"
    for name, doc in (
        ("indented code block", five + "\ntext:\n\n    | [#9](u) | a | b | c | d |\n\ntail\n"),
        (
            "fence indented four spaces inside a list",
            "- step:\n\n    ```markdown\n    | [#9](u) | a | b | c | d |\n    ```\n\ntail\n",
        ),
        (
            "fence indented four spaces at top level",
            "text\n\n    ```markdown\n    | [#9](u) | a | b | c | d |\n    ```\n\ntail\n",
        ),
    ):
        assert _all_offenders(doc) == [], f"{name}: flagged {_all_offenders(doc)}"

    # ...and the INVERSE: an indented line that interrupts a paragraph is a lazy
    # continuation, not code, so it is still checked.
    lazy = "text\n    | [#9](u) | a | b | c | d |\n\ntail\n"
    assert _unparsed_row_shaped_lines(lazy), (
        "a four-space-indented line that does NOT follow a blank line is a lazy "
        "paragraph continuation, not an indented code block, and must stay checked"
    )


# (name, template, is the `{p}` payload inside a code block?) -- the SHARED
# table above, applied to a row-shaped payload.
@pytest.mark.parametrize(
    ("name", "template", "in_code"), _CODE_BLOCK_SHAPES, ids=lambda v: str(v)[:44]
)
def test_the_code_block_tracker_is_flavour_aware(name: str, template: str, in_code: bool) -> None:
    """The row-shaped line is inside a code block, or it is not. No third answer.

    RED if `_FENCE_RE` drops the tilde flavour, drops its ` {0,3}` indent
    tolerance, stops requiring the closer to match the opener's character, stops
    requiring it to be at least as long, stops rejecting a backtick in a backtick
    fence's info string, or if `_INDENTED_CODE_RE` stops being consulted.
    """
    doc = template.format(p="| [#1](u) | a | b |")
    found = _unparsed_row_shaped_lines(doc)
    if in_code:
        assert found == [], f"{name}: a row inside a code block was reported: {found}"
    else:
        assert found, f"{name}: a row-shaped line OUTSIDE any code block was not reported"


# ---------------------------------------------------------------------------
# CHECK (C), the orphan check, and CHECK (D), the merged-table check. Both exist
# because the union of (A) and (B) accepted a file carrying the exact defect it
# guards: several shapes, each applied ALONE to the real `docs/BACKLOG.md`, all
# GREEN on the whole gate and all measured LOSSY through GitHub's renderer.
# ---------------------------------------------------------------------------

_ORPHAN_HDR_2 = "| Issue | Why it matters |\n|---|---|\n"


def test_check_c_reports_a_row_split_off_from_its_table_by_a_blank_line() -> None:
    """CHECK (C) bites on every shape that fell through (A) and (B).

    Each fixture is a well-formed table, a blank line, then a line that is
    unmistakably a row of it -- the state neither older check could see.

    RED if `_orphaned_rows` stops scanning past the blank line, stops comparing
    cell counts, stops being called by `_all_offenders`, or reports only the
    FIRST orphan of a split-off run.
    """
    # 1-2 header + delimiter, 3 the one good row, 4 the split, 5 the ORPHAN.
    # Counted by hand from the fixtures, never computed from the code under test.
    five = _HDR + "| [#1](u) | t | a | p | fine |\n"
    cases = (
        # An UNTAGGED row, the `docs/BACKLOG.md:88` shape.
        ("untagged row", five + "\n| - | untagged | a | p | o |\n\ntail\n", 5),
        # A WHITESPACE-ONLY line instead of a blank one.
        ("whitespace-only split", five + "   \n| - | untagged | a | p | o |\n\ntail\n", 5),
        # A ZERO-WIDTH SPACE in front of an otherwise perfect row.
        ("zero-width-space prefix", five + "\n\u200b| [#2](u) | t | a | p | o |\n\ntail\n", 5),
        # A de-piped row whose `[#N]` sits in CELL 2, so `_ROW_SHAPED` misses it.
        ("tag in cell 2, no leading pipe", five + "\n- | [#2](u) | a | p | o |\n\ntail\n", 5),
        # ONE CELL TOO MANY. Measured: it renders as a paragraph, so it is just
        # as lossy -- an equality test on the cell count declined it.
        ("orphan one cell too wide", five + "\n| - | u | a | p | o | extra |\n\ntail\n", 5),
        # Reached PAST a block-level structure, which the walk skips rather than
        # stopping at. Measured: the row still renders as a paragraph.
        ("orphan behind an HTML comment", five + "\n<!-- note -->\n| - | u | a | p | o |\n\n", 6),
        ("orphan behind a bullet", five + "\n- a bullet\n| - | u | a | p | o |\n\n", 6),
    )
    for name, doc, line_no in cases:
        found = _orphaned_rows(doc)
        assert len(found) == 1, f"{name}: want one orphan, got {found}"
        assert f"BACKLOG.md:{line_no}" in found[0], f"{name}: wrong line named: {found[0]!r}"
        # The message's FIELDS, so mutating any of them dies here rather than
        # silently shipping a report an operator cannot act on.
        assert "at least 5 cells" in found[0], f"{name}: no cell count: {found[0]!r}"
        assert "headed at line 1" in found[0], f"{name}: no header line: {found[0]!r}"
        assert "run to line 3" in found[0], f"{name}: no last-row line: {found[0]!r}"
        assert _all_offenders(doc), f"{name}: the union gate missed it"

    # An orphan ABOVE the first table in the file, which the forward walk can
    # never reach. Measured: it renders as a paragraph, outside the table.
    above = "| - | untagged | a | p | o |\n\n" + five + "\ntail\n"
    found = _orphaned_rows(above)
    assert len(found) == 1, f"an orphan above the table was not reported: {found}"
    assert "BACKLOG.md:1" in found[0] and "ABOVE" in found[0], f"wrong report: {found[0]!r}"

    # The 2-COLUMN archive shape, which loses BOTH outer pipes and is left with
    # exactly ONE unescaped pipe -- invisible to any `>= 2 pipes` rule.
    two_col = _ORPHAN_HDR_2 + "| [#1](u) | fine |\n\n[#2](u) | lost both pipes\n\ntail\n"
    found = _orphaned_rows(two_col)
    assert len(found) == 1, f"2-column archive row: want one orphan, got {found}"
    assert "BACKLOG.md:5" in found[0], f"wrong line named: {found[0]!r}"
    assert _all_offenders(two_col), "the union gate missed the 2-column orphan"

    # A ONE-COLUMN table, where a BLANK line itself splits into one "cell". The
    # walk steps onto the blank below the orphan, and only the blank test stops
    # that blank being reported as a second orphan -- measured, with the test
    # removed this fixture reports two. No wider table can show it: a blank
    # counts as one cell, which is already narrower than the table.
    one_col = "| Issue |\n|---|\n| [#1](u) |\n\n| [#2](u) |\n\n## next\n"
    found = _orphaned_rows(one_col)
    assert len(found) == 1, f"a 1-column table reported {len(found)} orphan(s): {found}"
    assert "BACKLOG.md:5" in found[0], f"wrong line named: {found[0]!r}"

    # A WHOLE TAIL split off, not just one row -- reporting the first and
    # stopping would let a sweep fix one line and believe the file was clean.
    run = five + "\n| - | first orphan | a | p | o |\n| - | second orphan | a | p | o |\n\ntail\n"
    found = _orphaned_rows(run)
    assert len(found) == 2, f"a two-row split-off tail reported {len(found)} orphan(s): {found}"
    named = {n for n in ("BACKLOG.md:5", "BACKLOG.md:6") if any(n in msg for msg in found)}
    assert named == {"BACKLOG.md:5", "BACKLOG.md:6"}, (
        f"both orphaned rows must be named; the report identifies {named or 'neither'}: {found!r}"
    )


def test_check_c_leaves_a_normally_terminated_table_alone() -> None:
    """The INVERSE, so (C) is not simply flagging whatever follows a table.

    The cell-count rule and the block-structure skip are the whole discriminator,
    so the cases that must stay green are the ones a pipe COUNT would have
    flagged: a heading, and prose carrying pipes.

    RED if `_orphaned_rows` drops its cell-count test or its block-structure
    skip.
    """
    five = _HDR + "| [#1](u) | t | a | p | fine |\n"
    for name, tail in (
        ("heading", "## next section\n"),
        ("prose with pipes", "A sentence mentioning `a | b` and `c | d`.\n"),
        ("bullet list", "- a bullet | with a pipe\n"),
        ("a row of a NARROWER width", "| [#2](u) | only | three |\n"),
    ):
        doc = five + "\n" + tail + "\ntail\n"
        assert _orphaned_rows(doc) == [], f"{name}: flagged {_orphaned_rows(doc)}"

    # A prose BULLET carrying one unescaped pipe, directly after the 2-column
    # table -- the shape `docs/BACKLOG.md:176` already has, one pipe short. The
    # block-structure skip is the only thing keeping it green.
    bullet = _ORPHAN_HDR_2 + "| [#1](u) | fine |\n\n- **[#161](u)** - CLOSED, `a | b`\n\ntail\n"
    assert _orphaned_rows(bullet) == [], f"a prose bullet was flagged: {_orphaned_rows(bullet)}"

    # A ONE-COLUMN table, where a blank line itself counts as one "cell": the
    # blank test in the report loop is what stops every blank line after such a
    # table being reported as an orphan.
    one_col = "| Issue |\n|---|\n| [#1](u) |\n\n\n## next\n"
    assert _orphaned_rows(one_col) == [], (
        f"a 1-column table flagged a blank: {_orphaned_rows(one_col)}"
    )
    assert len(_tables(one_col)) == 1, "the 1-column fixture must really parse as a table"

    # A SECOND TABLE of the same width, properly separated. Its header IS
    # row-shaped with a matching cell count, so only the `claimed` skip keeps it
    # out of the report -- dropping that skip survived until this case existed.
    twin = five + "\n" + _HDR + "| [#2](u) | t | a | p | fine |\n\ntail\n"
    assert _orphaned_rows(twin) == [], (
        f"a properly separated second table of the same width was reported as an "
        f"orphaned row: {_orphaned_rows(twin)}"
    )
    assert len(_tables(twin)) == 2, "the fixture must really contain two tables"
    # And on the real file, which is the measurement the threshold was chosen
    # from.
    assert _orphaned_rows(BACKLOG.read_text(encoding="utf-8")) == []


def test_check_d_reports_two_tables_merged_by_a_missing_blank_line() -> None:
    """CHECK (D) bites. Measured: GFM merges them, 2 `<table>` become 1.

    The second table's delimiter row renders as a DATA ROW of the first, and the
    parser swallows it the same way, which is why the gate stayed green.

    RED if `_merged_tables` is deleted, stops being called by `_all_offenders`,
    or matches the delimiter row with `re.match` instead of `re.fullmatch`.
    """
    merged = (
        _HDR  # 1-2
        + "| [#1](u) | t | a | p | fine |\n"  # 3
        + "| Issue | Why it matters |\n"  # 4 -- the second table's header
        + "|---|---|\n"  # 5 -- its delimiter row
        + "| [#2](u) | swallowed |\n"  # 6
        + "\ntail\n"
    )
    found = _merged_tables(merged)
    assert len(found) == 1, f"want one merged-table report, got {found}"
    assert "BACKLOG.md:5" in found[0], f"the delimiter row is line 5: {found[0]!r}"
    assert "headed at line 1" in found[0], f"the message must name the header line: {found[0]!r}"
    assert _all_offenders(merged), "the union gate missed the merge"

    # The same merge with a TRAILING SPACE on the delimiter row. Measured: GFM
    # merges it identically. Without the `.strip()` the whole check is escaped
    # by one invisible character.
    spaced = merged.replace("|---|---|\n", "|---|---| \n")
    assert _merged_tables(spaced), "a delimiter row with a trailing space escaped check (D)"

    # THREE tables run together, so `return offenders[:1]` is visible.
    triple = (
        _HDR  # 1-2
        + "| [#1](u) | t | a | p | fine |\n"  # 3
        + _ORPHAN_HDR_2  # 4-5, delimiter on 5
        + "| [#2](u) | swallowed |\n"  # 6
        + _ORPHAN_HDR_2  # 7-8, delimiter on 8
        + "| [#3](u) | swallowed too |\n"  # 9
        + "\ntail\n"
    )
    both = _merged_tables(triple)
    assert len(both) == 2, f"three merged tables produced {len(both)} report(s): {both}"
    named = {n for n in ("BACKLOG.md:5", "BACKLOG.md:8") if any(n in msg for msg in both)}
    assert named == {"BACKLOG.md:5", "BACKLOG.md:8"}, (
        f"both delimiter rows must be named; the report identifies {named or 'neither'}"
    )
    # ...and the SAME two tables with a blank line between them are clean.
    separated = (
        _HDR
        + "| [#1](u) | t | a | p | fine |\n"
        + "\n"
        + _ORPHAN_HDR_2
        + "| [#2](u) | fine |\n"
        + "\ntail\n"
    )
    assert _all_offenders(separated) == [], (
        f"two proper tables flagged: {_all_offenders(separated)}"
    )
    assert len(_tables(separated)) == 2, "the parser must see two separate tables"

    # ...and a row that merely CONTAINS a dash-only cell is not a delimiter row.
    # It matches `_SEPARATOR_ROW` as a SUBSTRING, so matching with `search`
    # instead of `fullmatch` reports every such row as a merged table.
    dash_cell = _HDR + "| - | untagged | a | p | o |\n\ntail\n"
    assert _merged_tables(dash_cell) == [], (
        f"a row with a dash-only CELL is not a delimiter row: {_merged_tables(dash_cell)}"
    )


def test_a_whitespace_only_terminator_is_a_legitimate_end_of_table() -> None:
    """A line of spaces is a BLANK line to CommonMark, so it ends a table.

    The inverse property that lets check (C) own the whitespace-split shape: (A)
    must NOT report it, or every table followed by a stray-space line goes red.

    RED if `terminator.strip()` in `_is_legitimate_terminator` loses its
    `.strip()`.
    """
    doc = _HDR + "| [#1](u) | t | a | p | fine |\n" + "   \n" + "## a heading\n"
    assert _truncated_tables(doc) == [], f"a whitespace-only terminator was reported: {doc!r}"
    assert _all_offenders(doc) == []


def test_an_INDENTED_row_shaped_line_is_reported_by_check_b() -> None:
    r"""The shape `_ROW_SHAPED`'s docstring explicitly claims to admit.

    An indented row still RENDERS inside the table (GFM strips up to three
    spaces) but the parser stops at it, so every later row goes unchecked.

    RED if `_ROW_SHAPED` loses its `^\s*`.
    """
    # 1-2 header + delimiter, 3 the good row, 4 blank, 5 the INDENTED row.
    doc = _HDR + "| [#1](u) | t | a | p | fine |\n\n  | [#2](u) | t | a | p | indented |\n\n"
    found = _unparsed_row_shaped_lines(doc)
    assert len(found) == 1, f"an indented row-shaped line was not reported: {found}"
    assert "BACKLOG.md:5" in found[0], f"wrong line named: {found[0]!r}"


def test_a_delimiter_row_with_trailing_junk_does_not_start_a_table() -> None:
    """`|---|---| oops` is not a GFM delimiter row, so there is no table.

    The rows below it are then unparsed, and check (B) reports them. If the
    delimiter test degrades to `re.match`, the junk is ignored, a table IS
    recognised, and the whole document reports clean.

    RED if `_SEPARATOR_ROW.fullmatch` in `_table_blocks` becomes `re.match`.
    """
    doc = (
        "| Issue | Title | Area | Priority | Origin |\n"
        "|---|---|---|---|---| oops\n"
        "| [#1](u) | t | a | p | fine |\n"
        "| [#2](u) | t | a | p | fine |\n"
        "\ntail\n"
    )
    assert _tables(doc) == [], f"a delimiter row with trailing junk started a table: {_tables(doc)}"
    assert len(_unparsed_row_shaped_lines(doc)) == 2, (
        f"both rows under the broken delimiter must be reported: {_unparsed_row_shaped_lines(doc)}"
    )
    assert _all_offenders(doc), "the union gate missed a table with a broken delimiter row"


def test_a_table_immediately_after_a_terminator_line_is_still_parsed() -> None:
    """The parser must resume looking for a header on the line AFTER the stop.

    RED if `i = j` in `_table_blocks` becomes `i = j + 2`, which skips the line
    directly after the terminator and so never sees the second table's header.
    (`i = j + 1` is equivalent -- the terminator cannot itself be a header,
    because the row loop only stops on a line that does not start with `|` --
    and is documented as such rather than pretended to be closed.)
    """
    doc = (
        _HDR  # 1-2
        + "| [#1](u) | t | a | p | fine |\n"  # 3
        + "## a heading, which legitimately ends the table\n"  # 4 -- terminator
        + _ORPHAN_HDR_2  # 5-6
        + "| [#2](u) | fine |\n"  # 7
        + "\ntail\n"
    )
    headers = [header_no for header_no, _c, _r in _tables(doc)]
    assert headers == [1, 5], f"both tables must be parsed; found headers {headers}"


def test_row_cell_count_reads_both_outer_pipes_as_optional() -> None:
    """`_row_cell_count` is the orphan check's whole discriminator.

    Pinned directly, with literals counted by hand, because the orphan check's
    only other assertion is "this equals the table's column count" -- which
    agrees with any bug in the counter.
    """
    cases = (
        ("| a | b | c | d | e |", 5),
        ("a | b | c | d | e |", 5),
        ("| a | b | c | d | e", 5),
        ("a | b | c | d | e", 5),
        ("\u200b| a | b |", 2),
        ("\ufeff| a | b |", 2),
        ("\u00ad| a | b |", 2),
        ("| a \\| still one cell | b |", 2),
        ("  | a | b |  ", 2),
        ("prose with no pipes at all", 1),
        # An EMPTY first cell. Exactly ONE outer pipe comes off each end, never
        # a run of them: `^\|+` would read this as two cells, not three.
        ("|| a | b |", 3),
        ("| a | b ||", 3),
    )
    for line, want in cases:
        assert _row_cell_count(line) == want, (
            f"{line!r} counted as {_row_cell_count(line)} cells, want {want}"
        )


def test_every_check_is_wired_into_the_union_gate() -> None:
    """No detector may be defined, and then quietly stop running.

    Each fixture is flagged by exactly ONE of the four checks, so dropping that
    check from `_all_offenders` reddens this test and nothing else does.
    """
    one_row = _HDR + "| [#1](u) | t | a | p | fine |\n"
    cases = (
        # (A)-ONLY, and it has to be: a de-piped TAGGED row is also seen by (B),
        # and one whose cell count matches its table by (C), so with either as
        # the fixture, dropping (A) from the union left every test green.
        ("(A) truncated", one_row + "- | untagged, lost its pipe | a |\n\ntail\n"),
        ("(C) orphaned", one_row + "\n| - | untagged | a | p | o |\n\ntail\n"),
        (
            "(D) merged",
            one_row + _ORPHAN_HDR_2 + "| [#2](u) | swallowed |\n\ntail\n",
        ),
        (
            "(pipe over-run)",
            _HDR + "| [#1](u) | t | a | p | prose with a | stray pipe |\n\n",
        ),
    )
    for name, doc in cases:
        assert _all_offenders(doc), f"{name}: the union gate reported nothing"

    # ...and the (A)-only fixture really is invisible to the other three, or the
    # assertion above would pass with (A) removed from the union.
    only_a = cases[0][1]
    assert _unparsed_row_shaped_lines(only_a) == [] and _orphaned_rows(only_a) == []
    assert _merged_tables(only_a) == [] and _offenders(only_a) == []
    assert _truncated_tables(only_a), "(A) must be the only check that sees this"

    # (B) needs its own fixture: every shape above that (B) can also see is
    # covered by another check, so this one is chosen to be invisible to the
    # other three -- a blank-split row whose cell count is NARROWER than its
    # table, so (C) declines it.
    only_b = _HDR + "| [#1](u) | t | a | p | fine |\n\n| [#2](u) | only | three |\n\ntail\n"
    assert _truncated_tables(only_b) == [] and _orphaned_rows(only_b) == []
    assert _merged_tables(only_b) == [] and _offenders(only_b) == []
    assert _unparsed_row_shaped_lines(only_b), "(B) must be the only check that sees this"
    assert _all_offenders(only_b), "(B) is not wired into the union gate"


def test_every_real_backlog_row_ends_on_an_unescaped_cell_wall() -> None:
    """The PREMISE check (B)'s rule rests on, asserted rather than assumed.

    (B) tells a de-piped row from a prose sentence by the trailing unescaped `|`.
    That is safe only while every real row has one -- measured at `5e383a1`, all
    94 do. If a row style arrives without one, this goes red HERE, naming the
    row, rather than (B) silently going blind to that row's whole shape.
    """
    text = BACKLOG.read_text(encoding="utf-8")
    rows = [(line_no, row) for _h, _c, rs in _tables(text) for line_no, row in rs]
    assert len(rows) >= 40, f"only {len(rows)} rows parsed -- the premise is untested"
    missing = [f"BACKLOG.md:{n}: {r[:60]!r}" for n, r in rows if not _ENDS_IN_A_CELL_WALL.search(r)]
    assert not missing, (
        "these rows do not end on an unescaped `|`, so check (B) cannot tell them "
        "from prose and is blind to their de-piped form:\n  " + "\n  ".join(missing)
    )
