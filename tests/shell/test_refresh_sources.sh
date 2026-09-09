#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_refresh_sources.sh — assertions for scripts/refresh_sources.sh (#375).
#
# Same convention as test_check_budget.sh / test_rollback_policy_warn.sh: no
# framework, each case runs the real script and asserts an exit code plus a
# stdout/stderr substring or a filesystem effect. Run from the repo root:
#
#   bash tests/shell/test_refresh_sources.sh
#
# Exit code 0 = all pass, 1 = at least one failure.
#
# NO NETWORK, EVER. Every case runs the script inside a throwaway fake repo
# root under $WORK with a stub `curl` FIRST on PATH. The stub logs its argv,
# writes canned bytes to the `-o` target, and can be told to fail. Nothing here
# resolves a hostname, and nothing here can touch the real db/seed/sources.
#
# The script is COPIED into the fake repo root, never cloned. `git clone` only
# carries COMMITTED history, so a clone-based harness silently tests the last
# committed script and reports green on stale code (test_rollback_policy_warn.sh
# carries the same warning for the same reason).
#
# Every run of the script under test is given `</dev/null`: test_check_budget.sh
# records that a shim which reads stdin HANGS the suite instead of failing it.
# (Case 25 is the deliberate exception — it must pipe the script itself in.)
#
# REFRESH_SOURCES_SCRIPT overrides which file is copied in. That hook exists so
# the RED proof and the mutation runs can point this suite at a byte-copy of a
# broken script without ever editing the tree.
#
# WHAT #375 ACTUALLY WAS. The filed issue named the `make refresh` collision,
# `--help`, and the tree-dirtying. Reproduction found a fourth, larger defect:
# change detection had NEVER worked. The writer emitted bare-key pseudo-JSON
# (`printf '  %q: {"stale": %s}'`) which jq refuses to parse, it never wrote a
# `sha256` field at all, and the reader's parse error was swallowed by
# `2>/dev/null || true` — so `prev_hash` was permanently "" and every feed
# reported "changed" on every run, forever. Cases 13-15 are that defect.
#
# Cases:
#    1. --help prints its header on STDOUT (USAGE + EXIT CODES), exits 0, and
#       the output is BOUNDED — not the whole file
#    2. --help makes ZERO curl invocations
#    3. --help creates ZERO files: no state dir, no <fakerepo>/db, no artifacts
#    4. unknown flag -> exit 2, message on stderr, ZERO curl invocations
#    5. --out with no value -> exit 2 naming `--out requires`
#    6. unexpected positional -> exit 2 naming `unexpected argument`
#    7. no UN-NEGATED `make refresh` mention anywhere in the script text
#    8. PARTNER for 7: it IS mentioned (negated), and the real editorial
#       pointer is present — an absence check with no partner is #381's sin
#    9. no `db/seed/sources` in the script, and a full DEFAULT-location run
#       exits 0, writes its manifest under <fakerepo>/artifacts/source-refresh,
#       and creates no <fakerepo>/db at all
#   10. --out DIR is honoured: the manifest lands under DIR
#   11. the manifest is VALID JSON (`jq -e .`)
#   12. the manifest carries a per-source sha256
#   13. IDEMPOTENCE (the core defect): identical bytes -> run 2 says unchanged
#   14. PARTNER for 13: different bytes -> run 2 says changed, marker recreated
#   15. a stale marker is WRITTEN on changed and CLEARED when the feed returns
#       to unchanged (both halves — the absence half alone passed vacuously)
#   16. every feed unreachable -> exit 0, says skipped, still no <fakerepo>/db
#   17. sha256sum absent but shasum present -> exit 0, manifest written, and
#       the digest is 64 hex chars EQUAL to the primary hasher's digest
#   18. both hashers absent -> exit 1 naming them, NOT a bare 127
#   19. a manifest whose feed value is not an object does not brick the script
#   20. a MULTI-DOCUMENT manifest resets once instead of reporting "changed"
#       forever
#   21. curl is given a SIZE cap, not only a time cap
#   22. an empty 200 body is skipped, not banked as a snapshot
#   23. a transport failure names curl's exit code, so a permanent 404 can be
#       told apart from a transient outage
#   24. --out with a flag-shaped value -> exit 2, not mkdir's exit 64
#   25. piped into bash (no BASH_SOURCE) -> refuses, never anchors state at /
#   26. the manifest lands world-readable, like the markers beside it
# ────────────────────────────────────────────────────────────────────────────

