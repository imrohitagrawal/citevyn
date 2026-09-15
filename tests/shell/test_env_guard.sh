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
#  11. public client token weak-secret check (#200), under BOTH #430 names:
#      empty / absent /
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
# The public client token joined this set when the guard grew the weak-secret check
# that mirrors Settings._is_weak_secret (#200). The value below is 32 hex chars,
# i.e. what ``openssl rand -hex 16`` produces, so it clears the 16-char floor.
REST_OK="CITEVYN_PUBLIC_HOST=citevyn.example.com"$'\n'"CITEVYN_DATABASE_URL=postgresql+psycopg://citevyn:s3cret@db:5432/citevyn"$'\n'"CITEVYN_LLM_PROVIDER=gemini"$'\n'"CITEVYN_PUBLIC_CLIENT_TOKEN=fixture-client-token-not-a-real-secret"$'\n'

# The same prefix WITHOUT the client token, for the token cases: each supplies its
# own CITEVYN_PUBLIC_CLIENT_TOKEN line. Sourcing sets the variable, so a fixture that
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
# Sibling of assert_guard that DELIBERATELY does not scrub CITEVYN_*, because its
# whole subject is what the guard does when a variable is exported in the
# OPERATOR'S SHELL but not declared in the .env FILE (#430 review).
#
# assert_guard unsets the namespace in the child -- correct for every other case,
# and precisely why no existing case could see this class of bug. The container is
# fed by `env_file` (infra/docker/docker-compose.yml names this variable nowhere
# else), so the file is the only thing that reaches the app and the guard's
# verdict must not depend on the terminal it was typed in.
#
#   $1 desc  $2 want_rc  $3 want_msg  $4 .env content  $5.. env assignments
assert_guard_with_ambient_env() {
    local desc="$1" want_rc="$2" want_msg="$3" content="$4"; shift 4
    local tmpdir got_rc=0 got_err="" ok=1
    tmpdir="$(mktemp -d)"
    printf '%s' "$content" > "${tmpdir}/.env"
    got_err="$(env -u CITEVYN_PUBLIC_CLIENT_TOKEN -u CITEVYN_DEMO_API_KEY "$@" \
        bash -c 'source "$1" "$2"' _ "${GUARD}" "${tmpdir}" 2>&1)" || got_rc=$?
    if [[ "${got_rc}" != "${want_rc}" ]]; then
        ok=0
        FAILURES+=("  [${desc}] expected rc=${want_rc}, got rc=${got_rc}")
    fi
    if [[ "${ok}" -eq 1 && -n "${want_msg}" ]] && ! grep -qF -- "${want_msg}" <<<"${got_err}"; then
        ok=0
        FAILURES+=("  [${desc}] expected stderr to contain '${want_msg}'")
        FAILURES+=("    actual stderr: ${got_err}")
    fi
    rm -rf "${tmpdir}"
    if [[ "${ok}" -eq 1 ]]; then
        PASS=$((PASS + 1)); echo "  ok  ${desc}"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL ${desc}"
    fi
}

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

# ─────────────── 11: the public client token (#200, renamed in #430) ───────
# prod.env.example ships the token empty and the app rejects a weak
# value once CITEVYN_ENVIRONMENT=production (which compose pins). Before #200
# the guard did not look at it at all, so a template-copying operator sailed
# through, deploy.sh burned its 60s health poll, and the real cause
# (a pydantic validation error) was visible only in container logs.
#
# The guard mirrors Settings._is_weak_secret: empty / 'local-demo-key'
# (case- and whitespace-insensitive) / under 16 chars are all rejected.

# 11a. Absent entirely — the literal prod.env.example-minus-the-field case.
# Cases 11c-11n cover the weak-secret predicate (default, case, whitespace, CRLF,
# the 16-char floor and its boundary); 11o-11z cover the empty-vs-unset
# distinction that pydantic draws and an `-n` test does not, the .env FILE being
# the subject rather than the operator's shell, and the RETIRED
# CITEVYN_DEMO_API_KEY spelling being inert in every direction (#430 step 2).
assert_guard "missing public client token is rejected" \
    1 \
    "CITEVYN_PUBLIC_CLIENT_TOKEN is not set" \
    "${BASE_OK}${REST_NO_DEMO}"

# 11b. Present but empty — exactly what prod.env.example ships.
assert_guard "empty CITEVYN_PUBLIC_CLIENT_TOKEN= is rejected" \
    1 \
    "CITEVYN_PUBLIC_CLIENT_TOKEN is not set" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN="$'\n'

