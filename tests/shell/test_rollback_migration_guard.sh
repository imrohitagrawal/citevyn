#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_rollback_migration_guard.sh — assertions for the migration-boundary
# REFUSAL in infra/docker/scripts/rollback.sh (#195).
#
# The bug: rolling back to a tag whose db/versions/ is missing a migration the
# live DB is already stamped at cannot work. alembic dies with
#   Can't locate revision identified by '0006'
# inside a one-shot container, mid-deploy. The old script printed a warning and
# proceeded anyway. It must now refuse BEFORE touching anything.
#
# Same style as test_env_guard.sh / test_rollback_policy_warn.sh: no framework,
# every case runs the REAL script (always --dry-run, which changes nothing)
# against throwaway tags in a throwaway clone. Run from the repo root:
#
#   bash tests/shell/test_rollback_migration_guard.sh
#
# Exit code 0 = all pass, 1 = at least one failure.
#
# The properties under test:
#   1. missing migration                        -> REFUSES, non-zero, no plan
#   2. missing migration + override flag        -> warns, exit 0, reaches plan
#   3. same migration generation                -> silent, exit 0, reaches plan
#   4. migration file CHANGED but still present -> NOT a refusal (it still
#      resolves by revision id; refusing here would block valid rollbacks)
#   5. target ships MORE migrations than HEAD   -> NOT a refusal (the live DB is
#      stamped at HEAD's newest, which the target still contains)
#   6. the refusal fires BEFORE the checkout    -> the clone is still on its
#      original branch afterwards
# ────────────────────────────────────────────────────────────────────────────

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FAILURES=0
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

pass() { echo "  ok   — $1"; }
fail() { echo "  FAIL — $1"; FAILURES=$((FAILURES + 1)); }

git clone --quiet --no-hardlinks "${REPO_ROOT}" "${WORK}/repo" 2>/dev/null
cd "${WORK}/repo" || exit 1
# `git clone` copies COMMITTED history only, so without this the suite would
# exercise the last committed scripts and report green on a stale tree.
#
# These copies are UNCOMMITTED modifications to tracked files, so anything that
# resets the working tree throws them away — `git reset --hard` in case 5 did
# exactly that, and every case after it silently ran the committed scripts. Hence
# a helper, re-invoked after any tree-resetting step, plus an assertion at the
# end that the copies really are still in place.
_install_scripts() {
    cp "${REPO_ROOT}/infra/docker/scripts/rollback.sh"       infra/docker/scripts/rollback.sh
    cp "${REPO_ROOT}/infra/docker/scripts/_migration_gen.sh" infra/docker/scripts/_migration_gen.sh
}
_install_scripts
git config user.email t@t.invalid
git config user.name t
rm -f infra/docker/.env   # the env guard is never reached under --dry-run

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

_run() {  # $@ = args to rollback.sh -> sets RC / OUT
    OUT="$(./infra/docker/scripts/rollback.sh "$@" --dry-run 2>&1)"
    RC=$?
}

_assert() {  # $1 = label, $2 = want-refusal (0/1)
    local refused=0 reached=0
    grep -q "cannot roll back to" <<<"${OUT}" && refused=1
    grep -q "would run" <<<"${OUT}" && reached=1
    if [[ "$2" -eq 1 ]]; then
        if [[ "${refused}" -ne 1 ]]; then
            fail "$1 — did not refuse; output: ${OUT}"
        elif [[ "${RC}" -eq 0 ]]; then
            fail "$1 — refused but exited 0 (callers branch on the exit code)"
        elif [[ "${reached}" -eq 1 ]]; then
            fail "$1 — refused but still printed a rollback plan"
        else
            pass "$1"
        fi
    else
        if [[ "${refused}" -eq 1 ]]; then
            fail "$1 — refused, but this rollback is possible"
        elif [[ "${RC}" -ne 0 ]]; then
            fail "$1 — exited ${RC} (a possible rollback must not be blocked)"
        elif [[ "${reached}" -ne 1 ]]; then
            fail "$1 — never reached the dry-run plan; output: ${OUT}"
        else
            pass "$1"
        fi
    fi
}

