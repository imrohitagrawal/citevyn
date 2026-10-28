#!/usr/bin/env bash
# Suite for the committed .claude/settings.json: it holds hooks ONLY (permissions
# and plugins are personal and live in the gitignored settings.local.json), and
# every hook it runs points at a script that exists, so a rename cannot silently
# turn a guard into a no-op (each command skips when its script is missing).
#
# Which change turns each case red:
#   hooks-only      — any key besides "hooks" added (permissions, enabledPlugins)
#   scripts-exist   — a hooked script renamed or deleted without updating the hook
#   guard-wired     — the SubagentStart, SubagentStop or (alert delivery) PreToolUse entry
#                     dropped, its command made
#                     a no-op or pointed at another script, or given a matcher
#   scripts-named   — the hooks file emptied or unparsed (fewer than 3 scripts named)
#   local-ignored   — `.claude/*` removed from .gitignore (global excludes ignored here)
#   settings-tracked — `!.claude/settings.json` removed from .gitignore

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SETTINGS="${ROOT}/.claude/settings.json"
PASS=0
FAIL=0

_check() {
  if [ "$2" = "$3" ]; then
    printf '  ok   %s\n' "$1"; PASS=$((PASS + 1))
  else
    printf '  FAIL %s — expected %s, got %s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1))
  fi
}

echo "test_claude_settings.sh"

_check "hooks-only" "hooks" "$(python3 -c 'import json,sys; print(",".join(sorted(json.load(open(sys.argv[1])))))' "$SETTINGS")"

# Every script a hook names, and how many of them are missing. The count of
# named scripts is checked too, so an empty or unparsed file cannot pass.
COUNTS="$(python3 -c '
import json, re, sys
from pathlib import Path
d = json.load(open(sys.argv[1]))
scripts = {m for g in d["hooks"].values() for grp in g for h in grp["hooks"]
           for m in re.findall(r"\$CLAUDE_PROJECT_DIR/(scripts/[\w./-]+)", h.get("command", ""))}
print(len(scripts), sum(1 for s in scripts if not (Path(sys.argv[2]) / s).is_file()))
' "$SETTINGS" "$ROOT")"
named="${COUNTS% *}"
missing="${COUNTS#* }"
_check "scripts-named" "yes" "$([ "$named" -ge 3 ] && echo yes || echo "no ($named)")"
_check "scripts-exist" "0" "$missing"

GUARD_CMD='[ -f "$CLAUDE_PROJECT_DIR/scripts/subagent_tree_guard.py" ] || exit 0; exec python3 "$CLAUDE_PROJECT_DIR/scripts/subagent_tree_guard.py"'
for ev in SubagentStart SubagentStop PreToolUse; do
  # The exact command, in a group with no matcher: a substring check let a no-op
  # command, the wrong script, or a matcher that matches no agent all pass.
  _check "guard-wired-$ev" "1" "$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(sum(h.get("command") == sys.argv[3] and "matcher" not in g
          for g in d["hooks"].get(sys.argv[2], []) for h in g["hooks"]))
' "$SETTINGS" "$ev" "$GUARD_CMD")"
done

# Only the repo's .gitignore counts: a global excludes file (one exists on the
# maintainer's machine) would keep local-ignored green whatever .gitignore says,
# and --no-index makes check-ignore judge a file git already tracks.
_ign() { git -C "$ROOT" -c core.excludesFile=/dev/null check-ignore -q --no-index "$1"; }
_check "local-ignored" "ignored" "$(_ign .claude/settings.local.json && echo ignored || echo tracked)"
_check "settings-tracked" "trackable" "$(_ign .claude/settings.json && echo ignored || echo trackable)"

echo "${PASS} passed, ${FAIL} failed"
[ "${FAIL}" -eq 0 ]
