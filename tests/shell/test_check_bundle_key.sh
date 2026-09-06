#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_check_bundle_key.sh — assertions for scripts/check_bundle_key.sh (#296).
#
# The check this guards replaced `grep -c local-demo-key … # must print 0`,
# which PASSED on a bundle built with an empty --build-arg — the exact outage
# shape its own runbook paragraph warned about. So the cases below are not
# hypothetical: each fixture reproduces a build I actually performed and
# measured.
#
# HOW THE FIXTURES WERE OBTAINED (2026-09-06, `vite build` at 0146c56):
#   VITE_API_DEMO_KEY unset      -> bundle contains  ,Cf="local-demo-key"
#   VITE_API_DEMO_KEY=""         -> bundle contains  ,Cf=""
#   VITE_API_DEMO_KEY=<a value>  -> bundle contains  ,Cf="<a value>"
# and the corresponding `docker build --build-arg VITE_API_DEMO_KEY=""`
# leaves the ARG EMPTY rather than falling back to its default, which is why
# the middle row exists at all. `Cf` is a minifier-assigned name and changes
# every build — which is precisely why the checker greps for the KEY VALUE and
# never for the variable, so these fixtures do not depend on it.
#
# Run from the repo root:
#   bash tests/shell/test_check_bundle_key.sh
# ────────────────────────────────────────────────────────────────────────────

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECK="${REPO_ROOT}/scripts/check_bundle_key.sh"
FAILURES=0
ASSERTIONS=0

pass() { echo "  ok   — $1"; ASSERTIONS=$((ASSERTIONS + 1)); }
fail() { echo "  FAIL — $1"; FAILURES=$((FAILURES + 1)); ASSERTIONS=$((ASSERTIONS + 1)); }

echo "test_check_bundle_key.sh"

# A stand-in for a real generated key. Not a secret: it has never been a
# CiteVyn key anywhere. 44 chars, matching the length of the one in production.
GOOD_KEY="tEsTkEy_0123456789abcdefghijklmnopqrstuvwxyz"

# The three real emitted shapes, plus surrounding minified noise so a fixture
# is not just the bare key on a line.
bundle_with() {  # bundle_with <baked value>
    printf 'var a=1;const Cf="%s";H.set("Authorization",`Bearer ${Cf}`);export{a};' "$1"
}

# ── 0. It parses. A syntax error here would only surface mid-deploy. ────────
if bash -n "${CHECK}" 2>/dev/null; then
    pass "check_bundle_key.sh parses"
else
    fail "check_bundle_key.sh has a syntax error"
fi

if [[ -x "${CHECK}" ]]; then
    pass "check_bundle_key.sh is executable"
else
    fail "check_bundle_key.sh is not executable (chmod +x)"
fi

# ── 1. The happy path. Without this, every 'must fail' case below could be
#      satisfied by a script that fails unconditionally. ────────────────────
OUT="$(bundle_with "${GOOD_KEY}" | CITEVYN_DEMO_API_KEY="${GOOD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 0 ]]; then
    pass "a bundle carrying the expected key PASSES"
else
    fail "a correct bundle was rejected (rc=${RC}): ${OUT}"
fi