_add_migration() {  # $1 = filename
    printf 'revision = "%s"\n' "$1" > "db/versions/$1"
    git add "db/versions/$1"
    git commit --quiet --no-verify -m "test: add $1"
}

echo "test_rollback_migration_guard.sh"

# _t_old ships the migrations main has; HEAD then gains one more, exactly like a
# release that migrated the schema.
git tag -f _t_old HEAD >/dev/null 2>&1
_add_migration 9001_test_added.py
git tag -f _t_new HEAD >/dev/null 2>&1

# 1. target is missing an applied migration -> refuse
_run _t_old
_assert "missing migration -> refuses, non-zero, no plan" 1

# 1b. the refusal names the missing file and the recovery path (an operator
#     under pressure needs both; a bare "refused" would be a worse UX than the
#     old warning).
if grep -q "9001_test_added.py" <<<"${OUT}" && grep -q "RUNBOOK" <<<"${OUT}" \
   && grep -q -- "--allow-migration-mismatch" <<<"${OUT}"; then
    pass "refusal names the missing migration, RUNBOOK §4.2 and the override"
else
    fail "refusal message is not actionable; output: ${OUT}"
fi

# 2. the explicit override proceeds (still loudly)
_run _t_old --allow-migration-mismatch
_assert "override flag -> proceeds to the plan" 0
if grep -q "allow-migration-mismatch was given" <<<"${OUT}"; then
    pass "override still warns"
else
    fail "override proceeded SILENTLY; output: ${OUT}"
fi

# 3. same migration generation -> untouched behaviour
_run _t_new
_assert "same migration generation -> proceeds" 0

# 4. a migration file that CHANGED but is still present is not a boundary
printf 'revision = "9001"  # edited\n' > db/versions/9001_test_added.py
# Explicit pathspec, NOT `-am`. `-a` stages every modified tracked file, which
# includes the two working-tree scripts copied over the clone above — and the
# `git reset --hard HEAD~1` in case 5 would then restore the COMMITTED versions
# of those scripts, silently reverting the code under test. From that point the
# suite would be exercising the last committed rollback.sh, so case 6 (the only
# assertion that runs it for real and the only proof that the refusal precedes
# the checkout) would pass against stale code no matter what the branch changed.
git commit --quiet --no-verify -m "test: edit 9001" -- db/versions/9001_test_added.py
_run _t_new
_assert "changed-but-present migration -> proceeds" 0

# 5. a target that ships MORE migrations than HEAD is not a boundary either
_add_migration 9002_test_target_only.py
git tag -f _t_ahead HEAD >/dev/null 2>&1
git reset --quiet --hard HEAD~1        # HEAD no longer has 9002; _t_ahead does
_install_scripts                       # --hard discarded the copies; put them back
_run _t_ahead
_assert "target ahead of HEAD -> proceeds" 0

# 6. the refusal must fire BEFORE the checkout — a rollback that dies half-way
#    leaves an operator on a detached HEAD during an incident.
OUT="$(./infra/docker/scripts/rollback.sh _t_old 2>&1)"; RC=$?
_here="$(git rev-parse --abbrev-ref HEAD)"
if [[ "${RC}" -eq 0 ]]; then
    fail "real run (no --dry-run) did not refuse"
elif [[ "${_here}" != "${BRANCH}" ]]; then
    fail "refusal happened AFTER the checkout (now on ${_here}, expected ${BRANCH})"
else
    pass "refusal fires before any checkout (still on ${BRANCH})"
fi

# ── The base-ref contract ──────────────────────────────────────────────────
# The guard compares the target tree against the DEPLOYED tree, using HEAD as
# the proxy. That proxy is only valid while HEAD is the deployed branch — and
# rollback.sh is itself how you get a detached HEAD, so chaining rollbacks is
# exactly when it would be wrong (the DB is still stamped at a revision HEAD no
# longer ships, so the boundary becomes INVISIBLE and the guard waves through
# the failure it exists to prevent).