set -uo pipefail

# The suite's own sandbox must not be steerable from the ambient environment.
# `env VAR=x cmd` ADDS to the inherited environment, it does not clear it, so an
# exported CITEVYN_REFRESH_STATE_DIR escaped into every `default`-mode case:
# measured, the suite then wrote a manifest and three markers OUTSIDE $WORK.
# Case 9 does notice (25 passed / 1 failed without this line) — but it reports a
# missing manifest, not an escape, so the diagnostic points at the script rather
# than at the environment. A developer who exports the natural value
# ($PWD/artifacts/source-refresh) and runs `make test-shell` had the suite write
# into the real repo tree. Under `set -u`, `unset` on an unset name is fine.
unset CITEVYN_REFRESH_STATE_DIR REFRESH_TIMEOUT

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_SRC="${REFRESH_SOURCES_SCRIPT:-${REPO_ROOT}/scripts/refresh_sources.sh}"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  [PASS] $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  [FAIL] $1" >&2; }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

echo "==> refresh_sources.sh  (script under test: ${SCRIPT_SRC})"

if [[ ! -f "${SCRIPT_SRC}" ]]; then
    echo "  [FAIL] script under test does not exist: ${SCRIPT_SRC}" >&2
    exit 1
fi

# ── sandbox ───────────────────────────────────────────────────────────────
# Each case gets its own fake repo root, its own stub curl and its own state
# dir, so no case can observe another's manifest or markers.

SBOX=""; OUTDIR=""; CURL_LOG=""; BODY=""; CURL_RC=""
RC=0; OUT_STD=""; OUT_ERR=""

sandbox() {  # $1 = name
    SBOX="${WORK}/$1"
    mkdir -p "${SBOX}/repo/scripts" "${SBOX}/bin"
    cp "${SCRIPT_SRC}" "${SBOX}/repo/scripts/refresh_sources.sh"
    chmod +x "${SBOX}/repo/scripts/refresh_sources.sh"

    OUTDIR="${SBOX}/state"
    CURL_LOG="${SBOX}/curl.log"; : > "${CURL_LOG}"
    BODY="${SBOX}/body";        printf 'upstream body v1\n' > "${BODY}"
    CURL_RC="${SBOX}/curl.rc";  printf '0\n' > "${CURL_RC}"

    cat > "${SBOX}/bin/curl" <<'SHIM'
#!/usr/bin/env bash
# Stub curl. NO NETWORK: logs argv, then copies canned bytes to the -o target.
printf '%s\n' "$*" >> "${SHIM_CURL_LOG}"
rc="$(cat "${SHIM_CURL_RC}" 2>/dev/null || echo 0)"
if [[ "${rc}" -ne 0 ]]; then
    echo "curl: (${rc}) stubbed transport failure" >&2
    exit "${rc}"
fi
out=""; prev=""
for a in "$@"; do
    if [[ "${prev}" == "-o" ]]; then out="${a}"; fi
    prev="${a}"
done
if [[ -n "${out}" ]]; then cat "${SHIM_CURL_BODY}" > "${out}"; fi
exit 0
SHIM
    chmod +x "${SBOX}/bin/curl"
}

