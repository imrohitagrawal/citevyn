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
#  11. CITEVYN_DEMO_API_KEY weak-secret check (#200): empty / absent /
#      'local-demo-key' / case-variant / trailing-space / quoted / 15-char
#      are rejected; 16-char, whitespace-padded strong, and a strong key
#      merely CONTAINING the default are accepted
# ────────────────────────────────────────────────────────────────────────────

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUARD="${REPO_ROOT}/infra/docker/scripts/_env_guard.sh"

# ─────────────────────── Test harness ───────────────────────

# The guard requires more fields than these fixtures originally supplied
# (CITEVYN_PUBLIC_HOST, CITEVYN_DATABASE_URL, CITEVYN_LLM_PROVIDER were added to
# _env_guard.sh later). Without them every "is accepted" case died on the first
# missing field and never reached the behaviour it was written to test — four
# silent false failures, invisible because tests/shell/ is not wired into CI.
# Reject-cases do NOT need this: the harness also asserts the stderr substring,
# so a wrong-reason rejection already fails.
#
# CITEVYN_DEMO_API_KEY joined this set when the guard grew the weak-secret check
# that mirrors Settings._is_weak_secret (#200). The value below is 32 hex chars,
# i.e. what ``openssl rand -hex 16`` produces, so it clears the 16-char floor.
REST_OK="CITEVYN_PUBLIC_HOST=citevyn.example.com"$'\n'"CITEVYN_DATABASE_URL=postgresql+psycopg://citevyn:s3cret@db:5432/citevyn"$'\n'"CITEVYN_LLM_PROVIDER=gemini"$'\n'"CITEVYN_DEMO_API_KEY=fixture-demo-key-not-a-real-secret"$'\n'

# The same prefix WITHOUT the demo key, for the demo-key cases: each supplies its
# own CITEVYN_DEMO_API_KEY line. Sourcing sets the variable, so a fixture that
# appended REST_OK and then its own line would still be testing REST_OK's value
# for a "missing key" case and the last-wins value everywhere else — exactly the
# kind of silent no-op #179 had to repair. Keeping the two prefixes separate
# makes the value under test the ONLY one in the file.
REST_NO_DEMO="CITEVYN_PUBLIC_HOST=citevyn.example.com"$'\n'"CITEVYN_DATABASE_URL=postgresql+psycopg://citevyn:s3cret@db:5432/citevyn"$'\n'"CITEVYN_LLM_PROVIDER=gemini"$'\n'

# Everything the guard requires BEFORE it reaches the demo-key block. A
# demo-key case must clear all of it, otherwise it would "pass" on an
# unrelated rejection.
BASE_OK="POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'


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
    # Scrub the guard's whole variable namespace in the child. Without this the
    # child INHERITS anything the caller exported, so a case that deliberately
    # OMITS a key from its .env would read the operator's exported value
    # instead of empty — and the "missing key is rejected" cases would flip to
    # FAIL on a shell where CITEVYN_DEMO_API_KEY happens to be exported. That
    # is not hypothetical: deploy_verify.sh documents exporting it as a
    # supported workflow, and this suite now runs in CI, so an env var at the
    # workflow level would produce a red build that is not a real regression.
    #
    # `${!PREFIX@}` (prefix expansion) predates bash 4.0, so it is safe on the
    # macOS 3.2 this suite is matrixed over. NOT `env -i`: that drops PATH and
    # the guard's own grep/tr/chmod calls would break.
    got_err="$(bash -c '
        for _v in ${!CITEVYN_@} ${!POSTGRES_@}; do unset "$_v"; done
        source "$1" "$2"
    ' _ "${GUARD}" "${tmpdir}" 2>&1)" || got_rc=$?

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
    "POSTGRES_PASSWORD=dev-only-change-me"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

# 1a. Local dev POSTGRES_PASSWORD=citevyn — the credential the make db-up
# bootstrap writes (matching DB_URL / smoke.sh / config.py / CI). It is a
# dev-only value and must be refused for a prod deploy just like the stub.
assert_guard "local dev POSTGRES_PASSWORD=citevyn is rejected" \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=citevyn"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

# 1a'. A real prod password that merely CONTAINS "citevyn" must be accepted —
# guards against a future regex regression that drops the anchors and starts
# substring-matching the dev credential.
assert_guard "prod password containing 'citevyn' is accepted" \
    0 \
    "" \
    "POSTGRES_PASSWORD=citevynS3cret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'"${REST_OK}"

