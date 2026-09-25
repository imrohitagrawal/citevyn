#!/usr/bin/env bash
# Suite for scripts/checkpoint.py — the automatic checkpoint taken after every
# file edit.
#
# The case that matters is the one this was written for, and it is the last test
# here: twenty minutes of uncommitted work, then a line broken on purpose, then
# the question "can I get the work back WITHOUT the break?". Before automatic
# checkpointing the honest answer was no. Both available options were wrong —
# revert to the last commit and lose the work, or recover a snapshot taken at
# the moment of the discard and get the work with the break welded to it.
#
# Everything runs in throwaway repositories under mktemp, never this checkout.

set -euo pipefail

HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/checkpoint.py"
CLI="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/checkpoints.sh"
PASS=0
FAIL=0

_check() {
  if [ "$2" = "$3" ]; then
    printf '  ok   %s\n' "$1"; PASS=$((PASS + 1))
  else
    printf '  FAIL %s — expected %s, got %s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1))
  fi
}

_edit() { printf '{"tool_name":"Edit","cwd":"%s"}' "$1" | python3 "$HOOK"; }
_count() { git -C "$1" for-each-ref --format='%(refname)' refs/checkpoint/ | grep -c . || true; }

_new_repo() {
  local d; d="$(mktemp -d)"
  git -C "$d" init -q
  git -C "$d" config user.email t@t.t
  git -C "$d" config user.name t
  printf 'line1\nline2\nassert x == 1\n' > "$d/work.py"
  git -C "$d" add -A
  git -C "$d" commit -qm base
  echo "$d"
}

echo "checkpoint: a clean tree produces nothing to restore"
REPO="$(_new_repo)"
_edit "$REPO"
_check "no checkpoint on a clean tree" "0" "$(_count "$REPO")"
rm -rf "$REPO"

echo "checkpoint: an edit to a tracked file is captured"
REPO="$(_new_repo)"
printf 'line1\nline2\nassert x == 1\nNEW\n' > "$REPO/work.py"
_edit "$REPO"
_check "one checkpoint after one edit" "1" "$(_count "$REPO")"
rm -rf "$REPO"

echo "checkpoint: re-running on unchanged content does not pile up duplicates"
# REGRESSION, and it only showed up on a slower machine. `git stash create`
# stamps each snapshot with the current time, so identical content yields a
# DIFFERENT commit id a second later. The first dedupe compared commit ids, so
# it worked only when both calls landed inside one second: green locally, red on
# the macOS runner with "expected 1, got 2". The sleeps below force the calls
# into different seconds so this cannot pass by being fast.
REPO="$(_new_repo)"
printf 'line1\nline2\nassert x == 1\nNEW\n' > "$REPO/work.py"
_edit "$REPO"; sleep 1.1; _edit "$REPO"; sleep 1.1; _edit "$REPO"
_check "still one checkpoint for one state, across seconds" "1" "$(_count "$REPO")"
rm -rf "$REPO"

echo "checkpoint: two edits in the SAME SECOND both survive"
# REGRESSION. The first version named refs by timestamp alone, at one-second
# resolution. Two edits inside one second produced the same ref name and the
# second overwrote the first — destroying exactly the checkpoint you need, the
# one from before the change you regret. Found by replaying the real scenario:
# two edits, one checkpoint. The object id in the ref name is what fixes it.
REPO="$(_new_repo)"
printf 'line1\nline2\nassert x == 1\nFIRST\n' > "$REPO/work.py"; _edit "$REPO"
printf 'line1\nline2\nassert x == 1\nSECOND\n' > "$REPO/work.py"; _edit "$REPO"
_check "two distinct checkpoints in one second" "2" "$(_count "$REPO")"
rm -rf "$REPO"

echo "checkpoint: the list is pruned rather than growing without bound"
REPO="$(_new_repo)"
for i in $(seq 1 45); do
  printf 'line1\nline2\nassert x == 1\nEDIT-%s\n' "$i" > "$REPO/work.py"
  _edit "$REPO"
done
COUNT="$(_count "$REPO")"
if [ "$COUNT" -le 40 ] && [ "$COUNT" -ge 39 ]; then
  printf '  ok   pruned to the keep limit (%s)\n' "$COUNT"; PASS=$((PASS + 1))
else
  printf '  FAIL prune: expected about 40, got %s\n' "$COUNT"; FAIL=$((FAIL + 1))
fi
rm -rf "$REPO"

echo "checkpoint: restore refuses over uncommitted work unless forced"
REPO="$(_new_repo)"
printf 'line1\nline2\nassert x == 1\nWORK\n' > "$REPO/work.py"; _edit "$REPO"
REF="$(git -C "$REPO" for-each-ref --format='%(refname)' refs/checkpoint/ | head -1)"
if (cd "$REPO" && "$CLI" restore "$REF" >/dev/null 2>&1); then
  printf '  FAIL restore did not refuse over uncommitted changes\n'; FAIL=$((FAIL + 1))
else
  printf '  ok   restore refuses over uncommitted changes\n'; PASS=$((PASS + 1))
fi
rm -rf "$REPO"

echo "checkpoint: THE SCENARIO — work back without the break, nothing committed"
REPO="$(_new_repo)"
# Twenty minutes of work. Not committed, not copied, no decision made.
printf 'line1\nline2\nassert x == 1\nGOOD-1\nGOOD-2\n' > "$REPO/work.py"
_edit "$REPO"
# Now break a line on purpose. This is itself an edit, so it is checkpointed too.
printf 'line1\nline2\nassert x == 999\nGOOD-1\nGOOD-2\n' > "$REPO/work.py"
_edit "$REPO"
# Sort by refname, which is the millisecond-stamped true order. Using
# --sort=creatordate here passed and failed at random, because both edits land
# inside one second and git's date resolution cannot separate them.
BEFORE="$(git -C "$REPO" for-each-ref --sort=refname --format='%(refname)' refs/checkpoint/ | head -1)"
CONTENT="$(git -C "$REPO" show "$BEFORE:work.py")"
_check "the pre-break checkpoint kept the good work" "2" "$(grep -c 'GOOD-' <<<"$CONTENT")"
_check "the pre-break checkpoint has the UNBROKEN line" "1" "$(grep -c 'assert x == 1$' <<<"$CONTENT")"
_check "and not the broken one" "0" "$(grep -c 'assert x == 999' <<<"$CONTENT")"
# And the last commit has neither, which is what made this the hard case.
HEADC="$(git -C "$REPO" show HEAD:work.py)"
_check "the last commit has none of the work" "0" "$(grep -c 'GOOD-' <<<"$HEADC")"
rm -rf "$REPO"

printf '\ncheckpoint: %s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
