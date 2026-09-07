#!/bin/bash
#
# Mutation harness for the #366 type-check-coverage guard.
#
#     bash frontend/scripts/mutate-typecheck-guard.sh
#
# WHAT IT PROTECTS. `frontend/tsconfig.json` said `"include": ["src", "e2e"]`
# and `frontend/e2e/` has never existed. tsc ignores an `include` entry that
# matches nothing SILENTLY — no warning, no error, exit 0 — so the required
# `type-check + unit tests + build` job was green while loading ZERO of the 9
# Playwright specs, `helpers.ts` and `fixtures.ts`. Playwright transpiles with
# esbuild, which erases types without checking them, so nothing else looked
# either.
#
# The guard is the `tsc type-checks the Playwright specs (#366)` block in
# `src/test/buildGuards.test.ts`. This script is the evidence for the
# "N mutants, N killed" claim in the PR — without it that number is an
# assertion, which is why every other guard in this repo ships a mutate-*.sh
# beside it.
#
# Discipline, matching scripts/mutate-a11y-guards.sh and mutate-font-guards.sh:
#   - prove a GREEN CONTROL first. Without it every "KILLED" is unfalsifiable.
#   - assert the mutation ACTUALLY APPLIED; a no-op edit reads as a survivor,
#     and this repo has been burned by exactly that.
#   - judge KILLED by the runner's EXIT STATUS, never by grepping its output.
#   - byte-copy restore verified with cmp after EVERY mutation, and on interrupt.
#   - name WHICH test died, so a mutant killed by something unrelated is visible.
#   - run sequentially in ONE tree; parallel writers corrupt each other.
#
# The last two mutants are META-mutants: they break the GUARD rather than the
# config, and prove the guard cannot be made vacuous by an empty population.
set -u
cd "$(dirname "$0")/.." || exit 99
S=$(mktemp -d)

TC=tsconfig.json
BG=src/test/buildGuards.test.ts
TESTS="src/test/buildGuards.test.ts"

restore_all () {
  for f in TC BG; do
    eval "t=\$$f"
    [ -f "$S/p.$f" ] && cp "$S/p.$f" "$t" 2>/dev/null
  done
}
trap 'restore_all; rm -rf "$S"' EXIT INT TERM

for f in TC BG; do
  eval "t=\$$f"
  if ! git diff --quiet -- "$t"; then
    echo "refusing to run: $t has uncommitted changes."
    echo "commit or stash first, so a restore cannot lose your work."
    exit 1
  fi
  cp "$t" "$S/p.$f"
done
K=0; SV=0

run () {
  npx vitest run $TESTS >"$S/out" 2>&1
  echo $?
}
killers () {
  grep -oE '^\s+× .*' "$S/out" 2>/dev/null | sed 's/^ *× //' | cut -c1-90 | sort -u | head -3 | tr '\n' ';'
}

one () {  # label file pristine old new
  local label="$1" t="$2" p="$3"
  OLD="$4" NEW="$5" T="$t" python3 -c "
import os,sys
t=os.environ['T'];old=os.environ['OLD'];new=os.environ['NEW']
s=open(t).read()
if old not in s: sys.exit(3)
open(t,'w').write(s.replace(old,new,1))
"
  if [ $? -ne 0 ]; then
    echo "!! ANCHOR MISSING: $label"; cp "$p" "$t"
    cmp -s "$t" "$p" || { echo "!! RESTORE FAILED: $label"; exit 98; }
    SV=$((SV+1)); return
  fi
  if cmp -s "$t" "$p"; then
    echo "!! NO-OP: $label"; cp "$p" "$t"; SV=$((SV+1)); return
  fi
  local c who; c=$(run); who=$(killers)
  cp "$p" "$t"; cmp -s "$t" "$p" || { echo "!! RESTORE FAILED: $label"; exit 98; }
  if [ "$c" -ne 0 ]; then
    echo "KILLED    <- $label"
    echo "             by: ${who:-<no test named>}"
    K=$((K+1))
  else
    echo "SURVIVED! <- $label"; SV=$((SV+1))
  fi
}

