#!/usr/bin/env bash
# Suite for scripts/check_next_prompt.py: `--file` checks a (private) prompt has
# its Process line and four sections; `--root` refuses any prompt file at the top
# of this public repository.
#
# Every case builds its own root under mktemp. The good prompt must PASS (the
# positive partner: without it, every "fails" case below could pass against a
# checker that fails everything); each broken one must FAIL.
#
# Which change turns each case red:
#   passes-good-prompt — shows_section broken, the "Items, ..." prefix rule made
#                       exact-only, or a section the good prompt lacks added
#   passes-process-text-on-next-line — the next-line clause of has_process_line removed
#   fails-missing-*   — a name removed from REQUIRED_SECTIONS, or the Process
#                       check made always-true, or the absent-file check removed
#   fails-empty-process-label, fails-process-label-then-heading — a blank line or a
#                       heading accepted as the Process paragraph's text
#   fails-items-prefix-is-not-a-word — the prefix rule allowing any next character
#   fails-level-3-heading — `###` counted as `##`
#   fails-fence-closed-by-* — a fence closed by the other fence character or a
#                       shorter fence
#   fails-hidden-*    — fence or comment stripping removed from visible_lines
#   root-fails-*      — root_prompts made to return nothing, or the case-insensitive
#                       "prompt" match made case-sensitive
#   root-passes-*     — root_prompts flagging every .md file (the "prompt" filter dropped)
#   *-list-item-fence — fences after a list marker not recognised
#   fails-list-marker-does-not-close-a-fence — the list-marker prefix allowed on a closing fence
#   passes-inline-*/byte-order-mark — inline code taken for a fence (the line sits
#                       before ## Items), an inline `<!--` taken for a comment, or the
#                       BOM not stripped (the BOM sits on the **Process:** line)
#   fails-process-label-then-quote — a block quote accepted as the Process text
#   fails-line-separator — splitlines() used (it splits on U+2028)

set -euo pipefail

CHECK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/check_next_prompt.py"
PASS=0
FAIL=0
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

GOOD='# Session prompt: example

**Process:** every item is built by .claude/skills/citevyn-delivery/SKILL.md.

## Read first
1. AGENTS.md.

## Items, in order (one PR each)
1. Something.

## Approvals
Nothing paid.

## End of session
1. Status block.'

_case() {
  # _case <label> <expected pass|fail> <prompt text, or "-" for no prompt file>
  local label="$1" want="$2" text="$3" root got
  root="$(mktemp -d "${WORK}/root.XXXXXX")"
  if [ "$text" != "-" ]; then printf '%s\n' "$text" > "${root}/NEXT-SESSION-PROMPT.md"; fi
  if python3 "$CHECK" --file "${root}/NEXT-SESSION-PROMPT.md" >/dev/null; then got=pass; else got=fail; fi
  _record "$label" "$want" "$got"
}

_root_case() {
  # _root_case <label> <expected pass|fail> <file names to create at the root...>
  local label="$1" want="$2" root got f
  shift 2
  root="$(mktemp -d "${WORK}/pub.XXXXXX")"
  for f in "$@"; do echo "x" > "${root}/${f}"; done
  if python3 "$CHECK" --root "$root" >/dev/null; then got=pass; else got=fail; fi
  _record "$label" "$want" "$got"
}

_record() {
  local label="$1" want="$2" got="$3"
  if [ "$got" = "$want" ]; then
    printf '  ok   %s\n' "$label"; PASS=$((PASS + 1))
  else
    printf '  FAIL %s — expected %s, got %s\n' "$label" "$want" "$got"; FAIL=$((FAIL + 1))
  fi
}

_without() {
  # _without <line to drop> -> GOOD minus that exact line
  printf '%s\n' "$GOOD" | grep -vxF -- "$1"
}

echo "test_check_next_prompt.sh"

_case "passes-good-prompt" pass "$GOOD"
_case "passes-process-text-on-next-line" pass "$(printf '%s\n' "$GOOD" | python3 -c 'import sys; print(sys.stdin.read().replace("**Process:** every", "**Process:**\nevery"))')"

