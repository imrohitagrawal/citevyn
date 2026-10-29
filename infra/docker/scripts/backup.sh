#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# backup.sh — on-demand Postgres dump to ./backups/.
#
# Wraps the one-shot ``backup`` profile container. Runs against the
# live database; the ``db`` service must be healthy first.
#
# Usage:
#   ./scripts/backup.sh
#
# The output file lands in ./backups/citevyn-<UTC timestamp>.dump.
#
# Then it deletes every citevyn-*.dump in ./backups/ that is 30 days old or
# older (owner decision on #546: the privacy page promises every backup copy of
# a deleted account is gone within 60 days, RUNBOOK §4.1). It prunes ONLY after
# this run wrote a new, non-empty dump: if the dump failed, the old ones are
# the only copies and nothing is deleted.
#
# Restore with:
#   pg_restore --clean --if-exists --no-owner --no-privileges \
#       -h <host> -U citevyn -d citevyn ./backups/citevyn-<ts>.dump
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${COMPOSE_DIR}"

if [[ ! -f .env ]]; then
    echo "error: .env not found at ${COMPOSE_DIR}/.env" >&2
    exit 1
fi
# Refuse to back up with the dev-only stub that ``make demo``
# auto-generates; share the guard with deploy.sh / refresh.sh.
# shellcheck source=infra/docker/scripts/_env_guard.sh
source "${COMPOSE_DIR}/scripts/_env_guard.sh" "${COMPOSE_DIR}"

mkdir -p ./backups

# The dumps already here, so this run's new one can be told apart by NAME
# (comparing timestamps would depend on the filesystem's clock resolution).
before="$(find ./backups -maxdepth 1 -type f -name 'citevyn-*.dump' | sort)"

# The profile ``backup`` service is configured to dump and exit;
# ``docker compose run`` waits for the container to finish, so the
# script blocks until the dump is on disk.
docker compose --profile backup run --rm backup

# ``docker compose run`` exiting 0 is not proof a dump was written (a container
# that exits 0 without writing the file would still pass): look for the file.
new_dump="$(find ./backups -maxdepth 1 -type f -name 'citevyn-*.dump' -size +0 | sort \
    | comm -13 <(printf '%s\n' "${before}") - | head -n 1)"
if [[ -z "${new_dump}" ]]; then
    echo "error: no new non-empty dump in ${COMPOSE_DIR}/backups/; old dumps kept" >&2
    exit 1
fi
echo "==> wrote ${new_dump}"

# -mtime +N matches files more than N whole days old, so +29 is "30 days or
# older". Only our own dump names, only this directory.
echo "==> deleting dumps 30 days old or older:"
find ./backups -maxdepth 1 -type f -name 'citevyn-*.dump' -mtime +29 -print -delete

echo
echo "==> dumps in ${COMPOSE_DIR}/backups/:"
ls -lh ./backups/ | tail -n +2