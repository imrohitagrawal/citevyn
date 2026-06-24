#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_env_guard.sh — bash assertions for infra/docker/scripts/_env_guard.sh.
#
# No external test framework (bats / shunit2) — each test runs the guard in
# a subshell against a temp fixture .env and asserts (a) the exit code and
# (b) that stderr contains an expected substring. Run from the repo root:
#
#   bash tests/shell/test_env_guard.sh
#
# Exit code 0 = all pass, 1 = at least one failure.
#
# These tests guard against regressions in the four critical paths:
#   1. stub POSTGRES_PASSWORD / CITEVYN_ADMIN_API_KEY — rejected
#   2. missing CITEVYN_ACME_EMAIL — rejected
#   3. CITEVYN_ACME_EMAIL=dev@local.invalid — rejected
#   4. real secrets + real ACME email — accepted
# Plus three edge cases found during the security review:
#   5. CRLF .env with stubs is still rejected (regression: bare ``$`` anchor)
#   6. CRLF .env with ACME=dev@local.invalid\r is still rejected
#   7. CITEVYN_ACME_EMAIL with trailing whitespace matches the default
# ────────────────────────────────────────────────────────────────────────────

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUARD="${REPO_ROOT}/infra/docker/scripts/_env_guard.sh"

# ─────────────────────── Test harness ───────────────────────

PASS=0
FAIL=0
FAILURES=()

# assert_guard <description> <expected_rc> <expected_stderr_substring> <env-content>
# Writes the env-content into a fresh temp .env, then sources the guard
# against a temp compose dir (containing only the .env) and checks the
# outer-shell exit code and stderr.
assert_guard() {
    local desc="$1" want_rc="$2" want_msg="$3" content="$4"
    local tmpdir
    tmpdir="$(mktemp -d)"
    printf '%s' "$content" > "${tmpdir}/.env"

    local got_rc=0 got_err=""
    got_err="$(bash -c "source '${GUARD}' '${tmpdir}'" 2>&1)" || got_rc=$?

    local ok=1
    if [[ "${got_rc}" != "${want_rc}" ]]; then
        ok=0
        FAILURES+=("  [${desc}] expected rc=${want_rc}, got rc=${got_rc}")
    fi
    if [[ "${ok}" -eq 1 ]] && ! grep -qF -- "${want_msg}" <<<"${got_err}"; then
        ok=0
        FAILURES+=("  [${desc}] expected stderr to contain '${want_msg}'")
        FAILURES+=("    actual stderr: ${got_err}")
    fi

    rm -rf "${tmpdir}"

    if [[ "${ok}" -eq 1 ]]; then
        PASS=$((PASS + 1))
        echo "  ok  ${desc}"
    else
        FAIL=$((FAIL + 1))
        echo "  FAIL ${desc}"
    fi
}

# ─────────────────────── Tests ───────────────────────

echo "test_env_guard.sh"

# 1. Stub POSTGRES_PASSWORD — most common failure mode after make demo.
assert_guard "stub POSTGRES_PASSWORD is rejected" \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=dev-only-change-me"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

# 1b. Stub CITEVYN_ADMIN_API_KEY.
assert_guard "stub CITEVYN_ADMIN_API_KEY is rejected" \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=dev-only-change-me"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

# 2. Missing CITEVYN_ACME_EMAIL — guard refuses.
assert_guard "missing CITEVYN_ACME_EMAIL is rejected" \
    1 \
    "CITEVYN_ACME_EMAIL is not set" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'

# 2b. Empty value (key present, empty) — guard refuses the same way.
assert_guard "empty CITEVYN_ACME_EMAIL= is rejected" \
    1 \
    "CITEVYN_ACME_EMAIL is not set" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL="$'\n'

# 3. Dev default — guard refuses.
assert_guard "CITEVYN_ACME_EMAIL=dev@local.invalid is rejected" \
    1 \
    "dev-time default" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=dev@local.invalid"$'\n'

# 3b. Double-quoted dev default — bash unquotes when sourcing, still rejected.
assert_guard 'CITEVYN_ACME_EMAIL="dev@local.invalid" is rejected' \
    1 \
    "dev-time default" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=\"dev@local.invalid\""$'\n'

# 4. Real secret + real email — guard accepts silently.
assert_guard "real secrets + real ACME email is accepted" \
    0 \
    "" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

# 5. CRLF .env with stubs — regression for the bare ``$`` anchor. The
# guard must still reject the stub.
assert_guard "CRLF .env with stub POSTGRES_PASSWORD is rejected" \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=dev-only-change-me"$'\r\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\r\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\r\n'

# 6. CRLF .env with ACME=dev@local.invalid\r — regression for the
# missing trim in the ACME check. The guard must still reject.
assert_guard "CRLF .env with ACME default is rejected" \
    1 \
    "dev-time default" \
    "POSTGRES_PASSWORD=realprodsecret"$'\r\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\r\n'"CITEVYN_ACME_EMAIL=dev@local.invalid"$'\r\n'

# 7. Trailing whitespace on CITEVYN_ACME_EMAIL — even with multiple
# trailing space / tab / CR chars, the guard must reject the dev
# default once trimmed.
assert_guard "ACME=dev@local.invalid with trailing spaces is rejected" \
    1 \
    "dev-time default" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=dev@local.invalid   "$'\n'

# ─────────────────────── Summary ───────────────────────

echo
echo "${PASS} passed, ${FAIL} failed"
if [[ "${FAIL}" -gt 0 ]]; then
    echo "Failures:"
    printf '%s\n' "${FAILURES[@]}"
    exit 1
fi
exit 0