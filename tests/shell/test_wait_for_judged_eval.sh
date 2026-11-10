#!/usr/bin/env bash
# Suite for scripts/wait_for_judged_eval.sh: the wait that stops a release tag
# publishing images until the judged answer-quality eval has passed on that commit.
#
# Run from anywhere:  bash tests/shell/test_wait_for_judged_eval.sh
#
# No network, no real waiting. `gh`, `date` and `sleep` are fakes on PATH: `sleep`
# advances the clock `date` reads, so a 75-minute timeout takes milliseconds and
# the deadlines are measured rather than assumed. `jq` is the real one.
#
# Every case asserts an exact exit code and a message. Asserting only "non-zero"
# would pass on the 127 a missing script gives.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/wait_for_judged_eval.sh"
PASS=0
FAIL=0

ok()  { PASS=$((PASS + 1)); echo "  [PASS] $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  [FAIL] $1" >&2; }

JOB='judged answer-quality eval (needs the OpenRouter and Gemini key secrets)'
EVAL_STEP='Judged eval — MIN_MEAN_JUDGE + groundedness + injection gates (--postgres)'
SHA_UNDER_TEST='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
OTHER_SHA='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
TOKEN='ghs_FAKE_TOKEN_VALUE_never_print_me'
CLOCK_START=1000

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
FAKE_BIN="${WORK}/bin"
FAKE_DIR="${WORK}/state"
mkdir -p "${FAKE_BIN}" "${FAKE_DIR}"

cat >"${FAKE_BIN}/gh" <<'FAKE'
#!/usr/bin/env bash
# Canned `gh api` answers: <key>.<n> is the n-th answer for that endpoint, and the
# highest <n> present repeats. <key>.<n>.fail holds "<exit code>" with the stderr
# text in <key>.<n>.stderr.
echo "$*" >>"${FAKE_DIR}/gh.log"
if [ -n "${GH_TOKEN:-}" ]; then : >"${FAKE_DIR}/token_seen"; fi
[ "${1:-}" = api ] || { echo "fake gh: not an api call" >&2; exit 98; }
path=""
for arg in "$@"; do case "${arg}" in repos/*) path="${arg}" ;; esac; done
case "${path}" in
    */actions/workflows/ci.yml/runs\?*) key=runs ;;
    */actions/runs/*/jobs\?*) id="${path%%/jobs*}"; key="jobs-${id##*/}" ;;
    *) echo "fake gh: unexpected path ${path} (HTTP 404)" >&2; exit 97 ;;
esac
counter="${FAKE_DIR}/${key}.count"
count=$(($(cat "${counter}" 2>/dev/null || echo 0) + 1))
echo "${count}" >"${counter}"
n="${count}"
while [ "${n}" -gt 1 ] && [ ! -f "${FAKE_DIR}/${key}.${n}" ] && [ ! -f "${FAKE_DIR}/${key}.${n}.fail" ]; do
    n=$((n - 1))
done
if [ -f "${FAKE_DIR}/${key}.${n}.fail" ]; then
    cat "${FAKE_DIR}/${key}.${n}.stderr" >&2
    exit "$(cat "${FAKE_DIR}/${key}.${n}.fail")"
fi
[ -f "${FAKE_DIR}/${key}.${n}" ] || { echo "fake gh: no answer for ${key}" >&2; exit 96; }
cat "${FAKE_DIR}/${key}.${n}"
FAKE

cat >"${FAKE_BIN}/date" <<'FAKE'
#!/usr/bin/env bash
[ "${1:-}" = "+%s" ] || { echo "fake date: unexpected $*" >&2; exit 99; }
cat "${FAKE_DIR}/clock"
FAKE

cat >"${FAKE_BIN}/sleep" <<'FAKE'
#!/usr/bin/env bash
echo "$1" >>"${FAKE_DIR}/sleeps.log"
echo $(($(cat "${FAKE_DIR}/clock") + $1)) >"${FAKE_DIR}/clock"
FAKE
chmod +x "${FAKE_BIN}/gh" "${FAKE_BIN}/date" "${FAKE_BIN}/sleep"