_exec() {  # $1 = path-mode (outdir|default|minpath:<dir>), rest = script args
    local mode="$1"; shift
    local path_prefix="${SBOX}/bin:${PATH}"
    local -a env_state=()
    case "${mode}" in
        outdir)     env_state=("CITEVYN_REFRESH_STATE_DIR=${OUTDIR}") ;;
        default)    env_state=() ;;
        minpath:*)  path_prefix="${mode#minpath:}"
                    env_state=("CITEVYN_REFRESH_STATE_DIR=${OUTDIR}") ;;
    esac
    env SHIM_CURL_LOG="${CURL_LOG}" SHIM_CURL_BODY="${BODY}" SHIM_CURL_RC="${CURL_RC}" \
        PATH="${path_prefix}" \
        ${env_state[@]+"${env_state[@]}"} \
        "${SBOX}/repo/scripts/refresh_sources.sh" "$@" \
        </dev/null >"${SBOX}/stdout" 2>"${SBOX}/stderr"
    RC=$?
    OUT_STD="$(cat "${SBOX}/stdout" 2>/dev/null)"
    OUT_ERR="$(cat "${SBOX}/stderr" 2>/dev/null)"
}

curl_calls() { wc -l < "${CURL_LOG}" | tr -d ' '; }
mode_of()    { ls -l "$1" | cut -c1-10; }

# ── 1-3. --help does nothing but print ────────────────────────────────────
# The output is BOUNDED deliberately. Asserting only that USAGE and EXIT CODES
# appear is vacuous against the exact failure the script's own comment records:
# a `sed` range that never terminates prints the WHOLE FILE, which contains both
# strings. Measured — with the range replaced by `2,$p` the help grew from 107
# lines to 373 and a USAGE/EXIT-CODES-only check stayed green. So: a line
# ceiling, plus a string that exists only BELOW the header.

sandbox help
_exec outdir --help
help_lines="$(printf '%s\n' "${OUT_STD}" | wc -l | tr -d ' ')"
if [[ ${RC} -eq 0 ]] \
   && grep -q "USAGE" <<<"${OUT_STD}" \
   && grep -q "EXIT CODES" <<<"${OUT_STD}" \
   && [[ "${help_lines}" -le 150 ]] \
   && ! grep -q 'set -Eeuo pipefail' <<<"${OUT_STD}"; then
    ok "--help prints a BOUNDED header (${help_lines} lines, USAGE + EXIT CODES, no script body), exit 0"
else
    bad "--help -> rc=${RC}, ${help_lines} line(s), body-leak=$(grep -c 'set -Eeuo pipefail' <<<"${OUT_STD}"), stdout head: $(head -3 <<<"${OUT_STD}" | tr '\n' ' ')"
fi

if [[ "$(curl_calls)" == "0" ]]; then
    ok "--help makes zero curl invocations"
else
    bad "--help invoked curl $(curl_calls) time(s): $(head -3 "${CURL_LOG}" | tr '\n' ' ')"
fi

created=""
[[ -e "${OUTDIR}" ]]                   && created="${created} state-dir"
[[ -e "${SBOX}/repo/db" ]]             && created="${created} repo/db"
[[ -e "${SBOX}/repo/artifacts" ]]      && created="${created} repo/artifacts"
if [[ -z "${created}" ]]; then
    ok "--help creates no state dir, no <fakerepo>/db, no <fakerepo>/artifacts"
else
    bad "--help dirtied the tree:${created}"
fi

# ── 4-6. usage errors ─────────────────────────────────────────────────────
# Each greps for its OWN message. Asserting only "exit 2 and something on
# stderr" lets any usage error stand in for any other, so a `--out` branch that
# started reporting an unrelated failure would still pass.

sandbox badflag
_exec outdir --definitely-not-a-flag
if [[ ${RC} -eq 2 ]] && grep -qi "unknown flag" <<<"${OUT_ERR}" && [[ "$(curl_calls)" == "0" ]]; then
    ok "unknown flag -> exit 2, stderr message, zero curl calls"
else
    bad "unknown flag -> rc=${RC}, curl=$(curl_calls), stderr: ${OUT_ERR}"
fi

sandbox outnoval
_exec outdir --out
if [[ ${RC} -eq 2 ]] && grep -q -- "--out requires" <<<"${OUT_ERR}"; then
    ok "--out with no value -> exit 2 naming '--out requires'"
else
    bad "--out with no value -> rc=${RC}, stderr: ${OUT_ERR}"
fi

sandbox positional
_exec outdir some-positional
if [[ ${RC} -eq 2 ]] && grep -q "unexpected argument" <<<"${OUT_ERR}"; then
    ok "unexpected positional -> exit 2 naming 'unexpected argument'"
