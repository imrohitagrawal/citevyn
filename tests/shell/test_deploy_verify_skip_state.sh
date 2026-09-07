#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_deploy_verify_skip_state.sh — the SKIP verdict and the zero-probe guard
# in deploy_verify.sh (#362).
#
# WHY THIS FILE EXISTS
# --------------------
# Before #362 a probe that neither passed nor failed simply did not call
# `record`: no row, no counter, gone from the summary. The operator read a green
# gate that had quietly run fewer probes than they thought, and NOTHING said so.
# `record` is the only thing that moves PASS_COUNT/FAIL_COUNT, so a skipped
# branch was invisible by construction.
#
# The fix adds a third verdict. The properties that make it a fix rather than a
# relabelling are all asserted here:
#   * a SKIP is NOT a pass — it moves neither counter;
#   * a SKIP is visible — it prints, and it lands in RESULTS for the summary;
#   * a SKIP carries a REASON, always;
#   * a run in which everything skipped still FAILS the zero-probe guard, which
#     now counts executed probes rather than rows (a row count would have been
#     made unfireable by the SKIP rows themselves).
#
# `record` and the main summary's zero-probe guard are both EXTRACTED from the
# shipped script and EXECUTED — never transcribed, and never merely grepped for.
# Case 9b goes further and runs the SHIPPED script end to end under
# --verify-only against a dead port, which is the only thing that proves §1b's
# two SKIPs, the `skipped:` tally and the `SKIP_COUNT=0` initialisation. Review
# found all three surviving every extraction-based assertion.
#
# WHAT IT CANNOT COVER, STATED PLAINLY
# ------------------------------------
# The MAIN summary cannot be reached without a live docker stack — it sits past
# the deploy and the rollback drills — so its zero-probe guard is driven in
# isolation with fabricated tallies. That proves the guard's own behaviour, not
# that the surrounding script reaches it; the ONE textual check in this file
# (that both summaries use the same executed-probe arithmetic) covers the second
# half, and it is labelled as such. The --verify-only summary IS reached for
# real in 9b.
#
# Run from the repo root:
#   bash tests/shell/test_deploy_verify_skip_state.sh
# ────────────────────────────────────────────────────────────────────────────

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATE="${REPO_ROOT}/infra/docker/scripts/deploy_verify.sh"
FAILURES=0
ASSERTIONS=0
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

pass() { echo "  ok   — $1"; ASSERTIONS=$((ASSERTIONS + 1)); }
fail() { echo "  FAIL — $1"; FAILURES=$((FAILURES + 1)); ASSERTIONS=$((ASSERTIONS + 1)); }

echo "test_deploy_verify_skip_state.sh"

# ── Extract the REAL `record` implementation. ─────────────────────────────
# The sanity marker is `RESULTS+=`, not `SKIP_COUNT`. It must survive every
# mutation of record()'s BODY, or a mutant is killed here — "the anchor moved" —
# instead of by the behavioural assertion that names the defect, and the day the
# behaviour tests stop biting nobody notices.
RECORD="$(awk '/^record\(\) \{$/,/^\}$/' "${GATE}")"
if [[ -n "${RECORD}" ]] && printf '%s' "${RECORD}" | grep -q 'RESULTS+='; then
    pass "record() was located in deploy_verify.sh"
else
    fail "could not extract record() from ${GATE} — the anchor moved"
    echo "test_deploy_verify_skip_state.sh: ${FAILURES} of ${ASSERTIONS} assertions FAILED"
    exit 1
fi

