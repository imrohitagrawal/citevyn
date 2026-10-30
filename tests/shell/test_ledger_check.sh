#!/usr/bin/env bash
# Suite for scripts/ledger_check.py: docs/BACKLOG.md must agree with GitHub.
#
#   R1  every open GitHub issue has an un-struck row in an open table
#   R2  every un-struck row in an open table is an open issue
#   R3  every struck row, and every row in a closed section, is a closed issue
#
# Pull requests are not issues. A failure to read GitHub is an error (exit 2),
# never a pass. Every case uses a fake `gh` on PATH that prints canned
# `gh api` output, so no case touches the network.
#
# Which change turns each case red:
#   clean-ledger-passes      — any rule firing on a correct ledger (a struck row
#                              in an open table read as un-struck, a "—" row read
#                              as an issue, a multi-issue cell reading one number)
#   clean-ledger-counts-rows — the parser reading no rows (a zero-violation pass
#                              that checked nothing); the summary counts must be
#                              the real ones
#   r1-missing-row           — R1 removed, or open issues not collected
#   r1-only-struck-row       — R1 satisfied by any row, struck or not
#   r1-ignores-open-prs      — R1 applied to pull requests
#   r2-closed-issue-unstruck — R2 removed, or state not compared
#   r2-unknown-number        — a number GitHub does not know treated as fine
#   r2-pull-request          — pull requests counted as issues in R2
#   r3-struck-but-open       — R3 not applied to struck rows in open tables
#   r3-closed-section-open   — closed sections not recognised from headings
#   r3-child-of-closed       — a sub-heading not inheriting its parent's closed state
#   r3-pull-request          — pull requests counted as issues in R3
#   section-reopens          — the closed state leaking into a later open section
#   mid-cell-strike-is-unstruck — "struck" read as "contains ~~" rather than
#                              "the title starts struck"
#   violations-are-counted   — the exit code or the summary count not following
#                              the violation lines (partner of clean-ledger-passes)
#   fetch-asks-for-all-states — the fetch dropping state=all or --paginate (closed
#                              issues, or every page after the first, go missing)
#   gh-fails-is-error        — a gh failure reported as a pass or as violations
#   gh-partial-page-is-error — gh's exit code ignored: valid lines from the first
#                              page, then an HTTP error and exit 1, read as a pass
#   gh-empty-is-error        — an empty answer from GitHub treated as "no issues"
#   gh-garbage-is-error      — an unreadable line skipped instead of failing
#   gh-missing-is-error      — gh not installed reported as a pass
#   backlog-missing-is-error — a missing ledger reported as a pass
#   will-close-struck-row-passes — --will-close ignored, so a pull request that
#                              strikes the row of the issue it closes reads as R3
#   will-close-unstruck-row-fails — --will-close ignored, so a row left un-struck
#                              for an issue the pull request closes passes
#   will-close-only-named-issues — --will-close treating every issue as closed
#   fence-row-ignored / tilde-fence-row-ignored — rows inside ``` or ~~~ fences
#                              read, so an example row satisfies R1
#   fence-heading-ignored    — a `#` line inside a fence read as a heading, which
#                              resets the closed-section state
#   comment-row-ignored      — rows inside a multi-line <!-- --> comment read
#   comment-heading-ignored  — a heading inside a multi-line comment read
#   other-repo-ref-ignored   — `other/repo#7` in a first cell read as this repo's #7
#   r1-says-what-to-change   — R1 without the fix, or without the issue's title
#   r2-says-what-to-change   — R2 without the fix, or without GitHub's closing date
#   r3-says-what-to-change   — R3 without the fix
#   unclosed-fence-is-error / unclosed-comment-is-error — a fence or comment left
#                              open at the end hiding the rest of the ledger,
#                              which then passes on what is left
#   longer-fence-not-closed-by-shorter — a ```` fence closed by a ``` line
#   title-with-line-separator — gh's output split on more than "\n" (U+2028 in a
#                              title then breaks the line), or titles not JSON
#   repeated-number-is-error — a later line for the same number silently winning

set -euo pipefail

CHECK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/ledger_check.py"
PASS=0
FAIL=0
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# The fake gh: logs its arguments, prints FAKE_GH_STDERR to stderr, prints the
# file FAKE_GH_OUT, exits FAKE_GH_RC.
mkdir -p "${WORK}/bin"
cat > "${WORK}/bin/gh" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [ -n "${FAKE_GH_STDERR:-}" ]; then printf '%s\n' "${FAKE_GH_STDERR}" >&2; fi
if [ -n "${FAKE_GH_OUT:-}" ]; then cat "${FAKE_GH_OUT}"; fi
exit "${FAKE_GH_RC:-0}"
SH
chmod +x "${WORK}/bin/gh"