# 11b-ii. The RETIRED name, present but empty, is not seen at all: the verdict is
#         "the live variable is not set", which is exactly what an operator
#         carrying only the old line needs to be told.
assert_guard "an empty retired CITEVYN_DEMO_API_KEY= is rejected as 'not set'" \
    1 \
    "CITEVYN_PUBLIC_CLIENT_TOKEN is not set" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY="$'\n'

# 11c. The publicly-known default from the open-source repo.
assert_guard "CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key is rejected" \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key"$'\n'

# 11d. Case variant of the default. ``LOCAL-DEMO-KEY`` is just as guessable;
# the Python predicate lower-cases before comparing and so must the guard.
# (It is also under 16 chars, so assert the DEFAULT message specifically —
# otherwise this case would pass on the length branch and prove nothing.)
assert_guard "CITEVYN_PUBLIC_CLIENT_TOKEN=LOCAL-DEMO-KEY is rejected as the default" \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=LOCAL-DEMO-KEY"$'\n'

# 11e. Default plus trailing spaces. NOTE, honestly: this does NOT exercise
# _strip — bash's own parser drops trailing whitespace from an unquoted
# assignment before the guard ever sees it, so this case still passes with
# _strip removed (verified by mutation). It is kept because it pins the
# OBSERVABLE contract an operator cares about ("a key typed with a stray
# space is still the default, and is still refused"), not because it covers
# the helper. Case 11k below is the one that covers _strip.
assert_guard "CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key with trailing spaces is rejected" \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key   "$'\n'

# 11f. Quoted default. Same honesty note as 11e: bash removes the quotes when
# sourcing, so _strip is not what catches this. Pinned because docker compose
# also strips one matched pair, so this .env really would run the weak value.
assert_guard 'CITEVYN_PUBLIC_CLIENT_TOKEN="local-demo-key" is rejected' \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=\"local-demo-key\""$'\n'

# 11k. CRLF .env — THE case that requires _strip. Unlike a trailing space,
# ``\r`` is not IFS whitespace, so bash keeps it in the sourced value and the
# raw variable is ``local-demo-key\r``. Without _strip the default comparison
# misses, and the operator gets the WRONG diagnosis (a length complaint about
# a key that is actually the published default) — which is why this case
# asserts the default-specific message rather than just rc=1.
assert_guard "CRLF .env with CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key is rejected" \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key"$'\r\n'

# 11l. CRLF .env with a SHORT non-default key. Without _strip the trailing CR
# inflates the length by one, so a 15-char key measures 16 and clears the
# floor — the guard would wave through a key the app then rejects at boot.
assert_guard "CRLF .env with a 15-character CITEVYN_PUBLIC_CLIENT_TOKEN is rejected" \
    1 \
    "shorter than the 16-character" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=abcdefghijklmno"$'\r\n'

# 11g. Short but NOT the default — rejecting only the known string would still
# accept ``x``. 15 chars is one below the floor.
assert_guard "15-character CITEVYN_PUBLIC_CLIENT_TOKEN is rejected" \
    1 \
    "shorter than the 16-character" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=abcdefghijklmno"$'\n'

# 11h. Boundary: exactly 16 characters is ACCEPTED (the floor is ``< 16``, not
# ``<= 16``). Guards against an off-by-one that would reject a valid secret and
# block every prod entry point.
assert_guard "16-character CITEVYN_PUBLIC_CLIENT_TOKEN is accepted" \
    0 \
    "" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=abcdefghijklmnop"$'\n'

# 11i. A strong key whose trailing whitespace must be stripped BEFORE the
# length check, not after — 32 hex chars plus spaces is still valid.
assert_guard "strong CITEVYN_PUBLIC_CLIENT_TOKEN with trailing spaces is accepted" \
    0 \
    "" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=fixture-demo-key-not-a-real-secret  "$'\n'

# 11j. A strong key that merely CONTAINS the default substring must be
# accepted — guards against a future regression that drops the anchoring and
# starts substring-matching ``local-demo-key``.
assert_guard "strong key containing 'local-demo-key' is accepted" \
    0 \
    "" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key-but-longer-and-not-the-default"$'\n'

