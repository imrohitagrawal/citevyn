#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# check_bundle_key.sh — assert a SERVED browser bundle carries the demo bearer
# the running server will actually accept (#296).
#
# WHY THIS EXISTS
# ---------------
# `infra/docker/Dockerfile.api` bakes VITE_API_DEMO_KEY into the JS bundle at
# BUILD time. If the deploy passes the wrong value — or none — every browser
# call 401s while /health stays green. That is release v6 (2026-09-02): the
# site was down for about an hour behind a healthy health check.
#
# The check this replaces was:
#
#     curl -s ".../$J" | grep -c local-demo-key   # must print 0
#
# It asserts the ABSENCE of the old default, and that is the wrong shape twice:
#
#   1. It is blind to the failure its own runbook paragraph warned about.
#      MEASURED with docker: `--build-arg VITE_API_DEMO_KEY=""` does NOT fall
#      back to the ARG default — it leaves the arg EMPTY. Vite then bakes
#      `const K = ""` (`??` in frontend/src/lib/api.ts does not fire on an
#      empty string, only on null/undefined), the browser sends a bare
#      `Authorization: Bearer `, and production 401s exactly as in v6 — but via
#      a different string. `grep -c local-demo-key` prints 0 on that bundle and
#      the operator is told everything is fine.
#
#   2. An absence check fails OPEN under refactoring. If the key ever moves to
#      a lazily-imported chunk, "not found" still reads as success. A presence
#      check fails CLOSED: it goes red and an operator investigates.
#
# So this asserts PRESENCE of the expected, non-empty key instead.
#
# THE PARTNER CHECK IS LOAD-BEARING, NOT DECORATION
# -------------------------------------------------
# A presence check has its own vacuity trap: `grep -F ""` matches every file.
# If the expected key were empty — `fly ssh console` against a machine that has
# scaled to zero returns nothing — a naive presence check would pass on ANY
# bundle. So the empty-key guard below runs FIRST and is a hard failure. It is
# the half that proves the thing being counted exists.
#
# SECRETS
# -------
# The key is read from the environment, never from argv, so it is not visible
# to `ps aux` (the same reasoning as deploy_verify.sh's `curl --config -`). It
# is never echoed: this script prints its LENGTH and a short SHA-256 prefix,
# which are enough to tell two keys apart in an incident and useless to an
# attacker.
#
# USAGE
#   CITEVYN_DEMO_API_KEY="$key" ./scripts/check_bundle_key.sh < bundle.js
#   curl -sS "$BASE/$CHUNK" | CITEVYN_DEMO_API_KEY="$key" ./scripts/check_bundle_key.sh
#
# EXIT CODES
#   0  the bundle carries the expected key
#   1  it does not, or the expected key is empty/unset, or the bundle is empty
#   2  usage error
# ────────────────────────────────────────────────────────────────────────────

set -uo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n '2,/^# ────/p' "${BASH_SOURCE[0]}"
    exit 0
fi