new_case() {
    rm -rf "${FAKE_DIR}"
    mkdir -p "${FAKE_DIR}"
    echo "${CLOCK_START}" >"${FAKE_DIR}/clock"
}

run_obj() {  # run_obj <id> [sha] [event]
    printf '{"id":%s,"event":"%s","head_sha":"%s","html_url":"https://example.test/runs/%s"}' \
        "$1" "${3:-push}" "${2:-${SHA_UNDER_TEST}}" "$1"
}
runs_json() {  # runs_json <run-object>...
    local joined
    joined="$(IFS=,; echo "$*")"
    printf '{"total_count":%s,"workflow_runs":[%s]}' "$#" "${joined}"
}
step_obj() {  # step_obj <name> <conclusion|null>
    local conclusion='null'
    [ "$2" = null ] || conclusion="\"$2\""
    printf '{"name":"%s","status":"completed","conclusion":%s}' "$1" "${conclusion}"
}
job_obj() {  # job_obj <id> <name> <status> <conclusion|null> [steps as a JSON array]
    # A job marked success gets a passing eval step unless the case says otherwise.
    local conclusion='null' steps='[]'
    [ "$4" = null ] || conclusion="\"$4\""
    if [ "$4" = success ]; then steps="[$(step_obj "${EVAL_STEP}" success)]"; fi
    if [ -n "${5:-}" ]; then steps="$5"; fi
    printf '{"id":%s,"name":"%s","status":"%s","conclusion":%s,"steps":%s}' \
        "$1" "$2" "$3" "${conclusion}" "${steps}"
}
jobs_json() {  # jobs_json <job-object>...
    local joined
    joined="$(IFS=,; echo "$*")"
    printf '{"total_count":%s,"jobs":[%s]}' "$#" "${joined}"
}
put() { printf '%s' "$3" >"${FAKE_DIR}/$1.$2"; }  # put <key> <n> <body>
put_fail() {  # put_fail <key> <n> <exit> <stderr>
    echo "$3" >"${FAKE_DIR}/$1.$2.fail"
    printf '%s\n' "$4" >"${FAKE_DIR}/$1.$2.stderr"
}

# One run (id 201) holding the judged job in the given state.
one_job() {  # one_job <status> <conclusion|null>
    put runs 1 "$(runs_json "$(run_obj 201)")"
    put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" "$1" "$2")")"
}

OUT=""
RC=0
run_bare() {  # run_bare [VAR=value ...]: only the named settings are set
    OUT="$(env -u REPO -u SHA -u POLL_SECONDS -u START_GRACE_SECONDS -u TOTAL_TIMEOUT_SECONDS \
        PATH="${FAKE_BIN}:${PATH}" FAKE_DIR="${FAKE_DIR}" GH_TOKEN="${TOKEN}" \
        "$@" bash "${SCRIPT}" 2>&1)"
    RC=$?
}
run_gate() {  # run_gate [VAR=value ...]: a later assignment overrides the base one
    run_bare REPO=owner/repo SHA="${SHA_UNDER_TEST}" \
        POLL_SECONDS=30 START_GRACE_SECONDS=900 TOTAL_TIMEOUT_SECONDS=4500 "$@"
}
sleeps()  { if [ -f "${FAKE_DIR}/sleeps.log" ]; then wc -l <"${FAKE_DIR}/sleeps.log" | tr -d ' '; else echo 0; fi; }
elapsed() { echo $(($(cat "${FAKE_DIR}/clock") - CLOCK_START)); }
gh_calls() { if [ -f "${FAKE_DIR}/gh.log" ]; then wc -l <"${FAKE_DIR}/gh.log" | tr -d ' '; else echo 0; fi; }