else
    bad "unexpected positional -> rc=${RC}, stderr: ${OUT_ERR}"
fi

# ── 7-8. the `make refresh` guard, and the partner that makes it mean ─────
# 7 alone counts NOTHING, and this repo has a standing rule (#381) that such a
# check needs a partner proving the thing counted exists. The script MUST talk
# about `make refresh` — to warn operators off it — so the guard cannot be a
# plain substring absence. It asserts instead that NO line mentions it without
# an explicit "Do NOT run" beside it. Case 8 asserts the mention exists at all,
# plus the editorial pointer that replaced the old bad advice, so neither an
# emptied nor a renamed file can pass.
#
# KNOWN LIMIT, stated rather than engineered around: the negation is matched on
# the SAME LINE. A line holding both `make refresh` and `Do NOT run` passes even
# if it recommends exactly what is forbidden, and a warning wrapped onto the
# next line reads as un-negated. Its bite for the real regression — the old
# header's and summary's bare "run `make refresh` to ingest the stale sources" —
# is intact, and that is what it is for.

SCRIPT_TEXT="${SBOX}/repo/scripts/refresh_sources.sh"

mentions="$(grep -c 'make refresh' "${SCRIPT_TEXT}")"
unnegated="$(grep 'make refresh' "${SCRIPT_TEXT}" | grep -vc 'Do NOT run')"
if [[ "${unnegated}" == "0" ]]; then
    ok "no un-negated 'make refresh' mention (${mentions} mention(s), all warned against)"
else
    bad "${unnegated} line(s) mention 'make refresh' without a 'Do NOT run' warning: $(grep 'make refresh' "${SCRIPT_TEXT}" | grep -v 'Do NOT run' | head -2 | tr '\n' ' ')"
fi

missing=""
[[ "${mentions}" -ge 1 ]]                                       || missing="${missing} make-refresh-warning"
grep -q 'backend/app/worker/sources' "${SCRIPT_TEXT}"           || missing="${missing} worker/sources"
grep -q 'RUNBOOK.md' "${SCRIPT_TEXT}"                           || missing="${missing} RUNBOOK.md"
grep -q 'app.worker.cli' "${SCRIPT_TEXT}"                       || missing="${missing} app.worker.cli"
if [[ -z "${missing}" ]]; then
    ok "partner: it warns about 'make refresh' AND points at the real editorial path"
else
    bad "the editorial pointer is missing:${missing}"
fi

# ── 9. the output leaves db/ alone entirely, and lands where it says ──────
# The absence half ("no <fakerepo>/db") passes against a zero-byte script, so it
# is paired with the positive observation: the run must SUCCEED and its manifest
# must appear at the DEFAULT location, under artifacts/, with no --out and no
# environment override in sight.

if grep -q 'db/seed/sources' "${SCRIPT_TEXT}"; then
    bad "the script still references db/seed/sources"
else
    sandbox nodb
    _exec default
    default_manifest="${SBOX}/repo/artifacts/source-refresh/refresh-manifest.json"
    if [[ ${RC} -eq 0 ]] && [[ -f "${default_manifest}" ]] && [[ ! -e "${SBOX}/repo/db" ]]; then
        ok "default run: exit 0, manifest at <fakerepo>/artifacts/source-refresh/, no <fakerepo>/db"
    else
        bad "default run: rc=${RC}, default manifest=$([[ -f "${default_manifest}" ]] && echo yes || echo no), db=$([[ -e "${SBOX}/repo/db" ]] && echo present || echo absent), stderr: $(head -2 "${SBOX}/stderr" | tr '\n' ' ')"
    fi
fi

# ── 10-12. the manifest ───────────────────────────────────────────────────

sandbox manifest
_exec default --out "${OUTDIR}"
MANIFEST="${OUTDIR}/refresh-manifest.json"
if [[ ${RC} -eq 0 ]] && [[ -f "${MANIFEST}" ]]; then
    ok "--out DIR is honoured: the manifest lands under the given dir"
