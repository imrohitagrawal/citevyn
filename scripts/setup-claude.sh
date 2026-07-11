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

# settings.json / settings.local.json are gitignored and machine-local (they
# hold permissions + hooks, not skill content). This script cannot recreate
# their content — only note their absence so it can be restored from a backup.
for f in settings.json settings.local.json; do
    if [ ! -f "$PROJECT_ROOT/.claude/$f" ]; then
        echo -e "${YELLOW}! .claude/$f is missing — restore it from your backup"
        echo -e "  (gitignored, not reproducible here).${NC}"
    fi
done

echo -e "${GREEN}✓ Claude Code discovery layer ready.${NC}"