# ── 2. THE REGRESSION THIS FILE EXISTS FOR. An empty --build-arg bakes an
#      empty string; the browser then sends `Authorization: Bearer ` and every
#      call 401s. The old absence check printed 0 here and reported success.
OUT="$(bundle_with "" | CITEVYN_DEMO_API_KEY="${GOOD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "a bundle built with an EMPTY build arg FAILS (the #296 blind spot)"
else
    fail "an empty-key bundle passed — this is the exact hole being fixed"
fi
# ...and prove the OLD check really was blind to it, so the case above is not
# guarding something that was never broken.
if bundle_with "" | grep -qF 'local-demo-key'; then
    fail "fixture is wrong: the empty-arg bundle should NOT contain the default"
else
    pass "the old 'grep -c local-demo-key = 0' check would have PASSED this bundle"
fi

# ── 3. The original v6 outage: the arg was not passed at all. ──────────────
OUT="$(bundle_with "local-demo-key" | CITEVYN_DEMO_API_KEY="${GOOD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "a bundle carrying the public default FAILS (the v6 shape)"
else
    fail "a default-key bundle passed (rc=${RC})"
fi
if printf '%s' "${OUT}" | grep -q 'local-demo-key'; then
    pass "the default case names its own diagnosis"
else
    fail "the default case did not report the sharper diagnosis"
fi

# ── 4. A DIFFERENT non-empty key (rotated secret, wrong app) also fails. ───
OUT="$(bundle_with "some-other-key-that-is-long-enough-x" | CITEVYN_DEMO_API_KEY="${GOOD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "a bundle carrying a DIFFERENT key FAILS"
else
    fail "a mismatched key passed (rc=${RC})"
fi

# ── 5. The vacuity trap: an empty EXPECTED key must not make the check pass
#      on everything. `grep -F ""` matches any input, so without the partner
#      guard this whole gate would be a no-op that always reports success.
OUT="$(bundle_with "${GOOD_KEY}" | CITEVYN_DEMO_API_KEY="" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "an EMPTY expected key FAILS instead of matching everything"
else
    fail "an empty expected key passed — the gate is vacuous"
fi

# WHITESPACE-ONLY is not caught by `-z`, and a space occurs in essentially
# every bundle — so this passed on a bundle baked with "" until the guard
# stripped whitespace before testing.
# 20 spaces: long enough to clear the 16-char floor, so this reaches the
# whitespace guard itself. With a 3-char value the floor caught it first and
# the whitespace guard could be deleted with the suite still green.
OUT="$(bundle_with "" | CITEVYN_DEMO_API_KEY="                    " "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "a WHITESPACE-ONLY expected key FAILS (it would match almost anything)"
else
    fail "a whitespace-only expected key passed on an empty-arg bundle — vacuous"
fi

# A one-character key matches every bundle ever built.
OUT="$(bundle_with "" | CITEVYN_DEMO_API_KEY="a" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "a key below the 16-char production floor FAILS"
else
    fail "a 1-character expected key passed on an empty-arg bundle — vacuous"
fi
# ...but the publicly-known LOCAL DEFAULT is 14 chars and is a legitimate value
# on a developer's machine. The length floor must not break `make deploy-verify`
# against a local stack — a regression the first version of that floor caused,
# caught by tests/shell/test_deploy_verify_bundle_gate.sh.
OUT="$(bundle_with "local-demo-key" | CITEVYN_DEMO_API_KEY="local-demo-key" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 0 ]]; then
    pass "the 14-char local default is accepted when it is genuinely the key"
else
    fail "the local-dev default was rejected by the length floor (rc=${RC}): ${OUT}"
fi

# unset, not merely empty
OUT="$(bundle_with "${GOOD_KEY}" | env -u CITEVYN_DEMO_API_KEY "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "an UNSET expected key FAILS"
else
    fail "an unset expected key passed — the gate is vacuous"
fi

# ── 6. An empty bundle (a 404 fetched into the pipe) must FAIL, not read as
#      'key absent'... which it also is, but for a reason the operator needs.
OUT="$(printf '' | CITEVYN_DEMO_API_KEY="${GOOD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]] && printf '%s' "${OUT}" | grep -q 'empty'; then
    pass "an empty bundle FAILS and says the fetch produced nothing"
else
    fail "an empty bundle was not reported as a fetch problem (rc=${RC}): ${OUT}"
fi

# ── 7. The key must never be echoed. This script's whole reason for taking
#      the key via the environment is that it must not leak — into a terminal,
#      a CI log, or a pasted incident report.
for scenario in good bad; do
    if [[ "${scenario}" == good ]]; then
        OUT="$(bundle_with "${GOOD_KEY}" | CITEVYN_DEMO_API_KEY="${GOOD_KEY}" "${CHECK}" 2>&1)"
    else
        OUT="$(bundle_with "" | CITEVYN_DEMO_API_KEY="${GOOD_KEY}" "${CHECK}" 2>&1)"
    fi
    if printf '%s' "${OUT}" | grep -qF "${GOOD_KEY}"; then
        fail "the ${scenario} path PRINTED the key"
    else
        pass "the ${scenario} path never prints the key"
    fi
done

# ── 8. Literal-substring semantics: a key containing regex metacharacters
#      must be compared literally, or a '.' would match any character and a
#      near-miss bundle would pass.
META_KEY='a+b.c/d$e*f[g]h-0123456789abcdef'
OUT="$(bundle_with "${META_KEY}" | CITEVYN_DEMO_API_KEY="${META_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 0 ]]; then
    pass "a key with regex metacharacters matches itself"
else
    fail "a key with metacharacters was rejected (rc=${RC}): ${OUT}"
fi
OUT="$(bundle_with 'aXbYcZdQeWfAgBh-0123456789abcdef' | CITEVYN_DEMO_API_KEY="${META_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "metacharacters are NOT treated as regex wildcards"
else
    fail "'.' matched an arbitrary character — the comparison is not literal"
fi

# ── 9. Argument misuse must not be mistaken for a pass. ────────────────────
OUT="$(bundle_with "${GOOD_KEY}" | CITEVYN_DEMO_API_KEY="${GOOD_KEY}" "${CHECK}" "${GOOD_KEY}" 2>&1)"; RC=$?
if [[ ${RC} -eq 2 ]]; then
    pass "passing the key as an argument is a usage error (exit 2), not a pass"
else
    fail "expected exit 2 for argv misuse, got ${RC}"
fi

# ── Non-vacuity: this file must actually have asserted something. A suite
#    that silently collects zero cases reports green while guarding nothing.
# PINNED EXACTLY, not a floor. Under `>= 14` with 16 running, two whole cases
# could be deleted and the suite still printed "all passed" — demonstrated in
# review. Bump this deliberately when you add a case.
_EXPECTED_ASSERTIONS=19
if [[ ${ASSERTIONS} -ne ${_EXPECTED_ASSERTIONS} ]]; then
    echo "  FAIL — ${ASSERTIONS} assertions ran; expected exactly ${_EXPECTED_ASSERTIONS}."
    echo "         A case was added or removed: update _EXPECTED_ASSERTIONS on purpose."
    FAILURES=$((FAILURES + 1))
fi

echo
if [[ ${FAILURES} -eq 0 ]]; then
    echo "test_check_bundle_key.sh: ${ASSERTIONS} assertions, all passed"
    exit 0
fi
echo "test_check_bundle_key.sh: ${FAILURES} of ${ASSERTIONS} assertions FAILED"
exit 1