check() {  # check <name> <expected rc> [substring that must be in the output]...
    local name="$1" want="$2" needle
    shift 2
    if [ "${RC}" -ne "${want}" ]; then
        bad "${name}: expected exit ${want}, got ${RC}: ${OUT}"
        return
    fi
    for needle in "$@"; do
        if ! grep -qF -- "${needle}" <<<"${OUT}"; then
            bad "${name}: output lacks '${needle}': ${OUT}"
            return
        fi
    done
    ok "${name}"
}
check_eq() {  # check_eq <name> <actual> <expected>
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: got '$2', expected '$3'"; fi
}

echo "==> wait_for_judged_eval.sh"

# --- A finished job ---------------------------------------------------------

# Red when: the success path is removed, or the job is matched on anything but
# its exact name.
new_case; one_job completed success; run_gate
check "success passes and names the run" 0 "https://example.test/runs/201"
# Red when: "success" is reached without reading the job. This is the partner that
# stops a no-job path passing as a success: a pass needs the jobs call to have run.
check_eq "partner: a pass read the job of the run it reports" \
    "$(grep -c 'actions/runs/201/jobs' "${FAKE_DIR}/gh.log" 2>/dev/null)" 1
check_eq "partner: a finished job needs no waiting" "$(sleeps)" 0

# Red when: any of these conclusions joins the allowlist, or the script stops
# naming the conclusion and the run.
for conclusion in failure cancelled timed_out action_required neutral startup_failure stale a_value_nobody_has_seen; do
    new_case; one_job completed "${conclusion}"; run_gate
    check "conclusion ${conclusion} fails" 1 "${conclusion}" "https://example.test/runs/201"
done

# Red when: a completed job with no conclusion is read as anything but a failure.
new_case; one_job completed null; run_gate
check "completed with no conclusion fails" 1 "none" "https://example.test/runs/201"

# --- Waiting ----------------------------------------------------------------

# Red when: a running job is decided on instead of waited for.
for status in in_progress queued waiting; do
    new_case
    put runs 1 "$(runs_json "$(run_obj 201)")"
    put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" "${status}" null)")"
    put jobs-201 2 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
    run_gate
    check "${status} then success passes" 0 "https://example.test/runs/201"
    check_eq "${status} waits one poll" "$(sleeps)" 1
done

# Red when: a job that was running is judged by its earlier state.
new_case
put runs 1 "$(runs_json "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" in_progress null)")"
put jobs-201 2 "$(jobs_json "$(job_obj 501 "${JOB}" completed failure)")"
run_gate
check "in_progress then failure fails" 1 "failure"

# Red when: the total timeout is dropped, or the grace period also ends a job that
# has already appeared.
new_case; one_job in_progress null; run_gate TOTAL_TIMEOUT_SECONDS=1000 START_GRACE_SECONDS=60
check "total timeout fails" 1 "timed out"
check_eq "total timeout is measured from the clock" "$(elapsed)" 1020

# Red when: the defaults change: 30 s poll and 75 minutes in total.
new_case; one_job in_progress null
run_bare REPO=owner/repo SHA="${SHA_UNDER_TEST}"
check "defaults: running forever times out" 1 "timed out"
check_eq "defaults: 4500 s in total" "$(elapsed)" 4500
check_eq "defaults: every poll is 30 s" "$(sort -u "${FAKE_DIR}/sleeps.log")" 30

# --- No job in the grace period ---------------------------------------------

# Red when: having no runs passes, or the failure stops saying how to fix it.
new_case; put runs 1 "$(runs_json)"; run_gate
check "no runs fails after the grace period" 1 "Run the ci workflow manually on this commit"
check_eq "no runs waits the whole grace period" "$(elapsed)" 900

# Red when: a skipped job counts as a result.
new_case; one_job completed skipped; run_gate
check "skipped only fails after the grace period" 1 "Run the ci workflow manually on this commit"
check_eq "skipped only waits the whole grace period" "$(elapsed)" 900

