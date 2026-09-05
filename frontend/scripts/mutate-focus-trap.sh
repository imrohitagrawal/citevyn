#!/bin/bash
#
# Mutation harness for the #331 dialog focus traps.
#
# WHY THIS IS IN THE REPO. "19/19 mutations killed" in a PR body is a claim
# nobody can check; a reviewer said so of this very PR, and was right — the
# bundle-budget work (#323) already ships its harness and this should match.
# Run it with:
#
#     bash frontend/scripts/mutate-focus-trap.sh
#
# A guard is only a guard if deleting it turns a test red. Each mutation removes
# or inverts exactly one guard and expects the suite to FAIL.
#
# Discipline this encodes, learned from this repo's own near-misses:
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
HOOK=src/hooks/useFocusTrap.ts
HD=src/components/HistoryDrawer.tsx
CD=src/components/ConnectedAccountsDrawer.tsx
AM=src/components/AuthModal.tsx
LS=src/hooks/useLandingState.ts
DS=src/lib/dialogStack.ts
TESTS="src/components/HistoryDrawer.test.tsx src/components/ConnectedAccountsDrawer.test.tsx src/components/ConnectedAccountsDrawer.coldStart.test.tsx src/components/AuthModal.test.tsx src/hooks/useFocusTrap.test.tsx src/hooks/useLandingState.test.tsx"

for f in HOOK HD CD AM LS DS; do
  eval "t=\$$f"
  if ! git diff --quiet -- "$t"; then
    echo "refusing to run: $t has uncommitted changes."
    echo "commit or stash first, so a restore cannot lose your work."
    exit 1
  fi
  cp "$t" "$S/p.$f"
done
K=0; SV=0

run () { npx vitest run $TESTS >"$S/m.out" 2>&1; echo $?; }

one () {  # label file pristine old new
  local label="$1" t="$2" p="$3"
  OLD="$4" NEW="$5" T="$t" python3 -c "
import os,sys
t=os.environ['T'];old=os.environ['OLD'];new=os.environ['NEW']
s=open(t).read()
if old not in s: sys.exit(3)
open(t,'w').write(s.replace(old,new,1))
"
  if [ $? -ne 0 ]; then echo "!! ANCHOR MISSING: $label"; cp "$p" "$t"; SV=$((SV+1)); return; fi
  if cmp -s "$t" "$p"; then echo "!! NO-OP: $label"; cp "$p" "$t"; SV=$((SV+1)); return; fi
  local c; c=$(run)
  cp "$p" "$t"; cmp -s "$t" "$p" || { echo "!! RESTORE FAILED: $label"; exit 98; }
  if [ "$c" -ne 0 ]; then echo "KILLED    <- $label"; K=$((K+1)); else echo "SURVIVED! <- $label"; SV=$((SV+1)); fi
}

echo "=== the / shortcut guard (blocker A) ==="
one "slash: drop the open-dialog guard" "$LS" "$S/p.LS" \
'        !isModalDialogOpen() &&' ''

echo
echo "=== the topmost-dialog stack (blocker B) ==="
one "stack: every trap acts, not just the top-most" "$HOOK" "$S/p.HOOK" \
'      if (!isTopTrap(token)) return;' ''
one "stack: isTopTrap always true" "$DS" "$S/p.DS" \
'  return trapStack[trapStack.length - 1] === token;' '  return true;'
one "stack: never unregister on unmount" "$HOOK" "$S/p.HOOK" \
'      removeTrap(token);' ''
one "stack: removeTrap is a no-op" "$DS" "$S/p.DS" \
'  const i = trapStack.indexOf(token);
  if (i !== -1) trapStack.splice(i, 1);' ''
one "stack: isModalDialogOpen always reports false" "$DS" "$S/p.DS" \
'  return trapStack.length > 0;' '  return false;'

