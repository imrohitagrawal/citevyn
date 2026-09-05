#!/bin/bash
#
# Mutation harness for the eager-bundle budget guards (#323).
#
# WHY THIS IS IN THE REPO. A PR body saying "16/16 mutations killed" is a claim
# nobody can check; a reviewer said so, and was right. This script names every
# mutation and lets anyone re-run the whole set in about a minute:
#
#     bash frontend/scripts/mutate-bundle-guards.sh
#
# A guard is only a guard if deleting it turns a test red. Each mutation below
# removes or inverts exactly one guard in bundle-budget.mjs / check-bundle-size.mjs
# / package.json and expects the suite to FAIL.
#
# Discipline this encodes, learned from this repo's own near-misses:
#   - assert the mutation ACTUALLY APPLIED; a no-op edit otherwise reads as a
#     surviving mutant (two of these anchors were silently missing at first)
#   - measure by EXIT CODE, never by grepping output for the word "FAIL"; a
#     harness that dies before any check runs prints no "FAIL" either
#   - byte-copy restore and verify with cmp after EVERY mutation
#   - run sequentially in one tree; parallel writers corrupt each other
#
# It edits tracked files in place and restores them. It refuses to start if the
# working tree is dirty, so an interrupted run can never be confused with your
# own edits.
set -u

cd "$(dirname "$0")/.." || exit 99

PURE=scripts/bundle-budget.mjs
RUN=scripts/check-bundle-size.mjs
PKG=package.json
VC=vite.config.ts
WF=../.github/workflows/frontend.yml
TESTS="scripts/bundle-budget.test.mjs src/test/buildGuards.test.ts"

if ! git diff --quiet -- "$PURE" "$RUN" "$PKG" "$VC" "$WF"; then
  echo "refusing to run: one of $PURE / $RUN / $PKG / $VC / $WF has uncommitted changes."
  echo "commit or stash them first, so a restore cannot lose your work."
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cp "$PURE" "$TMP/pure"; cp "$RUN" "$TMP/run"; cp "$PKG" "$TMP/pkg"
cp "$VC" "$TMP/vc"; cp "$WF" "$TMP/wf"

KILLED=0; SURVIVED=0

