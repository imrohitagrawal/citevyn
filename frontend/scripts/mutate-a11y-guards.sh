#!/bin/bash
#
# Mutation harness for the #356 announcements (an arriving answer, and a REFUSED
# submit), the parked-question notice and the toast stacking limits.
#
#     bash frontend/scripts/mutate-a11y-guards.sh
#
# Companion to mutate-focus-ring.sh (the CSS half, driven through a real
# browser) and mutate-bundle-guards.sh. This one is vitest-only and therefore
# fast: every guard here is observable in jsdom.
#
# WHY IT EXISTS. The first version of the announcement passed eight unit tests
# while being unable to produce the string those tests asserted: they drove
# `pending: true -> false` against a hand-built, already-settled message list,
# and the application never has that shape at that instant. Three reviewers
# reproduced it independently. So the tests were rewritten to drive the shape
# `streamBot` really produces (append with `sources: []` and `streaming: true`,
# then finish), and this script is how "they bite now" stops being a claim.
#
# Discipline, same as its siblings:
#   - assert the mutation ACTUALLY APPLIED; a no-op edit reads as a survivor
#   - measure by EXIT CODE, never by grepping output for "FAIL"
#   - byte-copy restore, verified with cmp, after EVERY mutation
#   - name WHICH test died, so a mutant killed by something unrelated is visible
#   - run sequentially in one tree; parallel writers corrupt each other
set -u
cd "$(dirname "$0")/.." || exit 99
S=$(mktemp -d)

CV=src/components/ChatView.tsx
LS=src/hooks/useLandingState.ts
UT=src/hooks/useToast.ts
TESTS="src/components/ChatView.test.tsx src/hooks/useLandingState.test.tsx src/hooks/useToast.test.tsx"

restore_all () {
  for f in CV LS UT; do
    eval "t=\$$f"
    [ -f "$S/p.$f" ] && cp "$S/p.$f" "$t" 2>/dev/null
  done
}
trap 'restore_all; rm -rf "$S"' EXIT INT TERM

for f in CV LS UT; do
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

echo "=== the arriving-answer announcement (#356) ==="
# THE ORIGINAL DEFECT: edge on `pending` instead of on the bubble finishing.
one "announce: key the edge on \`pending\` again" "$CV" "$S/p.CV" \
'      if (m.streaming) {' '      if (pending) {'
# THE SECOND DEFECT, found by a skeptic on the first fix: a boolean latch
# reading the TAIL, which announced a RESUMED transcript as a fresh arrival.
one "announce: latch on a boolean instead of the streaming bubble's identity" "$CV" "$S/p.CV" \
'      } else if (watching.current.delete(m.msgId)) {' \
'      } else if (watching.current.size > 0 ? (watching.current.clear(), true) : false) {'
one "announce: watch by list POSITION, which a resume re-uses" "$CV" "$S/p.CV" \
'        watching.current.add(m.msgId);' \
'        watching.current.add(Number(m.domId.replace("cv-msg-", "")));'
one "announce: never clear on a new question (stale text over a failure)" "$CV" "$S/p.CV" \
'    if (!last || last.isUser || last.streaming) {' '    if (false) {'
# THE THIRD DEFECT: dropping just the `|| last.streaming` clause. In demo mode
# the user bubble and the bot bubble land in ONE React commit, so the tail is
# never a user message and the region never returns to "" -- and `role=status`
# announces on a text CHANGE, so the SECOND consecutive answer was announced to
# nobody. Measured in a real browser: the region took two values across two
# questions where it should take four.
one "announce: clear only on a user tail (the second answer goes silent)" "$CV" "$S/p.CV" \
'    if (!last || last.isUser || last.streaming) {' '    if (!last || last.isUser) {'
one "announce: drop the transport-failure guard" "$CV" "$S/p.CV" \
'    if (arrived.errorKind) {' '    if (false) {'
one "announce: drop the refusal branch" "$CV" "$S/p.CV" \
'      arrived.refusal' '      false'
one "announce: always pluralise sources" "$CV" "$S/p.CV" \
'${n === 1 ? "" : "s"}' 's'
one "announce: emit the source clause unconditionally" "$CV" "$S/p.CV" \
'`Answer ready.${n ? ` ${n} source' '`Answer ready.${true ? ` ${n} source'
one "landmark: drop the accessible name" "$CV" "$S/p.CV" \
' aria-label="Chat"' ''

