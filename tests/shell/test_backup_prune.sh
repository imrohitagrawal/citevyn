#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_backup_prune.sh — backup.sh deletes dumps 30 days old or older, and only
# after it wrote a new one (#546).
#
# The privacy page promises every backup copy of a deleted account is gone
# within 60 days, so pruning is part of the backup itself, not a separate cron
# line a human has to remember. The dangerous direction is pruning after a
# FAILED dump: then the old dumps are the only copies left.
#
# `docker` is stubbed on PATH and backup.sh runs from a fake compose dir, so
# this runs anywhere: no daemon, no database.
#
# Which change turns each case red:
#   ok     — the prune line removed, or its age changed from +29 (the
#            30-day file catches +30, the 29-day file catches +28)
#   others — the name filter (citevyn-*.dump) or -maxdepth 1 dropped
#   fail / stale / empty — the "new non-empty dump" check removed or moved
#                          after the prune
# ────────────────────────────────────────────────────────────────────────────
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
SCRIPTS="${REPO_ROOT}/infra/docker/scripts"

FAILURES=0
pass() { echo "  ok   — $1"; }
fail() { echo "  FAIL — $1"; FAILURES=$((FAILURES + 1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

echo "test_backup_prune.sh"

age() {  # age <days> <file>: set the file's modification time <days> days back
    perl -e '$t = time - $ARGV[0] * 86400; utime $t, $t, $ARGV[1] or die' "$1" "$2"
}

# run_backup <docker behaviour>: ok | fail | stale | empty. Sets DIR and RC.
run_backup() {
    local behaviour="$1"
    DIR="${WORK}/case-${behaviour}"
    mkdir -p "${DIR}/scripts" "${DIR}/backups" "${DIR}/bin" "${DIR}/backups/sub"
    cp "${SCRIPTS}/backup.sh" "${SCRIPTS}/_env_guard.sh" "${DIR}/scripts/"
    cat > "${DIR}/.env" <<'ENV'
POSTGRES_PASSWORD=0123456789abcdef0123456789abcdef
CITEVYN_ADMIN_API_KEY=fedcba9876543210fedcba9876543210
CITEVYN_ACME_EMAIL=ops@example.com
CITEVYN_PUBLIC_HOST=citevyn.example.com
CITEVYN_DATABASE_URL=postgresql+psycopg://citevyn:s3cret@db:5432/citevyn
CITEVYN_LLM_PROVIDER=gemini
CITEVYN_PUBLIC_CLIENT_TOKEN=fixture-client-token-not-a-real-secret
ENV

    echo old > "${DIR}/backups/citevyn-20260901T030000Z.dump"; age 31 "${DIR}/backups/citevyn-20260901T030000Z.dump"
    echo edge > "${DIR}/backups/citevyn-20260929T030000Z.dump"; age 29 "${DIR}/backups/citevyn-20260929T030000Z.dump"
    echo edge > "${DIR}/backups/citevyn-20260928T030000Z.dump"; age 30.01 "${DIR}/backups/citevyn-20260928T030000Z.dump"
    echo keep > "${DIR}/backups/notes.txt"; age 40 "${DIR}/backups/notes.txt"
    echo keep > "${DIR}/backups/citevyn-20260801T030000Z.sql"; age 40 "${DIR}/backups/citevyn-20260801T030000Z.sql"
    echo keep > "${DIR}/backups/sub/citevyn-20260801T030000Z.dump"; age 40 "${DIR}/backups/sub/citevyn-20260801T030000Z.dump"

    local write=''
    case "${behaviour}" in
        ok)    write='echo dump > "${PWD}/backups/citevyn-20261028T030000Z.dump"; exit 0' ;;
        fail)  write='exit 1' ;;
        stale) write='exit 0' ;;
        empty) write=': > "${PWD}/backups/citevyn-20261028T030000Z.dump"; exit 0' ;;
    esac
    printf '#!/usr/bin/env bash\n%s\n' "${write}" > "${DIR}/bin/docker"
    chmod +x "${DIR}/bin/docker"

    PATH="${DIR}/bin:${PATH}" bash "${DIR}/scripts/backup.sh" > "${DIR}/out.txt" 2>&1
    RC=$?
}

present() { [[ -f "${DIR}/backups/$1" ]]; }

# ── A good dump prunes ─────────────────────────────────────────────────────
run_backup ok
[[ ${RC} -eq 0 ]] && pass "ok: exits 0" || { fail "ok: exit ${RC}"; cat "${DIR}/out.txt"; }
present citevyn-20261028T030000Z.dump && pass "ok: the new dump is kept" || fail "ok: new dump missing"
! present citevyn-20260901T030000Z.dump && pass "ok: a 31-day-old dump is deleted" || fail "ok: 31-day-old dump kept"
! present citevyn-20260928T030000Z.dump && pass "ok: a 30-day-old dump is deleted" || fail "ok: 30-day-old dump kept"
present citevyn-20260929T030000Z.dump && pass "ok: a 29-day-old dump is kept" || fail "ok: 29-day-old dump deleted"
present notes.txt && present citevyn-20260801T030000Z.sql && pass "ok: other files are never touched" || fail "ok: a non-dump file was deleted"
present sub/citevyn-20260801T030000Z.dump && pass "ok: subdirectories are not searched" || fail "ok: a dump in a subdirectory was deleted"

# ── A failed dump never prunes ─────────────────────────────────────────────
for behaviour in fail stale empty; do
    run_backup "${behaviour}"
    [[ ${RC} -ne 0 ]] && pass "${behaviour}: exits non-zero" || fail "${behaviour}: exited 0"
    present citevyn-20260901T030000Z.dump && pass "${behaviour}: old dumps are kept" || fail "${behaviour}: pruned after a failed dump"
done

if [[ ${FAILURES} -ne 0 ]]; then
    echo "${FAILURES} failure(s)"
    exit 1
fi
echo "all passed"