# Red when: the default grace period changes from 900 s.
new_case; one_job completed skipped
run_bare REPO=owner/repo SHA="${SHA_UNDER_TEST}"
check "defaults: grace expiry fails" 1 "Run the ci workflow manually on this commit"
check_eq "defaults: 900 s of grace" "$(elapsed)" 900

# Red when: the grace period ends a job that shows up just before it runs out.
new_case
put runs 1 "$(runs_json)"
put runs 2 "$(runs_json "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
run_gate START_GRACE_SECONDS=60
check "a job appearing inside the grace period passes" 0 "https://example.test/runs/201"

# Red when: the name comparison is loosened to a prefix, substring or any-case match.
new_case
put runs 1 "$(runs_json "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json \
    "$(job_obj 501 "judged answer-quality eval" completed success)" \
    "$(job_obj 502 "${JOB} (retry)" completed success)" \
    "$(job_obj 503 "x ${JOB}" completed success)" \
    "$(job_obj 504 "JUDGED ANSWER-QUALITY EVAL (NEEDS THE OPENROUTER AND GEMINI KEY SECRETS)" completed success)")"
run_gate
check "near-identical job names are ignored" 1 "Run the ci workflow manually on this commit"

# Red when: a run the API returns for a different commit is trusted.
new_case
put runs 1 "$(runs_json "$(run_obj 201 "${OTHER_SHA}")")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
run_gate
check "a run of another commit is ignored" 1 "Run the ci workflow manually on this commit"

# --- Which job decides ------------------------------------------------------

# Red when: the oldest job decides, or any success anywhere passes. The runs are
# listed in both orders, so array position cannot stand in for age.
two_runs() {  # two_runs <older conclusion> <newer conclusion> <list order: old-first|new-first>
    new_case
    if [ "$3" = old-first ]; then
        put runs 1 "$(runs_json "$(run_obj 201)" "$(run_obj 202)")"
    else
        put runs 1 "$(runs_json "$(run_obj 202)" "$(run_obj 201)")"
    fi
    put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed "$1")")"
    put jobs-202 1 "$(jobs_json "$(job_obj 502 "${JOB}" completed "$2")")"
}
two_runs success failure new-first; run_gate
check "older success, newer failure fails" 1 "https://example.test/runs/202"
two_runs success failure old-first; run_gate
check "older success, newer failure fails (other order)" 1 "https://example.test/runs/202"
two_runs failure success new-first; run_gate
check "older failure, newer success passes" 0 "https://example.test/runs/202"
two_runs failure success old-first; run_gate
check "older failure, newer success passes (other order)" 0 "https://example.test/runs/202"

# Red when: a newer skipped job hides the older real result, in either direction.
two_runs success skipped new-first; run_gate
check "newer skipped does not hide an older success" 0 "https://example.test/runs/201"
two_runs failure skipped new-first; run_gate
check "newer skipped does not hide an older failure" 1 "https://example.test/runs/201"

# Red when: a re-run of an OLDER run is judged by the run's id, not the job's. The
# rerun of run 201 made job 503, newer than run 202's job 502.
new_case
put runs 1 "$(runs_json "$(run_obj 202)" "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 503 "${JOB}" completed failure)")"
put jobs-202 1 "$(jobs_json "$(job_obj 502 "${JOB}" completed success)")"
run_gate
check "a re-run in an older run is the newest result" 1 "https://example.test/runs/201"

# Red when: a newer job still running is passed over for an older success.
new_case
put runs 1 "$(runs_json "$(run_obj 202)" "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
put jobs-202 1 "$(jobs_json "$(job_obj 502 "${JOB}" in_progress null)")"
put jobs-202 2 "$(jobs_json "$(job_obj 502 "${JOB}" completed failure)")"
run_gate
check "a newer running job is waited for" 1 "https://example.test/runs/202"

