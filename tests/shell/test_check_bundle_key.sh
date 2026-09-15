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
#   no token build arg                 -> bundle contains  ,Cf="local-demo-key"
#   VITE_PUBLIC_CLIENT_TOKEN=""        -> bundle contains  ,Cf=""
#   a token build arg=<value>           -> bundle contains  ,Cf="<a value>"
# and the corresponding `docker build --build-arg VITE_PUBLIC_CLIENT_TOKEN=""`
# leaves the ARG EMPTY rather than falling back to its default, which is why the
# middle row exists at all. (That row was measured while api.ts joined the
# operand with `??`; #430 made it `||`, so an empty arg now bakes the default
# instead. The middle fixture is KEPT: it is still a bundle the checker must
# reject, and it is what a lazily-restored `??` would produce again.) `Cf` is a minifier-assigned name and changes
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
OUT="$(bundle_with "${GOOD_KEY}" | CITEVYN_PUBLIC_CLIENT_TOKEN="${GOOD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 0 ]]; then
    pass "a bundle carrying the expected key PASSES"
else
    fail "a correct bundle was rejected (rc=${RC}): ${OUT}"
fi

# ── 2. THE REGRESSION THIS FILE EXISTS FOR. An empty --build-arg bakes an
#      empty string; the browser then sends `Authorization: Bearer ` and every
#      call 401s. The old absence check printed 0 here and reported success.
OUT="$(bundle_with "" | CITEVYN_PUBLIC_CLIENT_TOKEN="${GOOD_KEY}" "${CHECK}" 2>&1)"; RC=$?
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
OUT="$(bundle_with "local-demo-key" | CITEVYN_PUBLIC_CLIENT_TOKEN="${GOOD_KEY}" "${CHECK}" 2>&1)"; RC=$?
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
OUT="$(bundle_with "some-other-key-that-is-long-enough-x" | CITEVYN_PUBLIC_CLIENT_TOKEN="${GOOD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "a bundle carrying a DIFFERENT key FAILS"
else
    fail "a mismatched key passed (rc=${RC})"
fi

# ── 5. The vacuity trap: an empty EXPECTED key must not make the check pass
#      on everything. `grep -F ""` matches any input, so without the partner
#      guard this whole gate would be a no-op that always reports success.
OUT="$(bundle_with "${GOOD_KEY}" | CITEVYN_PUBLIC_CLIENT_TOKEN="" "${CHECK}" 2>&1)"; RC=$?
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
OUT="$(bundle_with "" | CITEVYN_PUBLIC_CLIENT_TOKEN="                    " "${CHECK}" 2>&1)"; RC=$?
# The REASON is asserted, not just rc=1. Review deleted the whitespace-strip
# from the checker and this case still printed ok: the mutant exited 1 too, for
# the unrelated reason "key not found in bundle". A case that cannot tell those
# apart is not testing the guard it names — and the mutant DID produce a false
# PASS on an indented index.html, which the gate now cats.
if [[ ${RC} -eq 1 ]] && printf '%s' "${OUT}" | grep -q 'whitespace only'; then
    pass "a WHITESPACE-ONLY expected key FAILS, and says why"
else
    fail "a whitespace-only expected key was not rejected by the whitespace guard (rc=${RC}): ${OUT}"
fi

# The same key against a bundle that CONTAINS runs of spaces — an indented
# index.html, which deploy_verify.sh now cats. Without the whitespace guard
# this is a false PASS, not merely a right answer for the wrong reason.
OUT="$(printf '<html>\n                    <body>x</body>\n</html>' | CITEVYN_PUBLIC_CLIENT_TOKEN="                    " "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "a whitespace key does NOT match an indented HTML bundle"
else
    fail "a whitespace-only key matched indented HTML — false PASS (rc=${RC})"
fi

# A one-character key matches every bundle ever built.
OUT="$(bundle_with "" | CITEVYN_PUBLIC_CLIENT_TOKEN="a" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "a key below the 16-char production floor FAILS"
else
    fail "a 1-character expected key passed on an empty-arg bundle — vacuous"
fi
# ...but the publicly-known LOCAL DEFAULT is 14 chars and is a legitimate value
# on a developer's machine. The length floor must not break `make deploy-verify`
# against a local stack — a regression the first version of that floor caused,
# caught by tests/shell/test_deploy_verify_bundle_gate.sh.
OUT="$(bundle_with "local-demo-key" | CITEVYN_PUBLIC_CLIENT_TOKEN="local-demo-key" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 0 ]]; then
    pass "the 14-char local default is accepted when it is genuinely the key"
else
    fail "the local-dev default was rejected by the length floor (rc=${RC}): ${OUT}"
fi

# unset, not merely empty
OUT="$(bundle_with "${GOOD_KEY}" | env -u CITEVYN_PUBLIC_CLIENT_TOKEN -u CITEVYN_DEMO_API_KEY "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "an UNSET expected key FAILS"
else
    fail "an unset expected key passed — the gate is vacuous"
fi

# ── 5b. ONE #430 spelling. The rename is finished: the Fly secret moved and the
#       deprecated CITEVYN_DEMO_API_KEY is read nowhere. The checker has to
#       resolve the token the SAME WAY the server does -- a checker that read a
#       name Settings ignores would report [PASS] on a bundle the server 401s,
#       which is the precise failure this script exists to prevent. So the
#       retired name is asserted INERT, in the direction that can fail.
NEW_KEY="new-name-key-0123456789abcdef"
OLD_KEY="old-name-key-0123456789abcdef"

OUT="$(bundle_with "${NEW_KEY}" | env -u CITEVYN_DEMO_API_KEY CITEVYN_PUBLIC_CLIENT_TOKEN="${NEW_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 0 ]]; then
    pass "CITEVYN_PUBLIC_CLIENT_TOKEN alone is read (the only name)"
