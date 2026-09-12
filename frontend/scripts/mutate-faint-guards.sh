#!/bin/bash
#
# Mutation harness for the #396 `--faint` guards.
#
# WHY THIS IS IN THE REPO. "the guard bites" in a PR body is a claim nobody can
# check; the bundle-budget (#323), focus-trap (#331) and focus-ring (#355) work
# each ship one, and this matches them. Run it with:
#
#     bash frontend/scripts/mutate-faint-guards.sh
#
# WHY IT DRIVES TWO RUNNERS. The #396 guard is deliberately two halves that see
# different things, so a single-runner harness would prove only one of them:
#
#   - `scripts/faint-contrast.test.mjs` (vitest) parses the STYLESHEET and the
#     SOURCES. It is the only half that can see a `.css` file at all —
#     `vite.config.ts` sets `css: false` for vitest, so no unit test in this
#     repo observes a computed style.
#   - `tests/faint-contrast.spec.ts` (Playwright) reads the RENDERED page. It is
#     the only half that can see the EFFECTIVE painted colour: an inherited font
#     size, a real painted backdrop, a runtime-built value that appears in no
#     source as a literal, and `-webkit-text-fill-color` overriding `color`.
#
# NOT "the only half that sees an inline style". Both halves catch a `Hero.tsx`
# inline revert, which is why the two `tsx:` mutants below are run under BOTH
# runners rather than under `pw` alone: the browser half sees it RENDERED, the
# source scan sees it in the FILE. An earlier version of this header, and of the
# CHANGELOG, claimed the browser half was the only net over it. That was
# measured and it is false.
#
# Discipline this encodes, learned from this repo's own near-misses:
#   - assert the mutation ACTUALLY APPLIED; a no-op edit reads as a survivor
#   - measure by EXIT CODE, never by grepping output for "FAIL"
#   - byte-copy restore and verify with cmp after EVERY mutation
#   - run sequentially in one tree; parallel writers corrupt each other
#   - a CONTROL RUN FIRST, or a harness whose every run fails for an unrelated
#     reason (a busy port, a broken dev server) reports every mutant killed
#   - report survivors BY NAME. "KILLED: 41 SURVIVED: 0" is the counted
#     phrasing this repo forbids, and this script printed exactly that until a
#     review caught it. A count cannot be checked; a name can.
#
# DIRTY-TREE POLICY, AND ITS ONE DOCUMENTED ESCAPE. Like the focus-ring harness
# this refuses to start on a dirty tree, so an interrupted run can never be
# confused with your own edits. The #396 change itself is delivered UNCOMMITTED
# for review, so `CV_ALLOW_DIRTY=1` exists to run it in that state. It is not a
# loosening of the safety property: what actually protects your work is the
# pristine byte-copy taken before the first mutant and the `cmp -s` after every
# single one, and any failed restore ABORTS the run with exit 98 rather than
# continuing. The git check is the belt; the byte-copy is the braces.
#
# THE DIRTY CHECK USES `git status --porcelain`, NOT `git diff --quiet`.
# `git diff` compares the index to the working tree and says NOTHING about a
# file git has never seen: it exits 0 for an UNTRACKED path. Four of the nine
# files guarded here are untracked while #396 is in review, so the refusal
# silently did not apply to them — the safety check was off for exactly the
# files this change created.
#
# PW_CONFIG exists because port 3000 is not always free on a developer's
# machine (an unrelated local server makes every run fail identically, which
# reads as "everything killed" and proves nothing):
#
#     PW_CONFIG=pwlocal.config.ts CV_ALLOW_DIRTY=1 bash scripts/mutate-faint-guards.sh
#
# CI uses the default, which is the config the required check runs.
set -u
cd "$(dirname "$0")/.." || exit 99
PW_CONFIG=${PW_CONFIG:-playwright.demo-ci.config.ts}
CV_ALLOW_DIRTY=${CV_ALLOW_DIRTY:-0}
S=$(mktemp -d)

FILES="LANDING HERO TOKENS ANALYZER GUARD SPEC KIT FIDELITY VITECONF PWCONF"
LANDING=src/styles/landing.css
HERO=src/components/Hero.tsx
TOKENS=src/styles/tokens.css
ANALYZER=scripts/faint-contrast.mjs
GUARD=scripts/faint-contrast.test.mjs
SPEC=tests/faint-contrast.spec.ts
# #403 moved the browser sweep OUT of the spec and into a kit both specs
# import. Thirteen mutants below still named $SPEC after that move, so their
# anchors matched nothing — and `one` reports an unmatched anchor as ANCHOR
# MISSING, which lands in the survivor list. Mutants that cannot APPLY are
# exactly how a dead guard keeps looking alive, so they now name $KIT.
# The suite is still `pw` (faint-contrast.spec.ts): it calls `installKit`,
# so it is still the runner that must catch a sabotaged sweep.
KIT=tests/contrast-kit.ts
FIDELITY=tests/fidelity.spec.ts
VITECONF=vite.config.ts
PWCONF=playwright.config.ts