# --- The eval step inside a job marked success -------------------------------

job_with_steps() {  # job_with_steps <steps as a JSON array>
    put runs 1 "$(runs_json "$(run_obj 201)")"
    put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success "$1")")"
}

# Red when: the job's conclusion alone is trusted again. This is the job that stayed
# green while the guard step skipped everything for want of the secret.
new_case; job_with_steps "[$(step_obj "${EVAL_STEP}" skipped)]"; run_gate
check "job success with the eval step skipped fails" 1 "did not run" \
    "CITEVYN_OPENROUTER_API_KEY" "https://example.test/runs/201"

# Red when: a job with no eval step at all is read as having judged anything.
new_case; job_with_steps "[$(step_obj "Set up job" success)]"; run_gate
check "eval step absent fails" 1 "did not run" "absent" "https://example.test/runs/201"
new_case; job_with_steps '[]'; run_gate
check "no steps at all fails" 1 "did not run" "absent"
new_case
put runs 1 "$(runs_json "$(run_obj 201)")"
put jobs-201 1 "{\"jobs\":[{\"id\":501,\"name\":\"${JOB}\",\"status\":\"completed\",\"conclusion\":\"success\"}]}"
run_gate
check "a job with no steps field fails" 1 "did not run" "absent"

# Red when: the eval step's own result is ignored, or any value joins "success".
for result in failure cancelled neutral a_value_nobody_has_seen; do
    new_case; job_with_steps "[$(step_obj "${EVAL_STEP}" "${result}")]"; run_gate
    check "eval step ${result} inside a job marked success fails" 1 "did not run" "${result}"
done
new_case; job_with_steps "[$(step_obj "${EVAL_STEP}" null)]"; run_gate
check "eval step with no conclusion fails" 1 "did not run" "none"

# Red when: two steps of that name are settled by picking one.
new_case
job_with_steps "[$(step_obj "${EVAL_STEP}" success),$(step_obj "${EVAL_STEP}" success)]"
run_gate
check "two eval steps are ambiguous and fail" 1 "did not run" "duplicate"

# Red when: the step is matched on anything but its exact name.
new_case
job_with_steps "[$(step_obj "Judged eval" success),$(step_obj "${EVAL_STEP} (retry)" success)]"
run_gate
check "near-identical step names are ignored" 1 "did not run" "absent"

# Red when: the step check rejects the real thing, so nothing could ever publish.
# Partner of every case above: the same job, with the eval step passing, passes.
new_case
job_with_steps "[$(step_obj "Guard" success),$(step_obj "${EVAL_STEP}" success)]"
run_gate
check "job and eval step both success passes" 0 "https://example.test/runs/201"

# --- Which runs count ---------------------------------------------------------

# Red when: a pull_request run counts. It tests a merge ref, not this commit.
new_case
put runs 1 "$(runs_json "$(run_obj 201 "" pull_request)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
run_gate
check "a passing pull_request run alone does not pass" 1 "Run the ci workflow manually on this commit"

# Red when: a newer pull_request result hides the tag run, in either direction.
new_case
put runs 1 "$(runs_json "$(run_obj 202 "" pull_request)" "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed failure)")"
put jobs-202 1 "$(jobs_json "$(job_obj 502 "${JOB}" completed success)")"
run_gate
check "a newer passing pull_request run does not hide a failed tag run" 1 "https://example.test/runs/201"
new_case
put runs 1 "$(runs_json "$(run_obj 202 "" pull_request)" "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
put jobs-202 1 "$(jobs_json "$(job_obj 502 "${JOB}" completed failure)")"
run_gate
check "a newer failed pull_request run does not hide a passing tag run" 0 "https://example.test/runs/201"

# Red when: the event check becomes a denylist, so an unknown event counts.
for event in schedule workflow_run a_value_nobody_has_seen; do
    new_case
    put runs 1 "$(runs_json "$(run_obj 201 "" "${event}")")"
    put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
    run_gate
    check "event ${event} does not count" 1 "Run the ci workflow manually on this commit"