# 1a''. QUOTED dev sentinels must be rejected. docker compose strips one
# matched quote pair, so POSTGRES_PASSWORD="citevyn" would RUN with the weak
# value; the anchored greps see the quotes and miss it, so the sourced-subshell
# re-check must normalize (strip quotes/whitespace) and reject. Both single- and
# double-quoted, for the password and the admin key.
assert_guard 'double-quoted POSTGRES_PASSWORD="citevyn" is rejected' \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=\"citevyn\""$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

assert_guard "single-quoted POSTGRES_PASSWORD='dev-only-change-me' is rejected" \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD='dev-only-change-me'"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

assert_guard 'double-quoted CITEVYN_ADMIN_API_KEY="dev-only-change-me" is rejected' \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=\"dev-only-change-me\""$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

# 1b. Stub CITEVYN_ADMIN_API_KEY.
assert_guard "stub CITEVYN_ADMIN_API_KEY is rejected" \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=dev-only-change-me"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

# 2. Missing CITEVYN_ACME_EMAIL — guard refuses.
assert_guard "missing CITEVYN_ACME_EMAIL is rejected" \
    1 \
    "CITEVYN_ACME_EMAIL is not set" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'

# 2b. Empty value (key present, empty) — guard refuses the same way.
assert_guard "empty CITEVYN_ACME_EMAIL= is rejected" \
    1 \
    "CITEVYN_ACME_EMAIL is not set" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL="$'\n'

# 3. Dev default — guard refuses.
assert_guard "CITEVYN_ACME_EMAIL=dev@local.invalid is rejected" \
    1 \
    "dev-time default" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=dev@local.invalid"$'\n'

# 3b. Double-quoted dev default — bash unquotes when sourcing, still rejected.
assert_guard 'CITEVYN_ACME_EMAIL="dev@local.invalid" is rejected' \
    1 \
    "dev-time default" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=\"dev@local.invalid\""$'\n'

# 4. Real secret + real email — guard accepts silently.
assert_guard "real secrets + real ACME email is accepted" \
    0 \
    "" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'"${REST_OK}"

# 5. CRLF .env with stubs — regression for the bare ``$`` anchor. The
# guard must still reject the stub.
assert_guard "CRLF .env with stub POSTGRES_PASSWORD is rejected" \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=dev-only-change-me"$'\r\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\r\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\r\n'

# 6. CRLF .env with ACME=dev@local.invalid\r — regression for the
# missing trim in the ACME check. The guard must still reject.
assert_guard "CRLF .env with ACME default is rejected" \
    1 \
    "dev-time default" \
    "POSTGRES_PASSWORD=realprodsecret"$'\r\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\r\n'"CITEVYN_ACME_EMAIL=dev@local.invalid"$'\r\n'

# 7. Trailing whitespace on CITEVYN_ACME_EMAIL — even with multiple
# trailing space / tab / CR chars, the guard must reject the dev
# default once trimmed.
assert_guard "ACME=dev@local.invalid with trailing spaces is rejected" \
    1 \
    "dev-time default" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=dev@local.invalid   "$'\n'

# 8. Stub CITEVYN_ACME_EMAIL (Makefile bootstrap now rewrites it too).
assert_guard "stub CITEVYN_ACME_EMAIL is rejected" \
    1 \
    "dev-only stub secrets" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=dev-only-change-me"$'\n'

# 9. C1 (ruthless-critic): single-quoted ACME value. Bash preserves
# the quote chars literally when sourced; without the strip-quotes
# logic the guard would accept ``'ops@example.com'`` and Caddy would
# forward the literal-quoted string to Let's Encrypt. The guard
# strips a matched pair of leading/trailing single or double quotes,
# so the value passed downstream is plain ``ops@example.com``.
assert_guard "single-quoted ACME email is accepted (quotes stripped)" \
    0 \
    "" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL='ops@example.com'"$'\n'"${REST_OK}"

assert_guard "double-quoted ACME email is accepted (quotes stripped)" \
    0 \
    "" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=\"ops@example.com\""$'\n'"${REST_OK}"

