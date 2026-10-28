#!/usr/bin/env python3
"""SubagentStart / SubagentStop hook: a sub-agent must leave the main checkout alone.

WHY THIS EXISTS. Reviewers here are told to read, not write, and twice one did
write: a "read-only" review agent ran `git checkout` in the shared checkout and
silently reverted a branch, and eight mutation reviewers pointed at one tree
overwrote each other (AGENTS.md, "One writer per worktree"). The rule was
written down both times. A written rule does not fire; a hook does.

WHAT IT DOES

1. SubagentStart: records the main checkout's HEAD, branch,
   `git status --porcelain` and a hash of every uncommitted byte (tracked
   changes and untracked files; ignored files such as `.env` are NOT covered)
   under `<git-common-dir>/subagent-tree-guard/`, keyed by the agent's id.
   That folder is inside `.git`, so recording it does
   not change the status it records.
2. SubagentStop: compares with the start record.
   Unchanged: says nothing.
   Changed: blocks the stop ONCE and tells the sub-agent to end its report with
   a `MAIN CHECKOUT CHANGED:` line. A SubagentStop hook cannot speak to the
   main model directly; the sub-agent's report is the one channel that reaches
   it. Every later stop only shows the user the warning, once, so an agent is
   never trapped. If another hook is already holding the agent back
   (`stop_hook_active`), it does not block at all, only warns.
   No record (the start hook did not run): says so to the user, never silence.

A stop can fire more than once for one agent. Seen 2026-10-29 in a probe: with
the plugins installed here, an agent's first SubagentStop was held back by another
hook, and a second one followed with `stop_hook_active` true. So the
record is KEPT after a stop (deleting it made the second stop report "not
checked" for every clean agent), and records older than two days are pruned
when the next agent starts.

"Changed" cannot say WHO changed it: the sub-agent, or another session working
in the same checkout. Either way the orchestrator must stop and look.

The main checkout is the first entry of `git worktree list`, found from
`$CLAUDE_PROJECT_DIR` (or the hook's cwd), so a session started inside a
linked worktree still guards the main checkout, and work inside a linked
worktree is never flagged.

FAIL-OPEN, as `discard_guard.py` is: if this script raises, the agent is
allowed to stop and the user is shown a warning. A guard that crashed closed
would trap every sub-agent in the session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_STATE_DIR = "subagent-tree-guard"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout


def main_checkout(start: Path) -> Path:
    """The first `worktree` entry of `git worktree list --porcelain`."""
    for line in _git(start, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree ") :])
    raise RuntimeError(f"no worktree listed from {start}")


def snapshot(main: Path) -> dict[str, str]:
    head = _git(main, "rev-parse", "HEAD").strip()
    branch = subprocess.run(
        ["git", "-C", str(main), "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,  # exits 1 on a detached HEAD, which is an answer
    ).stdout.strip()
    status = _git(main, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "head": head,
        "branch": branch or "(detached)",
        "status": status,
        "content": _content_hash(main),
        # Branch tips and the stash: `git branch -D` or `git stash drop` changes
        # neither HEAD nor status, and each can lose work.
        "refs": _git(
            main,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads",
            "refs/stash",
        ),
        # Branches checked out in linked worktrees move with every commit made
        # there, which is where agents are meant to work.
        "worktree_branches": "\n".join(
            line[len("branch ") :]
            for line in _git(main, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("branch ")
        ),
    }


def _lost_refs(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Refs that existed at the start and are gone or point elsewhere now: a
    deleted branch, a dropped or added stash. A new branch loses nothing, and a
    branch checked out in a linked worktree (then or now) is that worktree's."""

    def parse(text: str) -> dict[str, str]:
        return dict(line.split(" ", 1) for line in text.splitlines() if " " in line)

    was, now = parse(before.get("refs", "")), parse(after.get("refs", ""))
    busy = set(before.get("worktree_branches", "").split()) | set(
        after.get("worktree_branches", "").split()
    )
    lost = [r for r, sha in was.items() if now.get(r) != sha and r not in busy]
    if "refs/stash" in now and was.get("refs/stash") != now["refs/stash"]:
        lost.append("refs/stash")
    return sorted(set(lost))


