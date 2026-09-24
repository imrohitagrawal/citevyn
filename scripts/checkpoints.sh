#!/usr/bin/env bash
# Browse and recover the automatic checkpoints written by scripts/checkpoint.py.
#
# A checkpoint is taken after every file edit, so the one immediately BEFORE a
# change you regret holds your work without it. That is the whole point: you do
# not have to have decided, in advance, that the change was worth protecting.
#
#   scripts/checkpoints.sh list              newest first, with what each changed
#   scripts/checkpoints.sh show <ref>        the full diff against HEAD
#   scripts/checkpoints.sh diff <ref> <file> what that checkpoint had for one file
#   scripts/checkpoints.sh restore <ref>     put it back into the working tree
#   scripts/checkpoints.sh restore <ref> <file>   just that one file
#
# `restore` writes into the working tree, so it is itself a change. It refuses
# to run if that would overwrite uncommitted edits, unless you pass --force —
# recovering from a lost-work incident should not cause a second one.

set -euo pipefail

PREFIX="refs/checkpoint"
CMD="${1:-list}"

# Sort by refname: the names carry milliseconds, so lexical order IS time
# order. `--sort=creatordate` resolves only to the second and cannot separate
# two edits made inside one — which is exactly the pair you need to tell apart.
_refs() { git for-each-ref --sort=-refname --format='%(refname)' "$PREFIX"; }

case "$CMD" in
  list)
    found=0
    while read -r ref; do
      [ -n "$ref" ] || continue
      found=$((found + 1))
      when="$(git for-each-ref --format='%(creatordate:relative)' "$ref")"
      files="$(git diff --name-only HEAD "$ref" 2>/dev/null | tr '\n' ' ')"
      printf '%-44s %-16s %s\n' "$ref" "$when" "${files:-(no diff vs HEAD)}"
    done < <(_refs)
    if [ "$found" -eq 0 ]; then
      echo "No checkpoints yet. They are written after each file edit, and only"
      echo "when the tree has uncommitted TRACKED changes."
    fi
    ;;

  show)
    [ $# -ge 2 ] || { echo "usage: checkpoints.sh show <ref>" >&2; exit 2; }
    git diff HEAD "$2"
    ;;

  diff)
    [ $# -ge 3 ] || { echo "usage: checkpoints.sh diff <ref> <file>" >&2; exit 2; }
    git diff HEAD "$2" -- "$3"
    ;;

  restore)
    [ $# -ge 2 ] || { echo "usage: checkpoints.sh restore <ref> [file] [--force]" >&2; exit 2; }
    REF="$2"; shift 2
    FORCE=0; TARGET=""
    for a in "$@"; do
      case "$a" in
        --force) FORCE=1 ;;
        *) TARGET="$a" ;;
      esac
    done
    git rev-parse --verify --quiet "$REF" >/dev/null || {
      echo "no such checkpoint: $REF  (run: checkpoints.sh list)" >&2; exit 2; }

    if [ "$FORCE" -eq 0 ] && [ -n "$(git status --porcelain --untracked-files=no)" ]; then
      echo "Refusing to restore over uncommitted changes." >&2
      echo "Recovering from lost work should not cause a second loss." >&2
      echo >&2
      echo "  commit or stash what you have, then re-run" >&2
      echo "  or pass --force if you are sure this checkpoint is what you want" >&2
      exit 1
    fi

    if [ -n "$TARGET" ]; then
      git checkout "$REF" -- "$TARGET"
      echo "restored $TARGET from $REF"
    else
      git checkout "$REF" -- .
      echo "restored the whole tree from $REF"
    fi
    echo "note: this staged the restored paths; 'git restore --staged <path>' unstages."
    ;;

  *)
    echo "usage: checkpoints.sh {list|show|diff|restore}" >&2
    exit 2
    ;;
esac
