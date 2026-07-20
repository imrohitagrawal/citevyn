#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# restore.sh — restore a backup.sh dump into the live CiteVyn database.
#
# This is the OTHER half of the rollback story. rollback.sh rolls back CODE; it
# cannot reverse a forward-only migration, so when the schema moved the only
# real rollback is restoring a dump taken while the old release was live
# (RUNBOOK §4.2, RELEASE_PLAN §10 blocker 9).
#
# Usage:
#   ./scripts/restore.sh ./backups/citevyn-20260720T101500Z.dump
#
# Stop the writers first, or concurrent writes will race the restore:
#   docker compose --profile prod stop api worker
#   ./scripts/restore.sh <dump>
#   docker compose --profile prod up -d api
#
# Exit codes: 0 = restored, non-zero = nothing (or only part) was restored.
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DUMP="${1:-}"
if [[ -z "${DUMP}" ]]; then
    echo "usage: restore.sh <path/to/citevyn-*.dump>" >&2
    exit 2
fi
if [[ ! -f "${DUMP}" ]]; then
    echo "error: dump file not found: ${DUMP}" >&2
    exit 1
fi
DUMP_ABS="$(cd "$(dirname "${DUMP}")" && pwd)/$(basename "${DUMP}")"

cd "${COMPOSE_DIR}"

if [[ ! -f .env ]]; then
    echo "error: .env not found at ${COMPOSE_DIR}/.env" >&2
    exit 1
fi
# Same guard as backup.sh / refresh.sh: never touch a stub/dev env.
# shellcheck source=infra/docker/scripts/_env_guard.sh
source "${COMPOSE_DIR}/scripts/_env_guard.sh" "${COMPOSE_DIR}"

# The ``backup`` service bind-mounts ./backups to /backups. Feeding the archive
# through that mount rather than through the container's STDIN avoids two real
# failure modes: ``docker compose run`` allocating a TTY (which makes a
# redirected stdin unusable), and pg_restore needing to seek within a
# custom-format archive. A dump from elsewhere is copied in and removed after.
mkdir -p ./backups
CLEANUP_COPY=""
case "${DUMP_ABS}" in
    "${COMPOSE_DIR}/backups/"*)
        IN_CONTAINER="/backups/$(basename "${DUMP_ABS}")"
        ;;
    *)
        CLEANUP_COPY="${COMPOSE_DIR}/backups/.restore-$$-$(basename "${DUMP_ABS}")"
        cp "${DUMP_ABS}" "${CLEANUP_COPY}"
        IN_CONTAINER="/backups/$(basename "${CLEANUP_COPY}")"
        ;;
esac
if [[ -n "${CLEANUP_COPY}" ]]; then
    # shellcheck disable=SC2064  # expand CLEANUP_COPY now, on purpose
    trap "rm -f '${CLEANUP_COPY}'" EXIT
fi

echo "==> restoring ${DUMP_ABS} into the live database"
# PGPASSWORD, not POSTGRES_PASSWORD: pg_restore is a libpq CLIENT. env_file
# supplies POSTGRES_PASSWORD (what the postgres SERVER image reads), so without
# this export the restore dies with "fe_sendauth: no password supplied" — the
# exact defect that made `make backup` unusable until #199. `make restore` had
# the same bug and it had never been executed.
#
# --clean --if-exists: drop and recreate the objects present in the dump; rows
# outside the dump are untouched.
#
# Invoked exactly like the compose ``backup`` service's own command (sh -c
# through the postgres image's entrypoint, which execs any non-postgres argv),
# so the two stay symmetric.
docker compose --profile backup run --rm backup sh -c \
    "export PGPASSWORD=\"\$POSTGRES_PASSWORD\"; exec pg_restore --clean --if-exists --no-owner --no-privileges -h db -U citevyn -d citevyn '${IN_CONTAINER}'"

echo "==> restore complete"
echo "    NOTE: the database is now at the dump's schema AND its alembic stamp."
echo "          Deploy code from the matching release before starting the api."
