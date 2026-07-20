#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_check_budget.sh — bash assertions for scripts/check_budget.sh (#153 L5).
#
# Same convention as test_env_guard.sh: no framework, each case runs the unit
# under test in a subshell and asserts the exit code plus a stdout/stderr
# substring. Run from the repo root:
#
#   bash tests/shell/test_check_budget.sh
#
# NO NETWORK. The provider call is stubbed by injecting the response body, so
# this suite is free, hermetic, and safe in CI — which matters here more than
# usual: the script it tests exists to keep us from spending money.
#
# The exit-code contract is the thing worth guarding, because a deploy gate
# branches on it:
#   0 = checked, has headroom
#   1 = checked, and it is LOW (or the response was unparseable) -> block
#   2 = could NOT check (no key) -> warn, do not block
# Collapsing 1 and 2 would either block every deploy on a host without an
# OpenRouter key, or let an exhausted key through. Both are wrong.
#
# Cases:
#   1. no key anywhere                                  -> 2
#   2. key at 96.4% used, $0.04 left (the REAL incident) -> 1
#   3. plenty of headroom                                -> 0
#   4. no provider-side limit set at all                 -> 1 (§0 requires one)
#   5. unparseable response                              -> 1 (never a false pass)
#   6. exactly at the threshold                          -> 0 (>=, not >)
#   7. 90% used but above the threshold                  -> 0, with a warning
#   8. the provider key never appears in curl argv (stdin --config, #200)
# ────────────────────────────────────────────────────────────────────────────
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); echo "  [PASS] $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  [FAIL] $1" >&2; }

# Exercise the SAME parse/threshold logic the script runs, by extracting the
# embedded python program from the script itself rather than duplicating it here.
# A copy would drift, and a drifted copy of a budget check is worse than none.
extract_py() {
    awk '/^BUDGET_RESPONSE=/{flag=1; next} /^PY$/{flag=0} flag' "${REPO_ROOT}/scripts/check_budget.sh"
}

run_parse() {  # run_parse <json> <threshold> -> exit code; output on stdout+stderr
    BUDGET_RESPONSE="$1" python3 -c "$(extract_py)" "$2" 2>&1
}

echo "==> check_budget.sh"

# 1. No key anywhere -> exit 2 ("cannot check"), distinct from "low".
out="$(cd /tmp && CITEVYN_OPENROUTER_API_KEY= "${REPO_ROOT}/scripts/check_budget.sh" 2>&1)"
rc=$?
if [[ ${rc} -eq 2 ]] && grep -q "is not set" <<<"${out}"; then
    ok "no key -> exit 2 (cannot check, NOT 'low')"
else
    bad "no key -> expected exit 2, got ${rc}: ${out}"
fi

# 2. The real incident: $1.06 of $1.10.
out="$(run_parse '{"data":{"usage":1.06,"limit":1.10}}' 1)"; rc=$?
if [[ ${rc} -eq 1 ]] && grep -q "FAIL" <<<"${out}"; then
    ok "96.4% used, \$0.04 left -> exit 1 (the real incident)"
else
    bad "exhausted key -> expected exit 1, got ${rc}: ${out}"
fi

# 3. Healthy key.
out="$(run_parse '{"data":{"usage":0.5,"limit":10}}' 1)"; rc=$?
if [[ ${rc} -eq 0 ]] && grep -q "9.5000 remaining" <<<"${out}"; then
    ok "plenty of headroom -> exit 0"
else
    bad "healthy key -> expected exit 0, got ${rc}: ${out}"
fi

# 4. No provider-side limit. COST_CONTROLS §0 calls the provider cap the only
#    layer app code cannot bypass, so its ABSENCE must not read as a pass.
out="$(run_parse '{"data":{"usage":3.0,"limit":null}}' 1)"; rc=$?
if [[ ${rc} -eq 1 ]] && grep -q "NO provider-side limit" <<<"${out}"; then
    ok "no provider-side limit -> exit 1 (absence is not a pass)"
else
    bad "no limit -> expected exit 1, got ${rc}: ${out}"
fi

# 5. Garbage in must not become a green light.
out="$(run_parse '{"garbage":1}' 1)"; rc=$?
if [[ ${rc} -eq 1 ]] && grep -q "could not parse" <<<"${out}"; then
    ok "unparseable response -> exit 1 (never a false pass)"
else
    bad "garbage -> expected exit 1, got ${rc}: ${out}"
fi

# 6. Exactly at the threshold is ACCEPTABLE (the check is `remaining < threshold`).
out="$(run_parse '{"data":{"usage":9.0,"limit":10}}' 1)"; rc=$?
if [[ ${rc} -eq 0 ]]; then
    ok "remaining exactly == threshold -> exit 0"
else
    bad "at-threshold -> expected exit 0, got ${rc}: ${out}"
fi

# 7. Above the threshold but past 85%: pass, with a loud warning.
out="$(run_parse '{"data":{"usage":90,"limit":100}}' 1)"; rc=$?
if [[ ${rc} -eq 0 ]] && grep -q "85%" <<<"${out}"; then
    ok "90% used but above threshold -> exit 0 with an 85% warning"
else
    bad "85% warning -> expected exit 0 + warning, got ${rc}: ${out}"
fi

# 8. The provider key must NEVER appear in curl's argv — argv is world-readable
#    via `ps aux` on a multi-user host, and also lands in process accounting.
#    deploy_verify.sh already feeds bearers through `curl --config -` on stdin;
#    this asserts check_budget.sh does the same (#200).
#
#    Hermetic: a `curl` shim on PATH records its argv and its stdin, then emits
#    a healthy-key JSON body so the script proceeds normally. No network.
#
#    The script is run with stdin on /dev/null so the shim's `cat` cannot block:
#    under the OLD `-H` form curl inherits the caller's stdin rather than a
#    pipe, and without this the suite would HANG instead of failing.
shimdir="$(mktemp -d)"
cat > "${shimdir}/curl" <<'SHIM'
#!/usr/bin/env bash
printf '%s\n' "$*" > "${SHIM_ARGV_FILE}"
cat > "${SHIM_STDIN_FILE}"
printf '%s' '{"data":{"usage":0.5,"limit":10}}'
SHIM
chmod +x "${shimdir}/curl"

SHIM_ARGV_FILE="${shimdir}/argv" SHIM_STDIN_FILE="${shimdir}/stdin" \
    PATH="${shimdir}:${PATH}" \
    CITEVYN_OPENROUTER_API_KEY=sk-or-SENTINEL-DO-NOT-LEAK \
    bash -c "cd /tmp && '${REPO_ROOT}/scripts/check_budget.sh'" </dev/null >/dev/null 2>&1

argv="$(cat "${shimdir}/argv" 2>/dev/null || true)"
stdin_seen="$(cat "${shimdir}/stdin" 2>/dev/null || true)"

if grep -q "SENTINEL" <<<"${argv}"; then
    bad "provider key LEAKED into curl argv: ${argv}"
elif ! grep -q -- "--config" <<<"${argv}"; then
    bad "curl was not invoked with --config (argv: ${argv})"
elif ! grep -q "Authorization: Bearer sk-or-SENTINEL-DO-NOT-LEAK" <<<"${stdin_seen}"; then
    bad "the bearer never reached curl on stdin (stdin: ${stdin_seen})"
else
    ok "provider key goes to curl on stdin via --config, never in argv"
fi
rm -rf "${shimdir}"

echo "  passed: ${PASS}  failed: ${FAIL}"
[[ ${FAIL} -eq 0 ]] || exit 1
