#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# rollback.sh — roll the CiteVyn production stack back to a previous release.
#
# This is the INCIDENT tool: run it when a freshly-deployed version is bad and
# you need the last known good release serving again. It is also the drill step
# invoked by deploy_verify.sh so the documented rollback path is the one that
# actually gets exercised (RELEASE_PLAN §8, §10 blocker 9).
#
# What it does:
#   1. Refuses to run against a stub/dev .env (shared guard).
#   2. Checks out the target tag's SOURCE TREE (the compose file rebuilds the
#      images from source — see RUNBOOK §5.3), leaving you on a detached HEAD.
#   3. Re-deploys at that version via refresh.sh.
#   4. Waits for the api to report healthy.
#
# What it does NOT do:
#   - It does NOT reverse forward-only schema migrations. If the bad release
#     migrated the schema, restore a backup instead (RUNBOOK §4.2). This script
#     warns when the target tag is behind the current alembic head.
#   - It does NOT promote a previous index version. Index rollback is a separate
#     concern (RELEASE_PLAN §8) — use the admin promote API.
#
# Usage:
#   ./scripts/rollback.sh v0.9.0            # roll back to an explicit tag
#   ./scripts/rollback.sh --previous        # roll back to the tag before HEAD
#   ./scripts/rollback.sh v0.9.0 --dry-run  # print the plan, change nothing
#
# Exit codes: 0 = rolled back and healthy, non-zero = rollback failed.
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

TARGET=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --previous)
            TARGET="__PREVIOUS__"
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            sed -n '2,30p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        -*)
            echo "error: unknown flag '$1'" >&2
            exit 2
            ;;
        *)
            TARGET="$1"
            shift
            ;;
    esac
done

if [[ -z "${TARGET}" ]]; then
    echo "usage: rollback.sh <tag>|--previous [--dry-run]" >&2
    echo "       e.g. rollback.sh v0.9.0" >&2
    exit 2
fi

cd "${REPO_ROOT}"

# Resolve --previous to the tag immediately before the current HEAD so an
# operator under pressure does not have to look it up.
if [[ "${TARGET}" == "__PREVIOUS__" ]]; then
    # --sort=-version:refname gives newest-first; skip the tag that points at
    # HEAD (if any) and take the next one.
    _current_tag="$(git describe --tags --exact-match 2>/dev/null || true)"
    TARGET="$(git tag --list 'v*' --sort=-version:refname \
        | grep -v "^${_current_tag}$" \
        | head -1)"
    if [[ -z "${TARGET}" ]]; then
        echo "error: --previous found no earlier v* tag to roll back to" >&2
        echo "       list tags with: git tag --list 'v*' --sort=-version:refname" >&2
        exit 1
    fi
    echo "==> rollback.sh: --previous resolved to ${TARGET}"
fi

if ! git rev-parse -q --verify "refs/tags/${TARGET}" >/dev/null; then
    echo "error: tag '${TARGET}' does not exist" >&2
    echo "       available: $(git tag --list 'v*' --sort=-version:refname | tr '\n' ' ')" >&2
    exit 1
fi

echo "==> rollback.sh: target=${TARGET} (from $(git rev-parse --short HEAD))"

# Warn — do not block — when the target predates migrations that are already
# applied. The operator may still want the app rolled back; they just need to
# know the schema will NOT be reversed.
_migrations_ahead="$(git diff --name-only "${TARGET}..HEAD" -- db/versions 2>/dev/null | wc -l | tr -d ' ')"
if [[ "${_migrations_ahead}" != "0" ]]; then
    echo "WARNING: ${_migrations_ahead} migration file(s) landed after ${TARGET}." >&2
    echo "         rollback.sh does NOT reverse forward-only migrations." >&2
    echo "         If the bad release changed the schema, restore a backup" >&2
    echo "         instead — see RUNBOOK §4.2." >&2
fi

if [[ "${DRY_RUN}" == "1" ]]; then
    echo "==> --dry-run: would run:"
    echo "      git checkout ${TARGET}"
    echo "      VERSION=${TARGET} ./infra/docker/scripts/refresh.sh"
    exit 0
fi

# A dirty tree would be clobbered by the checkout. Refuse rather than lose work.
# Checked AFTER --dry-run: a dry run changes nothing, so it stays usable while
# you still have edits in flight (this is when you most want to plan a rollback).
if [[ -n "$(git status --porcelain)" ]]; then
    echo "error: working tree is dirty; commit or stash before rolling back" >&2
    git status --short >&2
    exit 1
fi

# Guard AFTER the dry-run branch so --dry-run stays usable on a dev box.
# shellcheck source=infra/docker/scripts/_env_guard.sh
source "${COMPOSE_DIR}/scripts/_env_guard.sh" "${COMPOSE_DIR}"

echo "==> checking out ${TARGET} (detached HEAD)"
git checkout --quiet "${TARGET}"

echo "==> re-deploying at ${TARGET}"
VERSION="${TARGET}" "${COMPOSE_DIR}/scripts/refresh.sh"

echo "==> rollback complete: ${TARGET} is deployed and healthy"
echo "    NOTE: you are on a detached HEAD. When the incident is resolved,"
echo "          return with:  git checkout main"