def _content_hash(main: Path) -> str:
    """A hash of every uncommitted byte git can see: tracked changes against HEAD,
    and each untracked (not ignored) file. The status line alone misses an agent
    that overwrites a file which was already modified or untracked when it
    started; that is the owner's uncommitted work, the worst thing to lose."""
    digest = hashlib.sha256()
    digest.update(
        subprocess.run(
            ["git", "-C", str(main), "diff", "HEAD", "--binary"],
            capture_output=True,
            check=True,
        ).stdout
    )
    untracked = _git(main, "ls-files", "--others", "--exclude-standard", "-z")
    for rel in sorted(filter(None, untracked.split("\0"))):
        path = main / rel
        digest.update(rel.encode() + b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _record_path(main: Path, agent_id: str) -> Path:
    common = Path(_git(main, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = main / common
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_id) or "unknown"
    return common / _STATE_DIR / f"{safe}.json"


def describe(before: dict[str, str], after: dict[str, str]) -> str:
    parts = []
    if before["head"] != after["head"]:
        parts.append(f"HEAD {before['head'][:10]} -> {after['head'][:10]}")
    if before["branch"] != after["branch"]:
        parts.append(f"branch {before['branch']} -> {after['branch']}")
    if before["status"] != after["status"]:
        was = hashlib.sha256(before["status"].encode()).hexdigest()[:8]
        now_lines = after["status"].splitlines()
        shown = "; ".join(now_lines[:5]) or "clean"
        more = f" (+{len(now_lines) - 5} more)" if len(now_lines) > 5 else ""
        parts.append(f"git status changed (was {was}), now: {shown}{more}")
    lost = _lost_refs(before, after)
    if lost:
        parts.append("deleted or moved: " + ", ".join(lost[:5]))
    if before["status"] == after["status"] and before.get("content") != after.get(
        "content"
    ):
        parts.append(
            "the contents of a file that was already modified or untracked changed"
        )
    return ", ".join(parts)


def handle(payload: dict[str, object], project_dir: Path) -> dict[str, object] | None:
    event = payload.get("hook_event_name")
    agent_id = str(payload.get("agent_id") or "")
    if not agent_id:
        # A shared fallback name would let one agent's start overwrite another's
        # record, and the first agent's change would then pass unseen.
        if event == "SubagentStop":
            return {
                "systemMessage": "subagent tree guard: no agent_id, so the main checkout was NOT checked."
            }
        return None
    main = main_checkout(project_dir)
    record = _record_path(main, agent_id)

    if event == "SubagentStart":
        record.parent.mkdir(parents=True, exist_ok=True)
        _prune(record.parent)
        state = {"start": snapshot(main), "blocked": False, "warned": False}
        record.write_text(json.dumps(state), encoding="utf-8")
        return None

    if event != "SubagentStop":
        return None

    if not record.exists():
        return {
            "systemMessage": (
                f"subagent tree guard: no start record for agent {agent_id}, so the "
                f"main checkout ({main}) was NOT checked for this agent."
            )
        }

    state = json.loads(record.read_text(encoding="utf-8"))
    change = describe(state["start"], snapshot(main))
    if not change or state["warned"]:
        return None

    warning = (
        f"MAIN CHECKOUT CHANGED while a sub-agent ran: {main}: {change}. "
        "Stop, find out who changed it, and report before doing anything else."
    )
    if state["blocked"] or payload.get("stop_hook_active"):
        state["warned"] = True
        record.write_text(json.dumps(state), encoding="utf-8")
        return {"systemMessage": warning}
    state["blocked"] = True
    record.write_text(json.dumps(state), encoding="utf-8")
    return {
        "decision": "block",
        "reason": (
            f"The main checkout {main} changed while you ran: {change}. Do not try "
            "to fix or revert it. End your report with one line that starts "
            "'MAIN CHECKOUT CHANGED:' and says what changed and whether you did it."
        ),
    }


def _prune(folder: Path) -> None:
    """Delete start records older than two days (agents that are long gone)."""
    cutoff = time.time() - 2 * 24 * 3600
    for old in folder.glob("*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        project = Path(
            os.environ.get("CLAUDE_PROJECT_DIR") or str(payload.get("cwd") or ".")
        )
        out = handle(payload, project)
    except Exception as exc:  # noqa: BLE001 — fail open, loudly
        out = {"systemMessage": f"subagent tree guard failed open: {exc!r}"}
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
