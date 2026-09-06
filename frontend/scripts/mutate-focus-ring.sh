#!/bin/bash
#
# Mutation harness for the #355 focus-ring guard.
#
# WHY THIS IS IN THE REPO. "the guard bites" in a PR body is a claim nobody can
# check; the bundle-budget (#323) and focus-trap (#331) work each ship one, and
# this matches them. Run it with:
#
#     bash frontend/scripts/mutate-focus-ring.sh
#
# A guard is only a guard if breaking the thing it guards turns a test red. Each
# mutation below breaks exactly one part of the focus ring and expects
# tests/focus-ring.spec.ts to FAIL.
#
# WHY IT DRIVES PLAYWRIGHT AND NOT VITEST. `vite.config.ts` sets `css: false`
# for vitest, so no unit test in this repo can observe a computed style — the
# defect this guards was invisible to all 482 of them. And a grep-style guard
# could not have caught it either: the `outline` declaration was PRESENT in
# reset.css the whole time and simply did not compute. Only a real browser can
# tell the difference, which makes these runs slow (~20 s each) and worth it.
#
# Discipline this encodes, learned from this repo's own near-misses:
#   - assert the mutation ACTUALLY APPLIED; a no-op edit reads as a survivor
#   - measure by EXIT CODE, never by grepping output for "FAIL"
#   - byte-copy restore and verify with cmp after EVERY mutation
#   - run sequentially in one tree; parallel writers corrupt each other
#
# It edits tracked files in place and restores them, so it refuses to start on a
# dirty tree — an interrupted run can then never be confused with your own edits.
#
# PW_CONFIG exists because port 3000 is not always free on a developer's
# machine (an unrelated local server will make every run fail identically,
# which reads as "43 killed" and proves nothing). Point it at a copy of
# playwright.demo-ci.config.ts on another port if you need to. CI uses the
# default, which is the config the required check runs.
set -u
cd "$(dirname "$0")/.." || exit 99
PW_CONFIG=${PW_CONFIG:-playwright.demo-ci.config.ts}
S=$(mktemp -d)
# Restore on INT/TERM as well as EXIT. With only an EXIT trap, Ctrl-C during a
# ~20 s Playwright run left a MUTATION in the tree and then deleted the pristine
# copies on the way out — so the header's "restores them" was untrue in exactly
# the situation where you most need it. `restore_all` is idempotent and runs
# before the temp dir goes.
restore_all () {
  for f in TOKENS RESET LANDING SPEC; do
    eval "t=\$$f"
    [ -f "$S/p.$f" ] && cp "$S/p.$f" "$t" 2>/dev/null
  done
}
trap 'restore_all; rm -rf "$S"' EXIT INT TERM

TOKENS=src/styles/tokens.css
RESET=src/styles/reset.css
LANDING=src/styles/landing.css
SPEC=tests/focus-ring.spec.ts

for f in TOKENS RESET LANDING SPEC; do
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
  npx playwright test -c "$PW_CONFIG" focus-ring.spec.ts --reporter=line >"$S/out" 2>&1
  echo $?
}

# WHICH tests died, not just "something did". A mutant killed by an unrelated
# test is a mutant that proves nothing about the guard you were aiming at — a
# reviewer made exactly that point about the exit-code-only version of this
# harness, having done the per-test attribution by hand.
# ONLY the numbered failure blocks. The first version grepped every "›" line out
# of --reporter=line output, and that reporter prints one progress line PER TEST
# THAT RAN, pass or fail — so on a fully GREEN run it happily named four
# "killers". A reviewer proved that by running it unmutated. Since the same 28
# tests run every time and the list was sorted, the attribution was very nearly
# constant regardless of which mutant was applied, which defeats the entire
# purpose stated above it.
killers () {
  grep -aoE '^[[:space:]]+[0-9]+\) .*›.*$' "$S/out" 2>/dev/null |
    sed 's/.*› //' | cut -c1-70 | sort -u | head -4 | tr '\n' ';'
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
  # Both early returns verify the restore too. They used to `cp` and trust it,
  # unlike the main path, so a failed restore on an anchor typo would have left
  # the tree dirty and been reported as a mere harness warning.
  if [ $? -ne 0 ]; then
    echo "!! ANCHOR MISSING: $label"; cp "$p" "$t"
    cmp -s "$t" "$p" || { echo "!! RESTORE FAILED after anchor miss: $label"; exit 98; }
    SV=$((SV+1)); return
  fi
  if cmp -s "$t" "$p"; then
    echo "!! NO-OP: $label"; cp "$p" "$t"
    cmp -s "$t" "$p" || { echo "!! RESTORE FAILED after no-op: $label"; exit 98; }
    SV=$((SV+1)); return
  fi
  local c who; c=$(run); who=$(killers)
  cp "$p" "$t"; cmp -s "$t" "$p" || { echo "!! RESTORE FAILED: $label"; exit 98; }
  if [ "$c" -ne 0 ]; then
    echo "KILLED    <- $label"
    echo "             by: ${who:-<no test named; check $S/out>}"
    K=$((K+1))
  else
    echo "SURVIVED! <- $label"; SV=$((SV+1))
  fi
}