done

# Red when: a run with no event field counts.
new_case
put runs 1 "{\"workflow_runs\":[{\"id\":201,\"head_sha\":\"${SHA_UNDER_TEST}\",\"html_url\":\"https://example.test/runs/201\"}]}"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
run_gate
check "a run with no event does not count" 1 "Run the ci workflow manually on this commit"

# Red when: workflow_dispatch stops counting (it is the documented fix), or the
# older run decides.
new_case
put runs 1 "$(runs_json "$(run_obj 202 "" workflow_dispatch)" "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed failure)")"
put jobs-202 1 "$(jobs_json "$(job_obj 502 "${JOB}" completed success)")"
run_gate
check "a newer workflow_dispatch pass decides over an older push failure" 0 "https://example.test/runs/202"
new_case
put runs 1 "$(runs_json "$(run_obj 202 "" workflow_dispatch)" "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
put jobs-202 1 "$(jobs_json "$(job_obj 502 "${JOB}" completed failure)")"
run_gate
check "a newer workflow_dispatch failure decides over an older push pass" 1 "https://example.test/runs/202"

# --- The query --------------------------------------------------------------

# Red when: the commit filter, the ci.yml workflow, or the latest-attempt filter is
# dropped from what is asked of GitHub.
new_case; one_job completed success; run_gate
log="$(cat "${FAKE_DIR}/gh.log")"
if grep -qF "repos/owner/repo/actions/workflows/ci.yml/runs?" <<<"${log}" \
    && grep -qF "head_sha=${SHA_UNDER_TEST}" <<<"${log}" \
    && grep -qF "repos/owner/repo/actions/runs/201/jobs?" <<<"${log}" \
    && grep -qF "filter=latest" <<<"${log}"; then
    ok "asks for ci.yml runs of this commit and each run's latest attempt"
else
    bad "the query changed: ${log}"
fi

# --- gh errors --------------------------------------------------------------

# Red when: a single gh error ends the wait.
new_case
put_fail runs 1 1 "gh: Service Unavailable (HTTP 503)"
put_fail runs 2 1 "gh: Service Unavailable (HTTP 503)"
put runs 3 "$(runs_json "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
run_gate
check "transient gh errors recover" 0 "https://example.test/runs/201"

# Red when: the limit is raised above five.
new_case
for n in 1 2 3 4 5; do put_fail runs "${n}" 1 "gh: Bad Gateway (HTTP 502)"; done
put runs 6 "$(runs_json "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
run_gate
check "five gh errors in a row fail closed" 1 "5 consecutive"
check_eq "the fifth error ends it" "$(gh_calls)" 5

# Red when: the limit is lowered below five.
new_case
for n in 1 2 3 4; do put_fail runs "${n}" 1 "gh: Bad Gateway (HTTP 502)"; done
put runs 5 "$(runs_json "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
run_gate
check "four gh errors in a row still recover" 0 "https://example.test/runs/201"

# Red when: errors are counted in total rather than consecutively.
new_case
for n in 1 2 3 4 6 7 8 9; do put_fail runs "${n}" 1 "gh: Bad Gateway (HTTP 502)"; done
put runs 5 "$(runs_json "$(run_obj 201)")"
put runs 10 "$(runs_json "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" in_progress null)")"
put jobs-201 2 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
run_gate
check "a good poll resets the error count" 0 "https://example.test/runs/201"

# Red when: a failed jobs call drops that run from the poll: the older success would
# then decide, although the newest job could not be read.
new_case
put runs 1 "$(runs_json "$(run_obj 202)" "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
put_fail jobs-202 1 1 "gh: Server Error (HTTP 500)"
run_gate
check "an unreadable run never lets an older success decide" 1 "5 consecutive"

