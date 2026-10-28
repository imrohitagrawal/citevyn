#!/usr/bin/env bash
# Suite for scripts/subagent_tree_guard.py — the SubagentStart/SubagentStop hook
# that flags a sub-agent run during which the main checkout's HEAD, branch or
# `git status` changed.
#
# Both directions are tested: it must stay silent when nothing changed and when
# only a LINKED worktree changed (that is where agents are meant to write), and
# it must fire on each kind of change to the main checkout. Every case runs in a
# throwaway repository under mktemp, never against this checkout.
#
# Which change turns each case red:
#   silent-*  — the comparison made always-true, or the main checkout taken from
#               the hook's cwd instead of `git worktree list` (linked case)
#   fires-*   — a field (head, branch, status, content) dropped from the snapshot
#               or the comparison; fires-file-in-untracked-dir-names-it: --untracked-files=all
#               dropped (git then shows only `?? d/`, before and after);
#               fires-*-overwritten/rewritten: the content hash removed (a file
#               already dirty when the agent started keeps the same status line)
#   second    — the blocked/stop_hook_active branch removed (the agent would be
#               trapped), or the warned flag ignored (a warning on every stop)
#   held      — blocking even when another hook already holds the agent
#   double-stop — the record deleted after a clean stop (measured 2026-10-29:
#               each agent stops twice here, so the second said "not checked")
#   no-record — the missing-record branch returning nothing (silent non-check)
#   block-also-warns-the-user — the systemMessage dropped from the block (live
#               2026-10-29: a sub-agent ignored the block reason and no second stop came)
#   alert-*   — the alert file not written, not delivered to the main session's next
#               tool call, delivered to a sub-agent's call instead, or not cleared
#   branch-*  — refs/heads dropped from the snapshot (deleted), or new branches
#               flagged (created must stay silent)
#   no-agent-id — a missing agent_id falling back to one shared record
#   (every case) — the guard crashing: _verdict reports `crash`, never `message`;
#               crash-* also go red if a failure blocks (fails closed) while loud
#   silent-branch-checked-out-mid-run / silent-own-worktree-branch-removed — the
#               worktree-branch exclusion using only the start's, or only the stop's, list
#   no-agent-id-* — a missing agent_id falling back to any shared record name
#   prune     — _prune removed, its age test inverted, or its cutoff moved so it
#               deletes a running agent's fresh record (keeps-other-agents)
#   blocked-flag — `blocked` not saved, so a plain stop after a block blocks again
#   crash-*   — an exception swallowed silently or turned into a block (fail closed)
#   record-kept-out-of-status — the record written in the checkout instead of .git

set -euo pipefail

GUARD="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/subagent_tree_guard.py"
PASS=0
FAIL=0
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

_check() {
  # _check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then
    printf '  ok   %s\n' "$1"; PASS=$((PASS + 1))
  else
    printf '  FAIL %s — expected %s, got %s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1))
  fi
}

_hook() {
  # _hook <project_dir> <event> <agent_id> [stop_hook_active] -> prints the hook's stdout
  local active="${4:-false}"
  python3 -c '
import json, sys
print(json.dumps({"hook_event_name": sys.argv[1], "agent_id": sys.argv[2],
                  "stop_hook_active": sys.argv[3] == "true", "cwd": "/"}))
' "$2" "$3" "$active" | CLAUDE_PROJECT_DIR="$1" python3 "$GUARD"
}

_pretool() {
  # _pretool <project_dir> [agent_id] -> prints the hook's stdout for a PreToolUse call
  python3 -c '
import json, sys
d = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/"}
if sys.argv[1]:
    d["agent_id"] = sys.argv[1]
print(json.dumps(d))
' "${2:-}" | CLAUDE_PROJECT_DIR="$1" python3 "$GUARD"
}

_verdict() {
  # stdin: hook stdout -> "silent", "block" or "message"
  python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    print("silent")
else:
    out = json.loads(raw)
    msg = out.get("systemMessage", "")
    if out.get("decision") == "block":
        print("block")
    elif "failed open" in msg:
        print("crash")
    elif "MAIN CHECKOUT CHANGED" in msg:
        print("message")
    elif "NOT checked" in msg:
        print("notchecked")
    else:
        print("other")
'
}