# A control run FIRST. Without it, a harness in which every run fails for an
# unrelated reason (a busy port, a broken dev server) reports every mutant
# killed and proves nothing at all.
echo "=== control: the unmutated tree must be GREEN ==="
c=$(run)
if [ "$c" -ne 0 ]; then
  echo "!! the suite is already failing on an unmutated tree — nothing below means anything."
  sed -n '1,40p' "$S/out"
  exit 96
fi
echo "control OK"
echo

echo "=== the ring itself ==="
# The exact #355 defect, reintroduced. --accent is defined only under the three
# unreachable [data-style] skins.
one "ring: point it back at the undefined --accent" "$RESET" "$S/p.RESET" \
'  outline: 2px solid var(--focus-ring);' '  outline: 2px solid var(--accent);'
one "ring: drop the token definition entirely" "$TOKENS" "$S/p.TOKENS" \
'  --focus-ring: var(--ink);' ''
# The trap the /about work caught: the "obvious" focus token measures 1.32:1 on
# --bg in light mode, i.e. WORSE than the browser default it replaces.
one "ring: use the yellow --border-focus (1.32:1 light)" "$TOKENS" "$S/p.TOKENS" \
'  --focus-ring: var(--ink);' '  --focus-ring: var(--border-focus);'
one "ring: zero width (declaration present, nothing drawn)" "$RESET" "$S/p.RESET" \
'  outline: 2px solid var(--focus-ring);' '  outline: 0 solid var(--focus-ring);'
one "ring: outline-style none (the shorthand still reads fine)" "$RESET" "$S/p.RESET" \
'  outline: 2px solid var(--focus-ring);' '  outline: 2px none var(--focus-ring);'

echo
echo "=== the inverted panel, where one flat token is not enough ==="
one "inverted: drop the .cta-banner override (--ink on --ink = 1.00:1)" "$LANDING" "$S/p.LANDING" \
'  --focus-ring: var(--bg);
  background: var(--ink);' '  background: var(--ink);'

echo
echo "=== the marquee, whose edge fades paint over a focused chip ==="
one "ticker: stop lifting the track above the strip's fades" "$LANDING" "$S/p.LANDING" \
'.ticker-strip:focus-within .ticker-track {
  position: relative;
  z-index: 2;
}' ''
one "ticker: lift the CHIP instead of the track (a transform makes the track a stacking context)" "$LANDING" "$S/p.LANDING" \
'.ticker-strip:focus-within .ticker-track {' '.ticker-chip:focus-visible {'

echo
echo "=== the exempt text inputs, which must signal focus another way ==="
one "hero box: drop the :focus-within border" "$LANDING" "$S/p.LANDING" \
'.hero-input-box:focus-within {
  border-color: var(--ink);
}' ''
one "composer: drop the :focus-within border" "$LANDING" "$S/p.LANDING" \
'.composer-box:focus-within {
  border-color: var(--ink);
}' ''

