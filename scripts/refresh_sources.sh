#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# refresh_sources.sh — detect when an upstream docs feed has CHANGED (#375).
#
# WHAT THIS IS
#   A change DETECTOR, and nothing else. For each configured upstream feed it
#   fetches the response, hashes it, compares that hash to the previous run,
#   and reports which feeds moved. It records the hashes in a JSON manifest and
#   drops a `<name>.stale` marker beside it, in a state directory that lives
#   OUTSIDE the source tree.
#
# WHAT THIS IS NOT
#   It is NOT an ingester. It does not download corpus content, embed, index,
#   seed, promote or deploy anything. NOTHING IN PRODUCTION consumes its
#   output: no worker reads the state directory, no seed path reads it, and no
#   deploy step reads it. `grep -rn refresh_sources Makefile .github/` finds
#   nothing — but that grep is not the whole story, and reading it as "no
#   caller at all" would be wrong. `tests/shell/test_refresh_sources.sh` runs
#   this script (against a stub `curl`, in a throwaway sandbox), and it IS
#   reached by `make test-shell` and by the `shell-tests` CI job, both of which
#   pick suites up through a `tests/shell/*.sh` GLOB rather than by name. That
#   test suite is the one caller; nothing else runs this, and nothing at all
#   reads what it writes.
#
#   There is no implemented pipeline for turning an upstream snapshot into
#   corpus content. `docs/ADR/0003-embeddings-provider.md`
#   (Deferred / Future Work, item 7) records it plainly: "the upstream-snapshot
#   ingestion module (`db/ingest/`) referenced by `scripts/refresh_sources.sh`
#   still does not exist; real (licensed) corpus refresh is a separate effort."
#   It never has existed.
#
#   Do NOT run `make refresh` on the strength of anything this script prints.
#   The `Makefile` defines that target as "Rebuild + re-deploy in place" and it
#   runs `infra/docker/scripts/refresh.sh` — a PRODUCTION REDEPLOY that also
#   applies `alembic upgrade head` against the live database. It has nothing to
#   do with refreshing sources; the name collision is the whole resemblance.
#   An earlier version of this header, and of its own closing summary, told
#   operators to run it. That was wrong twice over and is corrected here.
#
# WHAT TO DO WITH A "changed" REPORT
#   Treat it as an EDITORIAL signal for a human, never as an automated step:
#
#     1. Read the upstream page and decide whether anything CiteVyn documents
#        actually moved. A feed's bytes change for reasons we do not care about
#        (build ids, timestamps, unrelated products).
#     2. Hand-edit the affected file under `backend/app/worker/sources/*.md`.
#        That directory is the ground truth every answer is generated from.
#     3. Re-ingest and promote per `docs/RUNBOOK.md` §3.7, "Editing a source
#        doc (corpus correction)", whose procedure is:
#
#            python -m app.worker.cli run
#            python -m app.worker.cli evaluate --index-version v-local
#            # then promote via POST /v1/admin/index_versions/{version}/promote
#
#        §3.7 also lists the downstream copies a corpus edit has to reach
#        (the test fixture, the frontend offline KB, the mirror manifest).
#     4. Locally, `make seed` re-runs that ingest against a dev database — the
#        `Makefile` describes it as "Seed demo users + ingest the shipped
#        corpus into index v1 (idempotent)", and `README.md` says editing those
#        files and re-running `make seed` is "the only way to change what a
#        local stack answers".
#
# OUTPUT LOCATION
#   Defaults to `artifacts/source-refresh/` under the repo root. `artifacts/`
#   is ALREADY in `.gitignore`, so this needs NO new ignore entry. It is also
#   reached by no `COPY` line in either Dockerfile, so nothing written here can
#   be baked into a production image. That is exactly why the old location —
#   a staging directory under `db/` — was wrong, and why gitignoring it would
#   have been worse than leaving it: `infra/docker/Dockerfile.api` does
#   `COPY db /build/db`, so the junk shipped inside the API image either way,
#   and an ignore rule would only have made it invisible in `git status`.
#
# USAGE
#   scripts/refresh_sources.sh [--out DIR]
#   scripts/refresh_sources.sh --help
#
#     --out DIR    write the manifest and stale markers to DIR
#     -h, --help   print this header and exit
#
#   `--out` rejects a flag-shaped value. Without that, `--out -h` reached
#   `mkdir` and surfaced as "mkdir: illegal option -- h" with an undocumented
#   exit 64; every path that names the directory also passes `--` for the same
#   reason.
#
# ENVIRONMENT
#   CITEVYN_REFRESH_STATE_DIR   default state directory (--out overrides it)
#   REFRESH_TIMEOUT             per-request curl timeout, seconds (default 30)
#
# LIMITS
#   Each response is capped at 10 MB (`curl --max-filesize`) as well as by
#   `REFRESH_TIMEOUT`. A time limit alone is not a size limit: a fast host can
#   serve gigabytes inside 30 seconds, and during review of this very change an
#   unbounded download filled a workstation's disk. Over the cap curl exits 63
#   and the feed is skipped like any other transport failure.
#
# EXIT CODES
#   0  checked. Includes "every feed was unreachable", deliberately: this is
#      safe to run from cron and a network outage must not page anyone.
#   1  a required tool is missing (curl, jq, or a SHA-256 hasher), or the
#      script was piped into a shell instead of being run as a file. This runs
#      under `set -e`, so an UNEXPECTED internal failure also leaves a non-zero
#      status — 1 means "deliberately refused", not "the only way to get 1".
#   2  usage error.
#
# Run from anywhere; the script resolves its own repo root — which is why it
# must be run as a FILE (`bash scripts/refresh_sources.sh`) and not on stdin.
# Requirements: curl, jq, and either sha256sum or shasum.
# ────────────────────────────────────────────────────────────────────────────

