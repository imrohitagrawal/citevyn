"""#410: a BACKLOG row must not claim its fix is unmerged once the fix is on main.

THE DEFECT. Rows in ``docs/BACKLOG.md`` are written while a PR is open ("FIX
OPEN AS PR #N -- not merged") and become FALSE the moment that PR merges.
Nothing detected the transition, so they accumulated: ten were stale in a single
session (2026-09-10, swept in PR #409), and three of those ten went stale
*minutes* earlier when PR #408 merged. #375 and #381 needed the same correction
an hour before that. The rot is produced by the normal merge workflow, so prose
asking people to remember cannot fix it.

WHY THIS GUARD NEEDS ``fetch-depth: 0``, and why that is the whole difficulty.
The only local source for "is this fix merged?" is git history. Under the
default depth-1 checkout ``git log --grep`` sees exactly ONE commit, finds
nothing for any issue, and concludes every row is fine -- a guard that reports
success on a repo full of stale rows. Proven with a real shallow clone before
this file was written::

    $ git clone --depth 1 file://$PWD /tmp/demo
    $ git -C /tmp/demo log --grep='#384' --oneline
    (nothing)          # #384 was fixed in cdd8d06, long an ancestor of main

That is the #381 defect exactly: a check that counts nothing and passes. So
``test_the_history_this_guard_needs_is_present`` below is not decoration -- it
is the partner that turns a silent vacuous pass into a loud failure, and it is
the reason ``.github/workflows/ci.yml`` carries ``fetch-depth: 0`` on the
``pytest + lint`` job.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKLOG = REPO_ROOT / "docs" / "BACKLOG.md"

# Phrases that claim a fix has NOT landed. Matched ONLY inside a **bold** span,
# because that is the file's actual convention for a status marker and because
# a bare substring search over the prose is measurably wrong: it flagged #331
# on "drawer still open" (a UI drawer) and #84 on "item 4, which is still open
# here" (a sub-item), neither of which is a merge claim. Both were false
# positives from the first version of this guard, found by running it.
#
# Six shapes appear in the real file. The 2026-09-10 sweep knew only four, and
# a matcher built for one of them caught 7 of 10 stale rows -- which is why
# these are collected from the file rather than imagined:
#   **FIX OPEN AS A PR - not merged.**
#   **FIX OPEN AS PR [#398](...) - not merged.**
#   **fix open as PR #350, not yet merged**
#   **fix open as a PR, not merged; issue #400 still OPEN**
#   **FIX READY, PR OPEN - not merged.**
#   **... FIXED in PR [#368](...), NOT merged**
OPEN_CLAIM_PATTERNS = (
    "fix open as",
    "not yet merged",
    "not merged",
)

# A **bold** span. Non-greedy so two markers on one row stay separate.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# A `code span`. Stripped BEFORE looking for bold markers, because a row that
# QUOTES a marker shape is documenting it, not claiming it. CI caught this on
# the very first run of this guard: #410's own row quotes the two shapes it
# discovered -- ``(`**FIX READY, PR OPEN — not merged.**`, ...)`` -- and the
# guard reported its own row stale. A row must be able to talk about markers.
_CODE_SPAN_RE = re.compile(r"`[^`]*`")

# A row links its own issue as the FIRST issues/<n> URL in the line.
_ROW_ISSUE_RE = re.compile(r"issues/(\d+)")


def _row_lines(text: str) -> list[str]:
    """Every table row in the file, in order."""
    return [ln for ln in text.split("\n") if ln.startswith("| [#")]


def _claims_open(line: str) -> bool:
    """True if a BOLD span OUTSIDE any code span claims the fix has not landed."""
    outside_code = _CODE_SPAN_RE.sub(" ", line)
    return any(
        p in span.lower() for span in _BOLD_RE.findall(outside_code) for p in OPEN_CLAIM_PATTERNS
    )


def _row_issue(line: str) -> int | None:
    m = _ROW_ISSUE_RE.search(line)
    return int(m.group(1)) if m else None


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def _fix_commits_for(issue: int) -> list[str]:
    """Commits reachable from HEAD whose SUBJECT names ``#issue``.

    SUBJECT ONLY, deliberately. Searching the whole message matches any commit
    that merely MENTIONS an issue, and this repo mentions issues in bodies
    constantly -- PR #409's own body lists ten issue numbers, which would have
    marked all ten "fixed" no matter what it actually changed. Measured: a
    full-message search reported #231 fixed by `chore(docker): bump frontend
    base image node 22 -> 26 (#227) (#269)`, whose subject does not name #231
    at all. Right answer, wrong reason -- and the same latitude produces wrong
    answers elsewhere.

    A squash-merge subject carries both the issues it closes (`fix(#366, #356)`)
    and its own PR number (`(#376)`). Both are legitimate "this number is done"
    signals: GitHub numbers issues and PRs in ONE sequence, so a number that is
    a PR is never also an issue, and no BACKLOG row can be about it.

    The boundary is a non-digit or end-of-string, so ``#40`` does not match the
    commit that fixed ``#400``.
    """
    subjects = _git("log", "--format=%h %s", "HEAD").split("\n")
    pat = re.compile(rf"#{issue}(?!\d)")
    return [ln for ln in subjects if ln and pat.search(ln.split(" ", 1)[-1])]


def _is_shallow() -> bool:
    return _git("rev-parse", "--is-shallow-repository") == "true"


# ---------------------------------------------------------------------------
# The partner. Without this, everything below can pass by seeing nothing.
# ---------------------------------------------------------------------------


def test_the_history_this_guard_needs_is_present() -> None:
    """RED if ``fetch-depth: 0`` is dropped from ci.yml's ``pytest + lint`` job.

    Turns the vacuous pass into a loud failure. Asserts the PROPERTY (the repo
    is not shallow) rather than a commit count, so it cannot rot the way a
    ``>= N`` constant does -- that is exactly how the BACKLOG guard rotted while
    46 of 93 rows silently left coverage.
    """
    assert not _is_shallow(), (
        "This repository is a SHALLOW clone, so `git log --grep` cannot see "
        "whether a fix is merged and every staleness check below would pass by "
        "finding nothing. Add `with: {fetch-depth: 0}` to the actions/checkout "
        "step of the `pytest + lint` job in .github/workflows/ci.yml (#410)."
    )


def test_ci_still_declares_the_full_history_this_guard_needs() -> None:
    """Static partner: ``pytest + lint`` must keep ``fetch-depth: 0``.

    ``test_the_history_this_guard_needs_is_present`` above checks the RUNTIME
    property and is the stronger guarantee, but it can only speak for whichever
    job happens to be running. ``quality-gate`` is a reusable workflow from
    another repository, SHA-pinned, whose checkout this repo does not control --
    measured: it currently supplies full history, but nothing here can hold it
    to that. This test is hermetic, needs no git, and pins the one checkout this
    repo DOES own, so deleting that line fails immediately and by name.
    """
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    workflow = yaml.safe_load(ci)
    steps = workflow["jobs"]["test"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout.get("with", {}).get("fetch-depth") == 0, (
        "The `pytest + lint` job's actions/checkout no longer sets `fetch-depth: 0`. "
        "Without full history `git log` cannot see whether a fix is merged and the "
        "staleness guard would pass by finding nothing (#410)."
    )


def test_the_finder_finds_a_fix_that_is_known_to_be_merged() -> None:
    """Positive partner: proves ``_fix_commits_for`` can return anything at all.

    The issue number is DERIVED from history rather than hardcoded: take the
    newest ``fix(#N)`` subject on the branch and ask the finder for that N. So
    it tracks reality and cannot rot into a stale constant, and it does not
    depend on the message of the commit that adds this file -- an earlier draft
    asserted on its own issue number and could not pass until it was committed.
    """
    subjects = _git("log", "--format=%s", "HEAD").split("\n")
    newest = next(
        (m.group(1) for ln in subjects if (m := re.search(r"^fix\(#(\d+)", ln))),
        None,
    )
    assert newest is not None, "no fix(#N) commit in history; cannot prove the finder works"
    assert _fix_commits_for(int(newest)), (
        f"_fix_commits_for(#{newest}) found nothing, but a fix(#{newest}) subject is "
        "in this branch's history. The finder is broken, or history is unreachable -- "
        "either way every staleness assertion below is vacuous."
    )


def test_the_issue_number_boundary_is_not_a_prefix_match() -> None:
    """``#40`` must not match the commit that fixed ``#400``.

    Without the boundary in the ``--grep`` regex, a low-numbered issue inherits
    every higher issue that starts with its digits, and its row can never be
    reported stale.
    """
    four_hundred = _fix_commits_for(400)
    assert four_hundred, "precondition: #400's fix should be on main"
    assert _fix_commits_for(40) != four_hundred, (
        "#40 matched exactly what #400 matched -- the --grep boundary is gone, "
        "so prefix collisions silently suppress staleness for #1..#99."
    )


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_no_backlog_row_claims_open_for_an_issue_whose_fix_is_on_main() -> None:
    """RED if a row says "not merged" for an issue already fixed on main.

    Revert PR #409's BACKLOG edit and this goes red on ten rows.
    """
    stale: list[tuple[int, str]] = []
    for line in _row_lines(BACKLOG.read_text()):
        if not _claims_open(line):
            continue
        issue = _row_issue(line)
        if issue is None:
            continue
        commits = _fix_commits_for(issue)
        if commits:
            stale.append((issue, commits[0]))

    assert not stale, "BACKLOG rows claim an unmerged fix that is already on main:\n" + "\n".join(
        f"  #{n} -- claims not merged, but {c}" for n, c in stale
    )


# ---------------------------------------------------------------------------
# Non-vacuity: the detector must actually detect, and must not fire on
# everything. A guard that is always green and one that is always red are the
# same defect.
# ---------------------------------------------------------------------------

# A multi-row fixture whose FIRST row is COMPLIANT. If the scan ever degrades
# from `continue` to `break`, it stops at row one and reports nothing -- the
# survivor that has silently disabled a guard in this repo before.
# Rows are built through a helper so the fixtures stay under the line limit and
# so the SHAPE under test (bold marker vs prose) is the only thing that varies.
# `_row_issue` only needs an `issues/<n>` substring, so the host is a stub.
_ISSUES = "https://github.com/imrohitagrawal/citevyn/issues"
_PULLS = "https://github.com/imrohitagrawal/citevyn/pull"


def _row(issue: int, title: str, priority: str = "Low", origin: str = "done") -> str:
    return f"| [#{issue}]({_ISSUES}/{issue}) | {title} | area | {priority} | {origin} |"


_OPEN_MARKER = "**FIX OPEN AS A PR — not merged.**"

# The FIRST row is COMPLIANT on purpose: if the scan ever degrades from
# `continue` to `break` it stops here and reports nothing -- the survivor that
# has silently disabled a guard in this repo before.
_FIXTURE_ROWS = [
    # 1. compliant: closed issue, no marker
    _row(400, "fixed"),
    # 2. compliant: marker, but no fix commit exists for this number
    _row(999997, "a", origin=_OPEN_MARKER),
    # 3. compliant: "still open" in unbolded PROSE, about a UI drawer. This row
    #    is why _claims_open reads bold spans only -- an earlier draft flagged
    #    the real #331 and #84 rows on exactly this shape.
    _row(400, "the drawer is still open"),
    # 4. VIOLATION: bold marker, and #400's fix is on main
    _row(400, "b", origin=_OPEN_MARKER),
    # 5. VIOLATION in the lower-cased priority-cell shape
    _row(401, "c", priority="**Low — fix open as PR #408, not yet merged**"),
]


def _scan(rows: list[str]) -> list[int]:
    found = []
    for line in rows:
        if not _claims_open(line):
            continue
        issue = _row_issue(line)
        if issue is not None and _fix_commits_for(issue):
            found.append(issue)
    return found


def test_the_detector_actually_detects() -> None:
    """Both planted violations are caught, and no compliant row is."""
    assert _scan(_FIXTURE_ROWS) == [400, 401]


def test_the_detector_ignores_a_genuinely_open_row() -> None:
    """A row claiming open for an issue with no fix commit must NOT be flagged.

    Without this, a guard that flagged every marker unconditionally would still
    satisfy the detector test above while making the file unmaintainable.
    """
    assert _scan([_FIXTURE_ROWS[1]]) == []


def test_a_marker_QUOTED_in_a_code_span_is_not_a_claim() -> None:
    """RED if _claims_open stops stripping `code spans` first.

    Found by CI, not locally, on this guard's first run: #410's own row quotes
    the two marker shapes it discovered, and the guard reported that row stale.
    A row documenting a marker is not a row claiming one.

    It escaped local testing because the row was verified BEFORE the commit
    naming #410 existed -- with no fix commit to find, the check could not fire
    whatever the row said. Hence this fixture, which does not depend on history.
    """
    quoted = _row(
        999998,
        "catalogued two new shapes (`**FIX READY, PR OPEN — not merged.**`)",
    )
    assert not _claims_open(quoted)


def test_a_real_marker_beside_a_quoted_one_is_still_caught() -> None:
    """Partner: stripping code spans must not blind the check to a real marker.

    Without this, `_claims_open` could return False for the whole row the moment
    any code span appeared, and every row containing a backtick would go unchecked.
    """
    both = _row(
        999998,
        "quoted (`**FIX READY, PR OPEN — not merged.**`)",
        origin=_OPEN_MARKER,
    )
    assert _claims_open(both)


def test_unbolded_prose_is_not_a_merge_claim() -> None:
    """RED if _claims_open goes back to scanning the whole line.

    Measured: the first version of this guard reported #331 stale on "drawer
    still open" and #84 on "item 4, which is still open here". Both are prose.
    """
    assert _scan([_FIXTURE_ROWS[2]]) == []


@pytest.mark.parametrize(
    "marker",
    [
        "**FIX OPEN AS A PR — not merged.**",
        f"**FIX OPEN AS PR [#398]({_PULLS}/398) — not merged.**",
        "**fix open as PR #350, not yet merged**",
        "**fix open as a PR, not merged; issue #400 still OPEN**",
        "**FIX READY, PR OPEN — not merged.**",
        f"**FIXED in PR [#368]({_PULLS}/368), NOT merged**",
    ],
)
def test_every_marker_shape_seen_in_the_real_file_is_recognised(marker: str) -> None:
    """All six shapes the file actually uses.

    The 2026-09-10 sweep knew only four; a matcher built for one caught 7 of 10.
    Shapes five and six were found by this guard on its first run.
    """
    assert _claims_open(f"| [#400](https://github.com/x/y/issues/400) | t | a | Low | {marker} |")
