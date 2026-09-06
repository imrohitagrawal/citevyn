#!/bin/bash
#
# Mutation harness for the third-party font stub (tests/fixtures.ts).
#
# WHAT IT PROTECTS. index.html render-blocks on a fonts.googleapis.com
# stylesheet, and a render-blocking stylesheet also blocks <script type="module">
# from executing — so while that request hangs the app does not mount and
# `.theme-toggle` does not exist. In run 34050016261 that left
# landing.spec.ts:96 on a blank page for exactly 30.0 s, 0.2 s past its
# waitForSelector, and reddened `main` on the required `flaky != 0` check.
# tests/fixtures.ts stubs the request; this proves the stub and its guards bite.
#
# Run it with:
#
#     bash frontend/scripts/mutate-font-stub.sh
#
# Discipline this encodes, matching scripts/mutate-focus-trap.sh:
#   - assert the mutation ACTUALLY APPLIED; a no-op edit reads as a survivor
#   - measure by EXIT CODE, never by grepping output for "FAIL"
#   - byte-copy restore and verify with cmp after EVERY mutation
#   - run sequentially in one tree; parallel writers corrupt each other
#
# It edits tracked files in place and restores them, so it refuses to start on a
# dirty tree — an interrupted run can then never be confused with your own edits.
set -u
cd "$(dirname "$0")/.." || exit 99
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT

FIX=tests/fixtures.ts
LAND=tests/landing.spec.ts
VIS=tests/visual.spec.ts
HTML=index.html
GUARD=src/test/buildGuards.test.ts
FILES="$FIX $LAND $VIS $HTML $GUARD"

for t in $FILES; do
  if ! git diff --quiet -- "$t"; then
    echo "refusing to run: $t has uncommitted changes."
    echo "commit or stash first, so a restore cannot lose your work."
    exit 1
  fi
  cp "$t" "$S/$(echo "$t" | tr / _)"
done

pass=0
fail=0

restore() {
  cp "$S/$(echo "$1" | tr / _)" "$1"
  cmp -s "$S/$(echo "$1" | tr / _)" "$1" || { echo "FATAL: could not restore $1"; exit 99; }
}

# $1 label  $2 file  $3 literal text proving the mutation landed  $4 victim command
check() {
  local label=$1 file=$2 proof=$3 victim=$4
  if ! grep -qF -- "$proof" "$file"; then
    echo "  BROKEN   $label -- mutation did not apply to $file (looked for: $proof)"
    restore "$file"
    fail=$((fail + 1))
    return
  fi
  eval "$victim" >"$S/out.txt" 2>&1
  local status=$?
  restore "$file"
  if [ $status -ne 0 ]; then
    # Print WHAT went red, not just that something did. A mutant that dies for
    # an unrelated reason (a syntax error, a crashed runner, another guard
    # tripping first) is a false kill, and an exit code alone cannot tell the
    # difference. This repo has been burned by exactly that.
    echo "  KILLED   $label"
    sed $'s/\033\\[[0-9;]*m//g' "$S/out.txt" |
      grep -E '^\s*[0-9]+\) |Error: |AssertionError|^\s*(×|✘|FAIL) ' |
      cut -c1-110 | head -4 | sed 's/^/             /'
    pass=$((pass + 1))
  else
    echo "  SURVIVED $label  <-- nothing bites; this guard is decorative"
    tail -20 "$S/out.txt"
    fail=$((fail + 1))
  fi
}

PW="npx playwright test tests/harness.spec.ts -c playwright.demo-ci.config.ts --reporter=line"
VT="npx vitest run src/test/buildGuards.test.ts"

echo "== M1: the font route matches a host nothing requests =="
perl -pi -e 's/googleapis\|gstatic/nosuchhost|alsonone/' "$FIX"
check "font route neutered" "$FIX" "nosuchhost|alsonone" "$PW"

echo "== M2: the fixture stops extending the base test =="
perl -0pi -e 's{export const test = base\.extend\(\{.*?\n\}\);}{export const test = base;}s' "$FIX"
check "fixture not applied at all" "$FIX" "export const test = base;" "$PW"

echo "== M3: a spec goes back to importing test from @playwright/test =="
perl -pi -e 's{from "\./fixtures";}{from "\@playwright/test";}' "$LAND"
check "landing.spec.ts bypasses the fixture" "$LAND" 'from "@playwright/test";' "$VT"

echo "== M4: a SECOND spec joins the visual.spec.ts exception =="
perl -pi -e 's{from "\@playwright/test";}{from "./fixtures";}' "$VIS"
check "visual.spec.ts silently switched" "$VIS" 'from "./fixtures";' "$VT"

echo "== M5: index.html stops loading a third-party stylesheet =="
perl -0pi -e 's{https://fonts\.googleapis\.com/css2\?[^"]*}{/local-fonts.css}' "$HTML"
check "nothing left for the stub to intercept" "$HTML" "/local-fonts.css" "$PW"

echo "== M6: the import parser always answers ./fixtures =="
perl -pi -e 's{return \[\.\.\.new Set\(found\)\];}{return ["./fixtures"];}' "$GUARD"
check "parser made vacuous" "$GUARD" 'return ["./fixtures"];' "$VT"

echo
echo "killed=$pass  survived/broken=$fail"
git status --porcelain -- $FILES
[ $fail -eq 0 ] || exit 1