# $1 label, $2 target, $3 pristine, $4 anchor, $5 replacement
mutate () {
  local label="$1" target="$2" pristine="$3"
  OLD="$4" NEW="$5" TARGET="$target" python3 - <<'PY'
import os, sys
t = os.environ["TARGET"]; old = os.environ["OLD"]; new = os.environ["NEW"]
s = open(t).read()
if old not in s:
    sys.exit(3)
open(t, "w").write(s.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then
    echo "!! HARNESS ERROR (anchor not found, mutation never applied): $label"
    cp "$pristine" "$target"; SURVIVED=$((SURVIVED+1)); return
  fi
  if cmp -s "$target" "$pristine"; then
    echo "!! HARNESS ERROR (mutation was a no-op): $label"
    cp "$pristine" "$target"; SURVIVED=$((SURVIVED+1)); return
  fi

  npx vitest run $TESTS >"$TMP/out" 2>&1
  local code=$?

  cp "$pristine" "$target"
  cmp -s "$target" "$pristine" || { echo "!! RESTORE FAILED: $label"; exit 98; }

  if [ $code -ne 0 ]; then
    echo "KILLED    <- $label"; KILLED=$((KILLED+1))
  else
    echo "SURVIVED! <- $label"; SURVIVED=$((SURVIVED+1))
  fi
}

echo "=== mutating the bundle-budget guards ==="

# --- parseBudget: the #323 defect itself -------------------------------------
mutate "budget validation: drop the whole check" "$PURE" "$TMP/pure" \
'  if (!Number.isInteger(max) || max <= 0) {' '  if (false) {'
mutate "budget validation: accept non-integers" "$PURE" "$TMP/pure" \
'  if (!Number.isInteger(max) || max <= 0) {' '  if (max <= 0) {'
mutate "budget validation: accept zero and negatives" "$PURE" "$TMP/pure" \
'  if (!Number.isInteger(max) || max <= 0) {' '  if (!Number.isInteger(max)) {'
mutate "budget: drop the object-shape check" "$PURE" "$TMP/pure" \
'  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {' '  if (false) {'

# --- buildCommand: WHICH artifact gets measured ------------------------------
mutate "build: stop pinning the TS config (stale vite.config.js can win)" "$PURE" "$TMP/pure" \
'"--config", "vite.config.ts", ' ''
mutate "build: drop --manifest" "$PURE" "$TMP/pure" \
', "--manifest"' ''
mutate "build: drop production mode" "$PURE" "$TMP/pure" \
'"--mode", "production", ' ''
mutate "build: stop building the LIVE variant" "$PURE" "$TMP/pure" \
'env: { VITE_API_LIVE: "true" },' 'env: {},'

# --- the eager graph ---------------------------------------------------------
mutate "graph: stop following static imports (the modulePreload:false blind spot)" "$PURE" "$TMP/pure" \
'    for (const imported of record.imports ?? []) visit(imported);' '    void record;'
mutate "graph: also follow dynamicImports (counts lazy chunks as eager)" "$PURE" "$TMP/pure" \
'    for (const imported of record.imports ?? []) visit(imported);' \
'    for (const imported of [...(record.imports ?? []), ...(record.dynamicImports ?? [])]) visit(imported);'
mutate "graph: drop the fail-closed no-entry throw" "$PURE" "$TMP/pure" \
'  if (entries.length === 0) {' '  if (false) {'
mutate "graph: drop the unknown-chunk throw" "$PURE" "$TMP/pure" \
'    if (!record) {' '    if (false) {'
mutate "graph: drop the missing-file throw" "$PURE" "$TMP/pure" \
'    if (typeof record.file !== "string" || record.file === "") {' '    if (false) {'
mutate "graph: drop cycle protection" "$PURE" "$TMP/pure" \
'    if (seen.has(key)) return;' '    if (false) return;'
mutate "graph: drop file deduplication" "$PURE" "$TMP/pure" \
'  return [...new Set(files)];' '  return files;'
mutate "graph: drop the manifest object-shape check" "$PURE" "$TMP/pure" \
'  if (manifest === null || typeof manifest !== "object" || Array.isArray(manifest)) {' '  if (false) {'

# --- containment -------------------------------------------------------------
mutate "path: drop the dist/ containment guard" "$PURE" "$TMP/pure" \
'  if (!resolved.startsWith(distRoot + sep)) {' '  if (false) {'
mutate "path: stop resolving symlinks on the candidate" "$PURE" "$TMP/pure" \
'    resolved = realpath(candidate);' '    resolved = candidate;'
mutate "path: use the raw distDir (trailing separator bug)" "$PURE" "$TMP/pure" \
'  const distRoot = realpath(resolve(distDir));' '  const distRoot = distDir;'

# --- arithmetic and the verdict ---------------------------------------------
mutate "measure: take the largest file instead of summing" "$PURE" "$TMP/pure" \
'  const totalGzip = measured.reduce((sum, f) => sum + f.gzip, 0);' \
'  const totalGzip = Math.max(...measured.map((f) => f.gzip));'
mutate "evaluate: off-by-one at the ceiling" "$PURE" "$TMP/pure" \
'  return { ok: totalGzip <= max, line, totalGzip, headroom };' \
'  return { ok: totalGzip < max, line, totalGzip, headroom };'
mutate "evaluate: always pass" "$PURE" "$TMP/pure" \
'  return { ok: totalGzip <= max, line, totalGzip, headroom };' \
'  return { ok: true, line, totalGzip, headroom };'
mutate "evaluate: drop the plausibility floor" "$PURE" "$TMP/pure" \
'  if (totalGzip < MIN_PLAUSIBLE_GZIP) {' '  if (false) {'
mutate "evaluate: report constant zeros instead of the real numbers" "$PURE" "$TMP/pure" \
'    `${totalRaw} B raw / ${totalGzip} B gzip total (budget ${max} B, headroom ${headroom} B)`;' \
'    `0 B raw / 0 B gzip total (budget 0 B, headroom 0 B)`;'

# --- the runner and the CI invocation ---------------------------------------
mutate "runner: swallow the failure and exit 0" "$RUN" "$TMP/run" \
'  process.exit(1);' '  process.exit(0);'
mutate "package.json: sneak a --dist bypass into the CI invocation" "$PKG" "$TMP/pkg" \
'"check:bundle": "node scripts/check-bundle-size.mjs"' \
'"check:bundle": "node scripts/check-bundle-size.mjs --dist dist"'

# --- THE META-GUARDS: can the guards themselves be switched off? -------------
#
# Every mutation below was a WORKING BYPASS at some point in this PR's history.
# The first version of the workflow assertion was a substring grep; the second
# sliced a 3-line window above the `run:` line; the selection pin matched
# vite.config.ts's TEXT. A skeptic round defeated all three. They now parse the
# workflow YAML and ask vitest what it actually selects.

echo
echo "--- workflow: can the gate be switched off? ---"
mutate "workflow: if: written AFTER run: (YAML key order is not semantic)" "$WF" "$TMP/wf" \
'      - name: Bundle budget
        run: npm run check:bundle' \
'      - name: Bundle budget
        run: npm run check:bundle
        if: ${{ false }}'
mutate "workflow: if: above run:, pushed out of a 3-line window by comments" "$WF" "$TMP/wf" \
'      - name: Bundle budget
        run: npm run check:bundle' \
'      - name: Bundle budget
        if: ${{ false }}
        # one
        # two
        # three
        run: npm run check:bundle'
mutate "workflow: JOB-level if: on build:" "$WF" "$TMP/wf" \
'  build:
    name: type-check' '  build:
    if: ${{ false }}
    name: type-check'
mutate "workflow: step-level continue-on-error" "$WF" "$TMP/wf" \
'      - name: Bundle budget
        run:' '      - name: Bundle budget
        continue-on-error: true
        run:'
mutate "workflow: JOB-level continue-on-error" "$WF" "$TMP/wf" \
'  build:
    name: type-check' '  build:
    continue-on-error: true
    name: type-check'
mutate "workflow: || true appended to the gate" "$WF" "$TMP/wf" \
'        run: npm run check:bundle' '        run: npm run check:bundle || true'
mutate "workflow: gate step commented out" "$WF" "$TMP/wf" \
'      - name: Bundle budget
        run: npm run check:bundle' '      # - name: Bundle budget
      #   run: npm run check:bundle'
mutate "workflow: --dist bypass wired into CI" "$WF" "$TMP/wf" \
'        run: npm run check:bundle' '        run: npm run check:bundle --dist dist'
mutate "workflow: job renamed (required context would hang forever)" "$WF" "$TMP/wf" \
'    name: type-check + unit tests + build' '    name: type-check + unit tests + build RENAMED'
mutate "workflow: paths: filter reintroduced (#326)" "$WF" "$TMP/wf" \
'  pull_request:' '  pull_request:
    paths:
      - "frontend/**"'

echo
echo "--- vitest selection: can the scripts suite be silently deselected? ---"
mutate "select: exclude scripts/** (include left untouched)" "$VC" "$TMP/vc" \
'    exclude: ["e2e/**", "node_modules/**"],' '    exclude: ["e2e/**", "node_modules/**", "scripts/**"],'
mutate "select: narrow the glob extension (the text still matches)" "$VC" "$TMP/vc" \
'"scripts/**/*.{test,spec}.mjs"' '"scripts/**/*.{test,spec}.ts"'
mutate "select: comment the real glob out, leaving the text present" "$VC" "$TMP/vc" \
'    include: ["src/**/*.{test,spec}.{ts,tsx}", "scripts/**/*.{test,spec}.mjs"],' \
'    // include: ["src/**/*.{test,spec}.{ts,tsx}", "scripts/**/*.{test,spec}.mjs"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],'
mutate "select: a projects key takes over selection" "$VC" "$TMP/vc" \
'    include: ["src/**/*.{test,spec}.{ts,tsx}", "scripts/**/*.{test,spec}.mjs"],' \
'    include: ["src/**/*.{test,spec}.{ts,tsx}", "scripts/**/*.{test,spec}.mjs"],
    projects: [{ test: { name: "app", include: ["src/**/*.{test,spec}.{ts,tsx}"] } }],'
mutate "select: plain revert of the glob (the control case)" "$VC" "$TMP/vc" \
'"scripts/**/*.{test,spec}.mjs"' '"scriptsNOPE/**/*.{test,spec}.mjs"'

echo
echo "--- argv: narrowing the run without touching vite.config.ts ---"
mutate "argv: npm test narrowed to src" "$PKG" "$TMP/pkg" \
'"test": "vitest run"' '"test": "vitest run src"'
mutate "argv: the workflow passes a path to npm test" "$WF" "$TMP/wf" \
'        run: npm test' '        run: npm test -- src'

echo
echo "=== KILLED: $KILLED    SURVIVED/ERROR: $SURVIVED ==="
cmp -s "$PURE" "$TMP/pure" && cmp -s "$RUN" "$TMP/run" && cmp -s "$PKG" "$TMP/pkg" \
  && cmp -s "$VC" "$TMP/vc" && cmp -s "$WF" "$TMP/wf" \
  && echo "all files restored byte-identical" || { echo "TREE DIRTY -- restore by hand"; exit 97; }
[ "$SURVIVED" -eq 0 ]
