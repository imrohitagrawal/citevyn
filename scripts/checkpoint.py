#!/usr/bin/env python3
"""PostToolUse hook: checkpoint the tree after every file edit, automatically.

THE PROBLEM THIS SOLVES, which the discard guard alone does not.

`scripts/discard_guard.py` saves your work at the moment you try to throw it
away. That prevents total loss, but it saves the tree AS IT IS THEN — twenty
minutes of good work with the line you deliberately broke welded to it. You get
everything back, mixed together, and separate it by hand.

The clean answer is to hold a copy from BEFORE the break. The usual advice is
"commit first, then experiment", and it fails for a reason worth stating: at
minute twenty you do not know you are about to break something. The break feels
like a trivial experiment, not a moment worth protecting. Any rule that depends
on recognising that moment will be skipped exactly when it matters — which is
how the incident that prompted all of this actually happened.

So this removes the decision instead of trying to win it. Every edit is
checkpointed. When you break a line, the break is itself an edit, and the
checkpoint taken immediately before it holds your work WITHOUT the break. You
never decide anything, and the clean restore point exists by construction.

COST, measured before this was put on the hot path: `git stash create` is ~7ms
on this repository and writes a commit object without touching the worktree, the
index or the stash list. Identical content produces the same object, so an edit
that changes nothing adds nothing.

WHAT IT DOES NOT DO. Untracked files are not captured: `git stash create` emits
nothing for them, measured, with or without -u. The command that destroys those
is `git clean`, and the discard guard copies them aside at that moment. This is
stated rather than papered over — a safety net you believe covers more than it
does is worse than one whose edges you know.

RECOVERY
    scripts/checkpoints.sh list            # newest first, with what changed
    scripts/checkpoints.sh restore <ref>   # into the worktree
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

# Keep the recent past, not all of it. Forty covers a long working session; the
# refs are objects git already stores, so the cost is a ref file each.
_KEEP = 40
_PREFIX = "refs/checkpoint"


def _git(args: list[str], cwd: str) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False
        )
    except Exception:
        return (1, "")
    return (p.returncode, (p.stdout or "").strip())


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    cwd = payload.get("cwd") or os.getcwd()
    if _git(["rev-parse", "--git-dir"], cwd)[0] != 0:
        return 0

    # Nothing uncommitted means nothing to protect.
    if not _git(["status", "--porcelain", "--untracked-files=no"], cwd)[1]:
        return 0

    code, sha = _git(["stash", "create"], cwd)
    if code != 0 or not sha:
        return 0

    # Skip if the newest checkpoint already holds this exact CONTENT, so a burst
    # of edits that cancel out does not fill the list with duplicates.
    #
    # Compare TREES, not commit ids. `git stash create` stamps each snapshot
    # with the current time, so identical content produces a DIFFERENT commit a
    # second later. Comparing commits therefore deduped only when both calls
    # landed inside the same second — it passed on my machine and failed on the
    # macOS runner, and it is the explanation for a local flake I had recorded
    # as unexplained. Measured: same content one second apart gives different
    # commit ids and the same tree id.
    newest_commit = _git(
        ["for-each-ref", "--sort=-refname", "--count=1", "--format=%(objectname)", _PREFIX],
        cwd,
    )[1]
    if newest_commit:
        new_tree = _git(["rev-parse", f"{sha}^{{tree}}"], cwd)[1]
        old_tree = _git(["rev-parse", f"{newest_commit}^{{tree}}"], cwd)[1]
        if new_tree and new_tree == old_tree:
            return 0

    # The ref name has to do two jobs, and BOTH were got wrong on the first
    # attempt, each found by replaying the real scenario rather than by
    # reasoning:
    #
    #   1. Be UNIQUE. With a seconds-resolution stamp alone, two edits in the
    #      same second produced the same name and the second overwrote the
    #      first — destroying precisely the checkpoint you need. The object id
    #      fixes that.
    #   2. SORT chronologically. Even with both refs present, `git for-each-ref
    #      --sort=creatordate` cannot order them: its resolution is one second,
    #      so "the checkpoint before the break" was a coin toss. Milliseconds in
    #      the name make a plain lexical sort the true order.
    #
    # Ordering is not a nicety here. The entire value of this hook is being able
    # to say which checkpoint came BEFORE the change you regret.
    now = time.time()
    stamp = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{int(now * 1000) % 1000:03d}"
    tool = (payload.get("tool_name") or "edit").lower()
    ref = f"{_PREFIX}/{stamp}-{tool}-{sha[:8]}"
    _git(["update-ref", ref, sha], cwd)

    # Sort by refname, not creatordate: the names are millisecond-stamped and
    # therefore lexically chronological, while creatordate ties at one second.
    refs = _git(
        ["for-each-ref", "--sort=-refname", "--format=%(refname)", _PREFIX], cwd
    )[1].splitlines()
    for stale in refs[_KEEP:]:
        _git(["update-ref", "-d", stale], cwd)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Never break an edit because the safety net failed. Say so and move on.
        print(f"checkpoint: {exc}", file=sys.stderr)
        sys.exit(0)
