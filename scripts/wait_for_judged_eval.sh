#!/usr/bin/env bash
# wait_for_judged_eval.sh: hold a release until the judged answer-quality eval has
# passed on the commit being released. release.yml runs it before anything is built
# or published.
#
# Required env:  REPO (owner/name), SHA (the commit), GH_TOKEN (read by gh).
# Optional env:  POLL_SECONDS (30), START_GRACE_SECONDS (900), TOTAL_TIMEOUT_SECONDS
#                (4500). Each must be a positive integer; set-but-invalid is refused.
#
# Exit 0 only when the newest judged job on this commit concluded `success` AND
# exactly one step of it named JUDGED_STEP_NAME concluded `success`. Everything else
# exits 1: any other conclusion, an eval step that is skipped, absent, duplicated or
# anything but `success`, no judged job within the grace period, the total timeout,
# or five `gh` failures in a row.
#
# Why the step as well as the job. When the OpenRouter secret is absent the job's
# guard skips every step and the job still concludes `success`, so a green job can
# mean nothing was judged. A skipped eval stops the release.
#
# What decides. Runs of ci.yml on this commit are read, the latest attempt of each.
# Only the events `push` (the tag) and `workflow_dispatch` (the manual fix) count: a
# `pull_request` run tests a merge ref, not this commit, and an event GitHub adds
# later does not count until someone decides it should. The job whose name equals
# JUDGED_JOB_NAME exactly is kept. Skipped jobs do not
# count, because an ordinary merge skips this job on the same commit. The highest
# job id is the newest job, so a re-run inside an older run still counts as newest;
# comparing run ids would get that wrong.
#
# A poll with any unreadable answer is discarded whole. Deciding on the runs that
# could be read would let an older success stand in for a newer failure.
#
# The grace period is 900 s because the judged job `needs` the test job, and GitHub
# only creates it once that job has finished.
#
# gh's error text is never printed, only the HTTP status taken from it, so nothing
# gh writes can put a credential in the log. Do not add `set -x` here.
set -uo pipefail

# Must equal the `name:` of the answer-quality-eval job in ci.yml.
# backend/tests/test_release_waits_for_judged_eval.py fails if the two differ.
JUDGED_JOB_NAME='judged answer-quality eval (needs the OpenRouter and Gemini key secrets)'
# Must equal the `name:` of the step in that job that runs the eval (same test).
JUDGED_STEP_NAME='Judged eval — MIN_MEAN_JUDGE + groundedness + injection gates (--postgres)'
MAX_CONSECUTIVE_GH_FAILURES=5
NEXT_STEP="Next step: read that run's report, fix the cause on main, then cut a new tag. If the failure was an outage, re-run the eval, then re-run this release."

log() { echo "wait_for_judged_eval: $*"; }
die() { echo "wait_for_judged_eval: ERROR: $*" >&2; exit 1; }

[ -n "${REPO:-}" ] || die "REPO is required (owner/name)"
[ -n "${SHA:-}" ] || die "SHA is required (the commit being released)"

# ${VAR-default}, not ${VAR:-default}: set-but-empty is a mistake to refuse, not a
# request for the default.
POLL_SECONDS="${POLL_SECONDS-30}"
START_GRACE_SECONDS="${START_GRACE_SECONDS-900}"
TOTAL_TIMEOUT_SECONDS="${TOTAL_TIMEOUT_SECONDS-4500}"
for setting in POLL_SECONDS START_GRACE_SECONDS TOTAL_TIMEOUT_SECONDS; do
    # No leading zero (bash reads it as octal) and at most nine digits (no overflow).
    [[ "${!setting}" =~ ^[1-9][0-9]{0,8}$ ]] || die "${setting} must be a positive integer"
done

WORK="$(mktemp -d)" || die "could not create a temporary directory"
trap 'rm -rf "${WORK}"' EXIT

# gh_get <output file> <api path>: fails on a gh error or an empty answer.
gh_get() {
    local out="$1" path="$2" rc http_status
    gh api --paginate "${path}" >"${out}" 2>"${WORK}/gh.err" </dev/null
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        http_status="$(grep -oE 'HTTP [0-9]{3}' "${WORK}/gh.err" | head -n 1)"
        log "gh api failed (exit ${rc}${http_status:+, ${http_status}}): ${path}"
        return 1
    fi
    if [ ! -s "${out}" ]; then
        log "gh api returned nothing: ${path}"
        return 1
    fi
}