U="https://github.com/example/repo/issues"

# A ledger in the real file's shape: open tables, a struck row kept in an open
# table, a "—" row with no issue, a multi-issue cell, a closed sub-section and a
# closed top-level section with a child heading.
CLEAN="# Backlog

## Roadmap milestones

### V1 milestone

| Issue | Title | Area | Notes |
|---|---|---|---|
| [#1](${U}/1) | Open thing | backend | x |
| [#2](${U}/2) | ~~Fixed thing~~ **FIXED.** | backend | x |

## Open follow-ups (unmilestoned)

| Issue | Title | Area | Priority | Origin |
|---|---|---|---|---|
| [#5](${U}/5) / [#6](${U}/6) | Two open things, ~~one old note~~ kept | ops | Low | x |
| — | A note with no issue | ops | Low | x |

### Closed — kept for the record

| Issue | Title | Area | Priority | Origin |
|---|---|---|---|---|
| [#3](${U}/3) | Closed thing, title not struck | docs | Low | x |

## Earlier follow-ups (all resolved)

### Kept for the record

| Issue | Why it matters |
|---|---|
| [#4](${U}/4) | Old closed thing |
"

# GitHub's answer for CLEAN: four issues in the right states, plus an open and a
# closed pull request that no row names.
CLEAN_GH="1 open false
2 closed false
3 closed false
4 closed false
5 open false
6 open false
10 open true
11 closed true"

_run() {
  # _run <ledger text> <gh output text> [gh exit code] [gh stderr]
  # Sets OUT (stdout+stderr) and RC.
  local ledger="$1" ghout="$2" rc="${3:-0}" err="${4:-}"
  printf '%s' "$ledger" > "${WORK}/BACKLOG.md"
  printf '%s\n' "$ghout" > "${WORK}/gh.out"
  : > "${WORK}/gh.log"
  set +e
  OUT="$(PATH="${WORK}/bin:${PATH}" FAKE_GH_LOG="${WORK}/gh.log" FAKE_GH_OUT="${WORK}/gh.out" \
    FAKE_GH_RC="$rc" FAKE_GH_STDERR="$err" \
    python3 "$CHECK" --backlog "${WORK}/BACKLOG.md" --repo example/repo ${EXTRA_ARGS:-} 2>&1)"
  RC=$?
  set -e
}

_ok() { printf '  ok   %s\n' "$1"; PASS=$((PASS + 1)); }
_bad() {
  printf '  FAIL %s — %s\n' "$1" "$2"
  printf '%s\n' "$OUT" | sed 's/^/       /'
  FAIL=$((FAIL + 1))
}

_expect() {
  # _expect <label> <exit code> <grep -E pattern that must match> [pattern that must NOT match]
  local label="$1" want_rc="$2" must="$3" mustnt="${4:-}"
  if [ "$RC" != "$want_rc" ]; then _bad "$label" "exit ${RC}, wanted ${want_rc}"; return; fi
  if ! printf '%s\n' "$OUT" | grep -Eq -- "$must"; then _bad "$label" "no line matching: $must"; return; fi
  if [ -n "$mustnt" ] && printf '%s\n' "$OUT" | grep -Eq -- "$mustnt"; then
    _bad "$label" "a line matched: $mustnt"; return
  fi
  _ok "$label"
}

# --- clean ledger -----------------------------------------------------------
_run "$CLEAN" "$CLEAN_GH"
_expect clean-ledger-passes 0 '^ledger_check: 0 violations' '^R[123] '
_expect clean-ledger-counts-rows 0 'rows: 3 in open tables, 2 in closed sections; open issues on GitHub: 3'

# --- R1 ----------------------------------------------------------------------
_run "$CLEAN" "${CLEAN_GH}
7 open false"
_expect r1-missing-row 1 '^R1 #7: '

_run "$CLEAN" "1 open false
2 open false
3 closed false
4 closed false
5 open false
6 open false"
_expect r1-only-struck-row 1 '^R1 #2: '

_run "$CLEAN" "${CLEAN_GH}
12 open true"
_expect r1-ignores-open-prs 0 '^ledger_check: 0 violations' '#12'

# --- R2 ----------------------------------------------------------------------
_run "$CLEAN" "1 closed false
2 closed false
3 closed false
4 closed false
5 open false
6 open false"
_expect r2-closed-issue-unstruck 1 '^R2 #1: .*closed'

_run "${CLEAN}
## Another open table

| Issue | Title |
|---|---|
| [#99](${U}/99) | Not on GitHub |
" "$CLEAN_GH"
_expect r2-unknown-number 1 '^R2 #99: .*not found'

_run "${CLEAN}
## Another open table

| Issue | Title |
|---|---|
| [#10](${U}/10) | An open pull request listed as an issue |
" "$CLEAN_GH"
_expect r2-pull-request 1 '^R2 #10: .*pull request'

# --- R3 ----------------------------------------------------------------------
_run "$CLEAN" "1 open false
2 open false
3 closed false
4 closed false
5 open false
6 open false"
_expect r3-struck-but-open 1 '^R3 #2: .*open'

_run "$CLEAN" "1 open false
2 closed false
3 open false
4 closed false
5 open false
6 open false"
_expect r3-closed-section-open 1 '^R3 #3: .*open'

_run "$CLEAN" "1 open false
2 closed false
3 closed false
4 open false
5 open false
6 open false"
_expect r3-child-of-closed 1 '^R3 #4: .*open'

_run "${CLEAN}
| [#11](${U}/11) | A merged pull request kept for the record |
" "$CLEAN_GH"
_expect r3-pull-request 1 '^R3 #11: .*pull request'

# --- section boundaries and strike detection ---------------------------------
_run "${CLEAN}
## Roadmap: something new

| Issue | Title |
|---|---|
| [#7](${U}/7) | Open, after the closed sections |
" "${CLEAN_GH}
7 open false"
_expect section-reopens 0 '^ledger_check: 0 violations' '^R[123] '

# #5/#6's title has "~~one old note~~" mid-cell; both are open. Read as struck,
# R3 and R1 would fire for them.
_run "$CLEAN" "$CLEAN_GH"
_expect mid-cell-strike-is-unstruck 0 '^ledger_check: 0 violations' '#[56]'

# --- the partner: a violation is really counted -------------------------------
_run "$CLEAN" "1 closed false
2 closed false
3 closed false
4 closed false
5 open false
6 open false
7 open false"
_expect violations-are-counted 1 '^ledger_check: 2 violations'
n="$(printf '%s\n' "$OUT" | grep -Ec '^R[123] #' || true)"
if [ "$n" = "2" ]; then _ok violations-are-counted-lines; else _bad violations-are-counted-lines "wanted 2 violation lines, got $n"; fi

# --- a pull request's own closing issues (--will-close) -----------------------
# On a pull request the ledger is written in its final state: the row of an
# issue the pull request closes is already struck while GitHub still says open.
OPEN2="1 open false
2 open false
3 closed false
4 closed false
5 open false
6 open false"
EXTRA_ARGS="--will-close 2"
_run "$CLEAN" "$OPEN2"
_expect will-close-struck-row-passes 0 '^ledger_check: 0 violations' '^R[123] '

EXTRA_ARGS="--will-close 1 2"
_run "$CLEAN" "$OPEN2"
_expect will-close-unstruck-row-fails 1 '^R2 #1: .*closed by this pull request'

# Partner: naming another issue must not excuse #2's struck-but-open row.
EXTRA_ARGS="--will-close 7"
_run "$CLEAN" "$OPEN2"
_expect will-close-only-named-issues 1 '^R3 #2: '
EXTRA_ARGS=""

# --- text a reader never sees: fences and comments ---------------------------
_run "${CLEAN}
## Examples

\`\`\`
| [#7](${U}/7) | An example row inside a fence |
\`\`\`
" "${CLEAN_GH}
7 open false"
_expect fence-row-ignored 1 '^R1 #7: '

_run "${CLEAN}
## Examples

~~~markdown
| [#7](${U}/7) | An example row inside a tilde fence |
~~~
" "${CLEAN_GH}
7 open false"
_expect tilde-fence-row-ignored 1 '^R1 #7: '

_run "${CLEAN/Closed — kept for the record/Closed — kept for the record

\`\`\`
# not a heading
\`\`\`}" "$CLEAN_GH"
_expect fence-heading-ignored 0 '^ledger_check: 0 violations' '^R[123] '

_run "${CLEAN}
## Notes

<!--
still inside the comment
| [#7](${U}/7) | A row inside a comment |
-->
" "${CLEAN_GH}
7 open false"
_expect comment-row-ignored 1 '^R1 #7: '

_run "${CLEAN/Closed — kept for the record/Closed — kept for the record

<!-- a comment that spans lines
still inside the comment
# not a heading
-->}" "$CLEAN_GH"
_expect comment-heading-ignored 0 '^ledger_check: 0 violations' '^R[123] '

# A fence or comment left open hides everything after it. Opened just below the
# closed heading, it hides that section's rows, and what is left would pass.
_run "${CLEAN/Closed — kept for the record/Closed — kept for the record

\`\`\`}" "$CLEAN_GH"
_expect unclosed-fence-is-error 2 'ERROR: .*fence.*line [0-9]+' '0 violations|^R[123] '

_run "${CLEAN/Closed — kept for the record/Closed — kept for the record

<!-- never closed}" "$CLEAN_GH"
_expect unclosed-comment-is-error 2 'ERROR: .*comment.*line [0-9]+' '0 violations|^R[123] '

_run "${CLEAN}
## Examples

\`\`\`\`
\`\`\`
| [#7](${U}/7) | Inside a four-backtick fence |
\`\`\`\`
" "${CLEAN_GH}
7 open false"
_expect longer-fence-not-closed-by-shorter 1 '^R1 #7: '

# --- references to another repository ------------------------------------------
_run "${CLEAN}
## Upstream

| Issue | Title |
|---|---|
| other/repo#7 | Tracked in another repository |
" "$CLEAN_GH"
_expect other-repo-ref-ignored 0 '^ledger_check: 0 violations' '#7'

# --- each violation says what to change -------------------------------------------
_run "$CLEAN" "${CLEAN_GH}
7 open false - \"Seventh thing\""
_expect r1-says-what-to-change 1 '^R1 #7: .*add a row for #7 "Seventh thing" to an open table'

_run "$CLEAN" "1 closed false 2026-09-07 \"Open thing\"
2 closed false
3 closed false
4 closed false
5 open false
6 open false"
_expect r2-says-what-to-change 1 '^R2 #1: .*strike the title with ~~ and add \*\*CLOSED on GitHub 2026-09-07\.\*\*, or move the row to a closed section'

_run "$CLEAN" "$OPEN2"
_expect r3-says-what-to-change 1 '^R3 #2: .*un-strike it or move it to an open table, or close the issue'

# --- how GitHub is read --------------------------------------------------------
_run "$CLEAN" "$CLEAN_GH"
if grep -q -- '--paginate' "${WORK}/gh.log" && grep -q 'repos/example/repo/issues?state=all' "${WORK}/gh.log"; then
  _ok fetch-asks-for-all-states
else
  OUT="$(cat "${WORK}/gh.log")"; _bad fetch-asks-for-all-states "gh was called as shown"
fi

_run "$CLEAN" "" 1 "error connecting to api.github.com"
_expect gh-fails-is-error 2 'ERROR: .*GitHub' '0 violations|^R[123] '

# A partial answer: the first page arrives, then a later page fails. The lines
# alone would pass, so only the exit code can catch it.
_run "$CLEAN" "$CLEAN_GH" 1 "HTTP 502: Bad Gateway (https://api.github.com/repositories/1/issues?page=2)"
_expect gh-partial-page-is-error 2 'ERROR: .*gh exit 1.*HTTP 502' '0 violations|^R[123] '

_run "$CLEAN" ""
_expect gh-empty-is-error 2 'ERROR: .*no issues' '0 violations|^R[123] '

_run "$CLEAN" "${CLEAN_GH}
<html>rate limited</html>"
_expect gh-garbage-is-error 2 'ERROR: .*unexpected' '0 violations|^R[123] '

# U+2028 is a line separator to str.splitlines(); in a title it must not split
# the line. gh's jq writes it raw inside the JSON string, as here.
LS="$(printf '\342\200\250')"
_run "$CLEAN" "${CLEAN_GH}
7 open false - \"Seventh${LS}thing\""
_expect title-with-line-separator 1 '^R1 #7: .*add a row for #7 "Seventh' 'ERROR'

_run "$CLEAN" "${CLEAN_GH}
1 closed false 2026-09-07 \"A second line for #1\""
_expect repeated-number-is-error 2 'ERROR: .*#1.*more than once' '0 violations|^R[123] '

printf '%s' "$CLEAN" > "${WORK}/BACKLOG.md"
set +e
# An empty PATH directory, not /usr/bin: GitHub's Ubuntu runners install gh there.
mkdir -p "${WORK}/empty"
OUT="$(PATH="${WORK}/empty" "$(command -v python3)" "$CHECK" --backlog "${WORK}/BACKLOG.md" --repo example/repo 2>&1)"
RC=$?
set -e
_expect gh-missing-is-error 2 'ERROR: .*gh' '0 violations'

set +e
OUT="$(PATH="${WORK}/bin:${PATH}" FAKE_GH_LOG="${WORK}/gh.log" FAKE_GH_OUT="${WORK}/gh.out" \
  python3 "$CHECK" --backlog "${WORK}/no-such-file.md" --repo example/repo 2>&1)"
RC=$?
set -e
_expect backlog-missing-is-error 2 'ERROR: .*no-such-file' '0 violations'

echo "${PASS} passed, ${FAIL} failed"
[ "${FAIL}" -eq 0 ]