_new_repo() {
  local repo="$1"
  git init -q -b main "$repo"
  git -C "$repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
}

echo "test_subagent_tree_guard.sh"

R="${WORK}/main"
_new_repo "$R"
git -C "$R" worktree add -q -b side "${WORK}/linked"

_hook "$R" SubagentStart a1 >/dev/null
_check "silent-unchanged" silent "$(_hook "$R" SubagentStop a1 | _verdict)"
_check "double-stop-unchanged-stays-silent" silent "$(_hook "$R" SubagentStop a1 true | _verdict)"

_hook "$R" SubagentStart a2 >/dev/null
echo x > "${WORK}/linked/scratch.txt"
git -C "${WORK}/linked" -c user.email=t@t -c user.name=t commit -q --allow-empty -m side
_check "silent-linked-worktree-changed" silent "$(_hook "${WORK}/linked" SubagentStop a2 | _verdict)"
rm -f "${WORK}/linked/scratch.txt"

_hook "$R" SubagentStart a3 >/dev/null
echo x > "$R/untracked.txt"
OUT="$(_hook "$R" SubagentStop a3)"
_check "fires-untracked-file" block "$(printf '%s' "$OUT" | _verdict)"
_check "block-also-warns-the-user" yes "$(printf '%s' "$OUT" | grep -q '"systemMessage": "MAIN CHECKOUT CHANGED' && echo yes || echo no)"
_check "alert-silent-for-a-subagent-call" "" "$(_pretool "$R" some-agent)"
_check "alert-reaches-the-main-session" yes "$(_pretool "$R" | grep -q '"additionalContext": "MAIN CHECKOUT CHANGED' && echo yes || echo no)"
_check "alert-delivered-once" "" "$(_pretool "$R")"
_check "second-stop-lets-it-go" message "$(_hook "$R" SubagentStop a3 true | _verdict)"
_check "third-stop-is-quiet" silent "$(_hook "$R" SubagentStop a3 true | _verdict)"
_check "never-blocks-twice" silent "$(_hook "$R" SubagentStop a3 | _verdict)"
_hook "$R" SubagentStart a3c >/dev/null
echo w > "$R/w.txt"
_check "blocked-flag-first-stop" block "$(_hook "$R" SubagentStop a3c | _verdict)"
_check "blocked-flag-plain-stop-after-block-warns" message "$(_hook "$R" SubagentStop a3c | _verdict)"
rm -f "$R/w.txt"
mkdir -p "$R/newdir"; echo a > "$R/newdir/a.txt"
_hook "$R" SubagentStart a3d >/dev/null
echo b > "$R/newdir/b.txt"
OUT="$(_hook "$R" SubagentStop a3d)"
_check "fires-file-in-untracked-dir" block "$(printf '%s' "$OUT" | _verdict)"
_check "fires-file-in-untracked-dir-names-it" yes "$(printf '%s' "$OUT" | grep -q 'newdir/b.txt' && echo yes || echo no)"
rm -rf "$R/newdir"
_hook "$R" SubagentStart a3b >/dev/null
_check "held-by-another-hook-only-warns" message "$( (echo z > "$R/held.txt"; _hook "$R" SubagentStop a3b true) | _verdict)"
_check "held-agent-still-alerts-the-main-session" yes "$(_pretool "$R" | grep -q 'additionalContext' && echo yes || echo no)"
rm -f "$R/held.txt"
rm -f "$R/untracked.txt"

echo base > "$R/tracked.txt"; git -C "$R" add tracked.txt
git -C "$R" -c user.email=t@t -c user.name=t commit -q -m tracked
echo "owner's uncommitted edit" > "$R/tracked.txt"
echo "owner's notes" > "$R/notes.txt"
_hook "$R" SubagentStart a8 >/dev/null
echo "agent overwrote it" > "$R/tracked.txt"
_check "fires-dirty-file-overwritten" block "$(_hook "$R" SubagentStop a8 | _verdict)"
_hook "$R" SubagentStart a9 >/dev/null
echo "agent rewrote the notes" > "$R/notes.txt"
_check "fires-untracked-file-rewritten" block "$(_hook "$R" SubagentStop a9 | _verdict)"
git -C "$R" checkout -q -- tracked.txt; rm -f "$R/notes.txt"

