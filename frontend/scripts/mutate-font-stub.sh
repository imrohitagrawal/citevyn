#!/bin/bash
#
# Mutation harness for the vendored-font fixture (tests/fixtures.ts) that fixes
# the demo-suite flake, #364.
#
# WHAT IT PROTECTS. index.html render-blocks on a fonts.googleapis.com
# stylesheet, and a render-blocking stylesheet also blocks <script type="module">
# from executing — so while that request hangs the app does not mount and
# `.theme-toggle` does not exist. In run 34050016261 that left
# landing.spec.ts:96 on a blank page for exactly 30.0 s and reddened the
# required `flaky != 0` check. tests/fixtures.ts serves both font hosts from
# tests/fonts/; this proves that fixture and its guards bite.
#
# Run it with:
#
#     bash frontend/scripts/mutate-font-stub.sh
#
# Discipline this encodes, matching scripts/mutate-a11y-guards.sh and
# scripts/mutate-focus-ring.sh:
#   - prove a GREEN CONTROL first. Without it every "KILLED" is unfalsifiable:
#     these victims start their own dev server on port 3000, so anything already
#     holding that port fails all of them before an assertion runs, and an exit
#     code alone cannot tell that apart from the guard working.
#   - assert the mutation ACTUALLY APPLIED; a no-op edit reads as a survivor
#   - require the EXPECTED test to be the one that failed, not merely that
#     something did
#   - byte-copy restore and verify with cmp after EVERY mutation, and restore on
#     interrupt too — an EXIT trap that only deletes the backups leaves a
#     mutated file in the tree with no way back
#   - run sequentially in one tree; parallel writers corrupt each other
#
# It edits tracked files in place and restores them, so it refuses to start on a
# tree that is dirty RELATIVE TO HEAD (`git diff HEAD`, not `git diff` — the
# latter compares against the index and waves through anything `git add`ed).
set -u
cd "$(dirname "$0")/.." || exit 99
S=$(mktemp -d)

FIX=tests/fixtures.ts
LAND=tests/landing.spec.ts
HTML=index.html
GUARD=src/test/buildGuards.test.ts
FILES="$FIX $LAND $HTML $GUARD"

bak() { echo "$S/$(echo "$1" | tr / _)"; }

for t in $FILES; do
  if ! git diff --quiet HEAD -- "$t"; then
    echo "refusing to run: $t differs from HEAD."
    echo "commit or revert first, so a restore cannot lose your work."
    exit 1
  fi
  cp "$t" "$(bak "$t")"
done

restore_all() {
  for t in $FILES; do
    [ -f "$(bak "$t")" ] && cp "$(bak "$t")" "$t"
  done
}
# Restore BEFORE cleaning, and on INT/TERM as well as EXIT. A Ctrl-C during a
# victim run would otherwise delete the only copies and leave index.html or
# fixtures.ts mutated in the working tree.
trap 'restore_all; rm -rf "$S"' EXIT INT TERM

pass=0
fail=0

restore() {
  cp "$(bak "$1")" "$1"
  cmp -s "$(bak "$1")" "$1" || { echo "FATAL: could not restore $1"; exit 99; }
}

PW="npx playwright test tests/harness.spec.ts -c playwright.demo-ci.config.ts --reporter=line"
VT="npx vitest run src/test/buildGuards.test.ts"

echo "== control: the unmutated tree must be GREEN for both victims =="
for v in "$PW" "$VT"; do
  if ! eval "$v" >"$S/control.txt" 2>&1; then
    echo "  ABORT: \`$v\` fails on an UNMUTATED tree, so no mutation result below"
    echo "  would mean anything. Is something already listening on port 3000?"
    tail -25 "$S/control.txt"
    exit 1
  fi
done
echo "  both victims green"

