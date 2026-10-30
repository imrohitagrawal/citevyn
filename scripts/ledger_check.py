#!/usr/bin/env python3
"""Check that docs/BACKLOG.md (the ledger) agrees with GitHub's issues.

GitHub is the source of truth; the ledger is its index. Three rules:

- R1: every open GitHub issue has an un-struck row in an open table.
- R2: every un-struck row in an open table names only open issues.
- R3: every struck row, and every row in a closed section, names only closed
  issues.

Pull requests are not issues: a row that names one breaks R2 or R3, and an open
pull request needs no row.

How the ledger is read (line-based, not a full Markdown parser):

- A row is a table line (starts with `|`). The issue numbers are every `#N` in
  its first cell; a row with none (a `—` note, a header, a separator) is skipped.
- A row is struck when its second cell (the title) starts with `~~`, the
  ledger's convention for a closed item. A strike later in the title is a note,
  not a strike of the row.
- A section is closed when its heading, or any heading above it, has the word
  "closed" or "resolved" in it ("### Closed — kept for the record", "## Recently
  closed", "## Earlier follow-ups (all resolved)"). Every other table is open.

GitHub is read with `gh api` (all states, every page). Exit 0 when the ledger
agrees, 1 with one line per violation, 2 when GitHub or the ledger cannot be
read. A failure to read GitHub is never a pass.

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REPO = "imrohitagrawal/citevyn"
DEFAULT_BACKLOG = Path(__file__).resolve().parent.parent / "docs" / "BACKLOG.md"

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
CLOSED_HEADING = re.compile(r"\b(closed|resolved)\b", re.IGNORECASE)
ISSUE_REF = re.compile(r"#(\d+)")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
GH_LINE = re.compile(r"^(\d+) (open|closed) (true|false)$")
JQ = '.[] | "\\(.number) \\(.state) \\(has("pull_request"))"'


class ReadError(Exception):
    """GitHub or the ledger could not be read. Never a pass."""


@dataclass(frozen=True)
class Row:
    line: int
    numbers: tuple[int, ...]
    struck: bool
    closed_section: bool


@dataclass(frozen=True)
class Item:
    state: str  # "open" or "closed"
    is_pr: bool


@dataclass
class Report:
    violations: list[str] = field(default_factory=list)
    open_rows: int = 0
    closed_rows: int = 0
    open_issues: int = 0


def parse_ledger(text: str) -> list[Row]:
    rows: list[Row] = []
    stack: list[tuple[int, bool]] = []  # (heading level, closed)
    for lineno, line in enumerate(text.splitlines(), start=1):
        heading = HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent_closed = bool(stack) and stack[-1][1]
            stack.append((level, parent_closed or bool(CLOSED_HEADING.search(heading.group(2)))))
            continue
        if not line.startswith("|"):
            continue
        cells = CELL_SPLIT.split(line.strip())[1:]
        if len(cells) < 2:
            continue
        numbers = tuple(int(n) for n in ISSUE_REF.findall(cells[0]))
        if not numbers:
            continue
        rows.append(
            Row(
                line=lineno,
                numbers=numbers,
                struck=cells[1].strip().startswith("~~"),
                closed_section=bool(stack) and stack[-1][1],
            )
        )
    return rows


def fetch_items(repo: str) -> dict[int, Item]:
    cmd = ["gh", "api", "--paginate", f"repos/{repo}/issues?state=all&per_page=100", "--jq", JQ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    except FileNotFoundError as exc:
        raise ReadError("could not run gh (is the GitHub CLI installed?)") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReadError("could not read issues from GitHub: gh timed out") from exc
    if proc.returncode != 0:
        detail = (proc.stderr.strip().splitlines() or ["no message"])[0]
        raise ReadError(f"could not read issues from GitHub (gh exit {proc.returncode}): {detail}")
    items: dict[int, Item] = {}
    for raw in proc.stdout.splitlines():
        if not raw.strip():
            continue
        match = GH_LINE.match(raw.strip())
        if not match:
            raise ReadError(f"unexpected line from gh: {raw.strip()[:80]!r}")
        items[int(match.group(1))] = Item(state=match.group(2), is_pr=match.group(3) == "true")
    if not items:
        raise ReadError("GitHub returned no issues for this repository")
    return items


def check(rows: list[Row], items: dict[int, Item]) -> Report:
    report = Report()
    covered: set[int] = set()  # open issues with an un-struck row in an open table
    for row in rows:
        if row.closed_section:
            report.closed_rows += 1
        else:
            report.open_rows += 1
        if row.closed_section or row.struck:
            rule, want = "R3", "closed"
            where = "in a closed section" if row.closed_section else "struck"
        else:
            rule, want = "R2", "open"
            where = "un-struck in an open table"
        for number in row.numbers:
            item = items.get(number)
            if item is None:
                reason = "not found on GitHub"
            elif item.is_pr:
                reason = "is a pull request, not an issue"
            elif item.state != want:
                reason = f"is {item.state} on GitHub"
            else:
                if want == "open":
                    covered.add(number)
                continue
            report.violations.append(f"{rule} #{number}: {where} (line {row.line}) but {reason}")
    open_issues = sorted(n for n, item in items.items() if item.state == "open" and not item.is_pr)
    report.open_issues = len(open_issues)
    for number in open_issues:
        if number not in covered:
            report.violations.append(
                f"R1 #{number}: open on GitHub but has no un-struck row in an open table"
            )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backlog", type=Path, default=DEFAULT_BACKLOG)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="OWNER/NAME")
    args = parser.parse_args(argv)
    try:
        try:
            text = args.backlog.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReadError(f"could not read the ledger {args.backlog}: {exc.strerror}") from exc
        rows = parse_ledger(text)
        items = fetch_items(args.repo)
    except ReadError as exc:
        print(f"ledger_check: ERROR: {exc}", file=sys.stderr)
        return 2
    report = check(rows, items)
    for violation in report.violations:
        print(violation)
    print(
        f"ledger_check: {len(report.violations)} violations "
        f"(rows: {report.open_rows} in open tables, {report.closed_rows} in closed sections; "
        f"open issues on GitHub: {report.open_issues})"
    )
    return 1 if report.violations else 0


if __name__ == "__main__":
    sys.exit(main())