else
    bad "--out DIR -> rc=${RC}, manifest present=$([[ -f ${MANIFEST} ]] && echo yes || echo no); dir: $(ls -A "${OUTDIR}" 2>/dev/null | tr '\n' ' ')"
fi

if jq -e . "${MANIFEST}" >/dev/null 2>&1; then
    ok "the manifest is valid JSON"
else
    bad "the manifest is not valid JSON: $(head -c 200 "${MANIFEST}" 2>/dev/null | tr '\n' ' ')"
fi

if jq -e '.["anthropic-docs"].sha256 // empty' "${MANIFEST}" >/dev/null 2>&1; then
    ok "the manifest carries a per-source sha256"
else
    bad "no .[\"anthropic-docs\"].sha256 in the manifest: $(head -c 200 "${MANIFEST}" 2>/dev/null | tr '\n' ' ')"
fi

# ── 13-15. change detection: the defect the issue did not name ────────────

sandbox idempotent
_exec outdir
run1_rc=${RC}; run1="${OUT_STD}"
marker1=no; [[ -f "${OUTDIR}/anthropic-docs.stale" ]] && marker1=yes
_exec outdir
run2_rc=${RC}; run2="${OUT_STD}"
marker2=no; [[ -f "${OUTDIR}/anthropic-docs.stale" ]] && marker2=yes

if [[ ${run1_rc} -eq 0 && ${run2_rc} -eq 0 ]] \
   && grep -q "3 changed" <<<"${run1}" \
   && grep -q "0 changed" <<<"${run2}"; then
    ok "idempotence: identical bytes -> run 1 '3 changed', run 2 '0 changed'"
else
    bad "idempotence: rc=${run1_rc}/${run2_rc}; run1 summary: $(grep 'refresh complete' <<<"${run1}"); run2 summary: $(grep 'refresh complete' <<<"${run2}")"
fi

# 15 rides on the same sandbox: run 1 marked the feed stale, run 2 saw the same
# bytes, so the marker must be GONE. Nothing in the old script ever removed one.
#
# Both halves are asserted deliberately. "the marker is absent after run 2" on
# its own is an absence check, and it passed VACUOUSLY against the pre-fix
# script — which wrote its markers into a different directory entirely, so the
# state dir was empty for a reason that has nothing to do with clearing. The
# run-1 half is what proves the thing being cleared ever existed.
if [[ "${marker1}" == "yes" && "${marker2}" == "no" ]]; then
    ok "a stale marker is written on 'changed' and cleared when the feed returns to unchanged"
else
    bad "stale marker: after run 1 = ${marker1} (want yes), after run 2 = ${marker2} (want no)"
fi

# 14. Partner for 13: a hardcoded "unchanged" would satisfy 13 and fail here.
printf 'upstream body v2 — DIFFERENT\n' > "${BODY}"
_exec outdir
if [[ ${RC} -eq 0 ]] \
   && grep -q "3 changed" <<<"${OUT_STD}" \
   && [[ -f "${OUTDIR}/anthropic-docs.stale" ]]; then
    ok "partner: different bytes -> '3 changed' and the stale marker is recreated"
else
    bad "changed detection: rc=${RC}, summary: $(grep 'refresh complete' <<<"${OUT_STD}"), marker=$([[ -f "${OUTDIR}/anthropic-docs.stale" ]] && echo yes || echo no)"
fi

# ── 16. cron-safe: a total outage is not an error ─────────────────────────

sandbox outage
printf '7\n' > "${CURL_RC}"
_exec outdir
if [[ ${RC} -eq 0 ]] \
   && grep -q "3 skipped" <<<"${OUT_STD}" \
   && [[ ! -e "${SBOX}/repo/db" ]]; then
    ok "every feed unreachable -> exit 0, reports skipped, no <fakerepo>/db"
else
    bad "outage: rc=${RC}, db=$([[ -e "${SBOX}/repo/db" ]] && echo present || echo absent), summary: $(grep 'refresh complete' <<<"${OUT_STD}")"
fi