# 11m. LEADING whitespace. _strip peeled only the TRAILING end while Python's
#      _is_weak_secret strips BOTH (config.py:54-55), so '  local-demo-key' is
#      16 chars, is neither == the default nor < 16, and sailed through the
#      guard — then crash-looped the api on the pydantic check the guard exists
#      to pre-empt. The asymmetry, not the value, is the bug.
assert_guard 'leading-whitespace "  local-demo-key" is rejected (strip is symmetric)' \
    1 \
    "publicly-known default" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=\"  local-demo-key\""$'\n'

# 11n. ...and the same asymmetry hid a short key behind leading padding.
assert_guard 'leading-whitespace short key is rejected on length' \
    1 \
    "shorter than the 16-character" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=\"           abcdef\""$'\n'

# (The old 11o -- "the new name alone is accepted" -- was deleted rather than
#  renamed: 11h, 11i and 11j already feed a strong value under this name and
#  expect rc=0, so it asserted nothing they did not. 11p below stays, because
#  "accepted" and "validated" are different claims.)

# 11p. The live name is VALIDATED, not merely noticed. An implementation that
#      accepted it by SKIPPING the check whenever it was present would still pass
#      every rc=0 case above and let 'local-demo-key' into production.
assert_guard "CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key is rejected" \
    1 \
    "publicly-known" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key"$'\n'

# 11q. THE REMOVAL (#430 step 2), asserted in the direction that can fail. A
#      STRONG value under the retired name, and nothing else: the container would
#      receive a variable Settings does not read, fall back to the published
#      local-demo-key, and refuse to boot. The guard must reject, and must say the
#      live variable is not set rather than complaining about the value.
#      RED if the `elif _declared_in_env_file CITEVYN_DEMO_API_KEY` branch returns.
assert_guard "a STRONG retired CITEVYN_DEMO_API_KEY alone is rejected" \
    1 \
    "CITEVYN_PUBLIC_CLIENT_TOKEN is not set" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=fixture-client-token-not-a-real-secret"$'\n'

# 11r. ...and it cannot DEGRADE a good .env either: the live name strong, the
#      retired one weak, must be ACCEPTED. Non-vacuity partner for 11q -- without
#      it, a guard that rejected every .env mentioning the retired name at all
#      would satisfy 11q while blocking a correct deployment.
assert_guard "a weak retired name alongside a strong live one is accepted" \
    0 \
    "" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_DEMO_API_KEY=local-demo-key"$'\n'"CITEVYN_PUBLIC_CLIENT_TOKEN=fixture-client-token-not-a-real-secret"$'\n'

# 11s. EMPTY LIVE NAME + STRONG RETIRED NAME. The regression case, and the one the
#      first draft of this guard got wrong.
#
#      An empty variable IS present, so `min_length=1` rejects it and Settings
#      never boots. An `-n` presence test in the shell treats empty as absent, fell
#      through, and reported PASS. MEASURED on the first draft: guard exit 0,
#      `docker compose config` injecting CITEVYN_PUBLIC_CLIENT_TOKEN: "", and
#      Settings() refusing to construct from that same environment -- a green
#      preflight in front of a crash-looping api, which is the exact failure mode
#      section 11 exists to pre-empt.
#
#      The ingredient SHIPS: infra/docker/prod.env.example declares the name empty,
#      so an operator who copies the template and pastes back only their old
#      CITEVYN_DEMO_API_KEY line lands here.
#
#      RED if the guard stops treating a declared-but-empty live name as present,
#      and RED if the retired-name fallback comes back to rescue it.
assert_guard "empty live name is REJECTED even when the retired one is strong" \
    1 \
    "CITEVYN_PUBLIC_CLIENT_TOKEN is not set" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN="$'\n'"CITEVYN_DEMO_API_KEY=fixture-client-token-not-a-real-secret"$'\n'

# 11t. The partner that stops 11s being satisfied by "reject whenever the live
#      name is declared at all". Same .env shape, live name FILLED IN: must pass.
#      Without this, `_token_value=""` unconditionally would satisfy 11s and block
#      every correct deployment.
assert_guard "a declared AND filled-in live name is accepted alongside a stale retired one" \
    0 \
    "" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=fixture-client-token-not-a-real-secret"$'\n'"CITEVYN_DEMO_API_KEY=a-different-strong-old-value-here"$'\n'

# (The "unset new + empty old" case is already case 11b-ii above, which asserts
#  the stronger message. A second copy was written here and removed: review
#  showed it byte-identical to 11b-ii's fixture with a weaker assertion, and no
#  mutant killed it alone, so it was coverage theatre rather than coverage.)

