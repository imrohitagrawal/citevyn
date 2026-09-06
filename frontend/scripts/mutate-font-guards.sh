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
#   - judge KILLED by the runner's EXIT STATUS, never by grepping its output
#     for the word "failed" — `pytest -q` prints "1 xfailed" and one xfail
#     anywhere would false-kill every backend mutant
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

VITEST="cd '$FRONTEND' && npx vitest run src/test/emittedArtifact.test.ts src/test/buildGuards.test.ts"
PYTEST="cd '$BACKEND' && .venv/bin/python -m pytest -q -p no:randomly"

killed=0
survived=0

# What the tree looked like before we touched anything.
STATUS_BEFORE="$(cd "$REPO" && git status --porcelain)"

# apply <name> <file> <python-mutator-file> <command>
run_case() {
  local name="$1" file="$2" mutator="$3" cmd="$4"
  # Back up BEFORE arming the trap. The other order has a window in which an
  # interrupt makes the EXIT trap copy the PREVIOUS case's backup over this
  # file — restoring the wrong contents to the wrong path.
  cp "$file" "$BACKUP/keep"
  CURRENT="$file"
  # A mutator whose anchor has moved must report itself, not abort the run under
  # `set -e` and throw away every verdict printed so far.
  if ! python3 "$mutator" "$file" 2>"$BACKUP/muterr"; then
    echo "  NOT-APPLIED  $name - the mutator errored:"
    sed 's/^/      /' "$BACKUP/muterr" | tail -4
    cp "$BACKUP/keep" "$file"; CURRENT=""
    survived=$((survived + 1))
    return
  fi

  # The mutation must have CHANGED something. A silently-inapplicable edit
  # (a moved anchor, a reformat) otherwise reports as a survivor and sends the
  # next reader hunting a guard defect that does not exist.
  if cmp -s "$BACKUP/keep" "$file"; then
    echo "  NOT-APPLIED  $name — the anchor moved; fix the mutator, do not score this"
    cp "$BACKUP/keep" "$file"; CURRENT=""
    survived=$((survived + 1))
    return
  fi

  # The EXIT STATUS is the oracle, not a word in the output.
  #
  # This used to be `grep -qE 'FAILED|failed|...'` over the captured output,
  # and that is a false-kill waiting to happen: `pytest -q` prints "1 xfailed",
  # which contains "failed". One `@pytest.mark.xfail` anywhere in the six files
  # this runs would have converted all eleven backend mutants into permanent
  # KILLED verdicts that prove nothing — verified by adding one and watching
  # the pattern match. A test runner's exit code says "did anything fail", and
  # that is exactly the question.
  local out status
  set +e
  out="$(eval "$cmd" 2>&1)"
  status=$?
  set -e
  cp "$BACKUP/keep" "$file"
  cmp -s "$BACKUP/keep" "$file" || { echo "RESTORE FAILED for $file"; exit 2; }
  CURRENT=""

  if [ "$status" -ne 0 ]; then
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
echo "=== the EMITTED artifact (frontend/src/test/emittedArtifact.test.ts) ==="

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

run_case "off-origin stylesheet via a double-backslash URL" "$FRONTEND/index.html" \
  "$(mut c <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8").read()
open(p,"w",encoding="utf8").write(s.replace("  </head>",
  '    <link rel="stylesheet" href="https:' + chr(92)*2 + 'x.example/a.css" />' + chr(10) + '  </head>',1))
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
echo "=== the bypasses adversarial review demonstrated ==="

cat > "$M/v.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf8").read()
tag = '    <link rel="stylesheet" title="Brand > Fonts" href="https://fonts.googleapis.com/css2" />\n'
open(p, "w", encoding="utf8").write(s.replace("  </head>", tag + "  </head>", 1))
PY
run_case "off-origin stylesheet behind a quoted '>'" "$FRONTEND/index.html" \
  "$M/v.py" "$VITEST"

cat > "$M/w.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf8").read()
# A stray quote in an UNQUOTED value shifted quote parity for the rest of the
# document under the old hand-rolled tokeniser.
tag = '    <link rel=stylesheet a=x" b="c>d" href="https://fonts.googleapis.com/css2">\n'
open(p, "w", encoding="utf8").write(s.replace("  </head>", tag + "  </head>", 1))
PY
run_case "off-origin stylesheet behind a quote-parity shift" "$FRONTEND/index.html" \
  "$M/w.py" "$VITEST"

cat > "$M/x.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf8").read()
# ASCII tab is removed from anywhere in a URL by the WHATWG parser.
tag = '    <link rel="stylesheet" href="http:\t//fonts.googleapis.com/css2" />\n'
open(p, "w", encoding="utf8").write(s.replace("  </head>", tag + "  </head>", 1))
PY
run_case "off-origin stylesheet via a tab inside the URL" "$FRONTEND/index.html" \
  "$M/x.py" "$VITEST"

cat > "$M/y.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf8").read()
# The HTML parser decodes character references before the URL parser sees them.
tag = '    <link rel="stylesheet" href="http:&#x2F;&#x2F;fonts.googleapis.com/css2" />\n'
open(p, "w", encoding="utf8").write(s.replace("  </head>", tag + "  </head>", 1))
PY
run_case "off-origin stylesheet via character references" "$FRONTEND/index.html" \
  "$M/y.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/z.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf8").read()
# Form-feed terminator plus a backslash line continuation: both defeated the
# first version of the CSS escape reader, and the @import really loads.
inj = "@" + chr(92) + "69" + chr(12) + "mport " + chr(34) + "/" + chr(92) + chr(10)
inj += "/fonts.googleapis.com/css2" + chr(34) + ";" + chr(10)
open(p, "w", encoding="utf8").write(inj + s)
PY
run_case "about.css @import via form-feed + line continuation" "$FRONTEND/public/about.css" \
  "$M/z.py" "$PYTEST $BACKEND_FONT_TESTS"