# 10. C2 (ruthless-critic): garbage command in .env. A stray
# non-zero command (here ``false``) on the last line of a
# .env file should make the guard fail-closed with a clear
# error. Without the explicit ``_source_rc`` capture, the
# subshell would silently continue past the ``false``, see
# the valid CITEVYN_ACME_EMAIL, and accept the .env.
assert_guard ".env with stray non-zero command is rejected" \
    1 \
    "failed to source" \
    "POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ADMIN_API_KEY=fixture-admin-key-not-a-real-secret"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'"false"$'\n'

# ─────────────────────── 11: CITEVYN_DEMO_API_KEY (#200) ───────────────────
# prod.env.example ships CITEVYN_DEMO_API_KEY empty and the app rejects a weak
# value once CITEVYN_ENVIRONMENT=production (which compose pins). Before #200
# the guard did not look at it at all, so a template-copying operator sailed
# through, deploy.sh burned its 60s health poll, and the real cause
# (a pydantic validation error) was visible only in container logs.
#
# The guard mirrors Settings._is_weak_secret: empty / 'local-demo-key'
# (case- and whitespace-insensitive) / under 16 chars are all rejected.

# 11a. Absent entirely — the literal prod.env.example-minus-the-field case.
assert_guard "missing CITEVYN_DEMO_API_KEY is rejected" \
    1 \
    "CITEVYN_DEMO_API_KEY is not set" \
    "${BASE_OK}${REST_NO_DEMO}"

# 11b. Present but empty — exactly what prod.env.example ships.
assert_guard "empty CITEVYN_DEMO_API_KEY= is rejected" \
    1 \
    "CITEVYN_DEMO_API_KEY is not set" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY="$'\n'

# 11c. The publicly-known default from the open-source repo.
assert_guard "CITEVYN_DEMO_API_KEY=local-demo-key is rejected" \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=local-demo-key"$'\n'

# 11d. Case variant of the default. ``LOCAL-DEMO-KEY`` is just as guessable;
# the Python predicate lower-cases before comparing and so must the guard.
# (It is also under 16 chars, so assert the DEFAULT message specifically —
# otherwise this case would pass on the length branch and prove nothing.)
assert_guard "CITEVYN_DEMO_API_KEY=LOCAL-DEMO-KEY is rejected as the default" \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=LOCAL-DEMO-KEY"$'\n'

# 11e. Default plus trailing spaces. NOTE, honestly: this does NOT exercise
# _strip — bash's own parser drops trailing whitespace from an unquoted
# assignment before the guard ever sees it, so this case still passes with
# _strip removed (verified by mutation). It is kept because it pins the
# OBSERVABLE contract an operator cares about ("a key typed with a stray
# space is still the default, and is still refused"), not because it covers
# the helper. Case 11k below is the one that covers _strip.
assert_guard "CITEVYN_DEMO_API_KEY=local-demo-key with trailing spaces is rejected" \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=local-demo-key   "$'\n'

# 11f. Quoted default. Same honesty note as 11e: bash removes the quotes when
# sourcing, so _strip is not what catches this. Pinned because docker compose
# also strips one matched pair, so this .env really would run the weak value.
assert_guard 'CITEVYN_DEMO_API_KEY="local-demo-key" is rejected' \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=\"local-demo-key\""$'\n'

# 11k. CRLF .env — THE case that requires _strip. Unlike a trailing space,
# ``\r`` is not IFS whitespace, so bash keeps it in the sourced value and the
# raw variable is ``local-demo-key\r``. Without _strip the default comparison
# misses, and the operator gets the WRONG diagnosis (a length complaint about
# a key that is actually the published default) — which is why this case
# asserts the default-specific message rather than just rc=1.
assert_guard "CRLF .env with CITEVYN_DEMO_API_KEY=local-demo-key is rejected" \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=local-demo-key"$'\r\n'

# 11l. CRLF .env with a SHORT non-default key. Without _strip the trailing CR
# inflates the length by one, so a 15-char key measures 16 and clears the
# floor — the guard would wave through a key the app then rejects at boot.
assert_guard "CRLF .env with a 15-character CITEVYN_DEMO_API_KEY is rejected" \
    1 \
    "shorter than the 16-character" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=abcdefghijklmno"$'\r\n'