# 11v. A WEAK BUT NON-EMPTY value is reported as a BAD VALUE, naming the exact
#      line to edit, and NOT as "is not set" -- the two send an operator to
#      different places. Nothing else in this file separates those two messages
#      for a declared-and-filled variable.
assert_guard "a weak value names the variable and says it is the default" \
    1 \
    "error: CITEVYN_PUBLIC_CLIENT_TOKEN is still the publicly-known default" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key"$'\n'

# 11w. ...and its mirror: an EMPTY EFFECTIVE value says "is not set" instead.
#      `_strip` runs first, so whitespace-only counts as empty here. An earlier
#      draft tested the RAW value and called `X="   "` set, which is the wrong
#      words for a line that reads as blank to pydantic.
assert_guard "a whitespace-only value names the variable to SET" \
    1 \
    "error: CITEVYN_PUBLIC_CLIENT_TOKEN is not set" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=\"   \""$'\n'

# ── 11x-11z: the .env FILE is the subject, never the operator's shell ────────
#
# The api container is fed by `env_file`, and infra/docker/docker-compose.yml
# names this variable nowhere else -- so what the FILE declares is the only thing
# that reaches the app. Resolving against the shell instead is wrong in BOTH
# directions, and both were reproduced against the real guard before this block
# existed. assert_guard cannot express these: it scrubs CITEVYN_* in the child,
# which is exactly why the class was invisible.

# 11x. FALSE REJECT. The .env is fine; the operator merely happens to have the
#      name exported EMPTY (deploy_verify.sh documents exporting these names as a
#      supported workflow). The container is fed from the FILE and never sees the
#      shell, so blocking this deploy is a red gate on a correct deployment.
#      RED if the resolution goes back to reading the environment.
assert_guard_with_ambient_env "an exported EMPTY name does not block a good .env" \
    0 "" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=fixture-client-token-not-a-real-secret"$'\n' \
    CITEVYN_PUBLIC_CLIENT_TOKEN=

# 11y. FALSE PASS, the dangerous direction. Nothing in the .env, a strong value
#      only in the shell: the container receives NO token, falls back to the
#      published local-demo-key, and production refuses to boot. The guard must
#      reject. RED if the resolution reads the environment.
assert_guard_with_ambient_env "a token exported ONLY in the shell does not satisfy the guard" \
    1 "CITEVYN_PUBLIC_CLIENT_TOKEN is not set" \
    "${BASE_OK}${REST_NO_DEMO}" \
    CITEVYN_PUBLIC_CLIENT_TOKEN=a-strong-token-only-in-the-shell

# 11y-ii. The same false pass through the RETIRED name. Kept after #430 step 2
#      because it now guards a second thing at once: a guard that re-grew the old
#      fallback AND read it from the shell would pass while the container received
#      no token at all. Found originally by mutation -- reverting only the `elif`
#      branch to `${CITEVYN_DEMO_API_KEY+set}` survived every other case.
assert_guard_with_ambient_env "a RETIRED token exported only in the shell does not satisfy the guard" \
    1 "CITEVYN_PUBLIC_CLIENT_TOKEN is not set" \
    "${BASE_OK}${REST_NO_DEMO}" \
    CITEVYN_DEMO_API_KEY=a-strong-old-token-only-in-the-shell

# 11z. The partner that stops 11x/11y being satisfied by "always read the file
#      and always reject": the same ambient export alongside a .env that DOES
#      declare the new name must pass, and on the FILE's value.
assert_guard_with_ambient_env "a declared new name wins over anything in the shell" \
    0 "" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN=fixture-client-token-not-a-real-secret"$'\n' \
    CITEVYN_PUBLIC_CLIENT_TOKEN=local-demo-key

# 11aa. A COMMENTED-OUT declaration is documentation, not a declaration.
#       With one name left, this is only OBSERVABLE through the ambient-env
#       variant: the file's commented line must not make the guard start reading
#       the operator's shell. If `_declared_in_env_file` drops its `#` exclusion
#       the guard treats the file as declaring the name, reads the strong value
#       out of the environment, and passes a deployment whose container gets
#       nothing. RED exactly then.
assert_guard_with_ambient_env "a commented-out declaration does not count as declared" \
    1 "CITEVYN_PUBLIC_CLIENT_TOKEN is not set" \
    "${BASE_OK}${REST_NO_DEMO}""# CITEVYN_PUBLIC_CLIENT_TOKEN=fixture-client-token-not-a-real-secret"$'\n' \
    CITEVYN_PUBLIC_CLIENT_TOKEN=a-strong-token-only-in-the-shell

