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
#     REFUSES, before touching anything, when the target tag is missing a
#     migration that HEAD ships — because that rollback cannot work: the live DB
#     is stamped at a revision the target tree does not contain, so alembic dies
#     with "Can't locate revision identified by 'NNNN'" inside a container,
#     mid-deploy (#195). Override with --allow-migration-mismatch only when you
#     KNOW the schema is compatible (see below).
#   - It does NOT promote a previous index version. Index rollback is a separate
#     concern (RELEASE_PLAN §8) — use the admin promote API.
#   - It does NOT reset the ANSWER CACHE. `answer_policy_version` is part of the
#     cache-key pre-image, so a release bumps it when it makes previously-cached
#     answers WRONG. Rolling back restores the OLD value and brings those answers
#     back into scope for the rest of the cache TTL. This script warns when the
#     target tag ships a different value; the fix is to pin a THIRD value in
#     infra/docker/.env before rolling back, so the cache is cold both ways
#     (RUNBOOK §5.3a).
#
# Usage:
#   ./scripts/rollback.sh v0.9.0            # roll back to an explicit tag
#   ./scripts/rollback.sh --previous        # roll back to the tag before HEAD
#   ./scripts/rollback.sh v0.9.0 --dry-run  # print the plan, change nothing
#   ./scripts/rollback.sh v0.9.0 --allow-migration-mismatch
#                                           # proceed across a migration
#                                           # boundary anyway. ONLY correct when
#                                           # either (a) you have just restored a
#                                           # database backup from that release
#                                           # (RUNBOOK §4.2), or (b) you know the
#                                           # migrations since the target are
#                                           # additive-only AND the old code
#                                           # tolerates the new schema. Alembic
#                                           # will still fail if the live DB is
#                                           # stamped at a revision the target
#                                           # tree does not contain.
#
# Exit codes: 0 = rolled back and healthy, non-zero = rollback failed.
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

TARGET=""
DRY_RUN=0
ALLOW_MIGRATION_MISMATCH=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --previous)
            TARGET="__PREVIOUS__"
            shift
            ;;
        --allow-migration-mismatch)
            ALLOW_MIGRATION_MISMATCH=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            # Print the whole header block: line 2 through the closing ─── rule.
            # A hard-coded end line silently truncates --help whenever the header
            # grows (it did — the usage examples and exit codes vanished).
            sed -n '2,/^# ─\{10,\}/p' "${BASH_SOURCE[0]}"
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
    echo "usage: rollback.sh <tag>|--previous [--dry-run] [--allow-migration-mismatch]" >&2
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

# REFUSE — do not merely warn — when the target tree is missing a migration that
# HEAD ships. The old behaviour printed this warning and then proceeded, and the
# rollback died anyway a minute later inside a one-shot alembic container with
#   "Can't locate revision identified by '0006'"
# after the stack had already started rolling toward the old release (#195). A
# rollback tool that cannot succeed must say so BEFORE it touches production.
#
# Checked before --dry-run on purpose: a dry run that prints a plan which the
# real run would refuse is worse than useless during an incident.
# shellcheck source=infra/docker/scripts/_migration_gen.sh
source "${COMPOSE_DIR}/scripts/_migration_gen.sh"
_missing_migrations="$(migrations_missing_at "${TARGET}" HEAD)"
if [[ -n "${_missing_migrations}" ]]; then
    _missing_list="$(printf '%s' "${_missing_migrations}" | tr '\n' ' ')"
    if [[ "${ALLOW_MIGRATION_MISMATCH}" == "1" ]]; then
        echo "WARNING: ${TARGET} is missing applied migration(s): ${_missing_list}" >&2
        echo "         proceeding anyway because --allow-migration-mismatch was given." >&2
        echo "         This only works if the live database is stamped at a revision" >&2
        echo "         that ${TARGET} DOES contain (e.g. you just restored a backup" >&2
        echo "         from that release — RUNBOOK §4.2). Otherwise alembic will fail." >&2
    else
        echo "error: cannot roll back to ${TARGET} — it does not contain migration(s)" >&2
        echo "       that HEAD ships: ${_missing_list}" >&2
        echo "" >&2
        echo "       The live database is stamped at a revision that is NOT in" >&2
        echo "       ${TARGET}'s db/versions/, so 'alembic upgrade head' would fail with" >&2
        echo "         Can't locate revision identified by '<rev>'" >&2
        echo "       mid-deploy. A code-only rollback across a migration boundary is" >&2
        echo "       IMPOSSIBLE — no tag choice fixes it." >&2
        echo "" >&2
        echo "       Roll back the DATA instead (RUNBOOK §4.2), using a dump taken" >&2
        echo "       while ${TARGET} was live:" >&2
        echo "         docker compose --profile prod stop api worker" >&2
        echo "         ./infra/docker/scripts/restore.sh <dump-from-${TARGET}>" >&2
        echo "         ./infra/docker/scripts/rollback.sh ${TARGET} --allow-migration-mismatch" >&2
        echo "" >&2
        echo "       If instead you KNOW the migrations above are additive-only and" >&2
        echo "       ${TARGET}'s code tolerates the current schema, re-run with" >&2
        echo "       --allow-migration-mismatch." >&2
        exit 1
    fi
