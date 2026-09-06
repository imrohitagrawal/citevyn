#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_deploy_verify_bundle_gate.sh — assertions for deploy_verify.sh's
# frontend-bundle check (#296).
#
# WHY THIS FILE EXISTS
# --------------------
# `deploy_verify.sh` is the release gate, and its bundle check is the one part
# of it that cannot be exercised by `--dry-run`: the whole gate needs docker
# and a live compose stack, so in practice NOTHING turned red if that block
# broke. A typo in the `find … | check_bundle_key.sh` wiring would have shipped
# undetected — and the block's entire job is to stop a repeat of release v6.
#
# So this extracts the real block from the real script (never a copy that can
# drift) and drives it against fixture `frontend/dist` trees with a stub
# `record`. No docker, no compose, no network.
#
# WHAT IT CANNOT COVER, STATED PLAINLY
# ------------------------------------
# The surrounding gate — `read_env`, the probe suite, the rollback drills, the
# PASS/FAIL tally — still has no automated coverage and still needs a live
# stack. This covers the block that changed, not the script.
#
# Run from the repo root:
#   bash tests/shell/test_deploy_verify_bundle_gate.sh
# ────────────────────────────────────────────────────────────────────────────

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATE="${REPO_ROOT}/infra/docker/scripts/deploy_verify.sh"
FAILURES=0
ASSERTIONS=0
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

pass() { echo "  ok   — $1"; ASSERTIONS=$((ASSERTIONS + 1)); }
fail() { echo "  FAIL — $1"; FAILURES=$((FAILURES + 1)); ASSERTIONS=$((ASSERTIONS + 1)); }

echo "test_deploy_verify_bundle_gate.sh"

GOOD_KEY="tEsTkEy_0123456789abcdefghijklmnopqrstuvwxyz"

# ── Extract the block under test straight out of the shipped script. ───────
# Anchored on the real `if [[ -d ... ]]` line so this can never test a stale
# transcription of the logic.
BLOCK="$(awk '/^    if \[\[ -d "\$\{REPO_ROOT\}\/frontend\/dist" \]\]; then$/,/^    fi$/' "${GATE}")"

if [[ -n "${BLOCK}" ]] && printf '%s' "${BLOCK}" | grep -q 'check_bundle_key.sh'; then
    pass "the bundle-check block was located in deploy_verify.sh"
else
    fail "could not extract the bundle-check block from ${GATE} — the anchor moved"
    echo "test_deploy_verify_bundle_gate.sh: ${FAILURES} of ${ASSERTIONS} assertions FAILED"
    exit 1
fi

# ── Harness: a fixture repo root with a stub `record`, running the REAL block.
run_gate() {  # run_gate <demo-key> ; fixture dist already built at $WORK/root
    cat > "${WORK}/harness.sh" <<EOF
set -uo pipefail
REPO_ROOT="${WORK}/root"
DEMO_KEY="\$1"
record() { echo "[\$1]\${3:+ \$3}"; }
${BLOCK}
EOF
    bash "${WORK}/harness.sh" "$1" 2>&1
}

new_fixture() {  # new_fixture — empty dist tree, symlink the real scripts dir
    rm -rf "${WORK}/root"
    mkdir -p "${WORK}/root/frontend/dist/assets" "${WORK}/root/scripts"
    cp "${REPO_ROOT}/scripts/check_bundle_key.sh" "${WORK}/root/scripts/"
}

bundle() {  # bundle <baked-value> [relative-path]
    local path="${2:-frontend/dist/assets/index-abc123.js}"
    mkdir -p "${WORK}/root/$(dirname "${path}")"
    printf 'var a=1;const Cf="%s";H.set("Authorization",`Bearer ${Cf}`);' "$1" \
        > "${WORK}/root/${path}"
}

expect() {  # expect <PASS|FAIL> <label> <output>
    if printf '%s' "$3" | grep -q "^\[$1\]"; then
        pass "$2"
    else
        fail "$2 — expected [$1], got: $3"
    fi
}

# ── 1. Happy path. Without this every FAIL case below could be satisfied by a
#      block that fails unconditionally. ───────────────────────────────────
new_fixture; bundle "${GOOD_KEY}"
expect PASS "a bundle carrying the current demo key records PASS" "$(run_gate "${GOOD_KEY}")"

# ── 2. THE #296 BLIND SPOT: built with an empty --build-arg. ───────────────
new_fixture; bundle ""
expect FAIL "a bundle baked with an EMPTY build arg records FAIL" "$(run_gate "${GOOD_KEY}")"