cat > "$M/aa.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf8").read()
# The Dockerfile stops using `npm run build` while the words survive in a
# COMMENT — the exact edit that defeated the whole-file substring check.
i = s.index("RUN VITE_API_DEMO_KEY=")
j = s.index(chr(10) + chr(10), i)
repl = ("# Historically this was `npm run build`; now built directly." + chr(10)
        + "RUN npx vite build --manifest --mode staging " + chr(92) + chr(10)
        + "    && test -f dist/index.html")
open(p, "w", encoding="utf8").write(s[:i] + repl + s[j:])
PY
run_case "the image stops building via the pinned npm script" "$REPO/infra/docker/Dockerfile.api" \
  "$M/aa.py" "$VITEST"

cat > "$M/ab.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf8").read()
q = chr(34)
# A build plugin that hides from the guard by branching on vitest's own env.
#
# THIS MUST TYPE-CHECK. The first version of this mutator injected the helper
# names `char34`/`char39` as literal TypeScript identifiers, so `tsc -b` failed,
# `npm run build` exited non-zero, `beforeAll` threw, all 8 tests SKIPPED, and
# the exit-status oracle reported KILLED — while the env-parity assertion never
# ran. A false kill dressed as the headline result. Review caught it; the guard
# was sound and its evidence was not.
plugin = (
    "function smuggle() {" + chr(10)
    + "  return {" + chr(10)
    + "    name: " + q + "smuggle" + q + "," + chr(10)
    + "    apply: " + q + "build" + q + " as const," + chr(10)
    + "    transformIndexHtml(html: string) {" + chr(10)
    + "      if (process.env.VITEST) return html;" + chr(10)
    + "      return html.replace(" + chr(10)
    + "        " + q + "</head>" + q + "," + chr(10)
    + "        " + q + "<link rel=" + chr(92) + q + "stylesheet" + chr(92) + q
    + " href=" + chr(92) + q + "https://fonts.googleapis.com/css2" + chr(92) + q
    + "></head>" + q + "," + chr(10)
    + "      );" + chr(10)
    + "    }," + chr(10)
    + "  };" + chr(10)
    + "}" + chr(10)
)
old = "plugins: [react(), liveStubPlugin()],"
assert old in s, "plugins anchor missing"
new = "plugins: [react(), liveStubPlugin(), smuggle()],"
open(p, "w", encoding="utf8").write(plugin + s.replace(old, new, 1))
PY
run_case "a build plugin branching on the vitest environment" "$FRONTEND/vite.config.ts" \
  "$M/ab.py" "$VITEST"

cat > "$M/ac.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf8").read()
# A runtime-injected third-party stylesheet built from string PARTS, which the
# about-theme.js scan explicitly does NOT claim to catch. Recorded as a KNOWN
# SURVIVOR so the guard's stated limit is checked rather than merely asserted.
q = chr(34)
js = (chr(10) + "var h=[" + q + "ht" + q + "," + q + "tps:" + q + "," + q + "//" + q + ","
      + q + "fonts.googleapis.com/css2" + q + "].join(" + q + q + ");"
      + "var l=document.createElement(" + q + "link" + q + ");"
      + "l.rel=" + q + "stylesheet" + q + ";l.href=h;document.head.appendChild(l);" + chr(10))
open(p, "w", encoding="utf8").write(s + js)
PY
echo "  (next case is a KNOWN SURVIVOR - see the limits note in test_about_page_tokens.py)"
cp "$FRONTEND/public/about-theme.js" "$BACKUP/known_survivor"
CURRENT="$FRONTEND/public/about-theme.js"
python3 "$M/ac.py" "$CURRENT"
if cmp -s "$BACKUP/known_survivor" "$CURRENT"; then
  echo "  NOT-APPLIED  concatenated runtime injection"
  survived=$((survived + 1))
  cp "$BACKUP/known_survivor" "$CURRENT"; CURRENT=""
else
  set +e
  eval "$PYTEST $BACKEND_FONT_TESTS" >/dev/null 2>&1
  ks=$?
  set -e
  cp "$BACKUP/known_survivor" "$CURRENT"
  cmp -s "$BACKUP/known_survivor" "$CURRENT" || { echo "RESTORE FAILED"; exit 2; }
  CURRENT=""
  if [ "$ks" -eq 0 ]; then
    echo "  SURVIVED-AS-DOCUMENTED  concatenated runtime injection (a static scan cannot see it)"
  else
    echo "  UNEXPECTEDLY-KILLED  concatenated runtime injection - the guard got stronger;"
    echo "                       update its 'what it cannot see' note and this case"
    survived=$((survived + 1))
  fi
fi

echo
echo "KILLED: $killed   SURVIVED/NOT-APPLIED: $survived"

# Compare against the status recorded BEFORE the first mutation, not against a
# clean tree: this is meant to be runnable mid-change, and demanding a pristine
# repo would make it unusable exactly when it is most useful. What matters is
# that the run left NOTHING BEHIND — that the diff is unchanged either way.
cd "$REPO"
if ! diff -q <(printf '%s' "$STATUS_BEFORE") <(git status --porcelain) >/dev/null; then
  echo "THE RUN CHANGED THE TREE — a restore failed. Before vs after:" >&2
  diff <(printf '%s' "$STATUS_BEFORE") <(git status --porcelain) >&2 || true
  exit 2
fi
echo "tree unchanged by the run"
[ "$survived" -eq 0 ]