drive() {  # drive <record-calls...>  — run the REAL record(), print the tallies
    cat > "${WORK}/h.sh" <<EOF
set -uo pipefail
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
RESULTS=()
${RECORD}
$1
echo "TALLY pass=\${PASS_COUNT} fail=\${FAIL_COUNT} skip=\${SKIP_COUNT} rows=\${#RESULTS[@]}"
if [[ \${#RESULTS[@]} -gt 0 ]]; then printf 'ROW %s\n' "\${RESULTS[@]}"; fi
EOF
    bash "${WORK}/h.sh" 2>&1
}

# `--` before the pattern: a needle starting with `-` (every flag name here) is
# otherwise parsed by grep as an option, and the check silently tests something
# else. Caught by this file's own `--help` case, which "failed" while the flag
# was plainly in the output.
has() {  # has <needle> <label> <haystack>
    if printf '%s' "$3" | grep -qF -- "$1"; then pass "$2"; else fail "$2 — got: $3"; fi
}
hasnt() {  # hasnt <needle> <label> <haystack>
    if printf '%s' "$3" | grep -qF -- "$1"; then fail "$2 — got: $3"; else pass "$2"; fi
}

# ── 1. A SKIP is not a pass, and not a failure. ───────────────────────────
OUT="$(drive 'record SKIP "bundle probe" "built inside the image"')"
has "pass=0 fail=0 skip=1" "a SKIP moves neither PASS_COUNT nor FAIL_COUNT" "${OUT}"

# ── 2. …but it IS recorded, so the summary can print it. This is the whole
#      of #362: the old code path produced no row at all. ─────────────────
has "rows=1" "a SKIP still lands in RESULTS (the old silent branch did not)" "${OUT}"
has "ROW SKIP|bundle probe|built inside the image" \
    "the RESULTS row carries the status, name and reason" "${OUT}"
has "[SKIP] bundle probe — built inside the image" \
    "the SKIP prints a line the operator can see" "${OUT}"

# ── 3. PARTNER (non-vacuity): PASS and FAIL still work. Without this, a
#      record() that did nothing at all would satisfy every check above.
OUT="$(drive 'record PASS "a" ; record FAIL "b" "why" ; record SKIP "c" "reason"')"
has "pass=1 fail=1 skip=1 rows=3" "PASS and FAIL still move their own counters" "${OUT}"
has "ROW PASS|a|" "a PASS still lands in RESULTS" "${OUT}"
has "ROW FAIL|b|why" "a FAIL still lands in RESULTS with its detail" "${OUT}"

# ── 4. An unexplained SKIP is the silent state coming back. It must be
#      impossible to write one that says nothing. ────────────────────────
OUT="$(drive 'record SKIP "mystery"')"
has "this is a bug in deploy_verify.sh" \
    "a SKIP with no reason is labelled a bug, not printed blank" "${OUT}"
has "ROW SKIP|mystery|no reason given" \
    "the RESULTS row for a reasonless SKIP carries the bug marker, not an empty detail" "${OUT}"

# ── 5. An unknown verdict must not silently become a pass. `record` is fed a
#      literal at every call site today, but the else-branch is what decides
#      what a typo does, and "counts as PASS" would be the worst answer.
OUT="$(drive 'record PASSED "typo"')"
has "pass=0 fail=1" "an unrecognised verdict is treated as a FAIL, never a pass" "${OUT}"
# …and the SUMMARY must say so too. The counters were right while the recorded
# row still carried the raw word, so the operator's line-by-line list read
# "[PASSED]" on a probe that had failed.
has "ROW FAIL|typo|" "an unrecognised verdict is NORMALISED in the summary row" "${OUT}"
hasnt "ROW PASSED|" "the raw typo never reaches the summary" "${OUT}"

# ── 6. The zero-probe guard counts EXECUTED probes, not rows. Now that SKIP
#      rows populate RESULTS, a `${#RESULTS[@]} -eq 0` guard could never fire
#      again — an all-skipped run would read as a pass, which is precisely the
#      class of defect #362 is about. Both guards must key on PASS+FAIL.
# Whitespace-tolerant on purpose. The first version pinned the guard byte-exact
# and went RED on `$(( PASS_COUNT + FAIL_COUNT ))` — semantically identical,
# still-correct code. A test that fails on a reformat trains people to ignore it.
GUARDS="$(grep -cE 'PASS_COUNT[[:space:]]*\+[[:space:]]*FAIL_COUNT' "${GATE}")"
if [[ "${GUARDS}" -eq 2 ]]; then
    pass "both summaries guard on executed probes (found ${GUARDS})"
else
    fail "expected 2 executed-probe guards (verify-only + main summary), found ${GUARDS}"
fi
# shellcheck disable=SC2016  # single quotes are deliberate: this is a grep pattern
if grep -q '\${#RESULTS\[@\]}" -eq 0' "${GATE}"; then
    fail "a row-count zero-probe guard survives; SKIP rows make it unfireable"
else
    pass "no row-count zero-probe guard remains"
fi

# ── 7. EXECUTE the main summary's zero-probe guard. Not a grep for its text —
#      the guard is extracted from the shipped script and RUN against tallies,
#      so what is asserted is the exit code an operator would get.
GUARD="$(awk '/^# The same zero-probe guard --verify-only has carried since #195/,/^fi$/' "${GATE}")"
if [[ -n "${GUARD}" ]] && printf '%s' "${GUARD}" | grep -q 'no probes executed'; then
    pass "the main summary's zero-probe guard was located"
else
    fail "could not extract the main summary's zero-probe guard — the anchor moved"
fi

run_guard() {  # run_guard <pass> <fail> <skip>
    # RESULTS is populated to match the tallies ON PURPOSE. A guard mutated back
    # to `${#RESULTS[@]} -eq 0` must then die because it FAILS TO FIRE, not
    # because RESULTS is an unbound variable — a crash is a kill for the wrong
    # reason and would hide the day the array count came back.
    local rows=$(( $1 + $2 + $3 )) i list=""
    for ((i = 0; i < rows; i++)); do list="${list} \"PASS|probe${i}|\""; done
    cat > "${WORK}/g.sh" <<EOF
set -uo pipefail
PASS_COUNT=$1
FAIL_COUNT=$2
SKIP_COUNT=$3
RESULTS=(${list})
${GUARD}
echo "REACHED_SUMMARY_END"
EOF
    bash "${WORK}/g.sh" 2>&1; echo "RC=$?"
}

# THE #362 CASE: every probe skipped. Rows exist, so a `${#RESULTS[@]}` guard
# would have let this through as a pass.
OUT="$(run_guard 0 0 3)"
has "no probes executed" "an all-SKIPPED run is reported as a broken harness" "${OUT}"
has "RC=1" "an all-SKIPPED run exits non-zero" "${OUT}"
hasnt "REACHED_SUMMARY_END" "an all-SKIPPED run stops at the guard" "${OUT}"

# PARTNER (non-vacuity): one executed probe and the guard stands down. Without
# this, an `exit 1` written unconditionally would satisfy the three above.
OUT="$(run_guard 1 0 3)"
hasnt "no probes executed" "one PASS is enough for the guard to stand down" "${OUT}"
has "REACHED_SUMMARY_END" "the summary continues when a probe actually ran" "${OUT}"
# …and a FAIL counts as executed too (the guard is about the HARNESS, not the
# verdict; the FAIL_COUNT branch further down is what fails the gate).
OUT="$(run_guard 0 1 0)"
hasnt "no probes executed" "one FAIL also counts as an executed probe" "${OUT}"

# ── 7b. The SAME guard in the --verify-only summary, extracted and executed
#      SEPARATELY. Counting matching lines is blind to the COMPARISON: review
#      changed `-eq 0` to `-eq 99999` in this one — a guard that can never fire —
#      and every assertion in both suites stayed green, because the line still
#      matched the count pattern and case 9b below always has PASS+FAIL > 0.
#      Two guards, two executions.
VGUARD="$(awk '/^    # A run that EXECUTED nothing is a broken harness, not a pass\. Counted on$/,/^    fi$/' "${GATE}")"
if [[ -n "${VGUARD}" ]] && printf '%s' "${VGUARD}" | grep -q 'no probes executed'; then
    pass "the --verify-only summary's zero-probe guard was located"
else
    fail "could not extract the --verify-only zero-probe guard — the anchor moved"
fi

run_vguard() {  # run_vguard <pass> <fail>
    cat > "${WORK}/vg.sh" <<EOF
set -uo pipefail
PASS_COUNT=$1
FAIL_COUNT=$2
${VGUARD}
echo "REACHED_SUMMARY_END"
EOF
    bash "${WORK}/vg.sh" 2>&1; echo "RC=$?"
}

OUT="$(run_vguard 0 0)"
has "no probes executed" "the --verify-only guard fires on a zero-executed run" "${OUT}"
has "RC=1" "the --verify-only guard exits non-zero" "${OUT}"
# PARTNER: it stands down on a real run, so it is not an unconditional exit 1.
OUT="$(run_vguard 5 0)"
has "REACHED_SUMMARY_END" "the --verify-only guard stands down when probes ran" "${OUT}"

cp "${GATE}" "${WORK}/gate.sh"

# ── 8. --frontend-built-in-image is a REAL flag, not silently ignored. An
#      unknown argument exits 2 with 'unknown argument', so this proves the
#      parser ACCEPTS it — parsing happens before anything else, which is why
#      the run dying at the preflight `.env` check a moment later is fine.
#      (`--dry-run` is passed for good measure but never reached: the copied
#      script's COMPOSE_DIR is mktemp's parent, so preflight dies first. Stated
#      because the flag reads as if the plan runs, and it does not.)
OUT="$("${WORK}/gate.sh" --frontend-built-in-image --dry-run 2>&1)"; RC=$?
hasnt "unknown argument" "--frontend-built-in-image is a recognised flag" "${OUT}"
# PARTNER: the same invocation with a bogus flag IS rejected, so the check
# above is not passing because the parser accepts everything.
OUT="$("${WORK}/gate.sh" --frontend-built-in-imag --dry-run 2>&1)"; RC=$?
if [[ ${RC} -eq 2 ]] && printf '%s' "${OUT}" | grep -q 'unknown argument'; then
    pass "a mistyped flag is still rejected with exit 2"
else
    fail "a mistyped flag was not rejected (rc=${RC}): ${OUT}"
fi

# ── 9. --help must document the flag it now accepts.
OUT="$("${WORK}/gate.sh" --help 2>&1)"
has "--frontend-built-in-image" "--help documents the new flag" "${OUT}"
has "A SKIP is NOT a pass" "--help explains what a SKIP verdict means" "${OUT}"

# ── 9b. DRIVE THE REAL SCRIPT END TO END under --verify-only, against a dead
#      port. This is the only assertion in this file that runs the SHIPPED
#      file's §1b and its summary rather than an extraction, and it exists
#      because review found four mutants that survived everything above:
#        * replacing both `record SKIP` calls in the --verify-only else-branch
#          with `:` (the ORIGINAL silent state, and half of what #362 claims);
#        * deleting `skipped: ${SKIP_COUNT}` from either summary line;
#        * deleting `SKIP_COUNT=0` (fatal on bash 3.2 under `set -u`, and the
#          extraction harness above supplies its own initialisation, so it
#          could never have caught it).
#      Hermetic: no docker, no deploy, no network beyond a refused connection
#      to 127.0.0.1:1.
FIX="${WORK}/fixture"
mkdir -p "${FIX}/infra/docker/scripts" "${FIX}/scripts" "${FIX}/frontend/dist"
cp "${GATE}" "${FIX}/infra/docker/scripts/deploy_verify.sh"
printf 'CITEVYN_DEMO_API_KEY=fixturekey\nCITEVYN_PUBLIC_HOST=127.0.0.1\n' \
    > "${FIX}/infra/docker/.env"
# _env_guard.sh and check_budget.sh are not reached under --verify-only, but
# copy them so a future change that does reach them fails loudly, not on ENOENT.
cp "${REPO_ROOT}/infra/docker/scripts/_env_guard.sh" "${FIX}/infra/docker/scripts/" 2>/dev/null || true
cp "${REPO_ROOT}/scripts/check_budget.sh" "${FIX}/scripts/" 2>/dev/null || true
cp "${REPO_ROOT}/scripts/check_bundle_key.sh" "${FIX}/scripts/" 2>/dev/null || true

# STUB `docker`, do not assume it. `deploy_verify.sh` runs
# `command -v docker || die` and that check sits BELOW the --dry-run early exit,
# so --verify-only reaches it. The macos-latest CI leg ships no docker CLI at
# all — the gate's own comment records that leg going red for exactly this
# reason — so without the stub this case would fail on arrival in CI, and worse,
# two of the assertions below would then "pass" for the wrong reason (the
# absent-string and non-zero-exit checks are both satisfied by dying at
# preflight). A no-op stub is enough: --verify-only never invokes docker, it
# only requires the binary to exist.
mkdir -p "${FIX}/bin"
printf '#!/bin/sh\nexit 0\n' > "${FIX}/bin/docker"
chmod +x "${FIX}/bin/docker"

OUT="$(PATH="${FIX}/bin:${PATH}" BASE_URL="http://127.0.0.1:1" \
    CITEVYN_DEMO_API_KEY="fixturekey" \
    "${FIX}/infra/docker/scripts/deploy_verify.sh" --verify-only 2>&1)"; RC=$?

# PARTNER, and the thing that makes the four assertions below non-vacuous: the
# run must have got PAST preflight. Without this, a future preflight check the
# fixture does not satisfy would silently turn every case below into a
# passes-for-the-wrong-reason.
hasnt "not found on PATH" "the run cleared preflight (it did not die on a missing tool)" "${OUT}"
has "probing" "the run reached the --verify-only probe stage" "${OUT}"

has "[SKIP] provider key has budget remaining" \
    "--verify-only RECORDS its skipped budget probe (it used to skip silently)" "${OUT}"
has "[SKIP] frontend bundle carries the current demo key" \
    "--verify-only RECORDS its skipped bundle probe" "${OUT}"
has "runs only on a deploying run" \
    "each --verify-only SKIP carries its reason" "${OUT}"
has "skipped: 2" \
    "the summary prints the skipped COUNT the operator reads" "${OUT}"
# PARTNER: the run really did execute probes, so `skipped: 2` is not the whole
# story and the zero-probe guard correctly stood down.
hasnt "no probes executed" "probes DID execute, so the harness guard stayed quiet" "${OUT}"
if [[ ${RC} -ne 0 ]]; then
    pass "a --verify-only run against a dead port still FAILS (rc=${RC})"
else
    fail "a --verify-only run against a dead port exited 0: ${OUT}"
fi

# ── 10. The script still parses. ─────────────────────────────────────────
if bash -n "${GATE}" 2>/dev/null; then
    pass "deploy_verify.sh parses"
else
    fail "deploy_verify.sh has a syntax error"
fi

# ── Non-vacuity, pinned exactly. A slack floor lets a whole case be deleted
#    silently; bump this deliberately when you add one.
_EXPECTED_ASSERTIONS=39
if [[ ${ASSERTIONS} -ne ${_EXPECTED_ASSERTIONS} ]]; then
    echo "  FAIL — ${ASSERTIONS} assertions ran; expected exactly ${_EXPECTED_ASSERTIONS}."
    FAILURES=$((FAILURES + 1))
fi

echo
if [[ ${FAILURES} -eq 0 ]]; then
    echo "test_deploy_verify_skip_state.sh: ${ASSERTIONS} assertions, all passed"
    exit 0
fi
echo "test_deploy_verify_skip_state.sh: ${FAILURES} of ${ASSERTIONS} assertions FAILED"
exit 1