# ── 17-18. the hasher fallback ────────────────────────────────────────────
# PATH is rebuilt from scratch with symlinks to a named tool set, because there
# is no portable way to hide a hasher otherwise: macOS keeps sha256sum in
# /sbin, Linux in /usr/bin, and stripping either directory would take the rest
# of coreutils with it. `bash` is in the set because the shebang is
# `#!/usr/bin/env bash` and env resolves `bash` through PATH.

MIN_MISSING=""
min_bin() {  # $1 = dir, $2.. = tool names
    local d="$1"; shift
    mkdir -p "${d}"
    local t p
    MIN_MISSING=""
    for t in "$@"; do
        # Report what could not be linked. Swallowing it (`|| continue`) meant a
        # runner without `shasum` produced a minimal PATH missing the very tool
        # case 17 is about, and the case failed as if the SCRIPT were broken.
        if p="$(command -v "${t}" 2>/dev/null)" && [[ -n "${p}" ]]; then
            ln -sf "${p}" "${d}/${t}"
        else
            MIN_MISSING="${MIN_MISSING} ${t}"
        fi
    done
}

MIN_TOOLS=(bash sh jq awk sed mktemp mkdir rm mv touch chmod wc tr cat date dirname)

sandbox shasum_only
# The digest the harness's own hasher computes over the same bytes — the
# independent value the script's fallback has to reproduce.
expected_sha=""
if command -v sha256sum >/dev/null 2>&1; then
    expected_sha="$(sha256sum "${BODY}" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
    expected_sha="$(shasum -a 256 "${BODY}" | awk '{print $1}')"
fi

MIN="${SBOX}/minbin"
min_bin "${MIN}" "${MIN_TOOLS[@]}" shasum
missing_here="${MIN_MISSING}"
cp "${SBOX}/bin/curl" "${MIN}/curl"
_exec "minpath:${MIN}"
got_sha="$(jq -r '.["anthropic-docs"].sha256 // ""' "${OUTDIR}/refresh-manifest.json" 2>/dev/null)"
# 64 hex characters, and equal to the digest computed outside the script. A
# length check alone is the one that matters on a host with no sha256sum, where
# "primary" and "fallback" are the same binary: `shasum -a 1` would silently
# write a 40-char SHA-1 into a field named sha256, two hosts would disagree
# about identical bytes, and every feed would report "changed" forever — #375
# all over again.
if [[ -n "${missing_here}" ]]; then
    bad "shasum fallback: the minimal PATH could not be built, missing:${missing_here}"
elif [[ ${RC} -eq 0 ]] \
     && [[ -f "${OUTDIR}/refresh-manifest.json" ]] \
     && printf '%s' "${got_sha}" | grep -Eq '^[0-9a-f]{64}$' \
     && [[ -n "${expected_sha}" && "${got_sha}" == "${expected_sha}" ]]; then
    ok "sha256sum absent, shasum present -> exit 0 and a 64-hex SHA-256 matching the harness's own digest"
else
    bad "shasum fallback: rc=${RC}, manifest=$([[ -f "${OUTDIR}/refresh-manifest.json" ]] && echo yes || echo no), digest='${got_sha}' (${#got_sha} chars), want '${expected_sha}', stderr: $(head -2 "${SBOX}/stderr" | tr '\n' ' ')"
fi

sandbox no_hasher
MIN="${SBOX}/minbin"
min_bin "${MIN}" "${MIN_TOOLS[@]}"
missing_here="${MIN_MISSING}"
cp "${SBOX}/bin/curl" "${MIN}/curl"
_exec "minpath:${MIN}"
if [[ -n "${missing_here}" ]]; then
    bad "no hasher: the minimal PATH could not be built, missing:${missing_here}"
elif [[ ${RC} -eq 1 ]] \
     && grep -q "sha256sum" <<<"${OUT_ERR}${OUT_STD}" \
     && grep -q "shasum" <<<"${OUT_ERR}${OUT_STD}"; then
    ok "no hasher at all -> exit 1 naming sha256sum and shasum (not a bare 127)"
else
    bad "no hasher: rc=${RC} (want 1), stderr: $(head -3 "${SBOX}/stderr" | tr '\n' ' ')"
fi