# 11ac-11ae. The parser's three DOCUMENTED TOLERANCES, each of which was a
#      surviving mutant until it had a case: dropping the leading-whitespace
#      allowance, the `export ` alternative, or the whitespace-before-`=`
#      allowance each left all previous cases green. A tolerance that no test
#      asserts is a comment, not behaviour.
#
#      Each of these .env files declares the token ONLY in the tolerated shape,
#      with a strong value, and must therefore PASS. If the tolerance is dropped
#      the guard stops seeing a declaration, falls through to a file with no old
#      name either, and rejects.
assert_guard "an INDENTED declaration is still a declaration" \
    0 \
    "" \
    "${BASE_OK}${REST_NO_DEMO}""    CITEVYN_PUBLIC_CLIENT_TOKEN=fixture-client-token-not-a-real-secret"$'\n'

assert_guard "an 'export'-prefixed declaration is still a declaration" \
    0 \
    "" \
    "${BASE_OK}${REST_NO_DEMO}""export CITEVYN_PUBLIC_CLIENT_TOKEN=fixture-client-token-not-a-real-secret"$'\n'

# NB the third "tolerance" is NOT reached, and that is the point of this case.
# `docker compose` accepts `NAME =value`; bash does not, and `source` fails on it
# with `NAME: command not found` (status 127). The source-failure guard therefore
# fires FIRST, before any token resolution, and names the real problem -- a
# better diagnosis than "is not set" would have been. MEASURED against the guard
# at b4cdfa6, before this rename: identical rejection, so it is pre-existing
# behaviour of the source step rather than anything the #430 resolution
# introduced. Pinned here so a future change to the source step cannot silently
# turn a malformed .env into a token-resolution puzzle.
assert_guard "a space before '=' fails at SOURCE, naming the file" \
    1 \
    "failed to source" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN =fixture-client-token-not-a-real-secret"$'\n'

# 11ab. ...and an exact-match partner: a LONGER name must not satisfy a query for
#       the shorter one. Without the trailing `=` in the pattern,
#       CITEVYN_PUBLIC_CLIENT_TOKEN_EXTRA= would be read as declaring the token,
#       the guard would start trusting the environment for it, and this .env --
#       which declares no token at all -- would be blessed on a value the
#       container never receives. Same ambient-env shape as 11aa, different
#       mutation: 11y (no extra line) stays green when the `=` is dropped.
assert_guard_with_ambient_env "a longer variable name does not count as the token declaration" \
    1 "CITEVYN_PUBLIC_CLIENT_TOKEN is not set" \
    "${BASE_OK}${REST_NO_DEMO}""CITEVYN_PUBLIC_CLIENT_TOKEN_EXTRA=whatever"$'\n' \
    CITEVYN_PUBLIC_CLIENT_TOKEN=a-strong-token-only-in-the-shell

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

# Non-vacuity, pinned EXACTLY, the way tests/shell/test_check_bundle_key.sh and
# tests/shell/test_deploy_verify_skip_state.sh already pin theirs. This suite is
# the largest of the three and had no such guard until #430 step 2 reworked it
# most heavily -- so a case silently deleted here left no trace at all, and a
# suite that ran fewer assertions than its author thinks is exactly the shape
# this repo keeps finding. A floor would not do: `-ge` cannot see a deletion
# that lands alongside an addition, which is precisely what that rework was.
# BUMP THIS DELIBERATELY when you add or remove a case.
_EXPECTED_ASSERTIONS=57
_RAN_ASSERTIONS=$((PASS + FAIL))
if [[ "${_RAN_ASSERTIONS}" -ne "${_EXPECTED_ASSERTIONS}" ]]; then
    FAIL=$((FAIL + 1))
    FAILURES+=("  [assertion count] ${_RAN_ASSERTIONS} assertions ran; expected exactly ${_EXPECTED_ASSERTIONS}. Bump _EXPECTED_ASSERTIONS in the same commit as the case you added or removed.")
fi

echo
echo "${PASS} passed, ${FAIL} failed"
if [[ "${FAIL}" -gt 0 ]]; then
    echo "Failures:"
    printf '%s\n' "${FAILURES[@]}"
    exit 1
fi
exit 0