else
    fail "the new name was not read (rc=${RC}): ${OUT}"
fi

# The retired name alone, over a bundle that DOES carry its value. A checker
# that still read it would report [PASS] here; the one shipped has no needle at
# all and must fail with the empty-key diagnosis. Asserting the rc alone is not
# enough -- rc 1 is also "key not found in bundle" -- so the MESSAGE is checked,
# the same distinction case 5c relies on.
OUT="$(bundle_with "${OLD_KEY}" | env -u CITEVYN_PUBLIC_CLIENT_TOKEN CITEVYN_DEMO_API_KEY="${OLD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]] && printf '%s' "${OUT}" | grep -q 'whitespace only'; then
    pass "the retired CITEVYN_DEMO_API_KEY is NOT read (no needle, hard fail)"
else
    fail "the retired name was read, or failed for the wrong reason (rc=${RC}): ${OUT}"
fi

# ...and it cannot override the live name either: both set, bundle carries only
# the RETIRED value, so the checker must resolve the new one and report the
# mismatch. Asserting the pass direction instead would stay green under either
# resolution, since the retired value would simply be ignored -- proving nothing.
OUT="$(bundle_with "${OLD_KEY}" | CITEVYN_PUBLIC_CLIENT_TOKEN="${NEW_KEY}" CITEVYN_DEMO_API_KEY="${OLD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "a stale bundle is caught even with the retired name set alongside"
else
    fail "the checker read the retired name while both were set -- it would bless a bundle the server rejects (rc=${RC}): ${OUT}"
fi

# ...and its non-vacuity partner: the same pair over a bundle that DOES carry
# the live value must pass, or the case above would be satisfied by a checker
# that rejects every both-set invocation.
OUT="$(bundle_with "${NEW_KEY}" | CITEVYN_PUBLIC_CLIENT_TOKEN="${NEW_KEY}" CITEVYN_DEMO_API_KEY="${OLD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 0 ]]; then
    pass "both set, bundle carries the live value -> PASS (partner)"
else
    fail "a correct both-set invocation was rejected (rc=${RC}): ${OUT}"
fi

# An EMPTY live name is a hard [FAIL] now that there is nothing to fall through
# TO. It used to fall through to the retired name; that path is gone, and this
# is the case that changes direction -- the cheapest tripwire if it comes back.
OUT="$(bundle_with "${OLD_KEY}" | CITEVYN_PUBLIC_CLIENT_TOKEN="" CITEVYN_DEMO_API_KEY="${OLD_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]] && printf '%s' "${OUT}" | grep -q 'whitespace only'; then
    pass "an EMPTY live name is a hard failure, with no fallback to the retired one"
else
    fail "an empty live name fell through to the retired name (rc=${RC}): ${OUT}"
fi

# ── 6. An empty bundle (a 404 fetched into the pipe) must FAIL, not read as
#      'key absent'... which it also is, but for a reason the operator needs.
OUT="$(printf '' | CITEVYN_PUBLIC_CLIENT_TOKEN="${GOOD_KEY}" "${CHECK}" 2>&1)"; RC=$?
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
        OUT="$(bundle_with "${GOOD_KEY}" | CITEVYN_PUBLIC_CLIENT_TOKEN="${GOOD_KEY}" "${CHECK}" 2>&1)"
    else
        OUT="$(bundle_with "" | CITEVYN_PUBLIC_CLIENT_TOKEN="${GOOD_KEY}" "${CHECK}" 2>&1)"
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
OUT="$(bundle_with "${META_KEY}" | CITEVYN_PUBLIC_CLIENT_TOKEN="${META_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 0 ]]; then
    pass "a key with regex metacharacters matches itself"
else
    fail "a key with metacharacters was rejected (rc=${RC}): ${OUT}"
fi
OUT="$(bundle_with 'aXbYcZdQeWfAgBh-0123456789abcdef' | CITEVYN_PUBLIC_CLIENT_TOKEN="${META_KEY}" "${CHECK}" 2>&1)"; RC=$?
if [[ ${RC} -eq 1 ]]; then
    pass "metacharacters are NOT treated as regex wildcards"
else
    fail "'.' matched an arbitrary character — the comparison is not literal"
fi

# ── 9. Argument misuse must not be mistaken for a pass. ────────────────────
OUT="$(bundle_with "${GOOD_KEY}" | CITEVYN_PUBLIC_CLIENT_TOKEN="${GOOD_KEY}" "${CHECK}" "${GOOD_KEY}" 2>&1)"; RC=$?
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
# 20 before #430; +5 for the two-name resolution block (section 5b).
_EXPECTED_ASSERTIONS=25
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
