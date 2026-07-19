#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_rollback_policy_warn.sh — assertions for the answer_policy_version
# warning in infra/docker/scripts/rollback.sh.
#
# Same style as test_env_guard.sh: no external framework, each case runs the
# real script (always with --dry-run, which exits before any checkout, env
# guard, or deploy) against throwaway tags in a throwaway clone. Run from the
# repo root:
#
#   bash tests/shell/test_rollback_policy_warn.sh
#
# Exit code 0 = all pass, 1 = at least one failure.
#
# The property under test is NOT "does it warn" — it is "does it warn WITHOUT
# ever breaking a rollback". rollback.sh is the incident tool; a missed warning
# is acceptable, a script that aborts is not. So every case asserts the exit
# code and that the run still reached its dry-run plan:
#
#   1. versions differ            -> warns, exit 0, reaches the plan
#   2. versions match             -> silent, exit 0, reaches the plan
#   3. target predates config.py  -> silent, exit 0, reaches the plan
#      (regression guard for the `|| true`: without it this exits 128 under
#       `set -euo pipefail` — a warning turning into an incident-path outage)
#   4. Field(default="...") form  -> still warns (the field's style may change)
#   5. infra/docker/.env pins the version -> silent (an explicit pin beats the
#      code default at runtime, so the rollback does not change the effective
#      version and the warning would be actively wrong)
# ────────────────────────────────────────────────────────────────────────────

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FAILURES=0
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

pass() { echo "  ok   — $1"; }
fail() { echo "  FAIL — $1"; FAILURES=$((FAILURES + 1)); }

# A throwaway clone so throwaway tags/commits never touch the real repo.
git clone --quiet --no-hardlinks "${REPO_ROOT}" "${WORK}/repo" 2>/dev/null
cd "${WORK}/repo" || exit 1
# `git clone` copies COMMITTED history only, so without this the suite would
# silently exercise the last committed rollback.sh and report green on a stale
# script while your edits sit uncommitted. Test what is on disk.
cp "${REPO_ROOT}/infra/docker/scripts/rollback.sh" infra/docker/scripts/rollback.sh
git config user.email t@t.invalid
git config user.name t
# The env guard is never reached under --dry-run, but keep a .env absent by
# default so case 5 can introduce one deliberately.
rm -f infra/docker/.env

_set_policy() {  # $1 = literal replacement line
    python3 - "$1" <<'PY'
import re, sys, pathlib
p = pathlib.Path("backend/app/core/config.py")
s = p.read_text()
s = re.sub(r'^(\s*)answer_policy_version: str = .*$', lambda m: m.group(1) + sys.argv[1],
           s, count=1, flags=re.M)
p.write_text(s)
PY
    git commit --quiet --no-verify -am "test: $1"
}

_run() {  # $1 = tag -> sets RC / OUT
    OUT="$(./infra/docker/scripts/rollback.sh "$1" --dry-run 2>&1)"
    RC=$?
}

_assert() {  # $1 = label, $2 = want-warning (0/1)
    local warned=0 reached=0
    grep -q "answer_policy_version differs" <<<"${OUT}" && warned=1
    grep -q "would run" <<<"${OUT}" && reached=1
    if [[ "${RC}" -ne 0 ]]; then
        fail "$1 — script exited ${RC} (a warning must never break a rollback)"
    elif [[ "${reached}" -ne 1 ]]; then
        fail "$1 — never reached the dry-run plan"
    elif [[ "${warned}" -ne "$2" ]]; then
        fail "$1 — warning=${warned}, wanted $2"
    else
        pass "$1"
    fi
}

echo "test_rollback_policy_warn.sh"

# Baseline tag ships whatever main ships; HEAD is moved per case.
git tag -f _t_base HEAD >/dev/null 2>&1

# 1. versions differ -> warns
_set_policy 'answer_policy_version: str = "vTEST-NEW"'
_run _t_base; _assert "versions differ -> warns, exit 0, proceeds" 1

# 2. versions match -> silent
git tag -f _t_same HEAD >/dev/null 2>&1
_run _t_same; _assert "versions match -> silent, exit 0, proceeds" 0

# 3. target predates config.py -> silent, MUST NOT abort (guards the `|| true`)
git tag -f _t_root "$(git rev-list --max-parents=0 HEAD | head -1)" >/dev/null 2>&1
if git show "_t_root:backend/app/core/config.py" >/dev/null 2>&1; then
    echo "  skip — root commit already has config.py; cannot exercise the missing-file path"
else
    _run _t_root; _assert "target predates config.py -> silent, exit 0, proceeds" 0
fi

# 4. Field(default=...) form is still recognised
_set_policy 'answer_policy_version: str = Field(default="vTEST-FIELD")'
_run _t_base; _assert "Field(default=...) form -> still warns" 1

# 5. an explicit .env pin makes the rollback a no-op for the cache -> silent
echo 'CITEVYN_ANSWER_POLICY_VERSION=vPINNED' > infra/docker/.env
_run _t_base; _assert ".env pins the version -> silent" 0
rm -f infra/docker/.env

if [[ "${FAILURES}" -eq 0 ]]; then
    echo "all passed"
    exit 0
fi
echo "${FAILURES} failure(s)"
exit 1