# Restore on INT/TERM as well as EXIT. With only an EXIT trap, Ctrl-C during a
# Playwright run leaves a MUTATION in the tree and then deletes the pristine
# copies on the way out — untrue in exactly the situation where you most need
# it. `restore_all` is idempotent and runs before the temp dir goes.
restore_all () {
  for f in $FILES; do
    eval "t=\$$f"
    [ -f "$S/p.$f" ] && cp "$S/p.$f" "$t" 2>/dev/null
  done
}
trap 'restore_all; rm -rf "$S"' EXIT INT TERM

for f in $FILES; do
  eval "t=\$$f"
  if [ "$CV_ALLOW_DIRTY" != "1" ] && [ -n "$(git status --porcelain -- "$t")" ]; then
    echo "refusing to run: $t has uncommitted or untracked changes."
    echo "commit or stash first, so a restore cannot lose your work,"
    echo "or set CV_ALLOW_DIRTY=1 (see the header) to run against the working tree."
    exit 1
  fi
  cp "$t" "$S/p.$f"
done
[ "$CV_ALLOW_DIRTY" = "1" ] && echo "!! CV_ALLOW_DIRTY=1 — running against a DIRTY tree; restores are byte-verified."
K=0
SURVIVORS=""

# --- the suites this harness can run ----------------------------------------
# `unit` = the static half.  `sel` = the vitest-selection guard that keeps
# `unit` in the run at all.  `pin` = the Playwright-selection guard that keeps
# the BROWSER half in the run.  `pw` = the browser half.  `fid` = the two
# fidelity assertions #396 legitimately flipped.
run () {
  case "$1" in
    unit) npx vitest run scripts/faint-contrast.test.mjs >"$S/out" 2>&1 ;;
    sel)  npx vitest run src/test/buildGuards.test.ts -t "faint" >"$S/out" 2>&1 ;;
    pin)  npx vitest run src/test/buildGuards.test.ts -t "Playwright selects the specs" >"$S/out" 2>&1 ;;
    pw)   npx playwright test -c "$PW_CONFIG" faint-contrast.spec.ts --reporter=line >"$S/out" 2>&1 ;;
    fid)  npx playwright test -c "$PW_CONFIG" fidelity.spec.ts --reporter=line >"$S/out" 2>&1 ;;
    *)    echo "unknown suite $1" >&2; return 97 ;;
  esac
  echo $?
}

# WHICH tests died, not just "something did". ADVISORY ONLY — the verdict below
# comes from the exit code, never from this. Two shapes, because the two runners
# print differently: Playwright's `--reporter=line` numbers its failure blocks,
# vitest prints ` FAIL  <file> > <describe> > <test>`.
killers () {
  {
    grep -aoE '^[[:space:]]+[0-9]+\) .*›.*$' "$S/out" 2>/dev/null | sed 's/.*› //'
    grep -aoE '^[[:space:]]*FAIL[[:space:]]+.*$' "$S/out" 2>/dev/null | sed 's/.*> //'
  } | cut -c1-70 | sort -u | head -4 | tr '\n' ';'
}

survived () {  # label
  SURVIVORS="${SURVIVORS}
  - $1"
}

