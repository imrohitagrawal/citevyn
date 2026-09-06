#!/bin/bash
#
# Mutation harness for the self-hosted-font guards, #365.
#
# WHAT IT PROTECTS. index.html and the server-rendered /about page used to load
# their two faces with a render-blocking <link rel="stylesheet"> pointing at
# fonts.googleapis.com. A render-blocking stylesheet also blocks
# <script type="module"> from EXECUTING, and this app is one module entry — so a
# visitor whose network was slow to that host got a BLANK page, not an unstyled
# one. Measured on the production build: 193/194/245 ms to first rendered UI
# normally, >45 s (timed out, blank throughout) with that host stalled.
#
# The faces are now served from this origin, and the guards below are what stop
# that silently regressing. This script is the evidence for the "N mutants, N
# killed" claim in the commit message and docs/BACKLOG.md — without it that
# number is an assertion, which is why every other guard in this repo ships a
# mutate-*.sh beside it.
#
# Run it with:
#
#     bash frontend/scripts/mutate-font-guards.sh
#
# Discipline this encodes, matching scripts/mutate-a11y-guards.sh and
# scripts/mutate-focus-ring.sh:
#   - prove a GREEN CONTROL first. Without it every "KILLED" is unfalsifiable.
#   - assert the mutation ACTUALLY APPLIED; a no-op edit reads as a survivor,
#     and this repo has been burned by exactly that (ruff format silently
#     undoing a mutation, producing three phantom survivors).
#   - byte-copy restore and verify with cmp after EVERY mutation, and restore on
#     interrupt too.
#   - run sequentially in ONE tree; parallel writers corrupt each other.
#
# It does NOT run the Playwright specs: those need a dev server on port 3000 and
# are slow. tests/fonts-offline.spec.ts's own bite-proof is recorded in the PR —
# reverting the fix turns its first two tests into 20 s timeouts on a blank
# page. What this covers is the static half, which is the half that runs on
# every PR.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND="$REPO/frontend"
BACKEND="$REPO/backend"
BACKUP="$(mktemp -d)"
CURRENT=""

restore() {
  if [ -n "$CURRENT" ] && [ -f "$BACKUP/keep" ]; then
    cp "$BACKUP/keep" "$CURRENT"
  fi
}
# Restore on interrupt too. An EXIT trap that only deletes backups leaves a
# mutated file in the tree with no way back.
trap 'restore; rm -rf "$BACKUP"' EXIT INT TERM

VITEST="cd '$FRONTEND' && npx vitest run src/test/buildGuards.test.ts"
PYTEST="cd '$BACKEND' && .venv/bin/python -m pytest -q -p no:randomly"

killed=0
survived=0

# What the tree looked like before we touched anything.
STATUS_BEFORE="$(cd "$REPO" && git status --porcelain)"

# apply <name> <file> <python-mutator-file> <command>
run_case() {
  local name="$1" file="$2" mutator="$3" cmd="$4"
  CURRENT="$file"
  cp "$file" "$BACKUP/keep"
  python3 "$mutator" "$file"

  # The mutation must have CHANGED something. A silently-inapplicable edit
  # (a moved anchor, a reformat) otherwise reports as a survivor and sends the
  # next reader hunting a guard defect that does not exist.
  if cmp -s "$BACKUP/keep" "$file"; then
    echo "  NOT-APPLIED  $name — the anchor moved; fix the mutator, do not score this"
    cp "$BACKUP/keep" "$file"; CURRENT=""
    survived=$((survived + 1))
    return
  fi

  local out
  out="$(eval "$cmd" 2>&1 || true)"
  cp "$BACKUP/keep" "$file"
  cmp -s "$BACKUP/keep" "$file" || { echo "RESTORE FAILED for $file"; exit 2; }
  CURRENT=""

  if grep -qE 'FAILED|failed|✗|×' <<<"$out"; then
    echo "  KILLED       $name"
    killed=$((killed + 1))
  else
    echo "  SURVIVED     $name"
    echo "$out" | tail -20
    survived=$((survived + 1))
  fi
}

mut() { # write a python mutator to a temp file and echo its path
  local f
  f="$BACKUP/mut_$1.py"
  cat > "$f"
  echo "$f"
}