# NO MUTANT for `if (m.isUser) continue`. Removing it is an EQUIVALENT mutant,
# not a surviving one: a user bubble is never `streaming`, and its id was never
# added to `watching`, so the loop falls through both branches and does nothing
# either way. The line is defensive and cheap; a mutant of it would "survive"
# for a reason that says nothing about the guard, which is exactly the kind of
# number that makes a mutation score dishonest.
echo
echo "=== the REFUSED-submit announcement (#356 gap 2) ==="
# THE DEFECT ITSELF: the gate was a bare `return`. Measured with a
# MutationObserver over the whole body, ZERO DOM mutations on both the Enter
# path and the click path.
one "refusal: drop the signal, back to a silent return" "$LS" "$S/p.LS" \
'      dispatch({ type: "REFUSED_SUBMIT" });' '      void 0;'
# The trap docs/BACKLOG.md names explicitly: `pending` is a render behind the
# ref, so two Enter presses in ONE React batch both read 0.
one "refusal: derive it from \`pending\` instead of the ref" "$LS" "$S/p.LS" \
'    if (!t) return;
    if (inFlight.current) {' '    if (!t) return;
    if (state.pending) {'
# Announcing an EMPTY submit would tell an AT user "your text is kept" about a
# box with no text — the same class of false statement the parked-question
# toast had to be fixed for.
one "refusal: announce an empty submit too" "$LS" "$S/p.LS" \
'    if (!t) return;' '    if (false) return;'
# THE ROOT-CAUSE PROPERTY, and the one two successive view-side designs got
# wrong: the flag must be cleared by the SAME reducer action that clears
# `pending`. Latching it in the view left a stale refusal masking the arrival
# announcement; gating the render on `pending` only hid the latch, so the next
# genuine request announced "Not sent" about a question that WAS sent.
one "refusal: never clear the flag when the window closes" "$LS" "$S/p.LS" \
'        refusedInFlight: action.value === 0 ? false : state.refusedInFlight,' \
'        refusedInFlight: state.refusedInFlight,'
# The button was `onClick={pending ? undefined : onSendClick}`, so a click while
# gated never reached the hook and NO prop this component has could tell a
# refused click from no click.
one "refusal: re-gate the click in the view, so it never reaches the hook" "$CV" "$S/p.CV" \
'            onClick={onSendClick}' '            onClick={pending ? undefined : onSendClick}'
one "refusal: render the region without it" "$CV" "$S/p.CV" \
'          {refusedInFlight' '          {false'

echo "=== the parked-question notice, and its stale-closure trap ==="
one "park: read the STALE closure value again" "$LS" "$S/p.LS" \
'          if (!carried && !chatInputRef.current) {' '          if (!carried && !state.chatInput) {'
one "park: drop the notice entirely" "$LS" "$S/p.LS" \
'            addToast({
              kind: "info",
              title: "Kept your draft",' '            void ({
              kind: "info",
              title: "Kept your draft",'
one "park: notice on EVERY park, not only a drop" "$LS" "$S/p.LS" \
'          if (!carried && !chatInputRef.current) {' '          if (false) {'
one "setChatInput: stop writing the ref" "$LS" "$S/p.LS" \
'    chatInputRef.current = value;' ''

echo
echo "=== toast stacking limits ==="
one "toast: drop the identical-toast collapse" "$UT" "$S/p.UT" \
'      const rest = prev.filter(
        (t) =>
          !(t.kind === toast.kind && t.title === toast.title && t.message === toast.message),
      );' '      const rest = prev;'
one "toast: drop the cap" "$UT" "$S/p.UT" \
'.slice(-MAX_VISIBLE_TOASTS)' ''
one "toast: cap keeps the OLDEST instead of the newest" "$UT" "$S/p.UT" \
'.slice(-MAX_VISIBLE_TOASTS)' '.slice(0, MAX_VISIBLE_TOASTS)'

echo
echo "=== KILLED: $K   SURVIVED/ERROR: $SV ==="
ok=1
for f in CV LS UT; do
  eval "t=\$$f"
  cmp -s "$t" "$S/p.$f" || { echo "DIRTY: $f"; ok=0; }
done
[ $ok -eq 1 ] && echo "all restored byte-identical"
[ "$SV" -eq 0 ]