# ── 19-20. a corrupt manifest must be survivable, not permanent ───────────

# 19. A hand-edited non-object value under a feed name reached `.sha256` on a
# number, which is a jq FATAL (rc 5) — the run died before it could rewrite the
# file, so EVERY later run died identically until a human deleted the manifest.
sandbox badvalue
mkdir -p "${OUTDIR}"
printf '{"anthropic-docs": 5}\n' > "${OUTDIR}/refresh-manifest.json"
_exec outdir
if [[ ${RC} -eq 0 ]] \
   && ! grep -qi "Cannot index" <<<"${OUT_ERR}" \
   && jq -e '.["anthropic-docs"].sha256 // empty' "${OUTDIR}/refresh-manifest.json" >/dev/null 2>&1; then
    ok "a non-object feed value is tolerated: exit 0, no jq fatal, the manifest is repaired"
else
    bad "non-object feed value: rc=${RC} (want 0), stderr: $(head -2 "${SBOX}/stderr" | tr '\n' ' '), manifest: $(head -c 120 "${OUTDIR}/refresh-manifest.json" 2>/dev/null | tr '\n' ' ')"
fi

# 20. `{}\n{}\n` is a legal jq INPUT STREAM, not one object. Unslurped, the
# reader echoed two documents, `prev_hash` held two lines, and no single hash
# could ever equal it: three consecutive runs over byte-identical bytes all
# reported "3 changed", exit 0, no warning, self-perpetuating.
sandbox multidoc
mkdir -p "${OUTDIR}"
printf '{}\n{}\n' > "${OUTDIR}/refresh-manifest.json"
_exec outdir
md1="${OUT_STD}"; md1_rc=${RC}
_exec outdir
md2="${OUT_STD}"
_exec outdir
md3="${OUT_STD}"
if [[ ${md1_rc} -eq 0 ]] \
   && grep -q "3 changed" <<<"${md1}" \
   && grep -q "not a single JSON object" <<<"${md1}" \
   && grep -q "0 changed" <<<"${md2}" \
   && grep -q "0 changed" <<<"${md3}"; then
    ok "a multi-document manifest warns, resets ONCE, then goes quiet (not 'changed' forever)"
else
    bad "multi-doc manifest: rc=${md1_rc}; run1: $(grep 'refresh complete' <<<"${md1}"); run2: $(grep 'refresh complete' <<<"${md2}"); run3: $(grep 'refresh complete' <<<"${md3}"); warned=$(grep -c 'not a single JSON object' <<<"${md1}")"
fi

# ── 21-23. what the fetch is actually allowed to do ───────────────────────

# 21. A TIME cap is not a SIZE cap: a fast host can serve gigabytes inside 30s.
# Asserted through the stub's argv log rather than by downloading anything —
# during review of this change an unbounded fetch filled a workstation's disk,
# which is precisely the thing not to reproduce inside a test.
sandbox sizecap
_exec outdir
if [[ ${RC} -eq 0 ]] \
   && grep -q -- "--max-filesize" "${CURL_LOG}" \
   && grep -q -- "--max-time" "${CURL_LOG}"; then
    ok "curl is given a size cap as well as a time cap"
else
    bad "size cap: rc=${RC}, curl argv: $(head -1 "${CURL_LOG}")"
fi

# 22. An empty 200 is a broken upstream, not a snapshot. Banking it records the
# digest of nothing as the baseline: a spurious editorial review now, and a
# second one when the feed next returns real bytes.
sandbox emptybody
: > "${BODY}"
_exec outdir
if [[ ${RC} -eq 0 ]] \
   && grep -q "3 skipped" <<<"${OUT_STD}" \
   && grep -q "empty response body" <<<"${OUT_STD}" \
   && [[ ! -f "${OUTDIR}/anthropic-docs.stale" ]] \
   && ! jq -e '.["anthropic-docs"].sha256 // empty' "${OUTDIR}/refresh-manifest.json" >/dev/null 2>&1; then
    ok "an empty 200 body is skipped with a warning, not banked as a snapshot"