echo "=== GREEN CONTROL (every KILLED below is meaningless without this) ==="
if ! eval "$VITEST" >/dev/null 2>&1; then
  echo "CONTROL FAILED: buildGuards.test.ts is red before any mutation." >&2
  exit 1
fi
if ! eval "$PYTEST '$BACKEND/tests/test_csp_covers_the_pages_real_origins.py' \
  '$BACKEND/tests/test_about_page_tokens.py' '$BACKEND/tests/test_security_headers.py' \
  '$BACKEND/tests/test_about_page.py' '$BACKEND/tests/test_about_page_renderer.py' \
  '$BACKEND/tests/test_frontend_assets.py'" >/dev/null 2>&1; then
  echo "CONTROL FAILED: the backend font/CSP tests are red before any mutation." >&2
  exit 1
fi
echo "  control green"

BACKEND_FONT_TESTS="'$BACKEND/tests/test_csp_covers_the_pages_real_origins.py' \
'$BACKEND/tests/test_about_page_tokens.py' '$BACKEND/tests/test_security_headers.py' \
'$BACKEND/tests/test_about_page.py' '$BACKEND/tests/test_about_page_renderer.py' \
'$BACKEND/tests/test_frontend_assets.py'"

echo
echo "=== the EMITTED artifact (frontend/src/test/buildGuards.test.ts) ==="

run_case "off-origin stylesheet in index.html" "$FRONTEND/index.html" \
  "$(mut a <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(s.replace("  </head>",
  '    <link rel="stylesheet" href="https://x.example/a.css" />\n  </head>',1))
PY
)" "$VITEST"

run_case "off-origin stylesheet hidden behind a quoted '>'" "$FRONTEND/index.html" \
  "$(mut b <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(s.replace("  </head>",
  '    <link rel="stylesheet" title="Brand > Fonts" href="https://x.example/a.css" />\n  </head>',1))
PY
)" "$VITEST"

run_case "off-origin stylesheet via a backslash URL" "$FRONTEND/index.html" \
  "$(mut c <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(s.replace("  </head>",
  '    <link rel="stylesheet" href="https:\\x.example/a.css" />\n  </head>',1))
PY
)" "$VITEST"

run_case "off-origin preconnect in index.html" "$FRONTEND/index.html" \
  "$(mut d <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(s.replace("  </head>",
  '    <link rel="preconnect" href="https://fonts.gstatic.com" />\n  </head>',1))
PY
)" "$VITEST"

run_case "@font-face src back on gstatic" "$FRONTEND/src/styles/fonts.css" \
  "$(mut e <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(
  s.replace('url("/fonts/geist-latin.woff2")','url("https://fonts.gstatic.com/s/geist/v5/x.woff2")',1))
PY
)" "$VITEST"

run_case "emitted CSS @imports a third party (minified form)" "$FRONTEND/src/styles/fonts.css" \
  "$(mut f <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write('@import url("https://x.example/a.css");\n'+s)
PY
)" "$VITEST"

run_case "fonts.css never imported" "$FRONTEND/src/main.tsx" \
  "$(mut g <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(s.replace('import "./styles/fonts.css";\n','',1))
PY
)" "$VITEST"

run_case "a declared woff2 does not ship" "$FRONTEND/src/styles/fonts.css" \
  "$(mut h <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(
  s.replace('"/fonts/jetbrains-mono-latin.woff2"','"/fonts/nope.woff2"',1))
PY
)" "$VITEST"

run_case "the guard builds something other than what ships" "$FRONTEND/package.json" \
  "$(mut i <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(s.replace('"build": "tsc -b && vite build"','"build": "vite build"',1))
PY
)" "$VITEST"

run_case "fixture reads .woff2 from the wrong home" "$FRONTEND/tests/fixtures.ts" \
  "$(mut j <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(
  s.replace('join(HERE, "..", "public", "fonts")','join(HERE, "fonts")',1))
PY
)" "$VITEST"

echo
echo "=== the CSP and the two pages (backend) ==="

M="$BACKUP"

cat > "$M/k.py" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
q=chr(39)
old='"style-src '+q+'self'+q+'; "'
new='"style-src '+q+'self'+q+' https://fonts.googleapis.com; "'
assert old in s
open(p,"w",encoding="utf8").write(s.replace(old,new,1))
PY
run_case "CSP re-widened to Google Fonts" "$BACKEND/app/core/security_headers.py" \
  "$M/k.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/l.py" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