set -Eeuo pipefail

# --- helpers --------------------------------------------------------------

log()  { printf '\033[1;34m→\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

usage_error() { printf '%s\n' "error: $*" >&2
                printf 'usage: %s [--out DIR]   (--help for the full header)\n' "$0" >&2
                exit 2; }

# Every path below resolves the repo root from this file's own location, so the
# script has to BE a file. Piped in (`cat script | bash`) BASH_SOURCE[0] is
# unbound; `set -u` prints a message but `set -e` does NOT fire, because the
# surviving `cd "/.." && pwd` succeeds — leaving REPO_ROOT=/ and a state dir of
# //artifacts/source-refresh, i.e. writing at the filesystem root when run as
# root. Refuse instead of guessing.
[[ -n "${BASH_SOURCE[0]:-}" ]] || \
    fail "run this as a file (bash scripts/refresh_sources.sh), not on stdin"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${CITEVYN_REFRESH_STATE_DIR:-${REPO_ROOT}/artifacts/source-refresh}"
TIMEOUT="${REFRESH_TIMEOUT:-30}"

# --- arguments, BEFORE any mkdir, any network, any state ------------------
# Order is load-bearing. The previous version ran `mkdir -p` at the top of the
# file, so `--help` on an air-gapped host still dirtied the working tree.

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            # Print the whole header block: line 2 through the closing ─── rule.
            #
            # No `\{10,\}` interval on the rule: U+2500 is a 3-byte sequence and
            # a BRE interval binds its LAST byte, so under BSD sed the range
            # never terminates and --help dumps the entire script. rollback.sh
            # learned this the hard way; the literal prefix is byte-exact in
            # every locale.
            sed -n '2,/^# ────/p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        --out)
            [[ $# -ge 2 && -n "${2:-}" ]] || usage_error "--out requires a directory argument"
            # A flag-shaped value is a typo, not a directory. Taking it anyway
            # carried it all the way down to `mkdir -p "-h"`, which answered
            # "mkdir: illegal option -- h" and exited 64 — a code this script
            # does not document and did not mean.
            [[ "$2" != -* ]] || usage_error "--out requires a directory argument, got the flag '$2'"
            STATE_DIR="$2"
            shift 2
            ;;
        --out=*)
            STATE_DIR="${1#--out=}"
            [[ -n "${STATE_DIR}" ]] || usage_error "--out requires a directory argument"
            [[ "${STATE_DIR}" != -* ]] || \
                usage_error "--out requires a directory argument, got the flag '${STATE_DIR}'"
            shift
            ;;
        -*)
            usage_error "unknown flag '$1'"
            ;;
        *)
            usage_error "unexpected argument '$1'"
            ;;
    esac
done

# --- dependency preflight -------------------------------------------------
# `fail` exists so a missing tool reports what is missing instead of dying with
# a bare 127 several lines later.

command -v curl >/dev/null 2>&1 || fail "curl is required but was not found on PATH"
command -v jq   >/dev/null 2>&1 || fail "jq is required but was not found on PATH"

