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
cp "${REPO_ROOT}/infra/docker/scripts/rollback.sh"       infra/docker/scripts/rollback.sh
cp "${REPO_ROOT}/infra/docker/scripts/_migration_gen.sh" infra/docker/scripts/_migration_gen.sh
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
git commit --quiet --no-verify -am "test: edit 9001"
_run _t_new
_assert "changed-but-present migration -> proceeds" 0

# 5. a target that ships MORE migrations than HEAD is not a boundary either
_add_migration 9002_test_target_only.py
git tag -f _t_ahead HEAD >/dev/null 2>&1
git reset --quiet --hard HEAD~1        # HEAD no longer has 9002; _t_ahead does
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

if [[ "${FAILURES}" -eq 0 ]]; then
    echo "all passed"
    exit 0
fi
echo "${FAILURES} failure(s)"
exit 1