# 11g. Short but NOT the default — rejecting only the known string would still
# accept ``x``. 15 chars is one below the floor.
assert_guard "15-character CITEVYN_DEMO_API_KEY is rejected" \
    1 \
    "shorter than the 16-character" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=abcdefghijklmno"$'\n'

# 11h. Boundary: exactly 16 characters is ACCEPTED (the floor is ``< 16``, not
# ``<= 16``). Guards against an off-by-one that would reject a valid secret and
# block every prod entry point.
assert_guard "16-character CITEVYN_DEMO_API_KEY is accepted" \
    0 \
    "" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=abcdefghijklmnop"$'\n'

# 11i. A strong key whose trailing whitespace must be stripped BEFORE the
# length check, not after — 32 hex chars plus spaces is still valid.
assert_guard "strong CITEVYN_DEMO_API_KEY with trailing spaces is accepted" \
    0 \
    "" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=fixture-demo-key-not-a-real-secret  "$'\n'

# 11j. A strong key that merely CONTAINS the default substring must be
# accepted — guards against a future regression that drops the anchoring and
# starts substring-matching ``local-demo-key``.
assert_guard "strong key containing 'local-demo-key' is accepted" \
    0 \
    "" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=local-demo-key-but-longer-and-not-the-default"$'\n'

# 11m. LEADING whitespace. _strip peeled only the TRAILING end while Python's
#      _is_weak_secret strips BOTH (config.py:54-55), so '  local-demo-key' is
#      16 chars, is neither == the default nor < 16, and sailed through the
#      guard — then crash-looped the api on the pydantic check the guard exists
#      to pre-empt. The asymmetry, not the value, is the bug.
assert_guard 'leading-whitespace "  local-demo-key" is rejected (strip is symmetric)' \
    1 \
    "publicly-known default" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=\"  local-demo-key\""$'\n'

# 11n. ...and the same asymmetry hid a short key behind leading padding.
assert_guard 'leading-whitespace short key is rejected on length' \
    1 \
    "shorter than the 16-character" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=\"           abcdef\""$'\n'

# ─────────────── C2b: CITEVYN_ADMIN_API_KEY strength (#200) ───────────────
# The admin key had the IDENTICAL gap: it was only ever compared against the
# Makefile bootstrap stub 'dev-only-change-me', so empty, absent, and the
# PUBLISHED code default 'local-admin-key' (config.py:71) all passed — and this
# is the key that promotes an index and reads the budget.
BASE_NO_ADMIN="POSTGRES_PASSWORD=realprodsecret"$'\n'"CITEVYN_ACME_EMAIL=ops@example.com"$'\n'

assert_guard "empty CITEVYN_ADMIN_API_KEY is rejected" \
    1 \
    "CITEVYN_ADMIN_API_KEY is not set" \
    "${BASE_NO_ADMIN}${REST_OK}""CITEVYN_ADMIN_API_KEY="$'\n'

assert_guard "absent CITEVYN_ADMIN_API_KEY is rejected" \
    1 \
    "CITEVYN_ADMIN_API_KEY is not set" \
    "${BASE_NO_ADMIN}${REST_OK}"

assert_guard "published default CITEVYN_ADMIN_API_KEY=local-admin-key is rejected" \
    1 \
    "publicly-known default" \
    "${BASE_NO_ADMIN}${REST_OK}""CITEVYN_ADMIN_API_KEY=local-admin-key"$'\n'

assert_guard "case-variant CITEVYN_ADMIN_API_KEY=LOCAL-ADMIN-KEY is rejected" \
    1 \
    "publicly-known default" \
    "${BASE_NO_ADMIN}${REST_OK}""CITEVYN_ADMIN_API_KEY=LOCAL-ADMIN-KEY"$'\n'

assert_guard "15-character CITEVYN_ADMIN_API_KEY is rejected" \
    1 \
    "shorter than the 16-character" \
    "${BASE_NO_ADMIN}${REST_OK}""CITEVYN_ADMIN_API_KEY=fifteen-char-ky"$'\n'

assert_guard "16-character CITEVYN_ADMIN_API_KEY is accepted" \
    0 \
    "" \
    "${BASE_NO_ADMIN}${REST_OK}""CITEVYN_ADMIN_API_KEY=sixteen-char-key"$'\n'

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
    printf '%s' "${BASE_OK}${REST_OK}" > "${tmpdir}/.env"
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