echo
echo "=== the citation chip, which has its own rule at a higher specificity ==="
one "chip: hardcode --ink again instead of the token" "$LANDING" "$S/p.LANDING" \
'a.citation-chip:focus-visible {
  outline: 2px solid var(--focus-ring);' 'a.citation-chip:focus-visible {
  outline: 2px solid var(--ink);'

echo
# DROPPED MUTANT, recorded rather than silently absent: "sweep: stop at the
# first repeat". Its anchor changed when key-based de-duplication became element
# identity, and the commit that removed it said "its target no longer exists".
# That was the wrong reason. Repointed at the current line it applies cleanly
# and SURVIVES -- with identity de-duplication, breaking early on a repeat is
# behaviourally equivalent, because a repeat can no longer hide a distinct
# control. An EQUIVALENT MUTANT is a legitimate reason to drop one; "the anchor
# moved" is not, and the difference matters to anyone auditing this list.
# NO MUTANT for the canvas two-sentinel parse check. It fires only on a colour
# `getComputedStyle` cannot produce, so nothing a sweep walks can trigger it and
# any mutant of it would SURVIVE for that reason rather than because the check
# is weak. Its logic is asserted directly instead, in both directions, by
# "the canvas colour reader rejects a colour it cannot parse".
echo "=== META: can the sweep be made to check nothing? ==="
one "sweep: ignore the pixels and trust the computed colours again" "$SPEC" "$S/p.SPEC" \
'    if (s.pixels.strongPixels < 8) {' '    if (false) {'
one "sweep: accept a ring nothing on screen actually changed" "$SPEC" "$S/p.SPEC" \
'    if (s.pixels.changedPixels < 12) {' '    if (false) {'
one "sweep: never measure pixels at all" "$SPEC" "$S/p.SPEC" \
'    (stop as Stop).pixels = await measureFocusIndicator(page);' \
'    (stop as Stop).pixels = { changedPixels: 999, strongPixels: 999, ratio: 21, indicator: [0,0,0], adjacent: [255,255,255] };'
one "measure: stop freezing the marquee (the two frames drift apart)" "$SPEC" "$S/p.SPEC" \
'  await freezeAnimations(page);' ''
one "sweep: exempt by class TOKEN again, so any control sharing it is skipped" "$SPEC" "$S/p.SPEC" \
'    exempt: el.matches(EXEMPT_SELECTOR),' \
'    exempt: (typeof el.className === "string" ? el.className : "").split(/\\s+/).some((c) => ["hero-input", "chat-input"].includes(c)),'
one "sweep: never clear the identity stamps between walks" "$SPEC" "$S/p.SPEC" \
'  await page.evaluate(() =>
    document.querySelectorAll("[data-fr-seen]").forEach((n) => n.removeAttribute("data-fr-seen")),
  );
  for (let i = 0; i < max; i++) {' '  for (let i = 0; i < max; i++) {'
one "sweep: de-duplicate on a KEY again, so a broken twin hides" "$SPEC" "$S/p.SPEC" \
'      const seenBefore = el.hasAttribute("data-fr-seen");' \
'      const seenBefore = !!document.querySelector(
        `[data-fr-seen]${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\\s+/).join(".") : ""}`,
      );' 
# Every one of these was a live way to make the guard vacuous while it still
# reported green, which is this repo's most-repeated failure mode.
one "sweep: walk only 5 controls (the >= 20 stop count must bite)" "$SPEC" "$S/p.SPEC" \
'const stops = await tabWalk(page, 140);' 'const stops = await tabWalk(page, 5);'
one "sweep: exempt everything (checked == 0 must bite)" "$SPEC" "$S/p.SPEC" \
'function isExempt(s: Stop) {' 'function isExempt(s: Stop) {
  return true;'

echo
echo "=== KILLED: $K   SURVIVED/ERROR: $SV ==="
ok=1
# Quoted: an unquoted "$S" inside eval mis-parses if TMPDIR contains a space and
# then reports every file DIRTY for a reason that has nothing to do with the run.
for f in TOKENS RESET LANDING SPEC; do
  eval "t=\$$f"
  cmp -s "$t" "$S/p.$f" || { echo "DIRTY: $f"; ok=0; }
done
[ $ok -eq 1 ] && echo "all restored byte-identical"

[ "$SV" -eq 0 ]
