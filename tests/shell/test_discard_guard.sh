#!/usr/bin/env bash
# Suite for scripts/discard_guard.py — the PreToolUse hook that refuses a
# command which would discard uncommitted work, after saving it.
#
# A guard is only worth its lines if it fires on the real thing and stays silent
# otherwise. Both directions are tested here, because the failure mode of a
# noisy guard is that someone turns it off, and the failure mode of a quiet one
# is the incident it was written for.
#
# Every case runs in a THROWAWAY repository under mktemp, never against this
# checkout: a suite that tests a discard guard must not be able to discard
# anything real.

set -euo pipefail

GUARD="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/discard_guard.py"
PASS=0
FAIL=0

_decide() {
  # _decide <repo> <command> -> prints "deny" or "allow"
  local repo="$1" cmd="$2" out
  out="$(python3 -c '
import json, sys
print(json.dumps({"tool_name": "Bash", "tool_input": {"command": sys.argv[1]}, "cwd": sys.argv[2]}))
' "$cmd" "$repo" | python3 "$GUARD")"
  if [ -z "$out" ]; then echo "allow"; else
    python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["permissionDecision"])' <<<"$out"
  fi
}

_check() {
  # _check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then
    printf '  ok   %s\n' "$1"; PASS=$((PASS + 1))
  else
    printf '  FAIL %s — expected %s, got %s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1))
  fi
}

_new_repo() {
  local d; d="$(mktemp -d)"
  git -C "$d" init -q
  git -C "$d" config user.email t@t.t
  git -C "$d" config user.name t
  echo "original" > "$d/tracked.txt"
  git -C "$d" add -A
  git -C "$d" commit -qm base
  echo "$d"
}

echo "discard_guard: a clean tree is never blocked"
REPO="$(_new_repo)"
for cmd in "git checkout -- tracked.txt" "git reset --hard" "git clean -fd" "git stash"; do
  _check "clean tree: $cmd" "allow" "$(_decide "$REPO" "$cmd")"
done
rm -rf "$REPO"

echo "discard_guard: a branch switch is not a discard and must never be caught"
REPO="$(_new_repo)"
echo "edited" > "$REPO/tracked.txt"
for cmd in "git checkout -q main" "git checkout -b feature" "git status" "git stash list" "git restore --staged tracked.txt"; do
  _check "not a discard: $cmd" "allow" "$(_decide "$REPO" "$cmd")"
done
rm -rf "$REPO"

echo "discard_guard: the incident — a tracked file is modified"
REPO="$(_new_repo)"
echo "edited" > "$REPO/tracked.txt"
for cmd in "git checkout -- tracked.txt" "git checkout ." "git restore tracked.txt" "git reset --hard" "git stash" "cd x && git checkout -- tracked.txt" "make test; git reset --hard"; do
  _check "dirty tracked: $cmd" "deny" "$(_decide "$REPO" "$cmd")"
done
rm -rf "$REPO"

echo "discard_guard: untracked files — only the command that can delete them"
REPO="$(_new_repo)"
echo "new" > "$REPO/untracked.txt"
# `git checkout -- <tracked path>` cannot touch an untracked file, so firing
# here would be a false positive. `git clean -f` deletes it and git cannot undo
# that, so it must fire.
_check "untracked only: git checkout -- tracked.txt" "allow" "$(_decide "$REPO" "git checkout -- tracked.txt")"
_check "untracked only: git clean -fd" "deny" "$(_decide "$REPO" "git clean -fd")"
rm -rf "$REPO"

echo "discard_guard: the escape hatch is honoured"
REPO="$(_new_repo)"
echo "edited" > "$REPO/tracked.txt"
_check "DISCARD_OK=1 allows" "allow" "$(_decide "$REPO" "DISCARD_OK=1 git checkout -- tracked.txt")"
rm -rf "$REPO"

echo "discard_guard: the snapshot actually brings the work back"
REPO="$(_new_repo)"
printf 'SENTINEL-RECOVERABLE\n' >> "$REPO/tracked.txt"
DECISION="$(_decide "$REPO" "git checkout -- tracked.txt")"
_check "denied before recovery test" "deny" "$DECISION"
REF="$(git -C "$REPO" for-each-ref --format='%(refname)' refs/discard-guard/ | tail -1)"
if [ -z "$REF" ]; then
  printf '  FAIL no snapshot ref was created\n'; FAIL=$((FAIL + 1))
else
  git -C "$REPO" checkout -- tracked.txt                 # the discard, for real
  AFTER="$(grep -c SENTINEL-RECOVERABLE "$REPO/tracked.txt" || true)"
  _check "work is gone after the discard" "0" "$AFTER"
  git -C "$REPO" checkout "$REF" -- tracked.txt          # the documented recovery
  BACK="$(grep -c SENTINEL-RECOVERABLE "$REPO/tracked.txt" || true)"
  _check "work comes back from the snapshot" "1" "$BACK"
fi
rm -rf "$REPO"

echo "discard_guard: untracked work is copied aside, since git cannot object it"
REPO="$(_new_repo)"
printf 'SENTINEL-UNTRACKED\n' > "$REPO/scratch.txt"
_check "clean -f denied" "deny" "$(_decide "$REPO" "git clean -fd")"
SAVED="$(find "$REPO/.git/discard-guard" -name scratch.txt 2>/dev/null | head -1)"
if [ -n "$SAVED" ] && grep -q SENTINEL-UNTRACKED "$SAVED"; then
  printf '  ok   untracked file saved outside git objects\n'; PASS=$((PASS + 1))
else
  printf '  FAIL untracked file was not saved\n'; FAIL=$((FAIL + 1))
fi
rm -rf "$REPO"

printf '\ndiscard_guard: %s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