else
    bad "empty body: rc=${RC}, summary: $(grep 'refresh complete' <<<"${OUT_STD}"), marker=$([[ -f "${OUTDIR}/anthropic-docs.stale" ]] && echo yes || echo no), manifest: $(head -c 160 "${OUTDIR}/refresh-manifest.json" 2>/dev/null | tr '\n' ' ')"
fi

# 23. "unreachable" reads identically forever whether the host is down for an
# hour or the URL moved permanently, so a dead feed never escalates to a human.
# curl's code is the one bit that distinguishes them (22 = HTTP >= 400 under
# --fail, i.e. a 404).
sandbox curlcode
printf '22\n' > "${CURL_RC}"
_exec outdir
if [[ ${RC} -eq 0 ]] && grep -q "curl exit 22" <<<"${OUT_STD}"; then
    ok "a skip names curl's exit code, so a permanent 404 is distinguishable"
else
    bad "curl exit code: rc=${RC}, warnings: $(grep -i 'skipped' <<<"${OUT_STD}" | head -1)"
fi

# ── 24-26. argument and filesystem edges ──────────────────────────────────

# 24. `--out -h` used to be accepted as a directory and reached `mkdir -p "-h"`,
# which answered "mkdir: illegal option -- h" and exited 64 — a code this script
# never documented.
sandbox flagshaped
_exec outdir --out -h
if [[ ${RC} -eq 2 ]] \
   && grep -q -- "--out requires" <<<"${OUT_ERR}" \
   && ! grep -qi "illegal option" <<<"${OUT_ERR}" \
   && [[ ! -e "${SBOX}/repo/-h" ]]; then
    ok "--out with a flag-shaped value -> exit 2 with the usage error, not mkdir's exit 64"
else
    bad "--out -h: rc=${RC} (want 2), stderr: $(head -2 "${SBOX}/stderr" | tr '\n' ' ')"
fi

# 25. Piped in, `${BASH_SOURCE[0]}` is unbound. `set -u` complains but `set -e`
# does NOT fire, because the surviving `cd "/.." && pwd` succeeds — leaving
# REPO_ROOT=/ and a state dir of //artifacts/source-refresh, i.e. writing at the
# filesystem root when run as root. This is the one case that cannot use
# `</dev/null`: the script text IS the stdin.
sandbox stdin
stdin_out="$(cat "${SBOX}/repo/scripts/refresh_sources.sh" \
    | env SHIM_CURL_LOG="${CURL_LOG}" SHIM_CURL_BODY="${BODY}" SHIM_CURL_RC="${CURL_RC}" \
          PATH="${SBOX}/bin:${PATH}" CITEVYN_REFRESH_STATE_DIR="${OUTDIR}" \
          bash 2>&1)"
stdin_rc=$?
if [[ ${stdin_rc} -ne 0 ]] \
   && grep -qi "not on stdin" <<<"${stdin_out}" \
   && [[ "$(curl_calls)" == "0" ]]; then
    ok "piped into bash -> refuses (rc=${stdin_rc}) instead of anchoring state at /"
else
    bad "piped into bash: rc=${stdin_rc}, wanted a non-zero rc AND a 'not on stdin' refusal; curl=$(curl_calls), out: $(head -3 <<<"${stdin_out}" | tr '\n' ' ')"
fi

# 26. mktemp creates 0600 and `mv` preserves the mode, so the manifest used to
# land -rw------- while the `.stale` markers beside it were -rw-r--r--. Nothing
# in it is a secret, and the asymmetry is the tell that the write path was not
# thought through.
sandbox perms
_exec outdir
manifest_mode="$(mode_of "${OUTDIR}/refresh-manifest.json" 2>/dev/null || echo '?')"
if [[ ${RC} -eq 0 ]] && [[ "${manifest_mode}" == "-rw-r--r--" ]]; then
    ok "the manifest lands world-readable (${manifest_mode}), like the markers beside it"
else
    bad "manifest mode is ${manifest_mode} (want -rw-r--r--), rc=${RC}"
fi

echo "  passed: ${PASS}  failed: ${FAIL}"
[[ ${FAIL} -eq 0 ]] || exit 1