# 7. a bare detached HEAD is refused rather than reasoned from
git checkout --quiet --detach HEAD
OUT="$(./infra/docker/scripts/rollback.sh _t_old --dry-run 2>&1)"; RC=$?
if [[ "${RC}" -eq 0 ]]; then
    fail "detached HEAD: proceeded without a --base-ref"
elif ! grep -q "HEAD is detached" <<<"${OUT}"; then
    fail "detached HEAD: exited ${RC} but not for the detached-HEAD reason; output: ${OUT}"
else
    pass "detached HEAD without --base-ref -> refuses"
fi

# 8. --base-ref restores the check: naming the deployed tree reaches the REAL
#    migration verdict rather than the detached-HEAD refusal.
OUT="$(./infra/docker/scripts/rollback.sh _t_old --base-ref "${BRANCH}" --dry-run 2>&1)"; RC=$?
if grep -q "HEAD is detached" <<<"${OUT}"; then
    fail "--base-ref was ignored; still refused for detached HEAD"
elif ! grep -q "cannot roll back to" <<<"${OUT}"; then
    fail "--base-ref did not reach the migration refusal; output: ${OUT}"
else
    pass "--base-ref <deployed> reaches the migration verdict on a detached HEAD"
fi

# 9. ...and it is a REAL comparison, not a rubber stamp: with the base pointing
#    at a tree of the same generation as the target, the same invocation
#    proceeds. Without this case, case 8 would pass for a --base-ref that was
#    parsed and thrown away.
OUT="$(./infra/docker/scripts/rollback.sh _t_old --base-ref _t_old --dry-run 2>&1)"; RC=$?
if [[ "${RC}" -ne 0 ]]; then
    fail "--base-ref of the same generation was blocked (rc=${RC}); output: ${OUT}"
elif ! grep -q "would run" <<<"${OUT}"; then
    fail "--base-ref of the same generation never reached the plan; output: ${OUT}"
else
    pass "--base-ref is compared, not merely accepted"
fi

# 10. a --base-ref that does not resolve is rejected, not silently treated as
#     empty (an empty base lists no migrations, so EVERY rollback would look
#     same-generation and the guard would be off).
OUT="$(./infra/docker/scripts/rollback.sh _t_old --base-ref no_such_ref_xyz --dry-run 2>&1)"; RC=$?
if [[ "${RC}" -eq 0 ]] || ! grep -q "is not a valid git revision" <<<"${OUT}"; then
    fail "an unresolvable --base-ref was not rejected; rc=${RC}, output: ${OUT}"
else
    pass "an unresolvable --base-ref is rejected"
fi
# NB: deliberately NO _install_scripts here. `git checkout <branch>` from a
# detached HEAD carries uncommitted modifications across, so the copies survive
# — and re-installing them six lines above the cmp below would make that
# assertion compare a file against a copy just made from it, i.e. unable ever to
# fail. The whole point of case 11 is to detect a revert; it cannot do that if
# it repairs the tree first.
git checkout --quiet "${BRANCH}"

# 11. meta-assertion: every case above is only meaningful if the scripts under
#    test are still the WORKING-TREE ones. If a future step resets the tree and
#    nobody re-installs them, the whole suite silently degrades to testing the
#    last commit — which is precisely how a broken guard could ship green.
if cmp -s "${REPO_ROOT}/infra/docker/scripts/rollback.sh" infra/docker/scripts/rollback.sh \
   && cmp -s "${REPO_ROOT}/infra/docker/scripts/_migration_gen.sh" infra/docker/scripts/_migration_gen.sh; then
    pass "the scripts under test are the working-tree copies, not the committed ones"
else
    fail "the working-tree scripts were reverted mid-suite; earlier results are STALE"
fi

if [[ "${FAILURES}" -eq 0 ]]; then
    echo "all passed"
    exit 0
fi
echo "${FAILURES} failure(s)"
exit 1