if [[ $# -gt 0 ]]; then
    echo "error: this script takes no arguments; it reads the bundle on stdin" >&2
    echo "usage: CITEVYN_DEMO_API_KEY=... $0 < bundle.js" >&2
    exit 2
fi

KEY="${CITEVYN_DEMO_API_KEY:-}"

# ── Partner check: the expected key must exist AND be a plausible key ──────
# Without this, the presence test below degenerates towards `index($0, "")`,
# which matches anything, and the whole gate becomes a no-op that always
# passes. Three degenerate shapes, all of which reviewers demonstrated:
#
#   ""    -- the empty substitution from a machine that has scaled to zero
#   "  "  -- whitespace only; `-z` is FALSE for it, and a space occurs in
#            essentially every bundle, so it passed on a bundle baked with ""
#   "a"   -- one character; matches every bundle ever built
#
# The floor is 16 because that is what `Settings._is_weak_secret` already
# enforces for CITEVYN_DEMO_API_KEY in production, so no key this gate will
# ever legitimately see is shorter. Borrowing the existing threshold keeps one
# definition of "too weak to be real" rather than inventing a second.
_MIN_KEY_LENGTH=16
_STRIPPED="${KEY//[[:space:]]/}"

if [[ -z "${_STRIPPED}" ]]; then
    echo "[FAIL] CITEVYN_DEMO_API_KEY is empty, unset, or whitespace only." >&2
    echo "       Nothing to look for, so this check cannot pass — and a blank" >&2
    echo "       needle would match almost any bundle, reporting a false PASS." >&2
    echo "       The usual cause is that the Fly machine had scaled to zero and" >&2
    echo "       'fly ssh console -C printenv' returned nothing — wake it with" >&2
    echo "       'curl -sS https://citevyn.stackclimb.com/health' and retry." >&2
    exit 1
fi

# The publicly-known local default is 14 characters and is a LEGITIMATE value
# on a developer's machine — `make deploy-verify` against a local compose stack
# whose .env uses it must still pass, exactly as it did before this check
# existed. It is exempt by name rather than by lowering the floor, because
# lowering the floor to 14 would be an arbitrary number chosen to fit one
# string. It cannot be abused in production: Settings refuses to boot with this
# value when CITEVYN_ENVIRONMENT=production (_reject_default_demo_key_in_production).
if [[ "${#KEY}" -lt "${_MIN_KEY_LENGTH}" && "${KEY}" != "local-demo-key" ]]; then
    echo "[FAIL] CITEVYN_DEMO_API_KEY is ${#KEY} characters; production requires at" >&2
    echo "       least ${_MIN_KEY_LENGTH} (app/core/config.py: _is_weak_secret). A key this short is" >&2
    echo "       either truncated or not a real key, and a short needle can match a" >&2
    echo "       bundle by coincidence — which would be a false PASS." >&2
    exit 1
fi

BUNDLE="$(cat)"

if [[ -z "${BUNDLE}" ]]; then
    echo "[FAIL] the bundle on stdin is empty — nothing was fetched." >&2
    echo "       Check the asset URL: a 404 from the CDN/app also arrives as" >&2
    echo "       an empty or HTML body, which would otherwise read as 'key absent'." >&2
    exit 1
fi

# Fingerprint, never the value. `shasum -a 256` is present on macOS and on
# ubuntu-latest; `sha256sum` is Linux-only, so prefer the portable one.
if command -v shasum >/dev/null 2>&1; then
    KEY_FP="$(printf '%s' "${KEY}" | shasum -a 256 | cut -c1-12)"
elif command -v sha256sum >/dev/null 2>&1; then
    KEY_FP="$(printf '%s' "${KEY}" | sha256sum | cut -c1-12)"
else
    KEY_FP="(no sha256 tool)"
fi

# The comparison itself. `grep -F -e` would put the key in argv where `ps` can
# read it; passing it through the environment to awk keeps it out. `index()`
# is a literal substring test — no regex metacharacter can change its meaning,
# which matters because a generated key may contain '.', '+' or '/'.
if printf '%s' "${BUNDLE}" | KEY="${KEY}" awk 'index($0, ENVIRON["KEY"]) { found = 1 } END { exit !found }'; then
    echo "[PASS] the served bundle carries the expected demo key" \
         "(length ${#KEY}, sha256 ${KEY_FP}…)"
    exit 0
fi

echo "[FAIL] the served bundle does NOT carry the expected demo key" \
     "(length ${#KEY}, sha256 ${KEY_FP}…)." >&2
echo "       Every browser API call will return 401 while /health stays green." >&2
echo "       This is #296. Rebuild and redeploy with the build argument:" >&2
echo "         fly deploy --app citevyn --build-arg VITE_API_DEMO_KEY=\"\${DEMO_KEY:?}\" ..." >&2
echo "       See docs/DEPLOY_FLY.md §4.1." >&2

# Name the two known shapes, to save an incident a debugging round-trip. Both
# are reported as the same failure above; this only sharpens the diagnosis.
if printf '%s' "${BUNDLE}" | grep -qF 'local-demo-key'; then
    echo "       Diagnosis: the bundle carries the PUBLIC DEFAULT 'local-demo-key'," >&2
    echo "       so --build-arg VITE_API_DEMO_KEY was not passed at all (the v6 shape)." >&2
fi
exit 1
