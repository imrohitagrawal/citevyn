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
#   1. stub POSTGRES_PASSWORD / CITEVYN_ADMIN_API_KEY / CITEVYN_ACME_EMAIL
#   2. missing CITEVYN_ACME_EMAIL — rejected
#   3. CITEVYN_ACME_EMAIL=dev@local.invalid — rejected
#   4. real secrets + real ACME email — accepted (incl. single- and
#      double-quoted values; the guard strips a matched pair)
# Plus six edge cases found during security review:
#   5. CRLF .env with stubs is still rejected (regression: bare ``$`` anchor)
#   6. CRLF .env with ACME=dev@local.invalid\r is still rejected
#   7. CITEVYN_ACME_EMAIL with trailing whitespace matches the default
#   8. .env with a stray non-zero command (false) is rejected
#   9. .env file mode is tightened to 0600 by the guard as a side effect
#  10. CITEVYN_ACME_EMAIL='ops@example.com' (single-quoted) is accepted
#      with quotes stripped — regression for the bash-quote-preservation
#      bypass
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

# 1a. Local dev POSTGRES_PASSWORD=citevyn — the credential the make db-up
# bootstrap writes (matching DB_URL / smoke.sh / config.py / CI). It is a
# dev-only value and must be refused for a prod deploy just like the stub.
assert_guard "local dev POSTGRES_PASSWORD=citevyn is rejected" \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=citevyn"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

# 1a'. A real prod password that merely CONTAINS "citevyn" must be accepted —
# guards against a future regex regression that drops the anchors and starts
# substring-matching the dev credential.
assert_guard "prod password containing 'citevyn' is accepted" \
    0 \
    "" \
    "POSTGRES_PASSWORD=citevynS3cret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

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

# 8. Stub CITEVYN_ACME_EMAIL (Makefile bootstrap now rewrites it too).
assert_guard "stub CITEVYN_ACME_EMAIL is rejected" \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=dev-only-change-me"$'\n'

# 9. C1 (ruthless-critic): single-quoted ACME value. Bash preserves
# the quote chars literally when sourced; without the strip-quotes
# logic the guard would accept ``'ops@example.com'`` and Caddy would
# forward the literal-quoted string to Let's Encrypt. The guard
# strips a matched pair of leading/trailing single or double quotes,
# so the value passed downstream is plain ``ops@example.com``.
assert_guard "single-quoted ACME email is accepted (quotes stripped)" \
    0 \
    "" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL='ops@example.com'"$'\n'

assert_guard "double-quoted ACME email is accepted (quotes stripped)" \
    0 \
    "" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=\"ops@example.com\""$'\n'

# 10. C2 (ruthless-critic): garbage command in .env. A stray
# non-zero command (here ``false``) on the last line of a
# .env file should make the guard fail-closed with a clear
# error. Without the explicit ``_source_rc`` capture, the
# subshell would silently continue past the ``false``, see
# the valid CITEVYN_ACME_EMAIL, and accept the .env.
assert_guard ".env with stray non-zero command is rejected" \
    1 \
    "failed to source" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'"false"$'\n'

# ─────────────────────── C3: chmod 600 ───────────────────────
# The guard tightens the .env file mode to 0600 as a side effect.
# This is independent of the stub / ACME / quote logic — even a
# manually-created .env (e.g. ``cp prod.env.example .env``) should
# end up with mode 0600 after the guard runs.

echo
echo "chmod 600 side-effect"

# Start with a deliberately permissive mode, then run the guard
# with a real-secret .env, then check the mode.
chmod_test() {
    local tmpdir
    tmpdir="$(mktemp -d)"
    printf '%s' "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=realadminkey"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n' > "${tmpdir}/.env"
    chmod 0644 "${tmpdir}/.env"

    bash -c "source '${GUARD}' '${tmpdir}'" >/dev/null 2>&1

    local mode
    # ``stat -f %Lp`` is BSD/macOS; ``stat -c %a`` is GNU/Linux.
    # Use ls for portability.
    mode="$(ls -l "${tmpdir}/.env" | awk '{print $1}')"

    if [[ "${mode}" == "-rw-------"* ]]; then
        PASS=$((PASS + 1))
        echo "  ok  .env mode tightened to 0600 (saw ${mode})"
    else
        FAIL=$((FAIL + 1))
        echo "  FAIL .env mode not 0600 (saw ${mode})"
        FAILURES+=("  [.env mode] expected -rw-------, got ${mode}")
    fi

    rm -rf "${tmpdir}"
}

chmod_test

# ─────────────────────── Summary ───────────────────────

echo
echo "${PASS} passed, ${FAIL} failed"
if [[ "${FAIL}" -gt 0 ]]; then
    echo "Failures:"
    printf '%s\n' "${FAILURES[@]}"
    exit 1
fi
exit 0