q=chr(39)
old='"style-src '+q+'self'+q+'; "'
assert old in s
open(p,"w",encoding="utf8").write(s.replace(old,'"style-src *; "',1))
PY
run_case "CSP style-src wildcarded" "$BACKEND/app/core/security_headers.py" \
  "$M/l.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/m.py" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
q=chr(39)
old="        + f"+q+chr(60)+"link rel="+chr(34)+"stylesheet"+chr(34)+" href="+chr(34)+"{STYLESHEET_URL}"+chr(34)+chr(62)+chr(92)+"n"+q
new=("        + "+q+chr(60)+"link rel="+chr(34)+"stylesheet"+chr(34)+" href="+chr(34)
     +"https://fonts.googleapis.com/css2?family=Geist"+chr(34)+chr(62)+chr(92)+"n"+q+"\n"+old)
assert old in s, "anchor missing"
open(p,"w",encoding="utf8").write(s.replace(old,new,1))
PY
run_case "/about re-adds a third-party font stylesheet" "$BACKEND/app/services/about_page.py" \
  "$M/m.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/n.py" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
old=('_FONT_PRELOAD_URLS = (\n    "/fonts/geist-latin.woff2",\n'
     '    "/fonts/jetbrains-mono-latin.woff2",\n)')
assert old in s
open(p,"w",encoding="utf8").write(s.replace(old,"_FONT_PRELOAD_URLS: tuple[str, ...] = ()",1))
PY
run_case "/about drops its font preloads" "$BACKEND/app/services/about_page.py" \
  "$M/n.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/o.py" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write('@\\69mport "\\/\\/x.example/a.css";\n'+s)
PY
run_case "about.css @imports a third party (escaped at-keyword)" "$FRONTEND/public/about.css" \
  "$M/o.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/p.py" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
inject=('\nvar l=document.createElement("link");l.rel="stylesheet";'
        'l.href="https://fonts.googleapis.com/css2?family=Geist";document.head.appendChild(l);\n')
open(p,"w",encoding="utf8").write(s+inject)
PY
run_case "about-theme.js injects a third-party stylesheet" "$FRONTEND/public/about-theme.js" \
  "$M/p.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/q.py" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(s.replace("  font-weight: 400 700;","  font-weight: 300 700;",1))
PY
run_case "about.css @font-face weight drifts from the app" "$FRONTEND/public/about.css" \
  "$M/q.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/r.py" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(s.replace("  font-display: swap;\n","",1))
PY
run_case "font-display: swap dropped" "$FRONTEND/src/styles/fonts.css" \
  "$M/r.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/s.py" <<'PY'
import sys
p=sys.argv[1]; b=bytearray(open(p,"rb").read()); b[-1] ^= 0xFF
open(p,"wb").write(bytes(b))
PY
run_case "a shipped font binary is not the vetted bytes" "$FRONTEND/public/fonts/geist-latin.woff2" \
  "$M/s.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/t.py" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(s.replace(
  "Copyright (c) 2023 Vercel, in collaboration with basement.studio",
  "Copyright (c) 2023 Someone Else",1))
PY
run_case "the OFL copyright notice is altered" "$FRONTEND/public/fonts/OFL.txt" \
  "$M/t.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/u.py" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(
  s.replace('href="/fonts/geist-latin.woff2"','href="/fonts/geist-latin-v2.woff2"',1))
PY
run_case "index.html font preload points at a missing file" "$FRONTEND/index.html" \
  "$M/u.py" "$PYTEST $BACKEND_FONT_TESTS"

echo
echo "KILLED: $killed   SURVIVED/NOT-APPLIED: $survived"

# Compare against the status recorded BEFORE the first mutation, not against a
# clean tree: this is meant to be runnable mid-change, and demanding a pristine
# repo would make it unusable exactly when it is most useful. What matters is
# that the run left NOTHING BEHIND — that the diff is unchanged either way.
cd "$REPO"
if ! diff -q <(printf '%s\n' "$STATUS_BEFORE") <(git status --porcelain) >/dev/null; then
  echo "THE RUN CHANGED THE TREE — a restore failed. Before vs after:" >&2
  diff <(printf '%s\n' "$STATUS_BEFORE") <(git status --porcelain) >&2 || true
  exit 2
fi
echo "tree unchanged by the run"
[ "$survived" -eq 0 ]
