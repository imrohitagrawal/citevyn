#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_drill_crash_safety.sh — the data-recovery drill STOPS PRODUCTION, so the
# property that matters is not "does it restore" but "does it ever walk away
# with the writers still down".
#
# This is a regression suite for a real defect (#195): the drill ran
#     docker compose --profile prod stop api worker
#     restore.sh "${dump}" || return 1
# and on any downstream failure returned with production DOWN, nothing to
# restart it, and — because deploy_verify.sh runs `set -uo pipefail` with no
# `-e` — no abort either. The run continued to a summary that mentioned
# production state only when ENDED_ON != VERSION, which that path never set. The
# operator got "drill FAIL" and a stopped API with nothing connecting the two.
#
# Every case below therefore asserts on the ORDERED LOG of docker invocations,
# not on exit codes alone: a drill that fails is fine, a drill that fails with
# the api still stopped is not.
#
# `docker`, `backup.sh` and `restore.sh` are stubbed on PATH / in a fake compose
# dir, so this runs anywhere — no daemon, no database, no live stack.
# ────────────────────────────────────────────────────────────────────────────
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
LIB="${REPO_ROOT}/infra/docker/scripts/_drill_lib.sh"

FAILURES=0
pass() { echo "  ok   — $1"; }
fail() { echo "  FAIL — $1"; FAILURES=$((FAILURES + 1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

echo "test_drill_crash_safety.sh"

[[ -f "${LIB}" ]] || { echo "  FAIL — ${LIB} not found"; exit 1; }

# ── Harness ────────────────────────────────────────────────────────────────
# Builds a fake COMPOSE_DIR + a `docker` stub that appends every invocation to
# ${DOCKER_LOG}, then runs data_restore_drill in a SUBSHELL (so a trap firing on
# exit is a real trap firing on a real exit, exactly as in deploy_verify.sh).
#
#   $1 backup behaviour : ok | fail | stale   (stale = exit 0, write no new dump)
#   $2 restore behaviour: ok | fail
#   $3 up behaviour     : ok | fail
#   $4 health behaviour : ok | fail
#   $5 extra            : ''  | die-after-stop  (simulates an unrelated abort)
#   $6 stop behaviour   : ok | fail            (default ok)
# Sets: RC, OUT, DOCKER_LOG contents in LOG
run_drill() {
    local backup="$1" restore="$2" up="$3" health="$4" extra="${5:-}" stop="${6:-ok}"
    local dir="${WORK}/case-$$-${RANDOM}"
    mkdir -p "${dir}/scripts" "${dir}/backups" "${dir}/bin"

    # A pre-existing dump, older than any marker the drill writes. Its presence
    # is what makes the `stale` case dangerous: `ls -t | head -1` finds it, so
    # "no dumps at all" can never fire.
    : > "${dir}/backups/citevyn-00000000T000000Z.dump"
    touch -t 200001010000 "${dir}/backups/citevyn-00000000T000000Z.dump"

    case "${backup}" in
        ok)    printf '#!/usr/bin/env bash\nsleep 1\n: > "%s/backups/citevyn-fresh.dump"\nexit 0\n' "${dir}" > "${dir}/scripts/backup.sh" ;;
        fail)  printf '#!/usr/bin/env bash\nexit 1\n' > "${dir}/scripts/backup.sh" ;;
        stale) printf '#!/usr/bin/env bash\nexit 0\n' > "${dir}/scripts/backup.sh" ;;
    esac
    case "${restore}" in
        ok)   printf '#!/usr/bin/env bash\nexit 0\n' > "${dir}/scripts/restore.sh" ;;
        fail) printf '#!/usr/bin/env bash\nexit 1\n' > "${dir}/scripts/restore.sh" ;;
    esac
    chmod +x "${dir}/scripts/backup.sh" "${dir}/scripts/restore.sh"

    DOCKER_LOG="${dir}/docker.log"
    # The stub records the argv of every call, then fakes the outcome the case
    # asks for. `inspect` drives wait_api_healthy; `up` drives restart_writers.
    cat > "${dir}/bin/docker" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "${DOCKER_LOG}"
case "\$*" in
    *inspect*)          [[ "${health}" == "ok" ]] && { echo healthy; exit 0; }; echo unhealthy; exit 0 ;;
    *"up -d"*)          [[ "${up}"   == "ok" ]] || exit 1; exit 0 ;;
    *"stop api worker"*) [[ "${stop}" == "ok" ]] || exit 1; exit 0 ;;
esac
exit 0
EOF
    chmod +x "${dir}/bin/docker"

    # The driver runs as its OWN process, not a subshell of this one, so an EXIT
    # trap firing is a real trap on a real process exit — the same thing that
    # happens in deploy_verify.sh. A `$( ... )` subshell would be close enough to
    # look right and different enough to prove less.
    cat > "${dir}/driver.sh" <<'DRIVER'
#!/usr/bin/env bash
set -uo pipefail
# shellcheck disable=SC1090
source "${LIB}"
install_drill_traps
if [[ "${EXTRA}" == "die-after-stop" ]]; then
    # Abort BETWEEN the stop and the restart — an unrelated die(), or the
    # operator hitting Ctrl-C. Only the EXIT trap can save production here.
    STACK_STOPPED=1
    ( cd "${COMPOSE_DIR}" && docker compose --profile prod stop api worker ) >/dev/null 2>&1
    exit 7