_case "fails-missing-file" fail "-"
_case "fails-missing-read-first" fail "$(_without '## Read first')"
_case "fails-missing-items" fail "$(_without '## Items, in order (one PR each)')"
_case "fails-missing-approvals" fail "$(_without '## Approvals')"
_case "fails-missing-end-of-session" fail "$(_without '## End of session')"
_case "fails-missing-process" fail "$(_without '**Process:** every item is built by .claude/skills/citevyn-delivery/SKILL.md.')"
_case "fails-empty-process-label" fail "$(printf '%s\n' "$GOOD" | python3 -c 'import sys; print(sys.stdin.read().replace("**Process:** every item is built by .claude/skills/citevyn-delivery/SKILL.md.", "**Process:**\n"))')"
_case "fails-process-label-then-heading" fail "$(printf '%s\n' "$GOOD" | python3 -c 'import sys; print(sys.stdin.read().replace("**Process:** every item is built by .claude/skills/citevyn-delivery/SKILL.md.\n\n", "**Process:**\n"))')"
_case "fails-items-prefix-is-not-a-word" fail "$(printf '%s\n' "$GOOD" | python3 -c 'import sys; print(sys.stdin.read().replace("## Items, in order", "## Itemsx"))')"
_case "fails-level-3-heading" fail "$(printf '%s\n' "$GOOD" | python3 -c 'import sys; print(sys.stdin.read().replace("## Approvals", "### Approvals"))')"

_case "fails-hidden-in-fence" fail "$(_without '## Approvals')
\`\`\`
## Approvals
\`\`\`"
_case "fails-hidden-in-unclosed-fence" fail "$(_without '## End of session')
~~~
## End of session"
_case "fails-hidden-in-comment" fail "$(_without '## Read first')
<!--
## Read first
-->"
_case "fails-hidden-process-in-unclosed-comment" fail "<!--
$GOOD"

_case "fails-hidden-in-list-item-fence" fail "$(_without '## Approvals')
1. \`\`\`
   ## Approvals
   \`\`\`"
_case "passes-inline-code-with-three-backticks" pass "$(printf '%s\n' "$GOOD" | python3 -c 'import sys; print(sys.stdin.read().replace("## Items,", "\x60\x60\x60make prompt-check\x60\x60\x60 runs it.\n\n## Items,"))')"
_case "passes-inline-comment-marker-in-code-span" pass "$(printf '%s\n' "$GOOD" | python3 -c 'import sys; print(sys.stdin.read().replace("1. AGENTS.md.", "1. AGENTS.md; a \x60<!--\x60 in prose is text."))')"
_case "passes-byte-order-mark" pass "$(printf '\357\273\277%s' "$(printf '%s\n' "$GOOD" | sed 1,2d)")"
_case "fails-process-label-then-quote" fail "$(printf '%s\n' "$GOOD" | python3 -c 'import sys; print(sys.stdin.read().replace("**Process:** every item is built by .claude/skills/citevyn-delivery/SKILL.md.\n\n", "**Process:**\n> quoted\n\n"))')"
_case "fails-line-separator-fake-heading" fail "$(_without '## Approvals' | python3 -c 'import sys; print(sys.stdin.read().replace("Nothing paid.", "Nothing paid.\u2028## Approvals"))')"

_case "fails-list-marker-does-not-close-a-fence" fail "$(_without '## Approvals')
\`\`\`
- \`\`\`
## Approvals
\`\`\`"
_case "fails-fence-closed-by-other-char" fail "$(_without '## Approvals')
\`\`\`
~~~
## Approvals
\`\`\`"
_case "fails-fence-closed-by-shorter-fence" fail "$(_without '## Approvals')
\`\`\`\`
\`\`\`
## Approvals
\`\`\`\`"

_root_case "root-passes-without-prompt-files" pass README.md AGENTS.md
_root_case "root-fails-next-session-prompt" fail README.md NEXT-SESSION-PROMPT.md
_root_case "root-fails-any-case-prompt-name" fail OLD-Prompt.md

echo "${PASS} passed, ${FAIL} failed"
[ "${FAIL}" -eq 0 ]
