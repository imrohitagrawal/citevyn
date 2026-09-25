#!/usr/bin/env bash
# Mutate one file, run a command against it, put it back — provably.
#
# WHY THIS EXISTS. Proving a guard bites means breaking the code it guards, and
# the restore step is where the accidents happen. Two in one session here, both
# from `git checkout -- <file>`: that restores to the last COMMIT, so it also
# silently throws away any other uncommitted work in that file. This restores
# from a byte copy of what was ACTUALLY there a moment ago, and it can only ever
# touch the one file you name.
#
# It also closes the trap that makes a mutation result a lie rather than a
# failure:
#
#   - `ruff format`, prettier and editor-on-save rewrite files, so a mutation
#     can be reverted underneath you and the run reports a false SURVIVAL. This
#     verifies the mutation is present with a literal `grep -F` before running.
#   - CPython caches bytecode by mtime AND size, so a same-length edit inside one
#     timestamp ("128" -> "256") can leave the interpreter running the MUTATED
#     module after a byte-identical restore — defeating cmp, git status and
#     grep -F at once. This clears __pycache__ on both sides.
#   - A restore that looks right is not a restore. This proves it with `cmp` and
#     fails loudly if the bytes differ.
#   - A command that never ran exits non-zero and looks exactly like a mutant
#     dying. `pytest` on a path that does not exist exits 4 with "no tests ran";
#     a typo exits 127. So this runs your command on the PRISTINE file first and
#     refuses to report anything unless it passes there. A failure after the
#     mutation only means something if success before it did.
#
# USAGE
#   scripts/mutate.sh <file> <find> <replace> -- <command...>
#
#   scripts/mutate.sh backend/app/api/routes/messages.py \
#       'Message.session_id == session_id,' '' \
#       -- backend/.venv/bin/python -m pytest -q tests/test_session_ownership.py
#
# EXIT CODES
#   0  the command FAILED under mutation — the guard bites. This is success.
#   1  the command PASSED under mutation — the mutant SURVIVED. Investigate.
#   2  the mutation never applied, or the restore did not verify. No result.
#
# The inverted exit code is deliberate: a surviving mutant is the finding, so the
# script fails when your test failed to notice. Do not "fix" it by flipping.

set -euo pipefail

die() { printf 'mutate.sh: %s\n' "$*" >&2; exit 2; }

[ $# -ge 5 ] || die "usage: mutate.sh <file> <find> <replace> -- <command...>"

FILE="$1"; FIND="$2"; REPLACE="$3"; shift 3
[ "$1" = "--" ] || die "expected -- before the command, got '$1'"
shift

[ -f "$FILE" ] || die "no such file: $FILE"

SNAP="$(mktemp -t mutate-snapshot)"
cleanup() {
  if [ -f "$SNAP" ]; then
    cp "$SNAP" "$FILE"
    if cmp -s "$SNAP" "$FILE"; then
      printf 'mutate.sh: restored %s, byte-identical (cmp)\n' "$FILE" >&2
    else
      printf 'mutate.sh: RESTORE FAILED for %s — snapshot kept at %s\n' "$FILE" "$SNAP" >&2
      exit 2
    fi
    rm -f "$SNAP"
  fi
  _clear_pycache
}
trap cleanup EXIT INT TERM

_clear_pycache() {
  # Same-length edits can leave a stale .pyc that the interpreter prefers.
  find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
}

cp "$FILE" "$SNAP"
_clear_pycache

# BASELINE. Without this, "the command failed" is indistinguishable from "the
# command never ran", and the second reports a mutant dying that never lived.
# Caught by using this script: a pytest path relative to the wrong directory
# exited 4 with "no tests ran in 0.00s", and the first version of this harness
# called that a kill.
printf 'mutate.sh: baseline run on the UNMUTATED file...\n' >&2
set +e
"$@" >/dev/null 2>&1
BASELINE=$?
set -e
if [ "$BASELINE" -ne 0 ]; then
  printf 'mutate.sh: the command exits %s on the UNMUTATED file, so a failure after\n' "$BASELINE" >&2
  printf 'mutate.sh: the mutation would prove nothing. Fix the invocation first.\n' >&2
  printf 'mutate.sh: (pytest: 4 = usage/path error, 5 = no tests collected, 127 = not found)\n' >&2
  exit 2
fi
printf 'mutate.sh: baseline passes — a failure below is attributable to the mutation\n' >&2

# Apply with python rather than sed: sed chokes on patterns containing `|`, `/`
# or `&`, and has reported "32 passed" for a mutation that never applied.
FILE="$FILE" FIND="$FIND" REPLACE="$REPLACE" python3 - <<'PY' || die "could not apply the mutation"
import os, sys
path, find, repl = os.environ["FILE"], os.environ["FIND"], os.environ["REPLACE"]
s = open(path, encoding="utf-8").read()
n = s.count(find)
if n == 0:
    sys.exit("mutate.sh: the FIND text is not in the file — the anchor has moved")
if n > 1:
    sys.exit(f"mutate.sh: the FIND text appears {n} times; make it unique")
open(path, "w", encoding="utf-8").write(s.replace(find, repl))
PY

# Believe nothing until the file on disk says so.
if [ -n "$REPLACE" ]; then
  grep -qF -- "$REPLACE" "$FILE" || die "mutation did not land (grep -F found no replacement)"
fi
if grep -qF -- "$FIND" "$FILE"; then
  die "mutation did not land (grep -F still finds the original text)"
fi
printf 'mutate.sh: mutation applied and confirmed by grep -F\n' >&2

set +e
"$@"
STATUS=$?
set -e

if [ "$STATUS" -eq 0 ]; then
  printf 'mutate.sh: MUTANT SURVIVED — the command passed with the code broken.\n' >&2
  printf 'mutate.sh: nothing you have asserts this. That is the finding.\n' >&2
  exit 1
fi
printf 'mutate.sh: mutant DIED (command exited %s) — the guard bites.\n' "$STATUS" >&2
printf 'mutate.sh: now ask what ELSE could have produced that failure.\n' >&2
exit 0
