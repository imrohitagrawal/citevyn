#!/usr/bin/env python3
"""PreToolUse guard: make "discard my uncommitted work" reversible, or refuse it.

WHY THIS EXISTS, and why it is a hook rather than a line in a document.

`git checkout -- <path>`, `git restore <path>`, `git reset --hard` and
`git stash` all *look* like a restore and are in fact a DISCARD: they throw away
whatever is in the working tree that is not committed. On a clean tree they are
harmless, which is exactly what makes them dangerous — the same command is safe
a hundred times and then destroys an hour of work on the hundred-and-first.

It happened twice in one session in this repository. A builder used
`git checkout --` to undo a mutation and destroyed its own uncommitted review
fixes. The lesson was written down. Twenty minutes later the orchestrator ran
the same command to undo an experiment and destroyed two fixes it had just
written — because it was not "restoring a mutation", it was "undoing an
experiment". Same command, different label, so the written rule never fired.

That is the whole argument for a hook. A rule indexed by INTENT cannot fire,
because intent is what you get wrong in the moment. This fires on the COMMAND.
CLAUDE.md already says it: enforcement is mechanical, never prose.

WHAT IT DOES

1. If the command cannot discard anything, or the tree is clean, it stays out of
   the way and says nothing.
2. Otherwise it first takes a SNAPSHOT with `git stash create`, which builds a
   commit object from the dirty tree WITHOUT touching the working tree, the
   index, or the stash list. It anchors that object under `refs/discard-guard/`
   so it survives garbage collection.
3. Then it DENIES the command, and the denial names the snapshot ref and the one
   command that brings the work back.

So the answer to "how do I avoid needing to remember" is: the dangerous state is
removed automatically. By the time you are told no, the work is already saved.

ESCAPE HATCH, deliberate. Prefixing the command with `DISCARD_OK=1` allows it.
The snapshot is still taken. This is for the case where discarding is genuinely
what you want, and it keeps the guard from deadlocking a real workflow — the
point is to make the discard *considered*, not impossible.

FAIL-OPEN, also deliberate, and the trade is stated rather than hidden: if this
script raises, the command is allowed and a warning is printed. A guard that
fails closed on a parse bug would block every Bash call in the session. The
snapshot step is what carries the safety, and it runs before any decision.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

# What each command can actually destroy. Getting this wrong in either
# direction makes the guard useless: too broad and it cries wolf on every branch
# switch until someone disables it; too narrow and it misses the incident.
#
# TRACKED   — modifications to files git already knows about.
# UNTRACKED — files git has never seen. `git stash create` CANNOT snapshot
#             these (measured: it outputs nothing when the tree holds only
#             untracked files, with or without -u), so they are copied aside.
TRACKED, UNTRACKED, BOTH = "tracked", "untracked", "both"


def _run(args: list[str], cwd: str) -> tuple[int, str]:
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=15, check=False
    )
    return proc.returncode, (proc.stdout or "").strip()


def _is_discarding(tokens: list[str], cwd: str) -> tuple[str, str] | None:
    """Return (reason, what_it_can_destroy) if these tokens can discard work."""
    if not tokens or tokens[0] != "git":
        return None
    rest = [t for t in tokens[1:] if not t.startswith("-C")]
    if not rest:
        return None
    sub = rest[0]
    args = rest[1:]

    untracked_too = any(
        a in {"-u", "--include-untracked", "-a", "--all"} for a in args
    )

    if sub == "reset" and any(a == "--hard" for a in args):
        # --hard rewrites tracked files; it leaves untracked files alone.
        return ("`git reset --hard` throws away every tracked change in the tree", TRACKED)
    if sub == "clean" and any(a.startswith("-") and "f" in a for a in args):
        # The one case git itself cannot undo: untracked content is in no object.
        return (
            "`git clean -f` deletes UNTRACKED files, which exist in no git object "
            "and cannot be recovered by git at all",
            UNTRACKED,
        )
    if sub == "stash":
        # `list`, `show` and `create` are read-only; the rest move work away.
        if args and args[0] in {"list", "show", "create"}:
            return None
        return (
            "`git stash` moves uncommitted work out of the tree",
            BOTH if untracked_too else TRACKED,
        )
    if sub == "restore":
        # `--staged` alone only unstages; worktree content is untouched.
        flags = [a for a in args if a.startswith("-")]
        if flags and all(a in {"--staged", "-S"} for a in flags):
            if "--worktree" not in args and "-W" not in args:
                return None
        return ("`git restore` overwrites the working copy", TRACKED)
    if sub == "checkout":
        # The file-restore forms only. A branch switch is not a discard and must
        # never be caught here — it is one of the commonest commands in this repo.
        if "--" in args:
            return (
                "`git checkout -- <path>` discards uncommitted changes to those paths",
                TRACKED,
            )
        for a in args:
            if a.startswith("-"):
                continue
            if a == "." or (Path(cwd) / a).exists():
                return (
                    f"`git checkout {a}` discards uncommitted changes under that path",
                    TRACKED,
                )
            break
    return None


def _at_risk(cwd: str, kind: str) -> tuple[list[str], list[str]]:
    """(tracked, untracked) paths this command could actually destroy."""
    code, out = _run(["git", "status", "--porcelain"], cwd)
    if code != 0:
        return ([], [])
    tracked, untracked = [], []
    for line in out.splitlines():
        (untracked if line.startswith("??") else tracked).append(line[3:].strip())
    if kind == TRACKED:
        return (tracked, [])
    if kind == UNTRACKED:
        return ([], untracked)
    return (tracked, untracked)


def _snapshot(cwd: str, tracked: list[str], untracked: list[str]) -> list[str]:
    """Save what is at risk, without touching the working tree. Returns recovery lines."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    recovery: list[str] = []

    if tracked:
        # `git stash create` builds a commit object from the dirty tree and
        # changes nothing the caller can see — not the worktree, not the index,
        # not the stash list.
        code, sha = _run(["git", "stash", "create"], cwd)
        if code == 0 and sha:
            ref = f"refs/discard-guard/{stamp}"
            _run(["git", "update-ref", ref, sha], cwd)
            recovery.append(f"git checkout {ref} -- .    # tracked changes, back in place")

    if untracked:
        # Measured: `git stash create` emits nothing for untracked files, with
        # or without -u. They are in no object, so they are copied by hand.
        dest = Path(cwd) / ".git" / "discard-guard" / stamp
        saved = 0
        for rel in untracked:
            src = Path(cwd) / rel
            if not src.is_file():
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
            saved += 1
        if saved:
            recovery.append(f"cp -R {dest}/. .    # {saved} untracked file(s), back in place")

    return recovery


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        return 0
    if "DISCARD_OK=1" in command:
        return 0

    cwd = payload.get("cwd") or os.getcwd()
    try:
        tokens_sets = [shlex.split(seg) for seg in _split_command(command)]
    except ValueError:
        return 0

    hit = None
    for tokens in tokens_sets:
        hit = _is_discarding(tokens, cwd)
        if hit:
            break
    if not hit:
        return 0
    reason, kind = hit

    tracked, untracked = _at_risk(cwd, kind)
    if not tracked and not untracked:
        # Nothing this command can destroy is present. Stay out of the way —
        # a guard that fires on a safe command gets switched off.
        return 0

    lines = _snapshot(cwd, tracked, untracked)
    changed = len(tracked) + len(untracked)
    recovery = "\n  ".join(lines) if lines else "NO SNAPSHOT COULD BE TAKEN — stop and check by hand"
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"This discards uncommitted work. {reason}, and {changed} "
                        f"path(s) it can reach are uncommitted right now.\n\n"
                        f"Your work is ALREADY SAVED, before this refusal:\n  {recovery}\n\n"
                        "Pick one:\n"
                        "  1. Commit first, then re-run — the safest, and it makes the "
                        "discard a no-op.\n"
                        "  2. Restoring after an experiment? Use scripts/mutate.sh, which "
                        "snapshots the file, runs your command, restores it and proves "
                        "the restore with cmp. It cannot reach work in other files.\n"
                        "  3. Genuinely mean it? Re-run with DISCARD_OK=1 in front.\n\n"
                        "This fires on the COMMAND, not on what you believe you are doing: "
                        "two incidents in one session were both 'just undoing an "
                        "experiment'."
                    ),
                }
            }
        )
    )
    return 0


def _split_command(command: str) -> list[str]:
    """Split on shell separators so `cd x && git checkout -- y` is still seen."""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(command):
        two = command[i : i + 2]
        if two in {"&&", "||", ";;"}:
            out.append("".join(buf))
            buf = []
            i += 2
            continue
        if command[i] in {";", "|", "\n"}:
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(command[i])
        i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail-open, loudly
        print(f"discard_guard: {exc} (allowing command)", file=sys.stderr)
        sys.exit(0)