# Red when: a reply that is not the expected shape is read as "no runs".
for body in 'not json' '{"message":"Not Found"}' ''; do
    new_case; put runs 1 "${body}"; run_gate
    check "unusable reply '${body}' counts as a gh error" 1 "5 consecutive"
done
new_case
put runs 1 "$(runs_json "$(run_obj 202)" "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
put jobs-202 1 '{"message":"Not Found"}'
run_gate
check "an unusable jobs reply counts as a gh error" 1 "5 consecutive"

# --- Environment ------------------------------------------------------------

# Red when: a required value is no longer required. "gh not called" has its partner
# in the passing cases above, which do call it.
for var in REPO SHA; do
    new_case; one_job completed success; run_gate "${var}="
    check "empty ${var} fails" 1 "${var}"
    check_eq "empty ${var} never calls gh" "$(gh_calls)" 0
    new_case; one_job completed success
    if [ "${var}" = REPO ]; then run_bare SHA="${SHA_UNDER_TEST}"; else run_bare REPO=owner/repo; fi
    check "unset ${var} fails" 1 "${var}"
    check_eq "unset ${var} never calls gh" "$(gh_calls)" 0
done

# Red when: the number check is dropped or loosened (a leading zero is octal to bash,
# and more than nine digits can overflow its arithmetic).
for var in POLL_SECONDS START_GRACE_SECONDS TOTAL_TIMEOUT_SECONDS; do
    for value in 0 -1 abc 1.5 '' 010 '1 2' 12x 99999999999999999999; do
        new_case; one_job completed success; run_gate "${var}=${value}"
        check "${var}='${value}' is refused" 1 "${var}"
        check_eq "${var}='${value}' never calls gh" "$(gh_calls)" 0
    done
done

# --- Secrets ----------------------------------------------------------------

# Red when: gh's error text reaches the log, and with it anything gh put there.
new_case
for n in 1 2 3 4 5; do put_fail runs "${n}" 1 "gh: Bad credentials token=${TOKEN} (HTTP 401)"; done
run_gate
check "a gh error reports its status" 1 "HTTP 401"
if grep -qF -- "${TOKEN}" <<<"${OUT}"; then bad "the token reached the output"; else ok "the token is not in the output of a failing run"; fi
# Red when: the two checks above stop testing anything: the fake must really have
# been handed the token and really have printed it.
if [ -f "${FAKE_DIR}/token_seen" ] && grep -qF -- "${TOKEN}" "${FAKE_DIR}/runs.1.stderr"; then
    ok "partner: gh received the token and printed it"
else
    bad "partner: the fake never held the token"
fi

new_case
put_fail runs 1 1 "gh: flaky token=${TOKEN} (HTTP 500)"
put runs 2 "$(runs_json "$(run_obj 201)")"
put jobs-201 1 "$(jobs_json "$(job_obj 501 "${JOB}" completed success)")"
run_gate
check "a run recovering from a gh error passes" 0 "https://example.test/runs/201"
if grep -qF -- "${TOKEN}" <<<"${OUT}"; then bad "the token reached the output on recovery"; else ok "the token is not in the output of a passing run"; fi

# Red when: command tracing is switched on, which would print every expanded value.
TRACE_PATTERN='^[[:space:]]*set[[:space:]]+-[a-zA-Z]*x'
if grep -qE "${TRACE_PATTERN}" "${SCRIPT}"; then bad "the script turns on set -x"; else ok "the script never turns on set -x"; fi
# Red when: the pattern above stops matching a real trace line, so it checks nothing.
if printf 'set -ex\n' | grep -qE "${TRACE_PATTERN}" && [ -f "${SCRIPT}" ]; then
    ok "partner: the trace pattern matches 'set -ex' and the script exists"
else
    bad "partner: the trace pattern or the script is broken"
fi

echo
echo "wait_for_judged_eval: ${PASS} passed, ${FAIL} failed"
[ "${FAIL}" -eq 0 ]
