#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# _drill_lib.sh — the DATA-RECOVERY drill and its crash-safety machinery.
# Sourced by deploy_verify.sh. Sourceable on its own so it can be tested.
#
# WHY THIS IS A SEPARATE FILE
#
# This is the only code in the repo that deliberately STOPS PRODUCTION. It is
# also, therefore, the code most worth testing — and a function buried in the
# middle of a 700-line top-to-bottom script cannot be tested without running the
# whole gate against a live stack. Splitting it out follows the pattern already
# used by _env_guard.sh and _migration_gen.sh: a sourceable helper with no side
# effects at source time, so tests/shell/ can stub `docker`, backup.sh and
# restore.sh on PATH and assert on real behaviour instead of grepping the source.
#
# THE CONTRACT — every exit path leaves the writers RUNNING
#
# An earlier version of this drill ran
#     docker compose --profile prod stop api worker
#     restore.sh "${dump}" || return 1
# and on any downstream failure returned with production DOWN and nothing to
# restart it. deploy_verify.sh runs with `set -uo pipefail` and NO `-e`, so
# execution continued to the summary, which only mentioned production state when
# ENDED_ON != VERSION — and ENDED_ON is only advanced on success. Net effect: a
# "drill FAIL" line, a stopped API, and nothing telling the operator that the
# rehearsal had caused the outage it was rehearsing for.
#
# So: STACK_STOPPED is raised BEFORE the stop (the stop is not atomic — it can
# take the api down and still fail), restart_writers is idempotent, and an EXIT
# + INT/TERM trap calls it on every path including Ctrl-C and die().
#
# Callers must provide: COMPOSE_DIR.
# ────────────────────────────────────────────────────────────────────────────

# 1 while production is intentionally stopped by the drill. The trap reads it.
STACK_STOPPED="${STACK_STOPPED:-0}"

restart_writers() {  # idempotent; a no-op when nothing was stopped
    [[ "${STACK_STOPPED}" == "1" ]] || return 0
    echo "==> restarting the writers stopped by the data-recovery drill" >&2
    # Only the api comes back: the worker is a one-shot ingest job, not a
    # service (same reasoning as refresh.sh).
    if ( cd "${COMPOSE_DIR}" && docker compose --profile prod up -d --no-deps api ); then
        STACK_STOPPED=0
        return 0
    fi
    echo "    FAILED to restart the api — PRODUCTION IS DOWN. Recover with:" >&2
    echo "      cd ${COMPOSE_DIR} && docker compose --profile prod up -d api" >&2
    return 1
}

# Install the safety net. Called by deploy_verify.sh; tests call it too so they
# exercise the same trap the real run uses rather than a lookalike.
install_drill_traps() {
    trap 'restart_writers || true' EXIT
    trap 'echo "==> interrupted" >&2; restart_writers || true; exit 130' INT TERM
}

wait_api_healthy() {  # poll the api CONTAINER's health (max 60s)
    # Container health, not http://localhost/health: the :80 site 301s to HTTPS
    # and a bare curl would read the redirect as success from a dead api. Same
    # reasoning as refresh.sh.
    local _ state=""
    for _ in $(seq 1 "${DRILL_HEALTH_TRIES:-30}"); do
        state="$(docker inspect --format '{{.State.Health.Status}}' citevyn-api 2>/dev/null || true)"
        [[ "${state}" == "healthy" ]] && return 0
        sleep "${DRILL_HEALTH_SLEEP:-2}"
    done
    echo "    api did not become healthy within 60s (state=${state:-unknown})" >&2
    return 1
}

# ── Drill A: the DATA-recovery rollback (RUNBOOK §4.2) ─────────────────────
# Dump the live database, stop the writers, pg_restore that dump, bring the api
# back, and let the caller re-run the probe suite. This exercises every moving
# part of the recovery the runbook prescribes when a rollback crosses a
# migration boundary: backup.sh, the ./backups mount, pg_restore's credentials,
# and the stack's ability to serve after its database has been dropped and
# rebuilt underneath it.
#
# It takes a FRESH dump rather than reusing the pre-deploy one, deliberately.
# The pre-deploy dump predates this deploy, so if VERSION shipped a migration
# that dump is a generation BEHIND the running code; restoring it would not
# rehearse a rollback, it would prove that a mismatched restore breaks the app.
# Matching generations is what makes this a rehearsal instead of a corruption
# test — which is why the freshness check below is a hard failure, not a warning.
data_restore_drill() {
    local dump marker
    echo "  -- drill A: data-recovery (RUNBOOK §4.2) --"

    # Freshness marker. `ls -t | head -1` alone would silently hand us the
    # PRE-DEPLOY dump if backup.sh exited 0 without writing a new file — and
    # "no dumps at all" can never catch that, because the pre-deploy step
    # guarantees one is already sitting in the same directory.
    marker="${COMPOSE_DIR}/backups/.drill-marker-$$"
    mkdir -p "${COMPOSE_DIR}/backups"
    : > "${marker}"

    if ! "${COMPOSE_DIR}/scripts/backup.sh" >/dev/null; then
        rm -f "${marker}"
        echo "    backup.sh failed; nothing to restore" >&2
        return 1
    fi
    dump="$(ls -t "${COMPOSE_DIR}"/backups/citevyn-*.dump 2>/dev/null | head -1)"
    if [[ -z "${dump}" ]]; then
        rm -f "${marker}"
        echo "    backup.sh reported success but produced no dump" >&2
        return 1
    fi
    if [[ ! "${dump}" -nt "${marker}" ]]; then
        rm -f "${marker}"
        echo "    backup.sh exited 0 but wrote no NEW dump — refusing to restore" >&2
        echo "    $(basename "${dump}") predates this drill, so it may be the" >&2
        echo "    pre-deploy dump, which is a schema generation behind the running" >&2
        echo "    code. Restoring it would corrupt production, not rehearse a" >&2
        echo "    recovery." >&2
        return 1
    fi
    rm -f "${marker}"

    echo "    restoring $(basename "${dump}")"
    # From here until restart_writers succeeds, production is stopped and the
    # trap owns bringing it back. Raised BEFORE the stop because `stop` is not
    # atomic: it can take the api down and still exit non-zero.
    STACK_STOPPED=1
    ( cd "${COMPOSE_DIR}" && docker compose --profile prod stop api worker ) || return 1
    "${COMPOSE_DIR}/scripts/restore.sh" "${dump}" || return 1
    restart_writers || return 1
    wait_api_healthy
}
