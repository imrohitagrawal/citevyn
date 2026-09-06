#!/bin/bash
#
# Mutation harness for the #356 announcement, the parked-question notice and the
# toast stacking limits.
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
'    if (!last || last.isUser) {
      // A question just went in' '    if (false) {
      // A question just went in'
one "announce: drop the transport-failure guard" "$CV" "$S/p.CV" \
'    if (arrived.errorKind) {' '    if (false) {'
one "announce: drop the refusal branch" "$CV" "$S/p.CV" \
'      arrived.refusal' '      false'
one "announce: always pluralise sources" "$CV" "$S/p.CV" \
'${n === 1 ? "" : "s"}' 's'
one "announce: emit the source clause unconditionally" "$CV" "$S/p.CV" \
'`Answer ready.${n ? ` ${n} source' '`Answer ready.${true ? ` ${n} source'
one "announce: skip user bubbles no more (announce the reader back to themselves)" "$CV" "$S/p.CV" \
'      if (m.isUser) continue;' '      if (false) continue;'
one "landmark: drop the accessible name" "$CV" "$S/p.CV" \
' aria-label="Chat"' ''

echo
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