# poll_once: writes one line per counted job to found.tsv (id, status, conclusion,
# eval step result, run URL). The step result is its conclusion, `none` if it has
# none, or `absent` / `duplicate`. Returns non-zero if any answer was unusable.
poll_once() {
    local runs_path run_id run_url
    : >"${WORK}/found.tsv"
    runs_path="repos/${REPO}/actions/workflows/ci.yml/runs?head_sha=${SHA}&per_page=100"
    gh_get "${WORK}/runs.json" "${runs_path}" || return 1
    # head_sha is compared here as well: GitHub ignores a query filter it does not
    # recognise, and the result would be other commits' runs.
    if ! jq -r --arg sha "${SHA}" \
        '.workflow_runs[]
         | select(.head_sha == $sha and (.event == "push" or .event == "workflow_dispatch"))
         | [(.id | tostring), (.html_url // "(no URL)")] | @tsv' \
        "${WORK}/runs.json" >"${WORK}/runs.tsv" 2>/dev/null; then
        log "unusable answer listing the runs"
        return 1
    fi
    while IFS=$'\t' read -r run_id run_url; do
        gh_get "${WORK}/jobs.json" \
            "repos/${REPO}/actions/runs/${run_id}/jobs?filter=latest&per_page=100" || return 1
        if ! jq -r --arg name "${JUDGED_JOB_NAME}" --arg step "${JUDGED_STEP_NAME}" \
            --arg url "${run_url}" \
            '.jobs[] | select(.name == $name and .conclusion != "skipped")
             | [(.steps // [])[] | select(.name == $step) | (.conclusion // "none")] as $found
             | (if ($found | length) == 0 then "absent"
                elif ($found | length) > 1 then "duplicate"
                else $found[0] end) as $step_result
             | [(.id | tostring), .status, (.conclusion // "none"), $step_result, $url] | @tsv' \
            "${WORK}/jobs.json" >>"${WORK}/found.tsv" 2>/dev/null; then
            log "unusable answer listing the jobs of run ${run_id}"
            return 1
        fi
    done <"${WORK}/runs.tsv"
}

start="$(date +%s)" || die "could not read the clock"
NO_JOB_HELP="The judged job is skipped when the test job (pytest + lint) fails, so look at the ci run for this commit first. If that job passed: Run the ci workflow manually on a branch or tag that points at commit ${SHA}, then re-run this release."
last_status=""
last_run_url=""
consecutive_failures=0
log "waiting for '${JUDGED_JOB_NAME}' on ${SHA}"

while :; do
    state=unread
    if poll_once; then
        consecutive_failures=0
        newest="$(sort -t "$(printf '\t')" -k1,1n "${WORK}/found.tsv" | tail -n 1)"
        if [ -z "${newest}" ]; then
            state=no-job
        else
            IFS=$'\t' read -r job_id job_status conclusion step_result run_url <<<"${newest}"
            if [ "${job_status}" = completed ]; then
                # Allowlists of one: anything GitHub adds later fails closed.
                [ "${conclusion}" = success ] ||
                    die "judged eval concluded '${conclusion}': ${run_url}. ${NEXT_STEP}"
                if [ "${step_result}" = success ]; then
                    log "judged eval passed: job ${job_id}, ${run_url}"
                    exit 0
                fi
                if [ "${step_result}" = skipped ]; then
                    die "the judged eval did not run: its step was skipped, so the job's success judged nothing. The repository secret CITEVYN_OPENROUTER_API_KEY is missing. Add it under Settings > Secrets and variables > Actions, run the ci workflow on this commit, then re-run this release: ${run_url}"
                fi
                die "the judged eval did not run to a pass: step '${JUDGED_STEP_NAME}' is '${step_result}': ${run_url}. ${NEXT_STEP}"
            fi
            state=running
            last_status="${job_status}"
            last_run_url="${run_url}"
            log "job ${job_id} is ${job_status}: ${run_url}"
        fi
    else
        consecutive_failures=$((consecutive_failures + 1))
        if [ "${consecutive_failures}" -ge "${MAX_CONSECUTIVE_GH_FAILURES}" ]; then
            die "gh failed ${consecutive_failures} consecutive times; failing closed"
        fi
    fi

    now="$(date +%s)" || die "could not read the clock"
    elapsed=$((now - start))
    if [ "${elapsed}" -ge "${TOTAL_TIMEOUT_SECONDS}" ]; then
        if [ -n "${last_run_url}" ]; then
            die "timed out after ${elapsed}s waiting for the judged eval on ${SHA}: job is '${last_status}', no conclusion: ${last_run_url}. ${NEXT_STEP}"
        fi
        die "timed out after ${elapsed}s: no judged job appeared on ${SHA}. ${NO_JOB_HELP}"
    fi
    if [ "${state}" = no-job ] && [ "${elapsed}" -ge "${START_GRACE_SECONDS}" ]; then
        die "no judged job ran on ${SHA} within ${START_GRACE_SECONDS}s. ${NO_JOB_HELP}"
    fi
    sleep "${POLL_SECONDS}"
done
