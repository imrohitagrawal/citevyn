#!/bin/bash
##############################################################################
# CiteVyn: Claude Code discovery-layer setup (idempotent)
#
# The skill CONTENT lives in .agents/skills/* (git-tracked). Claude Code
# discovers skills from .claude/skills/*, which is gitignored — so a fresh
# clone has the content but not the symlinks, and every skill goes
# undiscovered until re-linked. This is exactly how the repo move lost the
# .claude layer. Run this after any clone/move to restore discovery:
#
#   ./scripts/setup-claude.sh
#
# Safe to run repeatedly; it only (re)creates the symlinks.
##############################################################################

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

SKILLS_SRC="$PROJECT_ROOT/.agents/skills"
SKILLS_DST="$PROJECT_ROOT/.claude/skills"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

if [ ! -d "$SKILLS_SRC" ]; then
    echo -e "${RED}✗ $SKILLS_SRC not found — is the repo checked out?${NC}" >&2
    exit 1
fi

mkdir -p "$SKILLS_DST"

echo -e "${YELLOW}→ Linking .claude/skills/* → .agents/skills/*${NC}"
linked=0
for skill_dir in "$SKILLS_SRC"/*/; do
    [ -d "$skill_dir" ] || continue
    name="$(basename "$skill_dir")"
    # Relative target keeps the link valid regardless of where the repo lives.
    ln -sfn "../../.agents/skills/$name" "$SKILLS_DST/$name"
    echo -e "${GREEN}  ✓ $name${NC}"
    linked=$((linked + 1))
done
echo -e "${GREEN}✓ Linked $linked skill(s).${NC}"

# settings.json / settings.local.json are gitignored and machine-local: they
# hold PERMISSIONS, which are a per-developer choice and should not be shared.
# HOOKS are different — they are how this repository enforces things that a
# written rule has already failed to enforce — so they live in a checked-in
# template and are merged in below.
for f in settings.json settings.local.json; do
    if [ ! -f "$PROJECT_ROOT/.claude/$f" ]; then
        echo -e "${YELLOW}! .claude/$f is missing — permissions must come from your"
        echo -e "  backup (gitignored). Hooks are restored automatically below.${NC}"
    fi
done

# Merge the repo's hooks into settings.json, by `command`, adding only what is
# absent. Never removes or reorders anything already there.
HOOK_TEMPLATE="$PROJECT_ROOT/scripts/claude-hooks.template.json"
if [ -f "$HOOK_TEMPLATE" ]; then
    python3 - "$PROJECT_ROOT" "$HOOK_TEMPLATE" <<'PYMERGE'
import json, sys
from pathlib import Path

root, template_path = Path(sys.argv[1]), Path(sys.argv[2])
settings_path = root / ".claude" / "settings.json"
template = json.loads(template_path.read_text(encoding="utf-8"))

settings = {}
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("! .claude/settings.json is not valid JSON — leaving it alone")
        raise SystemExit(0)

added = []
for event, groups in template.items():
    if event.startswith("_"):
        continue
    existing_groups = settings.setdefault("hooks", {}).setdefault(event, [])
    for group in groups:
        match = next(
            (g for g in existing_groups if g.get("matcher") == group.get("matcher")),
            None,
        )
        if match is None:
            existing_groups.append(group)
            added.extend(h.get("command", "?") for h in group.get("hooks", []))
            continue
        have = {h.get("command") for h in match.setdefault("hooks", [])}
        for hook in group.get("hooks", []):
            if hook.get("command") not in have:
                match["hooks"].insert(0, hook)
                added.append(hook.get("command", "?"))

if added:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    for command in added:
        print(f"  + hook installed: {command}")
else:
    print("  hooks already present")
PYMERGE
    echo -e "${GREEN}✓ Repo hooks present in .claude/settings.json.${NC}"
fi

echo -e "${GREEN}✓ Claude Code discovery layer ready.${NC}"