echo
echo "=== the cold window: restore the OLD enabled-flag design (two files) ==="
python3 - <<'PY'
h="src/hooks/useFocusTrap.ts"; s=open(h).read()
s=s.replace("type Options = {\n  /** Close handler for Escape. Omit to leave Escape alone. */\n  onEscape?: () => void;\n};",
            "type Options = {\n  onEscape?: () => void;\n  enabled?: boolean;\n};")
s=s.replace("  { onEscape }: Options = {},", "  { onEscape, enabled = true }: Options = {},")
s=s.replace("  useEffect(() => {\n    const token = {};", "  useEffect(() => {\n    if (!enabled) return;\n    const token = {};")
s=s.replace("  }, []);", "  }, [enabled]);")
open(h,"w").write(s)
c="src/components/ConnectedAccountsDrawer.tsx"; s=open(c).read()
s=s.replace("useFocusTrap(dialogRef, { onEscape: onClose });","useFocusTrap(dialogRef, { onEscape: onClose, enabled: !passwordOpen });")
open(c,"w").write(s)
PY
if cmp -s "$HOOK" "$S/p.HOOK"; then echo "!! NO-OP (two-file)"; SV=$((SV+1)); else
  c=$(run)
  cp "$S/p.HOOK" "$HOOK"; cp "$S/p.CD" "$CD"
  if [ "$c" -ne 0 ]; then echo "KILLED    <- cold window: the old enabled:!passwordOpen design"; K=$((K+1));
  else echo "SURVIVED! <- cold window: the old enabled:!passwordOpen design"; SV=$((SV+1)); fi
fi

echo
echo "=== the trap must still be wired up ==="
one "HistoryDrawer: remove the trap" "$HD" "$S/p.HD" \
'  useFocusTrap(dialogRef, { onEscape: onClose });' '  void useFocusTrap;'
one "ConnectedAccountsDrawer: remove the trap" "$CD" "$S/p.CD" \
'  useFocusTrap(dialogRef, { onEscape: onClose });' '  void useFocusTrap;'
one "AuthModal: remove the trap" "$AM" "$S/p.AM" \
'  useFocusTrap(dialogRef, { onEscape: onClose });' '  void useFocusTrap;'

echo
echo "=== each guard inside the hook ==="
one "hook: drop the forward wrap" "$HOOK" "$S/p.HOOK" \
'      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }' '      }'
one "hook: drop the backward wrap" "$HOOK" "$S/p.HOOK" \
'      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if' '      if (false) {
      } else if'
one "hook: drop the pull-back branch" "$HOOK" "$S/p.HOOK" \
'      if (!(active instanceof HTMLElement) || !focusable.includes(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
        return;
      }' ''
one "hook: pull-back always goes forward" "$HOOK" "$S/p.HOOK" \
'        (e.shiftKey ? last : first).focus();' '        first.focus();'
one "hook: stop handling Escape" "$HOOK" "$S/p.HOOK" \
'      if (e.key === "Escape" && onEscapeRef.current) {' '      if (false && onEscapeRef.current) {'
one "hook: let Tab through when nothing is focusable" "$HOOK" "$S/p.HOOK" \
'      if (focusable.length === 0) {
        // Nothing to move to, but Tab must still not leave the dialog.
        e.preventDefault();
        return;
      }' '      if (focusable.length === 0) return;'
one "hook: never listen at all" "$HOOK" "$S/p.HOOK" \
'    document.addEventListener("keydown", handleKeyDown);' '    void handleKeyDown;'
one "hook: leak the listener on unmount" "$HOOK" "$S/p.HOOK" \
'      document.removeEventListener("keydown", handleKeyDown);' ''
one "hook: stop refreshing the onEscape ref" "$HOOK" "$S/p.HOOK" \
'    onEscapeRef.current = onEscape;' '    void onEscape;'

echo
echo "=== KILLED: $K   SURVIVED/ERROR: $SV ==="
ok=1
for f in HOOK HD CD AM LS DS; do eval "cmp -s \$$f $S/p.\$f" || { echo "DIRTY: $f"; ok=0; }; done
[ $ok -eq 1 ] && echo "all restored byte-identical"

[ "$SV" -eq 0 ]