_hook "$R" SubagentStart a4 >/dev/null
git -C "$R" -c user.email=t@t -c user.name=t commit -q --allow-empty -m moved
_check "fires-head-moved" block "$(_hook "$R" SubagentStop a4 | _verdict)"

_hook "$R" SubagentStart a5 >/dev/null
git -C "$R" checkout -q -b other
_check "fires-branch-switched" block "$(_hook "$R" SubagentStop a5 | _verdict)"
git -C "$R" checkout -q main

_hook "$R" SubagentStart a6 >/dev/null
_check "fires-from-linked-session" block "$( (echo y > "$R/u2.txt"; _hook "${WORK}/linked" SubagentStop a6) | _verdict)"
rm -f "$R/u2.txt"

OLD="$(git -C "$R" rev-parse --git-common-dir)/subagent-tree-guard/stale-agent.json"
case "$OLD" in /*) ;; *) OLD="$R/$OLD" ;; esac
echo '{}' > "$OLD"; touch -t 202001010000 "$OLD"
_hook "$R" SubagentStart a7 >/dev/null
_check "prune-drops-old-records" absent "$([ -e "$OLD" ] && echo present || echo absent)"
_hook "$R" SubagentStart a7b >/dev/null
_check "prune-keeps-other-agents-records" present "$([ -e "$(dirname "$OLD")/a7.json" ] && echo present || echo absent)"
_check "prune-keeps-fresh-records" present "$([ -e "$(dirname "$OLD")/a7.json" ] && echo present || echo absent)"

_check "no-record-is-not-silent" notchecked "$(_hook "$R" SubagentStop never-started | _verdict)"
_hook "$R" SubagentStart a10 >/dev/null
git -C "$R" branch keepme
_check "silent-branch-created" silent "$(_hook "$R" SubagentStop a10 | _verdict)"
_hook "$R" SubagentStart a11 >/dev/null
git -C "$R" branch -D -q keepme
_check "fires-branch-deleted" block "$(_hook "$R" SubagentStop a11 | _verdict)"
git -C "$R" branch feat
_hook "$R" SubagentStart a13 >/dev/null
git -C "$R" worktree add -q "${WORK}/feat-wt" feat
git -C "${WORK}/feat-wt" -c user.email=t@t -c user.name=t commit -q --allow-empty -m wt
_check "silent-branch-checked-out-mid-run" silent "$(_hook "$R" SubagentStop a13 | _verdict)"
_hook "$R" SubagentStart a14 >/dev/null
git -C "$R" worktree remove "${WORK}/feat-wt"; git -C "$R" branch -D -q feat
_check "silent-own-worktree-branch-removed" silent "$(_hook "$R" SubagentStop a14 | _verdict)"
RECS="$(dirname "$OLD")"
BEFORE_LS="$(ls "$RECS")"
_hook "$R" SubagentStart "" >/dev/null
echo n > "$R/noid.txt"
_check "no-agent-id-start-then-stop-not-checked" notchecked "$(_hook "$R" SubagentStop "" | _verdict)"
_check "no-agent-id-writes-no-record" same "$([ "$(ls "$RECS")" = "$BEFORE_LS" ] && echo same || echo changed)"
rm -f "$R/noid.txt"
_check "no-agent-id-is-not-silent" notchecked "$(_hook "$R" SubagentStop "" | _verdict)"
_check "no-agent-id-start-writes-nothing" absent "$(_hook "$R" SubagentStart "" >/dev/null; [ -e "$(dirname "$OLD")/unknown.json" ] && echo present || echo absent)"

NOREPO="$(mktemp -d "${WORK}/norepo.XXXXXX")"
_check "crash-fails-open-loudly" crash "$(_hook "$NOREPO" SubagentStop a12 | _verdict)"
_check "crash-before-a-tool-call-is-quiet" "" "$(_pretool "$NOREPO")"
_check "crash-on-start-fails-open-loudly" crash "$(_hook "$NOREPO" SubagentStart a12 | _verdict)"
_check "record-kept-out-of-status" "" "$(git -C "$R" status --porcelain)"

echo "${PASS} passed, ${FAIL} failed"
[ "${FAIL}" -eq 0 ]
