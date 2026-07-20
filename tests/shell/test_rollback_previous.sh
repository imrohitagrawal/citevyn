#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_rollback_previous.sh — how `rollback.sh --previous` picks its target.
#
# This is an INCIDENT path: the operator types --previous precisely because they
# do not want to look a tag up under pressure. Two defects found by adversarial
# review, both reproduced against the real script, both regression-guarded here:
#
#   1. It could resolve to the CURRENTLY DEPLOYED release. The old code excluded
#      `git describe --exact-match`, which only answers "is there a tag on this
#      exact commit". One commit after the release — a doc merge, the normal
#      state of main after a cut — it found nothing, the exclusion filtered
#      nothing, and `head -1` returned the release being rolled back FROM. The
#      migration guard then passes trivially (same tree), refresh.sh rebuilds
#      the same bad image, and the script reports "rollback complete".
#
#   2. The "no earlier release tag" diagnosis was unreachable. Under
#      `set -euo pipefail` a grep matching nothing makes the pipeline non-zero,
#      the assignment inherits it, and set -e killed the script before the
#      message — a bare exit 1 with NO output, mid-incident.
#
# Every case builds a throwaway repo and runs the REAL script with --dry-run,
# which changes nothing.
# ────────────────────────────────────────────────────────────────────────────
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS="${REPO_ROOT}/infra/docker/scripts"
FAILURES=0
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

pass() { echo "  ok   — $1"; }
fail() { echo "  FAIL — $1"; FAILURES=$((FAILURES + 1)); }

echo "test_rollback_previous.sh"

# Build a repo with a real release history. Migrations are identical across the
# tags so the migration guard never fires and cannot mask a resolution bug.
_fixture() {  # $1 = dir name; echoes the path
    local d="${WORK}/$1"
    mkdir -p "${d}"/{db/versions,infra/docker/scripts}
    ( cd "${d}"
      git init -q .
      git config user.email t@t.invalid
      git config user.name t
      cp "${SCRIPTS}/rollback.sh" "${SCRIPTS}/_migration_gen.sh" infra/docker/scripts/
      echo 'revision = "0001"' > db/versions/0001_base.py
      git add -A && git commit -q -m base
    ) >/dev/null 2>&1
    printf '%s' "${d}"
}

_run_prev() {  # $1 = dir, rest = extra args -> sets RC / OUT
    OUT="$( cd "$1" && ./infra/docker/scripts/rollback.sh --previous "${@:2}" --dry-run 2>&1 )"
    RC=$?
}

# ── 1. THE regression: a commit after the release must not make --previous
#      select the release itself.
D="$(_fixture deployed)"
( cd "${D}"
  git tag v0.9.0
  git commit -q --allow-empty -m "v0.10.0 work"; git tag v0.10.0
  git commit -q --allow-empty -m "doc merge after the release"
) >/dev/null 2>&1
_run_prev "${D}"
if grep -q "resolved to v0.10.0" <<<"${OUT}"; then
    fail "REGRESSION: --previous selected the DEPLOYED release v0.10.0; output: ${OUT}"
elif ! grep -q "resolved to v0.9.0" <<<"${OUT}"; then
    fail "--previous did not resolve to v0.9.0; output: ${OUT}"
else
    pass "a commit after the release does not make --previous pick the deployed tag"
fi

# ── 2. The simple case still works: HEAD sits exactly on the release tag.
D="$(_fixture exact)"
( cd "${D}"
  git tag v0.9.0
  git commit -q --allow-empty -m "v0.10.0 work"; git tag v0.10.0
) >/dev/null 2>&1
_run_prev "${D}"
if ! grep -q "resolved to v0.9.0" <<<"${OUT}"; then
    fail "HEAD exactly on the release tag: expected v0.9.0; output: ${OUT}"
else
    pass "HEAD exactly on the release tag resolves to the one before it"
fi

# ── 3. --previous honours --base-ref. Without this, --previous + --base-ref is
#      incoherent: the operator names the deployed tree and --previous ignores
#      it — and that pairing is what the detached-HEAD refusal tells them to use.
D="$(_fixture baseref)"
( cd "${D}"
  git tag v0.9.0
  git commit -q --allow-empty -m "v0.10.0 work"; git tag v0.10.0
  git checkout -q --detach v0.9.0          # the state a prior rollback leaves
) >/dev/null 2>&1
_run_prev "${D}" --base-ref v0.10.0
if grep -q "resolved to v0.10.0" <<<"${OUT}"; then
    fail "--previous picked the deployed tree named by --base-ref; output: ${OUT}"
elif ! grep -q "resolved to v0.9.0" <<<"${OUT}"; then
    fail "--previous ignored --base-ref; output: ${OUT}"
else
    pass "--previous resolves relative to --base-ref, not to HEAD"
fi

# ── 4. Drill/pre-release tags are not eligible targets. version:refname sorts
#      them ABOVE the real release, so the bare `v*` glob would deploy an
#      unreviewed local commit to production.
D="$(_fixture drilltags)"
( cd "${D}"
  git tag v0.9.0
  git commit -q --allow-empty -m unreviewed; git tag v0.9.2-drill
  git commit -q --allow-empty -m "v0.10.0 work"; git tag v0.10.0
) >/dev/null 2>&1
_run_prev "${D}"
if grep -q "drill" <<<"${OUT}"; then
    fail "--previous selected a -drill tag; output: ${OUT}"
elif ! grep -q "resolved to v0.9.0" <<<"${OUT}"; then
    fail "expected v0.9.0; output: ${OUT}"
else
    pass "non-release tags (-drill, -rc) are never selected"
fi

# ── 5. THE second regression: when nothing is eligible the operator must get a
#      DIAGNOSIS, not a silent exit 1.
D="$(_fixture norelease)"
( cd "${D}"; git tag v1.0.0-rc1 ) >/dev/null 2>&1
_run_prev "${D}"
if [[ "${RC}" -eq 0 ]]; then
    fail "--previous succeeded with no eligible release tag"
elif [[ -z "${OUT}" ]]; then
    fail "REGRESSION: --previous died SILENTLY (rc=${RC}); the diagnosis is unreachable"
elif ! grep -q "no release tag" <<<"${OUT}"; then
    fail "--previous failed without naming the reason; output: ${OUT}"
elif ! grep -q "pass a tag explicitly" <<<"${OUT}"; then
    fail "--previous diagnosis is not actionable; output: ${OUT}"
else
    pass "no eligible release tag -> an actionable message, not a silent exit"
fi

# ── 6. ...and the same when the repo has NO tags at all.
D="$(_fixture notags)"
_run_prev "${D}"
if [[ "${RC}" -eq 0 ]]; then
    fail "--previous succeeded in a repo with no tags"
elif [[ -z "${OUT}" ]]; then
    fail "REGRESSION: --previous died SILENTLY in a repo with no tags (rc=${RC})"
else
    pass "no tags at all -> an actionable message, not a silent exit"
fi

if [[ "${FAILURES}" -eq 0 ]]; then
    echo "all passed"
    exit 0
fi
echo "${FAILURES} failure(s)"
exit 1