# macOS only gained /sbin/sha256sum recently and GitHub's macos-latest image is
# not guaranteed to carry it, so the shasum fallback is required, not polish.
if command -v sha256sum >/dev/null 2>&1; then
    HASHER="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    HASHER="shasum -a 256"
else
    fail "no SHA-256 tool on PATH: install sha256sum, or shasum (perl-shasum)"
fi

hash_file() { ${HASHER} "$1" | awk '{print $1}'; }

# --- temp-file hygiene ----------------------------------------------------
# The previous version was NOT leaking on every exit path: it ran
# `rm -f "$body_file"` both on the curl-failure branch and at the end of each
# loop iteration. What it had no defence against was `set -e` killing it
# MID-ITERATION, after the download and before that `rm`, because there was no
# trap. The measured case is a host with no SHA-256 hasher: the unguarded
# `sha256sum "$body_file"` died with a bare 127 and left the body file on disk.
# A trap covers that case and every future one.
#
# `mktemp -t` is not used: BSD mktemp treats the argument as a PREFIX while GNU
# treats it as a template, so the same call produces different names on the two
# platforms this is matrixed for.

declare -a TMP_FILES=()
cleanup() { rm -f ${TMP_FILES[@]+"${TMP_FILES[@]}"}; }
trap cleanup EXIT

# Sets TMP_PATH rather than printing it. `x="$(new_tmp_in ...)"` would run the
# append in a SUBSHELL, so the parent's TMP_FILES would stay empty and the trap
# would clean up nothing — the exact leak this replaces.
TMP_PATH=""
new_tmp_in() {  # $1 = directory to create the temp file in
    TMP_PATH="$(mktemp "$1/citevyn-refresh.XXXXXX")"
    TMP_FILES+=("${TMP_PATH}")
}

# --- state ----------------------------------------------------------------

# `--` so a directory name that starts with a dash is a PATH, never a flag.
mkdir -p -- "${STATE_DIR}"
MANIFEST="${STATE_DIR}/refresh-manifest.json"

# --- upstream registry ----------------------------------------------------
# Each entry is "<name> <manifest-url>". Keep this list short and curated.
# A source that cannot be reached is skipped with a warning, never fatal.

UPSTREAMS=(
  "anthropic-docs https://docs.anthropic.com/en/release-notes/rss.xml"
  "openai-docs    https://platform.openai.com/docs/sitemap.xml"
  "mcp-servers    https://registry.modelcontextprotocol.io/v0.1/servers"
)

# --- previous state -------------------------------------------------------
# Read with jq and VALIDATE. The previous version wrote bare-key pseudo-JSON
# (`printf '  %q: ...'`) that jq refused to parse, swallowed the error with
# `2>/dev/null || true`, and so compared every fresh hash against the empty
# string — which is why it reported every feed as "changed" on every run,
# forever. Both halves of that bug are fixed: the writer emits real JSON, and
# the writer actually writes the `sha256` field the reader looks for.

#
# The read SLURPS (`jq -s`). Without it a multi-document file — `{}\n{}\n`, the
# shape an interrupted or hand-concatenated write leaves behind — is legal input
# that jq happily echoes back as TWO documents. `prev_json` then held two lines,
# every later `jq -r ... .sha256` emitted two answers, `prev_hash` could never
# equal a single hash, and the "every feed changed, forever" bug came back
# silently and self-perpetuating. Slurping turns "not exactly one object" into
# the same loud reset as "not JSON at all".
prev_json='{}'
if [[ -f "${MANIFEST}" ]]; then
    # `null` is the sentinel for "parsed, but not a single JSON object" — no
    # object can serialise to it, so it cannot collide with a real manifest.
    candidate="$(jq -s -c \
        'if length == 1 and (.[0] | type) == "object" then .[0] else null end' \
        "${MANIFEST}" 2>/dev/null)" || candidate=""
    if [[ -z "${candidate}" ]]; then
        warn "previous manifest is not valid JSON, treating every feed as new: ${MANIFEST}"
    elif [[ "${candidate}" == "null" ]]; then
        warn "previous manifest is not a single JSON object, treating every feed as new: ${MANIFEST}"
    else
        prev_json="${candidate}"
    fi
fi
# Start from the previous state so a feed SKIPPED this run keeps its baseline.
# Dropping it would make the next reachable run report a false "changed".
next_json="${prev_json}"

# --- per-source check -----------------------------------------------------

declare -i CHANGED=0
declare -i UNCHANGED=0
declare -i SKIPPED=0

NOW="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