fi
data_restore_drill
DRIVER
    chmod +x "${dir}/driver.sh"

    OUT="$(
        PATH="${dir}/bin:${PATH}" \
        COMPOSE_DIR="${dir}" \
        LIB="${LIB}" \
        EXTRA="${extra}" \
        DRILL_HEALTH_TRIES=2 \
        DRILL_HEALTH_SLEEP=0 \
        bash "${dir}/driver.sh" 2>&1
    )"
    RC=$?
    LOG="$(cat "${DOCKER_LOG}" 2>/dev/null || true)"
}

# Did the writers get brought back after being stopped? Order matters: an `up`
# logged BEFORE the stop would not be a recovery.
restarted_after_stop() {
    local stop_line up_line
    stop_line="$(printf '%s\n' "${LOG}" | grep -n -- 'stop api worker' | head -1 | cut -d: -f1)"
    up_line="$(printf '%s\n' "${LOG}" | grep -n -- 'up -d' | tail -1 | cut -d: -f1)"
    [[ -n "${stop_line}" && -n "${up_line}" && "${up_line}" -gt "${stop_line}" ]]
}
stopped_anything() { printf '%s\n' "${LOG}" | grep -q -- 'stop api worker'; }

# ── 1. Happy path ──────────────────────────────────────────────────────────
run_drill ok ok ok ok
if [[ "${RC}" -ne 0 ]]; then
    fail "happy path: drill returned ${RC}; output: ${OUT}"
elif ! restarted_after_stop; then
    fail "happy path: writers were not restarted after the stop; log: ${LOG}"
else
    pass "happy path: stops the writers, restores, brings the api back"
fi

# ── 2. THE regression. restore fails AFTER the stop. ───────────────────────
#    This is the exact shape that shipped: `restore.sh ... || return 1`.
run_drill ok fail ok ok
if [[ "${RC}" -eq 0 ]]; then
    fail "failed restore reported success"
elif ! restarted_after_stop; then
    fail "REGRESSION #195: restore failed and production was left STOPPED; log: ${LOG}"
else
    pass "a failed restore still restarts the writers (#195 regression)"
fi

# ── 3. The restart itself fails — production really is down, so SAY SO. ────
run_drill ok ok fail ok
if [[ "${RC}" -eq 0 ]]; then
    fail "drill reported success although the api could not be restarted"
elif ! grep -q "PRODUCTION IS DOWN" <<<"${OUT}"; then
    fail "api could not be restarted and the operator was not told; output: ${OUT}"
elif ! grep -q "docker compose --profile prod up -d api" <<<"${OUT}"; then
    fail "no recovery command was printed; output: ${OUT}"
else
    pass "an unrecoverable restart says PRODUCTION IS DOWN and prints the fix"
fi

# ── 4. The api comes back but never turns healthy. ─────────────────────────
run_drill ok ok ok fail
if [[ "${RC}" -eq 0 ]]; then
    fail "drill passed with an api that never became healthy"
elif ! restarted_after_stop; then
    fail "unhealthy api: writers not restarted; log: ${LOG}"
else
    pass "an api that never becomes healthy fails the drill, writers restarted"
fi

# ── 5. backup fails -> production is never touched at all. ─────────────────
run_drill fail ok ok ok
if [[ "${RC}" -eq 0 ]]; then
    fail "drill passed although backup.sh failed"
elif stopped_anything; then
    fail "backup failed but the drill stopped production anyway; log: ${LOG}"
else
    pass "a failed backup stops nothing (no dump = no restore = no outage)"
fi

# ── 6. Freshness. backup exits 0 but writes no NEW dump. ──────────────────
#    Without the marker check, `ls -t | head -1` returns the PRE-DEPLOY dump —
#    a schema generation behind the running code — and the drill restores it,
#    corrupting production instead of rehearsing a recovery.
run_drill stale ok ok ok
if [[ "${RC}" -eq 0 ]]; then
    fail "drill restored a stale dump and reported success"
elif stopped_anything; then
    fail "stale dump was detected only AFTER stopping production; log: ${LOG}"
elif ! grep -q "no NEW dump" <<<"${OUT}"; then
    fail "stale dump rejected for the wrong reason; output: ${OUT}"
else
    pass "a stale dump is refused BEFORE production is stopped"
fi

# ── 7. An abort between the stop and the restart. ─────────────────────────
#    Nothing inside data_restore_drill runs here — only the EXIT trap can
#    bring production back. This is the Ctrl-C / die() case.
run_drill ok ok ok ok die-after-stop
if ! stopped_anything; then
    fail "harness error: the die-after-stop case never stopped anything"
elif ! restarted_after_stop; then
    fail "an abort between stop and restart left production DOWN; log: ${LOG}"
else
    pass "the EXIT trap restarts the writers after an unrelated abort"
fi

# ── 8. `docker compose stop` ITSELF fails. ────────────────────────────────
#    `stop` is not atomic: it can take the api down and still exit non-zero
#    (one container stops, the other times out). So the drill must consider
#    production stopped from the moment it ASKS, not from the moment the ask
#    succeeds. This case exists because raising STACK_STOPPED *after* the stop
#    instead of before it was a mutation that survived every other case here.
run_drill ok ok ok ok '' fail
if [[ "${RC}" -eq 0 ]]; then
    fail "drill passed although `stop` failed"
elif ! restarted_after_stop; then
    fail "a failed stop may still have downed the api, and nothing restarted it; log: ${LOG}"
else
    pass "a FAILED stop still triggers the restart (stop is not atomic)"
fi

if [[ "${FAILURES}" -eq 0 ]]; then
    echo "all passed"
    exit 0
fi
echo "${FAILURES} failure(s)"
exit 1