one () {  # label suite file pristine old new
  local label="$1" suite="$2" t="$3" p="$4" ANCHORS
  # Prints the number of times the anchor matched, so the caller can tell
  # "matched once" from "matched three times and we edited one of them".
  ANCHORS=$(OLD="$5" NEW="$6" T="$t" python3 -c "
import os,sys
t=os.environ['T'];old=os.environ['OLD'];new=os.environ['NEW']
s=open(t).read()
n=s.count(old)
if n==0: sys.exit(3)
open(t,'w').write(s.replace(old,new,1))
print(n)
")
  # All three early returns verify the restore too, rather than `cp`-and-trust:
  # a failed restore on an anchor typo would otherwise leave the tree dirty and
  # be reported as a mere harness warning.
  if [ $? -ne 0 ]; then
    echo "!! ANCHOR MISSING: $label"; cp "$p" "$t"
    cmp -s "$t" "$p" || { echo "!! RESTORE FAILED after anchor miss: $label"; exit 98; }
    survived "ANCHOR MISSING (harness fault, not a guard result): $label"; return
  fi
  # AMBIGUOUS ANCHOR: `replace(..., 1)` would edit whichever match came first,
  # so the mutant lands somewhere other than where its label says and the
  # verdict is about code nobody chose. Same class as ANCHOR MISSING — a result
  # you cannot trust — so it is reported the same loud way rather than silently
  # mutating site 1 of N.
  if [ "$ANCHORS" -gt 1 ]; then
    echo "!! ANCHOR AMBIGUOUS ($ANCHORS matches): $label"; cp "$p" "$t"
    cmp -s "$t" "$p" || { echo "!! RESTORE FAILED after ambiguous anchor: $label"; exit 98; }
    survived "ANCHOR AMBIGUOUS x$ANCHORS (harness fault, not a guard result): $label"; return
  fi
  if cmp -s "$t" "$p"; then
    echo "!! NO-OP: $label"; cp "$p" "$t"
    cmp -s "$t" "$p" || { echo "!! RESTORE FAILED after no-op: $label"; exit 98; }
    survived "NO-OP (harness fault, not a guard result): $label"; return
  fi
  local c who; c=$(run "$suite"); who=$(killers)
  cp "$p" "$t"; cmp -s "$t" "$p" || { echo "!! RESTORE FAILED: $label"; exit 98; }
  if [ "$c" -ne 0 ]; then
    echo "KILLED    <- [$suite] $label"
    echo "             by (advisory): ${who:-<no test named; check $S/out>}"
    K=$((K+1))
  else
    echo "SURVIVED! <- [$suite] $label"; survived "[$suite] $label"
  fi
}

echo "=== control: every suite must be GREEN on the unmutated tree ==="
for s in unit sel pin pw fid; do
  c=$(run "$s")
  if [ "$c" -ne 0 ]; then
    echo "!! suite '$s' is already failing unmutated — nothing below means anything."
    sed -n '1,40p' "$S/out"
    exit 96
  fi
  echo "control OK: $s"
done
echo

echo "=== the defect itself, put back one rule at a time ==="
one "css: .composer-hint back on --faint (the rule #396 was filed against)" unit "$LANDING" "$S/p.LANDING" \
'.composer-hint {
  margin: 10px 0 0;
  text-align: center;
  font-size: 11.5px;
  color: var(--muted);' '.composer-hint {
  margin: 10px 0 0;
  text-align: center;
  font-size: 11.5px;
  color: var(--faint);'
one "css: .composer-hint back on --faint, seen by the BROWSER half" pw "$LANDING" "$S/p.LANDING" \
'.composer-hint {
  margin: 10px 0 0;
  text-align: center;
  font-size: 11.5px;
  color: var(--muted);' '.composer-hint {
  margin: 10px 0 0;
  text-align: center;
  font-size: 11.5px;
  color: var(--faint);'
one "css: .step-number back on --faint (24px/700 — LARGE text, and still short)" pw "$LANDING" "$S/p.LANDING" \
'  font-size: 24px;
  font-weight: 700;
  color: var(--muted);' '  font-size: 24px;
  font-weight: 700;
  color: var(--faint);'
one "css: .chat-input::placeholder back on --faint (no font-size of its own)" fid "$LANDING" "$S/p.LANDING" \
'.chat-input::placeholder {
  color: var(--muted);
}' '.chat-input::placeholder {
  color: var(--faint);
}'
# The anchor is the WHOLE `.mono-label` block, not its last two lines:
# `text-transform: uppercase; color: var(--muted)` also matches `.status-badge`
# FIRST, which was already --muted before #396, so the short anchor would have
# mutated an innocent rule and proved nothing about the one under test.
one "css: .mono-label back on --faint (the fidelity assertion #396 flipped)" fid "$LANDING" "$S/p.LANDING" \
'.mono-label {
  font-family: "JetBrains Mono", monospace;
  font-size: 11.5px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted);' '.mono-label {
  font-family: "JetBrains Mono", monospace;
  font-size: 11.5px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--faint);'
one "css: a NEW --faint rule nested inside an @media block" unit "$LANDING" "$S/p.LANDING" \
'@media (max-width: 900px) {' '@media (max-width: 900px) {
  .cv396-nested { color: var(--faint); }'

echo
echo "=== the two FULL BYPASSES an adversarial review found, both measured green ==="
# BYPASS 1. `-webkit-text-fill-color` overrides `color` for the glyph fill while
# `getComputedStyle(el).color` keeps reporting the old value. Measured on
# `.composer-hint`: color read rgb(107,104,98) = 5.27:1 while the glyphs painted
# rgb(154,151,143) = 2.77:1, and the static suite, the browser suite and
# `npm run lint:css` were ALL green.
one "css: -webkit-text-fill-color on .composer-hint (STATIC half must see it)" unit "$LANDING" "$S/p.LANDING" \
'.composer-hint {
  margin: 10px 0 0;
  text-align: center;
  font-size: 11.5px;
  color: var(--muted);
}' '.composer-hint {
  margin: 10px 0 0;
  text-align: center;
  font-size: 11.5px;
  color: var(--muted);
  -webkit-text-fill-color: var(--faint);
}'
one "css: -webkit-text-fill-color on .composer-hint (BROWSER half must see it)" pw "$LANDING" "$S/p.LANDING" \
'.composer-hint {
  margin: 10px 0 0;
  text-align: center;
  font-size: 11.5px;
  color: var(--muted);
}' '.composer-hint {
  margin: 10px 0 0;
  text-align: center;
  font-size: 11.5px;
  color: var(--muted);
  -webkit-text-fill-color: var(--faint);
}'
# BYPASS 2. `VAR(--faint)` inside `@media (max-width: 600px)`. TWO independent
# causes: a case-sensitive `var` match in the analyzer, and a browser sweep that
# only ever ran at a desktop viewport. Both are mutated here, separately, so
# neither fix can be removed while the other covers for it.
one "css: VAR(--faint) uppercase, in a media query (STATIC half)" unit "$LANDING" "$S/p.LANDING" \
'@media (max-width: 600px) {
  .hero-title {' '@media (max-width: 600px) {
  .composer-hint { color: VAR(--faint); }

  .hero-title {'
one "css: VAR(--faint) uppercase, in a media query (BROWSER half, phone viewport)" pw "$LANDING" "$S/p.LANDING" \
'@media (max-width: 600px) {
  .hero-title {' '@media (max-width: 600px) {
  .composer-hint { color: VAR(--faint); }

  .hero-title {'
# BYPASS 3. A CSS IDENTIFIER ESCAPE in the function name, inside a media query
# the browser sweep cannot reach. This is the one a skeptic found in the FIX for
# bypass 2: `[vV][aA][rR]` still anchored on the SPELLING of `var`, and
# `\76 ar(`, `v\61 r(` and `var(/*c*/` are all spellings of it that Chromium
# renders. Planted inside `@media (prefers-reduced-motion: reduce)` — which the
# spec never emulates — it passed the static suite, stylelint AND the browser
# suite at once. The fix stopped matching the function name at all.
one "css: an ESCAPED var(), in a media query the sweep cannot reach" unit "$LANDING" "$S/p.LANDING" \
'@media (prefers-reduced-motion: reduce) {' '@media (prefers-reduced-motion: reduce) {
  .composer-hint { color: \76 ar(--faint); }
'
one "css: a comment INSIDE var(), which postcss keeps in the value" unit "$LANDING" "$S/p.LANDING" \
'@media (prefers-reduced-motion: reduce) {' '@media (prefers-reduced-motion: reduce) {
  .composer-hint { color: var(/*c*/--faint); }
'
# ANCHORED ON THE FOLLOWING LINE. `Hero.tsx` has TWO identical
# `color: "var(--muted)",` lines (the TRY: label and the > glyph), so the bare
# anchor matched both and `replace(...,1)` silently picked whichever came first
# — a mutant that lands somewhere other than where its label says. The 15px
# line below it belongs to the glyph span alone.
one "tsx: an ESCAPED var() inline style (source scan)" unit "$HERO" "$S/p.HERO" \
'                color: "var(--muted)",
                fontSize: "15px",' \
'                color: "\76 ar(--faint)",
                fontSize: "15px",'
one "analyzer: match `var` case-SENSITIVELY again (bypass 2, cause 1)" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return /--faint(?![\w-])/.test(value);' \
'  return /var\(\s*--faint(?![\w-])/.test(value);'
one "analyzer: anchor on the SPELLING of var again (bypass 3 — CSS escapes)" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return /--faint(?![\w-])/.test(value);' \
'  return /[vV][aA][rR]\(\s*--faint(?![\w-])/.test(value);'
one "analyzer: match --faint case-INSENSITIVELY (--FAINT is a DIFFERENT property)" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return /--faint(?![\w-])/.test(value);' \
'  return /--faint(?![\w-])/i.test(value);'
one "analyzer: drop the lookahead, so --faint-x reads as a violation" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return /--faint(?![\w-])/.test(value);' \
'  return /--faint/.test(value);'
one "analyzer: anchor the TSX scan on the spelling of var again" unit "$ANALYZER" "$S/p.ANALYZER" \
'  const re = /\([^)"'"'"'`]{0,40}--faint(?![\w-])[^)]*\)/g;' \
'  const re = /[vV][aA][rR]\(\s*--faint(?![\w-])[^)]*\)/g;'
one "analyzer: let the TSX gap swallow quotes, so PROSE reads as a violation" unit "$ANALYZER" "$S/p.ANALYZER" \
'  const re = /\([^)"'"'"'`]{0,40}--faint(?![\w-])[^)]*\)/g;' \
'  const re = /\([^)]{0,40}--faint(?![\w-])[^)]*\)/g;'
one "analyzer: blanket `i` flag, which wrongly reports the DIFFERENT --FAINT property" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return /--faint(?![\w-])/.test(value);' \
'  return /var\(\s*--faint(?![\w-])/i.test(value);'
one "spec: never leave the desktop viewport (bypass 2, cause 2)" pw "$SPEC" "$S/p.SPEC" \
'      await page.setViewportSize(PHONE);' ''
one "spec: sweep the phone viewport but skip the chat screen, where .composer-hint lives" pw "$SPEC" "$S/p.SPEC" \
'      await enterChat(page);
      expect(
        await page.evaluate(() => window.__cv396.count(".composer-hint")),' \
'      expect(
        await page.evaluate(() => window.__cv396.count(".composer-hint")),'
one "analyzer: drop -webkit-text-fill-color from the glyph-painting set (bypass 1)" unit "$ANALYZER" "$S/p.ANALYZER" \
'const TEXT_PAINT_PROPS = new Set(["color", "-webkit-text-fill-color"]);' \
'const TEXT_PAINT_PROPS = new Set(["color"]);'
one "analyzer: drop plain `color` from the glyph-painting set" unit "$ANALYZER" "$S/p.ANALYZER" \
'const TEXT_PAINT_PROPS = new Set(["color", "-webkit-text-fill-color"]);' \
'const TEXT_PAINT_PROPS = new Set(["-webkit-text-fill-color"]);'
one "analyzer: violation sweep narrows back to plain `color` only" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return findTextPaintDeclarations(cssText).filter((d) => usesFaint(d.value));' \
'  return findColorDeclarations(cssText).filter((d) => usesFaint(d.value));'
one "analyzer: WIDEN the narrow population, so the two counters stop agreeing" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return declarationsWhere(cssText, (prop) => prop === "color");' \
'  return declarationsWhere(cssText, (prop) => TEXT_PAINT_PROPS.has(prop));'
one "kit: sweep reads `color` only, never the text-fill override (bypass 1)" pw "$KIT" "$S/p.KIT" \
'            } else if (same(parse(s.webkitTextFillColor), rgb)) {
              offenders.push(`${describe(el, p ? p : "")} [-webkit-text-fill-color]`);
            }' '            }'

echo
echo "=== the INLINE styles — caught by BOTH halves, which is the point ==="
one "tsx: the TRY: label back on --faint (source scan)" unit "$HERO" "$S/p.HERO" \
'                fontSize: "11px",
                color: "var(--muted)",' '                fontSize: "11px",
                color: "var(--faint)",'
one "tsx: the TRY: label back on --faint (BROWSER sweep)" pw "$HERO" "$S/p.HERO" \
'                fontSize: "11px",
                color: "var(--muted)",' '                fontSize: "11px",
                color: "var(--faint)",'
one "tsx: the ▸ glyph back on --faint (source scan)" unit "$HERO" "$S/p.HERO" \
'                color: "var(--muted)",
                fontSize: "15px",' '                color: "var(--faint)",
                fontSize: "15px",'
one "tsx: the ▸ glyph back on --faint (BROWSER sweep)" pw "$HERO" "$S/p.HERO" \
'                color: "var(--muted)",
                fontSize: "15px",' '                color: "var(--faint)",
                fontSize: "15px",'
one "tsx: an UPPERCASE VAR(--faint) inline style (source scan)" unit "$HERO" "$S/p.HERO" \
'                fontSize: "11px",
                color: "var(--muted)",' '                fontSize: "11px",
                color: "VAR(--faint)",'

echo
echo "=== the CTA banner's invisible sentence (1.00:1 in BOTH themes) ==="
# NOT a --faint finding. The <p> had no rule at all, so it inherited
# `.cta-banner`'s `color: var(--ink)` onto its `background: var(--ink)`.
one "css: remove the rule that stops the CTA sentence painting in its own background" pw "$LANDING" "$S/p.LANDING" \
'.cta-banner > p:not(.cta-footnote) {
  color: var(--bg);
}' ''
one "css: point the CTA sentence back at --ink explicitly" pw "$LANDING" "$S/p.LANDING" \
'.cta-banner > p:not(.cta-footnote) {
  color: var(--bg);
}' '.cta-banner > p:not(.cta-footnote) {
  color: var(--ink);
}'

echo
echo "=== META: can the STATIC analyzer be made to check nothing? ==="
one "analyzer: report no violations at all" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return findTextPaintDeclarations(cssText).filter((d) => usesFaint(d.value));' \
'  return [];'
one "analyzer: stop at the FIRST violation" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return findTextPaintDeclarations(cssText).filter((d) => usesFaint(d.value));' \
'  return findTextPaintDeclarations(cssText).filter((d) => usesFaint(d.value)).slice(0, 1);'
one "analyzer: skip the first collected declaration (an off-by-one start index)" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return findTextPaintDeclarations(cssText).filter((d) => usesFaint(d.value));' \
'  return findTextPaintDeclarations(cssText).slice(1).filter((d) => usesFaint(d.value));'
one "analyzer: bail out after 5 declarations (the completeness partner must bite)" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return found;
}' '  return found.slice(0, 5);
}'
one "analyzer: point the detector at the WRONG token" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return /--faint(?![\w-])/.test(value);' \
'  return /[vV][aA][rR]\(\s*--nosuchtoken(?![\w-])/.test(value);'
one "analyzer: accept --faint-x too (the lookahead becomes a word boundary)" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return /--faint(?![\w-])/.test(value);' \
'  return /[vV][aA][rR]\(\s*--faint\b/.test(value);'
one "analyzer: report border-color and background-color as text violations" unit "$ANALYZER" "$S/p.ANALYZER" \
'    if (!keep(prop)) return;' \
'    if (!prop.includes("color")) return;'
one "analyzer: blank string literals in the TSX scan (an inline style IS a string)" unit "$ANALYZER" "$S/p.ANALYZER" \
'  const scrubbed = blankOut(sourceText, { js: true });' \
'  const scrubbed = blankOut(sourceText, { js: true, strings: true });'
one "analyzer: stop blanking comments, so a TSX comment reads as a violation" unit "$ANALYZER" "$S/p.ANALYZER" \
'  const scrubbed = blankOut(sourceText, { js: true });' \
'  const scrubbed = sourceText;'
one "analyzer: collapse comments instead of blanking them (line numbers shift)" unit "$ANALYZER" "$S/p.ANALYZER" \
'    for (let k = from; k < to && k < n; k++) if (out[k] !== "\n") out[k] = " ";' \
'    for (let k = from; k < to && k < n; k++) out[k] = "";'
one "analyzer: make the second counter agree by construction (no independence)" unit "$ANALYZER" "$S/p.ANALYZER" \
'  const scrubbed = blankOut(cssText, { strings: true });
  const m = scrubbed.match(/(?:^|[;{}])\s*color\s*:/gim);
  return m ? m.length : 0;' \
'  return findColorDeclarations(cssText).length;'
one "analyzer: parse with a REGEX instead of postcss (comments and strings fool it)" unit "$ANALYZER" "$S/p.ANALYZER" \
'  const root = postcss.parse(cssText);
  const found = [];
  root.walkDecls((decl) => {
    const prop = decl.prop.toLowerCase();
    if (!keep(prop)) return;
    found.push({
      selector: ownerSelector(decl),
      prop,
      line: decl.source?.start?.line ?? 0,
      value: decl.value.trim(),
      atRules: atRuleChain(decl),
    });
  });
  return found;' \
'  const found = [];
  const re = /(?:^|[;{}])\s*color\s*:([^;}]*)/gi;
  let m;
  while ((m = re.exec(cssText)) !== null) {
    found.push({ selector: "?", prop: "color", line: 1, value: m[1].trim(), atRules: [] });
  }
  return found;'
one "analyzer: ignore any declaration nested in an @rule (@media invisible)" unit "$ANALYZER" "$S/p.ANALYZER" \
'    if (!keep(prop)) return;' \
'    if (!keep(prop)) return;
    if (atRuleChain(decl).length) return;'
one "analyzer: pre-filter to --faint, so the COMPLIANT declarations vanish" unit "$ANALYZER" "$S/p.ANALYZER" \
'    if (!keep(prop)) return;' \
'    if (!keep(prop) || !usesFaint(decl.value)) return;'
one "analyzer: match the literal substring instead of the pattern (whitespace missed)" unit "$ANALYZER" "$S/p.ANALYZER" \
'  return /--faint(?![\w-])/.test(value);' \
'  return value.includes("var(--faint)");'
one "analyzer: report line 0 for every declaration" unit "$ANALYZER" "$S/p.ANALYZER" \
'      line: decl.source?.start?.line ?? 0,' '      line: 0,'
one "analyzer: drop the case-insensitive flag, so COLOR: disagrees with postcss" unit "$ANALYZER" "$S/p.ANALYZER" \
'  const m = scrubbed.match(/(?:^|[;{}])\s*color\s*:/gim);' \
'  const m = scrubbed.match(/(?:^|[;{}])\s*color\s*:/gm);'

echo
echo "=== META: can the GUARD FILE be made to check nothing? ==="
one "guard: sweep no stylesheets (an empty file list reports zero offenders)" unit "$GUARD" "$S/p.GUARD" \
'const CSS_FILES = [
  ...walk(join(frontendRoot, "src"), [".css"]),
  join(frontendRoot, "public", "about.css"),
];' 'const CSS_FILES = [];'
one "guard: sweep no sources, so the inline half covers nothing" unit "$GUARD" "$S/p.GUARD" \
'const SOURCE_FILES = walk(join(frontendRoot, "src"), [".tsx", ".ts"]);' \
'const SOURCE_FILES = [];'
# The named-member partner for the mutant above. `>= 20` had 46 files of slack
# (66 measured), so this filter left 65 files behind and passed the old floor.
one "guard: sweep every source EXCEPT the one the guard was built for" unit "$GUARD" "$S/p.GUARD" \
'const SOURCE_FILES = walk(join(frontendRoot, "src"), [".tsx", ".ts"]);' \
'const SOURCE_FILES = walk(join(frontendRoot, "src"), [".tsx", ".ts"]).filter(
  (f) => !f.endsWith("Hero.tsx"),
);'
# The DERIVED-SET partner. A hardcoded shortlist of selectors made this test
# fail for the WRONG REASON under the uppercase bypass; the set comparison is
# what a stubbed analyzer cannot satisfy. The mutant makes the partner's own
# swap a no-op, which is the shape that would turn it vacuous.
one "guard: the detector-still-works partner swaps nothing, so it detects nothing" unit "$GUARD" "$S/p.GUARD" \
'    const mutated = landingText.replaceAll("var(--muted)", "var(--faint)");' \
'    const mutated = landingText;'

echo
echo "=== META: can the BROWSER sweep be made to check nothing? ==="
# BOTH branches, not just the `color` one: the text-fill branch would otherwise
# catch every planted offender on its own (an unset `-webkit-text-fill-color`
# resolves TO `color`) and this mutant would survive while looking decisive.
one "kit: the sweep reports no offender, whatever it sees" pw "$KIT" "$S/p.KIT" \
'            if (same(parse(s.color), rgb)) {
              offenders.push(describe(el, p ? p : ""));
            } else if (same(parse(s.webkitTextFillColor), rgb)) {
              offenders.push(`${describe(el, p ? p : "")} [-webkit-text-fill-color]`);
            }' ''
# WIDENED, not just repointed: the kit holds TWO sweeps and both open with this
# exact line, so the bare anchor is ambiguous and `replace(...,1)` would pick
# whichever came first. The comment above it is unique to the #396 sweep.
one "kit: the sweep walks nothing (non-vacuity floor must bite)" pw "$KIT" "$S/p.KIT" \
'        // inflate the non-vacuity number this test reports.
        const all: Element[] = [document.body, ...Array.from(document.body.querySelectorAll("*"))];' \
'        // inflate the non-vacuity number this test reports.
        const all: Element[] = [];'
# WIDENED for the same reason: both sweeps push ::placeholder the same way.
# The single-line `pseudos` declaration belongs to the #396 sweep alone.
one "kit: never visit pseudo-elements, so ::placeholder is invisible" pw "$KIT" "$S/p.KIT" \
'          const pseudos: (string | undefined)[] = [undefined, "::before", "::after"];
          if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
            pseudos.push("::placeholder");
          }' \
'          const pseudos: (string | undefined)[] = [undefined, "::before", "::after"];'
# NOT `if (false)` on the `missing.push` line: every probe selector matches
# today, so that branch never runs and the mutant would be EQUIVALENT. Renaming
# a probe is what actually produces the zero-match case the check exists for.
one "spec: a probe class is renamed, so its sweep silently measures nothing" pw "$SPEC" "$S/p.SPEC" \
'  { selector: ".footer-copy", why: "the footer copyright line" },' \
'  { selector: ".footer-copy-renamed", why: "the footer copyright line" },'
# Likewise NOT `if (false)` on the ratio comparison: nothing is below 4.5:1
# today, so suppressing the branch changes no result. Raising the floor to an
# impossible value proves the comparison is EVALUATED; the "composite over
# BLACK" mutant below proves it can actually fail on a real measurement.
one "spec: raise the floor to an impossible 21:1 (is the ratio even compared?)" pw "$SPEC" "$S/p.SPEC" \
'const AA_SMALL_TEXT = 4.5;' 'const AA_SMALL_TEXT = 21;'
one "kit: assume every colour is opaque (alpha never recovered)" pw "$KIT" "$S/p.KIT" \
'          const a = 1 - (w[0] - k[0] + (w[1] - k[1]) + (w[2] - k[2])) / 3 / 255;' \
'          const a = 1;'
one "kit: score the text colour opaque instead of compositing its alpha" pw "$KIT" "$S/p.KIT" \
'  const a = colour[3] ?? 1;
  const over = [0, 1, 2].map((i) => Math.round(a * colour[i] + (1 - a) * backdrop[i]));
  return contrastRatio(over, backdrop);' \
'  return contrastRatio(colour.slice(0, 3).map(Math.round), backdrop);'
one "spec: fold alpha into the rounded rgb check, where Math.round(0.5) hides it" pw "$SPEC" "$S/p.SPEC" \
'  const alphaMatches = Math.abs((colour[3] ?? 1) - 1) < 0.01;' \
'  const alphaMatches = Math.round(colour[3] ?? 1) === 1;'
one "spec: accept ANY colour as the token (the rgb comparison stops comparing)" pw "$SPEC" "$S/p.SPEC" \
'  const rgbMatches =
    colour.slice(0, 3).map(Math.round).join() === want.slice(0, 3).map(Math.round).join();' \
'  const rgbMatches = true;'
one "kit: drop the second sentinel, so an unparseable colour reads as red" pw "$KIT" "$S/p.KIT" \
'        ctx.fillStyle = "#00ff00";
        ctx.fillStyle = css;
        const second = ctx.fillStyle;' '        const second = first;'
# RE-ANCHORED as well as repointed: prettier reflowed this onto two lines when
# it moved into the kit, so even the old file would no longer have matched it.
one "kit: stop reporting ancestors the compositor cannot model" pw "$KIT" "$S/p.KIT" \
'          if (s.backgroundImage !== "none")
            bad.push(`${name} background-image:${s.backgroundImage}`);' ''
# NEW FIXTURE, NEW MUTANT. The `backdrop-filter` push had no fixture, so
# deleting it was an EQUIVALENT mutation while the spec's RED-WHEN line claimed
# "any bad.push(...) removed" turns it red. Now it has one.
one "kit: stop reporting a backdrop-filter ancestor" pw "$KIT" "$S/p.KIT" \
'        if (s.backdropFilter && s.backdropFilter !== "none")
          bad.push(`${name} backdrop-filter:${s.backdropFilter}`);' ''
one "kit: never look at ::before/::after, the overlay shape this app SHIPS" pw "$KIT" "$S/p.KIT" \
'        for (const pseudo of ["::before", "::after"]) {' \
'        for (const pseudo of []) {'
one "kit: report a TEXT-ONLY pseudo as a blocker, which blocks every measurement" pw "$KIT" "$S/p.KIT" \
'          const paints = (pbg && pbg[3] > 0.001) || ps.backgroundImage !== "none";' \
'          const paints = true;'
# NO MUTANT for "composite over BLACK instead of white". It was RUN and it
# SURVIVED, and the reason is worth recording rather than deleting: the page has
# an opaque background, so the white base under <html> is covered on every
# element measured here and swapping it changes no answer. An EQUIVALENT mutant.
#
# THE REASON, CORRECTED. An earlier version of this note credited
# `reset.css`, whose `body` rule paints `var(--surface-base)` — a token only the
# DEAD `data-style` skins define, so it resolves to nothing here. The opaque
# background comes from `landing.css`'s own `body { background: var(--bg) }`
# (section 1, BASE), which exists precisely because `--surface-base` is
# undefined; its comment says so. The conclusion stands; the cause did not.
#
# The two below make the compositor genuinely wrong instead, and both bite in
# DARK mode (--muted on white is 2.20:1).
one "kit: never walk past the element itself (ancestor backgrounds ignored)" pw "$KIT" "$S/p.KIT" \
'      for (let n: Element | null = el; n; n = n.parentElement) {
        const c = parse(getComputedStyle(n).backgroundColor);' \
'      for (let n: Element | null = el; n; n = null) {
        const c = parse(getComputedStyle(n).backgroundColor);'
one "kit: collect no background layers at all (everything reads as page white)" pw "$KIT" "$S/p.KIT" \
'        if (c && c[3] > 0.001) layers.push(c);' '        if (false) layers.push(c!);'

echo
echo "=== META: are the two halves still SELECTED at all? ==="
one "vite.config: revert the scripts/** glob that puts the STATIC half in the run" sel "$VITECONF" "$S/p.VITECONF" \
'    include: ["src/**/*.{test,spec}.{ts,tsx}", "scripts/**/*.{test,spec}.mjs"],' \
'    include: ["src/**/*.{test,spec}.{ts,tsx}"],'
one "vite.config: exclude the STATIC half by name while leaving the glob intact" sel "$VITECONF" "$S/p.VITECONF" \
'    exclude: ["e2e/**", "node_modules/**"],' \
'    exclude: ["e2e/**", "node_modules/**", "scripts/faint-contrast.test.mjs"],'
# The BROWSER half had no pin at all: `buildGuards` pinned the static half's two
# `scripts/` files with `existsSync`, nothing pinned the spec, and the demo-CI
# selection differential (`196 == 218 - 22`) still balances when 16 tests vanish
# from BOTH sides of it. `testIgnore` is the text-editable form of that
# deletion — the pin reads Playwright's OWN resolved selection, so an actual
# `rm` of the file produces the identical failure.
one "playwright.config: deselect the BROWSER half entirely" pin "$PWCONF" "$S/p.PWCONF" \
'  testDir: "./tests",' \
'  testDir: "./tests",
  testIgnore: ["**/faint-contrast.spec.ts"],'
# #403's browser half, mutated the same way. Before the pin that this kills,
# deselecting `contrast-floor.spec.ts` was green on every gate in the repo:
# `buildGuards` named the file nowhere, and the demo-CI selection differential
# balances when a whole spec's tests leave both of its sides at once.
one "playwright.config: deselect the FLOOR SWEEP entirely" pin "$PWCONF" "$S/p.PWCONF" \
'  testDir: "./tests",' \
'  testDir: "./tests",
  testIgnore: ["**/contrast-floor.spec.ts"],'

echo
# NO MUTANT for "delete the --faint declaration from tokens.css". It would
# SURVIVE, and for a reason worth recording rather than hiding: after #396 the
# token has ZERO consumers, so removing it changes nothing any test can observe.
# That is the same fact that makes a `--faint`-counting non-vacuity partner
# vacuous by construction, and it is why this guard's partners are derived from
# the file instead. See the header of scripts/faint-contrast.test.mjs.
#
# NO MUTANT either for "stop scrolling the lazy strip into view before the
# sweep". On a desktop viewport the #358 sentinel already sits inside the
# observer's 400 px lead margin (measured in tests/lazy-strip.spec.ts), so the
# strip mounts on first paint and the mutant is EQUIVALENT here. It would bite
# on a phone viewport, which the phone sweep reaches only after the strip has
# already mounted in `beforeEach`.
if [ -z "$SURVIVORS" ]; then
  echo "=== KILLED: $K.  No survivor was observed in this run. ==="
else
  echo "=== KILLED: $K.  SURVIVORS, by name: ===$SURVIVORS"
fi
echo "(this list is NOT claimed to be exhaustive — it is the set that was run,"
echo " and 'no survivor observed' is a statement about these mutants only)"
ok=1
# Quoted: an unquoted "$S" inside eval mis-parses if TMPDIR contains a space and
# then reports every file DIRTY for a reason that has nothing to do with the run.
for f in $FILES; do
  eval "t=\$$f"
  cmp -s "$t" "$S/p.$f" || { echo "DIRTY: $f"; ok=0; }
done
[ $ok -eq 1 ] && echo "all restored byte-identical"

[ -z "$SURVIVORS" ]