# $1 label  $2 file  $3 literal text proving the mutation landed
# $4 victim command  $5 literal text that MUST appear in the victim's failure
check() {
  local label=$1 file=$2 proof=$3 victim=$4 expect=$5
  if ! grep -qF -- "$proof" "$file"; then
    echo "  BROKEN   $label -- mutation did not apply to $file (looked for: $proof)"
    restore "$file"
    fail=$((fail + 1))
    return
  fi
  eval "$victim" >"$S/out.txt" 2>&1
  local status=$?
  restore "$file"
  local plain
  plain=$(sed $'s/\033\\[[0-9;]*m//g' "$S/out.txt")
  if [ $status -eq 0 ]; then
    echo "  SURVIVED $label  <-- nothing bites; this guard is decorative"
    echo "$plain" | tail -15
    fail=$((fail + 1))
  elif ! grep -qF -- "$expect" <<<"$plain"; then
    # Died, but not from the guard this mutation targets. A crashed runner, a
    # syntax error, or a DIFFERENT guard tripping first all exit non-zero, and
    # counting those as kills is how a "6/6 killed" line stops meaning anything.
    echo "  WRONG-REASON $label  <-- failed, but not with: $expect"
    echo "$plain" | tail -15
    fail=$((fail + 1))
  else
    echo "  KILLED   $label"
    # `grep -v WebServer` first: the dev server logs a proxy ECONNREFUSED for
    # every /v1/auth/me (expected in demo mode, there is no backend), and those
    # lines would otherwise crowd out the assertion that actually fired.
    grep -v 'WebServer' <<<"$plain" |
      grep -E '^\s*[0-9]+\) |Error: |AssertionError' | cut -c1-108 | head -3 |
      sed 's/^/             /'
    pass=$((pass + 1))
  fi
}

echo "== M1: the stylesheet route matches a host nothing requests =="
perl -pi -e 's{fonts\\\.googleapis\\\.com}{fonts\\.nosuchhost\\.invalid}' "$FIX"
check "css route neutered" "$FIX" "nosuchhost" "$PW" \
  "the fonts.googleapis.com stylesheet reached the network"

echo "== M2: the fixture stops extending the base test =="
perl -0pi -e 's{export const test = base\.extend\(\{.*?\n\}\);}{export const test = base;}s' "$FIX"
check "fixture not applied at all" "$FIX" "export const test = base;" "$PW" \
  "the fonts.googleapis.com stylesheet reached the network"

echo "== M3: the vendored woff2 map loses an entry, so a subset 404s =="
perl -pi -e 's{"geist-latin\.woff2"}{"geist-MISSING.woff2"}' "$FIX"
check "a font subset is not vendored" "$FIX" "geist-MISSING.woff2" "$PW" \
  "a font subset was requested that tests/fonts/ does not have"

echo "== M4: index.html gains a SECOND third-party stylesheet the fixture does not serve =="
perl -0pi -e 's{(<link rel="preconnect" href="https://fonts\.googleapis\.com" />)}{$1\n    <link rel="stylesheet" href="https://cdn.example.invalid/x.css" />}' "$HTML"
check "an unintercepted third-party stylesheet" "$HTML" "cdn.example.invalid" "$PW" \
  "reached a third-party host the harness does not intercept"

echo "== M5: a spec goes back to importing test from @playwright/test =="
perl -pi -e 's{from "\./fixtures";}{from "\@playwright/test";}' "$LAND"
check "landing.spec.ts bypasses the fixture" "$LAND" 'from "@playwright/test";' "$VT" \
  "does not use tests/fixtures.ts"

echo "== M6: the import parser always answers ./fixtures =="
perl -pi -e 's{return \[\.\.\.new Set\(found\)\];}{return ["./fixtures"];}' "$GUARD"
check "parser made vacuous" "$GUARD" 'return ["./fixtures"];' "$VT" \
  "this form slips past the parser"

echo "== M7: the guard stops asking Playwright and hard-codes one file =="
perl -0pi -e 's{specs = \[\.\.\.new Set\(\(parsed\.suites \?\? \[\]\)\.map\(\(s\) => s\.file\)\.filter\(\(f\): f is string => !!f\)\)\]\.sort\(\);}{specs = ["landing.spec.ts"];}' "$GUARD"
check "spec list no longer resolved" "$GUARD" 'specs = ["landing.spec.ts"];' "$VT" \
  "Playwright selects the specs this guard thinks it does"

echo
echo "killed=$pass  survived/broken=$fail"
git status --porcelain -- $FILES
[ $fail -eq 0 ] || exit 1