for entry in "${UPSTREAMS[@]}"; do
    name="${entry%% *}"
    url="${entry##* }"
    marker="${STATE_DIR}/${name}.stale"

    log "checking ${name} (${url})"
    new_tmp_in "${TMPDIR:-/tmp}"; body_file="${TMP_PATH}"

    # `--max-filesize` is a SIZE cap and `--max-time` is a TIME cap; neither
    # substitutes for the other. Over the size cap curl exits 63, which lands
    # on the same skip path as any other transport failure.
    curl_rc=0
    curl --silent --show-error --fail \
         --max-time "${TIMEOUT}" --max-filesize 10M \
         -o "${body_file}" "${url}" || curl_rc=$?
    if (( curl_rc != 0 )); then
        # Report the code. "unreachable" alone reads the same forever whether
        # the host is down for an hour (6/7/28) or the URL moved permanently
        # (22 for a 404), so a dead feed never escalates to a human.
        warn "  unreachable, skipped (curl exit ${curl_rc}; not fatal — this script is cron-safe)"
        SKIPPED+=1
        continue
    fi

    size="$(wc -c < "${body_file}" | tr -d ' ')"
    if (( size == 0 )); then
        # An empty 200 is a broken upstream, not a snapshot. Recording it would
        # bank the digest of nothing (e3b0c442…) as this feed's baseline: one
        # spurious "changed" now, and a second one when the feed next returns
        # real bytes. Two editorial reviews, neither of them about content.
        warn "  empty response body, skipped (0 bytes with no transport error)"
        SKIPPED+=1
        continue
    fi

    hash="$(hash_file "${body_file}")"
    # A hand-edited manifest can hold a non-object under a feed name. `.sha256`
    # on a number is a jq FATAL (rc 5), which killed the run before it could
    # rewrite the file — so the bad value stayed and every later run died the
    # same way, permanently, until someone deleted the manifest by hand. Read
    # defensively; a value that is not an object simply means "no baseline".
    prev_hash="$(printf '%s\n' "${prev_json}" | jq -r --arg n "${name}" \
        '.[$n] | if type == "object" then (.sha256 // "") else "" end')"

    if [[ "${hash}" != "${prev_hash}" ]]; then
        log "  changed: ${size} bytes, sha256=${hash:0:12}…"
        CHANGED+=1
        stale=true
        touch "${marker}"
    else
        log "  unchanged"
        UNCHANGED+=1
        stale=false
        # Clear the marker. Nothing used to clear it, so a marker written once
        # stayed on disk forever and said "stale" about a feed that was not.
        rm -f "${marker}"
    fi

    next_json="$(printf '%s\n' "${next_json}" | jq -c \
        --arg n "${name}" --arg h "${hash}" --argjson b "${size}" \
        --argjson st "${stale}" --arg t "${NOW}" \
        '.[$n] = {sha256: $h, bytes: $b, stale: $st, checked_at: $t}')"
done

# --- write manifest -------------------------------------------------------

# The temp file is created INSIDE the state directory, not in $TMPDIR. `mv`
# across filesystems is not a rename: it degrades to copy-then-unlink, so a
# crash mid-copy leaves a TRUNCATED manifest where an atomic replace was
# intended — and $TMPDIR on a separate volume is the normal case on macOS.
# Same directory makes it a real rename(2).
#
# `chmod 644` because mktemp creates 0600 and `mv` preserves the mode, which
# left the manifest -rw------- while the `.stale` markers beside it (from
# `touch`, i.e. umask) were -rw-r--r--. Nothing here is a secret.
new_tmp_in "${STATE_DIR}"; tmp_manifest="${TMP_PATH}"
printf '%s\n' "${next_json}" | jq '.' > "${tmp_manifest}"
chmod 644 "${tmp_manifest}"
mv "${tmp_manifest}" "${MANIFEST}"

# --- summary --------------------------------------------------------------

log "refresh complete: ${CHANGED} changed, ${UNCHANGED} unchanged, ${SKIPPED} skipped (unreachable)"
log "state: ${MANIFEST}"
if (( CHANGED > 0 )); then
    log "Nothing in this repo ingests these snapshots — a 'changed' result is an"
    log "editorial signal, not a pipeline step. Review the upstream page, hand-edit"
    log "the affected backend/app/worker/sources/*.md, then re-ingest and promote"
    log "per docs/RUNBOOK.md §3.7 (python -m app.worker.cli run, then evaluate,"
    log "then the promote endpoint). Do NOT run \`make refresh\`: that is a"
    log "production redeploy, not an ingest."
fi
exit 0
