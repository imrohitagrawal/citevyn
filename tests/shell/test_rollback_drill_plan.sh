#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_rollback_drill_plan.sh — assertions for the two rollback drills the
# release gate now runs (#195), and for restore.sh's argument guards.
#
# What can honestly be tested WITHOUT docker: the plan deploy_verify.sh prints,
# the flag surface, and restore.sh's refusals before it ever reaches a
# container. The drills themselves need a live prod stack — see the note in
# RELEASE_PLAN §10 blocker 9 about what this suite does and does not cover.
#
# Run from the repo root:
#   bash tests/shell/test_rollback_drill_plan.sh
# ────────────────────────────────────────────────────────────────────────────

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS="${REPO_ROOT}/infra/docker/scripts"
FAILURES=0
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

pass() { echo "  ok   — $1"; }
fail() { echo "  FAIL — $1"; FAILURES=$((FAILURES + 1)); }

echo "test_rollback_drill_plan.sh"

# ── 1. Every operator script parses. restore.sh in particular has no runtime
#      coverage here (it needs a live database), so a syntax error in it would
#      otherwise only surface during an incident.
for s in "${SCRIPTS}"/*.sh; do
    if bash -n "${s}" 2>"${WORK}/syn.err"; then
        pass "$(basename "${s}") parses"
    else
        fail "$(basename "${s}") has a syntax error: $(cat "${WORK}/syn.err")"
    fi
done

# ── 2. restore.sh argument guards — real runs, no docker reached.
OUT="$("${SCRIPTS}/restore.sh" 2>&1)"; RC=$?
if [[ "${RC}" -eq 2 ]] && grep -q "usage:" <<<"${OUT}"; then
    pass "restore.sh with no argument -> usage, exit 2"
else
    fail "restore.sh with no argument -> rc=${RC}, out=${OUT}"
fi

OUT="$("${SCRIPTS}/restore.sh" "${WORK}/does-not-exist.dump" 2>&1)"; RC=$?
if [[ "${RC}" -eq 1 ]] && grep -q "dump file not found" <<<"${OUT}"; then
    pass "restore.sh with a missing dump -> refuses, exit 1"
else
    fail "restore.sh with a missing dump -> rc=${RC}, out=${OUT}"
fi

# ── 3. deploy_verify.sh's plan and flag surface, in a throwaway clone so the
#      stub .env cannot collide with a real one.
git clone --quiet --no-hardlinks "${REPO_ROOT}" "${WORK}/repo" 2>/dev/null
cd "${WORK}/repo" || exit 1
cp "${SCRIPTS}/deploy_verify.sh"  infra/docker/scripts/deploy_verify.sh
cp "${SCRIPTS}/_migration_gen.sh" infra/docker/scripts/_migration_gen.sh
# --dry-run exits before the env guard, so a stub .env is enough to get past the
# preflight file check.
printf 'CITEVYN_PUBLIC_HOST=citevyn.test\nCITEVYN_DEMO_API_KEY=k\n' > infra/docker/.env

OUT="$(./infra/docker/scripts/deploy_verify.sh --dry-run 2>&1)"; RC=$?
if [[ "${RC}" -ne 0 ]]; then
    fail "deploy_verify.sh --dry-run exited ${RC}: ${OUT}"
else
    if grep -q "drill A (data)" <<<"${OUT}" && grep -q "restore.sh" <<<"${OUT}"; then
        pass "dry-run plan includes the data-recovery drill"
    else
        fail "dry-run plan does not mention the data-recovery drill: ${OUT}"
    fi
    # The plan must state the CONDITION on drill B. A plan that promises a code
    # rollback it cannot perform is the #195 failure restated as documentation.
    if grep -q "drill B (code)" <<<"${OUT}" && grep -q "migration" <<<"${OUT}"; then
        pass "dry-run plan states drill B is conditional on the migration generation"
    else
        fail "dry-run plan does not qualify the code-rollback drill: ${OUT}"
    fi
fi

OUT="$(./infra/docker/scripts/deploy_verify.sh --data-rollback-only --dry-run 2>&1)"; RC=$?
if [[ "${RC}" -eq 0 ]]; then
    pass "--data-rollback-only is accepted"
else
    fail "--data-rollback-only rejected (rc=${RC}): ${OUT}"
fi

OUT="$(./infra/docker/scripts/deploy_verify.sh --no-such-flag 2>&1)"; RC=$?
if [[ "${RC}" -eq 2 ]]; then
    pass "an unknown flag still exits 2"
else
    fail "unknown flag -> rc=${RC} (expected 2)"
fi

# ── 4. The gate must not be able to claim blocker 9 is satisfied on a run where
#      the code rollback was not proven. Both flags gate that sentence.
if grep -q 'DATA_ROLLBACK_PROVEN}" == "1" && "${CODE_ROLLBACK_PROVEN}" == "1"' \
        "${SCRIPTS}/deploy_verify.sh"; then
    pass "the 'blocker 9 satisfied' claim is gated on BOTH drills"
else
    fail "the 'blocker 9 satisfied' claim is no longer gated on both drills"
fi

if [[ "${FAILURES}" -eq 0 ]]; then
    echo "all passed"
    exit 0
fi
echo "${FAILURES} failure(s)"
exit 1