# ── 3. The v6 outage: the arg was never passed. ───────────────────────────
new_fixture; bundle "local-demo-key"
OUT="$(run_gate "${GOOD_KEY}")"
expect FAIL "a bundle carrying the public default records FAIL" "${OUT}"
# F3 from review: the gate used to discard the checker's diagnosis with
# >/dev/null 2>&1 and blame the bundle for every cause, including a missing
# script. The sharper reason must survive into the recorded detail.
if printf '%s' "${OUT}" | grep -q 'PUBLIC DEFAULT'; then
    pass "the recorded FAIL carries the checker's own diagnosis"
else
    fail "the FAIL detail lost the checker's diagnosis: ${OUT}"
fi

# ── 4. A local developer whose .env legitimately uses the default. The OLD
#      check special-cased this with `&& [[ DEMO_KEY != local-demo-key ]]`;
#      the presence form must keep it passing without that special case.
new_fixture; bundle "local-demo-key"
expect PASS "a default-key bundle PASSES when .env also uses the default" \
    "$(run_gate "local-demo-key")"

# ── 5. Nested / non-glob locations. The first version globbed
#      dist/assets/*.js one level deep, so these recorded a FALSE FAIL on a
#      perfectly good bundle — the very chunk refactor a presence check is
#      supposed to survive.
new_fixture; bundle "${GOOD_KEY}" "frontend/dist/assets/sub/lazy-9f.js"
expect PASS "a key in a NESTED chunk still records PASS" "$(run_gate "${GOOD_KEY}")"

new_fixture; bundle "${GOOD_KEY}" "frontend/dist/index.html"
expect PASS "a key inlined into index.html still records PASS" "$(run_gate "${GOOD_KEY}")"

# ── 6. Fail-closed cases: nothing to read must never read as success. ──────
new_fixture   # dist/assets exists but is empty
expect FAIL "an empty dist records FAIL (never a silent PASS)" "$(run_gate "${GOOD_KEY}")"

new_fixture; bundle "${GOOD_KEY}"
rm -f "${WORK}/root/scripts/check_bundle_key.sh"
expect FAIL "a MISSING check_bundle_key.sh records FAIL, not PASS" "$(run_gate "${GOOD_KEY}")"

new_fixture; bundle "${GOOD_KEY}"
chmod 644 "${WORK}/root/scripts/check_bundle_key.sh"
expect FAIL "a NON-EXECUTABLE check_bundle_key.sh records FAIL, not PASS" "$(run_gate "${GOOD_KEY}")"

# ── 7. A repo root with spaces and a dollar sign — quoting regression. ────
rm -rf "${WORK}/ro ot\$x"
new_fixture; bundle "${GOOD_KEY}"
cp -R "${WORK}/root" "${WORK}/ro ot\$x"
OUT="$(
    cat > "${WORK}/h2.sh" <<EOF
set -uo pipefail
REPO_ROOT="${WORK}/ro ot\\\$x"
DEMO_KEY="\$1"
record() { echo "[\$1]\${3:+ \$3}"; }
${BLOCK}
EOF
    bash "${WORK}/h2.sh" "${GOOD_KEY}" 2>&1
)"
expect PASS "a REPO_ROOT containing spaces and \$ still records PASS" "${OUT}"

# ── 8. The script parses at all. ──────────────────────────────────────────
if bash -n "${GATE}" 2>/dev/null; then
    pass "deploy_verify.sh parses"
else
    fail "deploy_verify.sh has a syntax error"
fi

# ── Non-vacuity, pinned exactly. A slack floor lets a whole case be deleted
#    silently; bump this deliberately when you add one.
_EXPECTED_ASSERTIONS=13
if [[ ${ASSERTIONS} -ne ${_EXPECTED_ASSERTIONS} ]]; then
    echo "  FAIL — ${ASSERTIONS} assertions ran; expected exactly ${_EXPECTED_ASSERTIONS}."
    FAILURES=$((FAILURES + 1))
fi

echo
if [[ ${FAILURES} -eq 0 ]]; then
    echo "test_deploy_verify_bundle_gate.sh: ${ASSERTIONS} assertions, all passed"
    exit 0
fi
echo "test_deploy_verify_bundle_gate.sh: ${FAILURES} of ${ASSERTIONS} assertions FAILED"
exit 1