fi

# Warn — do not block — when the target tag ships a DIFFERENT answer_policy_version.
# That value is part of the answer-cache key pre-image, so a release bumps it when the
# release makes previously-cached answers WRONG (v1 -> v2 in #169, where follow-ups had
# been stored as verbatim duplicates of the previous turn's answer). Rolling back
# restores the OLD value, which brings those poisoned rows back INTO key scope and
# re-serves them for the remainder of CITEVYN_CACHE_TTL_SECONDS. Nothing else evicts
# them — their source_version_hash and embedder_identity are still perfectly valid.
#
# This one is worth warning loudly about because it is SILENT: no migration, no error,
# and the stale answer comes back cited and well-formed.
#
# Returns the code default for a revision, or EMPTY when that revision has no
# config.py (a tag old enough to predate it). The `|| true` is load-bearing: this
# script runs under `set -euo pipefail`, so a failing `git show` would otherwise take
# the pipeline's non-zero status and ABORT THE ROLLBACK — turning a best-effort warning
# into an incident-path outage. Verified: without it, rolling back to a pre-config.py
# tag exits 128.
#
# Matches both the bare `= "v2"` literal and the `= Field(default="v2")` form other
# settings in that file use, so a later refactor of the field cannot silently switch
# this guard off.
_read_policy_version() {  # $1 = git revision
    { git show "$1:backend/app/core/config.py" 2>/dev/null |
        sed -nE 's/^[[:space:]]*answer_policy_version[[:space:]]*:[[:space:]]*str[[:space:]]*=[[:space:]]*(Field\(default=)?"([^"]+)".*/\2/p' |
        head -1; } || true
}

# An explicit pin in infra/docker/.env BEATS the code default (pydantic-settings,
# env_prefix CITEVYN_), so when one is present the rollback does not change the
# effective version at all and the warning would be actively wrong — it would push the
# operator to burn a cache that is not affected. Stay silent in that case.
_policy_pinned="$(sed -nE 's/^[[:space:]]*CITEVYN_ANSWER_POLICY_VERSION[[:space:]]*=.*/pinned/p' \
    "${COMPOSE_DIR}/.env" 2>/dev/null | head -1 || true)"
_policy_now="$(_read_policy_version HEAD)"
_policy_target="$(_read_policy_version "${TARGET}")"
if [[ -z "${_policy_pinned}" && -n "${_policy_now}" && -n "${_policy_target}" &&
      "${_policy_now}" != "${_policy_target}" ]]; then
    echo "WARNING: answer_policy_version differs — ${TARGET} ships '${_policy_target}'," >&2
    echo "         the current tree ships '${_policy_now}'." >&2
    echo "         Rolling back RESTORES '${_policy_target}', so every answer cached" >&2
    echo "         under it is served again for up to CITEVYN_CACHE_TTL_SECONDS." >&2
    echo "         If '${_policy_now}' was bumped to EVICT bad answers, pin a THIRD" >&2
    echo "         value FIRST so the cache is cold in both directions:" >&2
    echo "" >&2
    echo "           echo 'CITEVYN_ANSWER_POLICY_VERSION=v-rollback-\$(date +%s)' \\" >&2
    echo "               >> ${COMPOSE_DIR}/.env" >&2
    echo "" >&2
    echo "         It MUST go in ${COMPOSE_DIR}/.env — the containers read their" >&2
    echo "         environment from env_file, so a host-shell variable does NOT" >&2
    echo "         reach the app. See RUNBOOK §5.3a." >&2
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