echo "=== control: the unmutated tree must be GREEN ==="
c=$(run)
if [ "$c" -ne 0 ]; then
  echo "!! already failing unmutated — nothing below means anything."
  sed -n '1,40p' "$S/out"; exit 96
fi
echo "control OK"; echo

echo "=== the include list ==="
# THE ORIGINAL DEFECT, verbatim.
one "include: point at the phantom \`e2e\` again" "$TC" "$S/p.TC" \
'"include": ["src", "tests"],' '"include": ["src", "e2e"],'
# A near-miss a string check would wave through: the word "tests" is present,
# the directory exists, and NO spec is loaded.
one "include: name one file under tests/ instead of the directory" "$TC" "$S/p.TC" \
'"include": ["src", "tests"],' '"include": ["src", "tests/helpers.ts"],'
# Another: `include` is untouched, `exclude` takes it all back.
one "include: keep it, then exclude tests/" "$TC" "$S/p.TC" \
'"include": ["src", "tests"],' '"exclude": ["tests"],
  "include": ["src", "tests"],'
# The two remaining shapes a `toContain("tests")` string check would wave
# through, added after review pointed out the docs claimed all four were here
# and only two were. A near-name that resolves to nothing...
one "include: a near-name (\"tests-old\") that resolves to nothing" "$TC" "$S/p.TC" \
'"include": ["src", "tests"],' '"include": ["src", "tests-old"],'
# ...and the word surviving only in a comment. tsconfig.json is JSONC and this
# file already carries comments, so this is a real edit, not a hypothetical.
one "include: leave \"tests\" in a comment only" "$TC" "$S/p.TC" \
'"include": ["src", "tests"],' '// "include": ["src", "tests"],
  "include": ["src"],'
# Swapping one for the other is not a fix.
one "include: drop src/, keeping only tests/" "$TC" "$S/p.TC" \
'"include": ["src", "tests"],' '"include": ["tests"],'
# The #343 half: widening the project must not make it an emitter.
one "emit: turn noEmit off, so 78 files become emit candidates" "$TC" "$S/p.TC" \
'"noEmit": true,' '"noEmit": false,'
# #366's shape one lever over: the files ARE loaded, and tsc checks none of them.
one "check: add noCheck, so tsc loads every spec and checks nothing" "$TC" "$S/p.TC" \
'"noEmit": true,' '"noEmit": true,
    "noCheck": true,'

echo
echo "=== META: the guard cannot pass on an empty population ==="
# The anchor carries the line ABOVE it deliberately. `specs =
# playwrightSelectedSpecs();` appears in BOTH describe blocks, and
# `str.replace(old, new, 1)` takes the FIRST — which is #364's. The first run of
# this script mutated #364's guard and reported a kill by #364's test, a mutant
# dying for the wrong reason. #366's block has no blank line between the
# declaration and the `beforeAll`; #364's does.
one "meta: hand the guard an empty spec list" "$BG" "$S/p.BG" \
'  let specs: string[] = [];
  beforeAll(() => {
    specs = playwrightSelectedSpecs();' \
'  let specs: string[] = [];
  beforeAll(() => {
    specs = [];'
one "meta: hand the guard an empty loaded-file set" "$BG" "$S/p.BG" \
'  const loaded = new Set(parsed.fileNames.map((f) => resolvePath(f)));' \
'  const loaded = new Set<string>();'

echo
echo "=== KILLED: $K   SURVIVED/ERROR: $SV ==="
ok=1
for f in TC BG; do
  eval "t=\$$f"
  cmp -s "$t" "$S/p.$f" || { echo "DIRTY: $f"; ok=0; }
done
[ $ok -eq 1 ] && echo "all restored byte-identical"
[ "$SV" -eq 0 ]
