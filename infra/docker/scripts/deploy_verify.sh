#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# deploy_verify.sh — the ONE-COMMAND live release gate for CiteVyn.
#
# This is the Phase-5 exit gate (RELEASE_PLAN §5) and it closes §10 blocker 9
# ("rollback is not tested"). It deploys a version, proves the DEPLOYED system
# actually answers correctly, then proves you can get back to the previous
# release and still serve — and finally rolls forward again.
#
# SCOPE — what this gate covers:
#   • deploy the target version onto the live stack (from that tag's tree)
#   • liveness + dependency + index health
#   • FUNCTIONAL proof against the running deployment:
#       - a grounded, CITED answer for an in-corpus question   (§10 blocker 3)
#       - a REFUSAL for an out-of-corpus question              (§10 blocker 5)
#       - exact lookup returns a hit                           (§10 blocker 4)
#       - admin endpoints reject an unauthenticated call       (§10 blocker 7)
#   • ROLLBACK DRILLS (§10 blocker 9) — see below
#   • roll forward to the target again + re-verify
#
# THE TWO ROLLBACK DRILLS, and why there are two (#195):
#   A. DATA-recovery drill (always runs). Dump the live database, stop the
#      writers, pg_restore that dump, bring the api back, re-verify. This is the
#      procedure RUNBOOK §4.2 prescribes and the ONLY rollback that works once a
#      forward-only migration has been applied.
#   B. CODE-rollback drill (runs only when it CAN work). Roll the code back to
#      PREV_VERSION, re-verify, roll forward, re-verify. A code-only rollback
#      across a migration boundary is impossible: the live DB stays stamped at a
#      revision the older tree does not contain, and alembic dies with
#      "Can't locate revision identified by 'NNNN'". So when PREV_VERSION is a
#      different migration generation this gate does NOT pretend to prove it —
#      it asserts that rollback.sh REFUSES fast, and then FAILS unless the
#      operator explicitly narrows the scope with --data-rollback-only.
#
# SCOPE — what this gate does NOT cover (deliberately):
#   It is NOT the full demo/release regression suite. The 50-case golden suite
#   (`make golden`), the judged answer-quality eval (`make eval`), Playwright UI
#   e2e (`make e2e`) and lint/typecheck/unit tests (`make ci`) all live in
#   docs/DEMO_CHECKLIST.md and run BEFORE the cut, mostly in CI against a
#   hermetic stack. This script answers a narrower question: "is the thing we
#   just deployed actually serving correct answers, and can we get back?"
#   A deploy that boots but cannot answer is a FAILED deploy — which is why the
#   functional probes are in scope even though the exhaustive suites are not.
#
# Usage:
#   VERSION=v0.10.0 PREV_VERSION=v0.9.0 make deploy-verify
#   ./scripts/deploy_verify.sh --skip-rollback-drill   # deploy + verify only
#   ./scripts/deploy_verify.sh --data-rollback-only    # gate drill A only, and
#                                                      # SAY SO in the summary
#                                                      # (blocker 9 stays PARTIAL)
#   ./scripts/deploy_verify.sh --dry-run               # print the plan, change nothing
#
# Env:
#   VERSION        release TAG to deploy+verify (default: tag at HEAD; must be a tag)
#   PREV_VERSION   rollback drill target        (default: previous v* tag)
#   BASE_URL       where to probe               (default: https://$CITEVYN_PUBLIC_HOST)
#   PRODUCT_AREA   area for the exact-lookup probe (default: codex)
#   CURL_OPTS      extra curl flags             (e.g. "-k" for a self-signed cert)
#
# Exit codes: 0 = gate PASSED, non-zero = gate FAILED (details in the summary).
# ────────────────────────────────────────────────────────────────────────────
set -uo pipefail   # NOTE: not -e; probes are allowed to fail so we report them all.

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

SKIP_ROLLBACK=0
DRY_RUN=0
VERIFY_ONLY=0
DATA_ROLLBACK_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-rollback-drill) SKIP_ROLLBACK=1; shift ;;
        --data-rollback-only)  DATA_ROLLBACK_ONLY=1; shift ;;
        --verify-only)         VERIFY_ONLY=1; shift ;;
        --dry-run)             DRY_RUN=1; shift ;;
        # Print the header block up to its closing rule. A hard-coded end line
        # (it was '2,43p') silently truncates --help every time the header grows.
        # No `\{10,\}` interval — see the note in rollback.sh: a BRE interval over
        # the 3-byte U+2500 rule never matches under LC_ALL=C with BSD sed, and
        # --help would dump the whole script.
        -h|--help)             sed -n '2,/^# ────/p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "error: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

cd "${REPO_ROOT}"

PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

record() {  # record <PASS|FAIL> <name> [detail]
    local status="$1" name="$2" detail="${3:-}"
    if [[ "${status}" == "PASS" ]]; then
        PASS_COUNT=$((PASS_COUNT + 1)); echo "    [PASS] ${name}"
    else
        FAIL_COUNT=$((FAIL_COUNT + 1)); echo "    [FAIL] ${name}${detail:+ — ${detail}}" >&2
    fi
    RESULTS+=("${status}|${name}|${detail}")
}

die() { echo "error: $*" >&2; exit 1; }

# ── 1. Preflight ───────────────────────────────────────────────────────────
echo "==> [1/6] preflight"

# NB: the docker/curl checks deliberately live BELOW the --dry-run early exit
# (search "dry-run tool checks"). A dry run starts no container and makes no
# request, so requiring a docker daemon to print a plan is wrong on its own
# terms — and it made the shell suite red on the macos-latest matrix leg, where
# the runners ship no docker CLI at all.
[[ -f "${COMPOSE_DIR}/.env" ]] || die "${COMPOSE_DIR}/.env not found; copy prod.env.example first"

# Caller-supplied values are captured FIRST. The prod .env also defines VERSION
# (prod.env.example ships ``VERSION=dev``), so sourcing it would silently
# clobber the release under test and we would verify the wrong image.
VERSION_REQUESTED="${VERSION:-}"
BASE_URL_REQUESTED="${BASE_URL:-}"

# Read only the values we need, in a SUBSHELL, so the full prod secret set is
# not exported into this shell and inherited by every child process
# (_env_guard.sh sources in a subshell for the same reason).
read_env() {  # read_env <KEY> -> normalized value on stdout (empty if unset)
    # Normalize the way docker compose's env-file parser does — bash `source`
    # keeps quote characters literally, so a perfectly valid
    #   CITEVYN_PUBLIC_HOST="citevyn.example.com"
    # would otherwise yield https://"citevyn.example.com" and fail every probe
    # (and a quoted/CRLF api key would put a stray quote in the bearer header).
    # Same peeling as _env_guard.sh's _strip(): trailing whitespace/CR first,
    # then one matched pair of surrounding quotes.
    local v
    v="$( set -a; . "${COMPOSE_DIR}/.env" >/dev/null 2>&1; set +a; printf '%s' "${!1:-}" )"
    while [[ "${v}" =~ [[:space:]]$ ]]; do v="${v%%[[:space:]]}"; done
    if [[ ${#v} -ge 2 ]]; then
        local f="${v:0:1}" l="${v: -1}"
        if [[ "${f}" == "'" && "${l}" == "'" ]] || [[ "${f}" == '"' && "${l}" == '"' ]]; then
            # NB: `${v:1:-1}` is bash 4.2+; on macOS's bash 3.2 it raises
            # "substring expression < 0", which would abort this function and
            # return an empty value (failing closed with a misleading "unset"
            # error). Compute the length explicitly so this stays 3.2-safe.
            v="${v:1:$((${#v} - 2))}"
        fi
    fi
    printf '%s' "${v}"
}
# An explicit env override wins over the compose .env. This is what makes
# --verify-only usable against a locally-run api whose key lives elsewhere
# (e.g. backend/.env) without editing the prod env file.
DEMO_KEY="${CITEVYN_DEMO_API_KEY:-$(read_env CITEVYN_DEMO_API_KEY)}"
PUBLIC_HOST="$(read_env CITEVYN_PUBLIC_HOST)"

VERSION="${VERSION_REQUESTED:-$(git describe --tags --exact-match 2>/dev/null || echo '')}"
if [[ -z "${PREV_VERSION:-}" ]]; then
    # Release-shaped tags ONLY. `v*` also matches throwaway drill tags — this
    # repo currently carries v0.9.1-drill / v0.9.2-drill / v0.10.0-drill — and
    # version:refname sorts them ABOVE the real v0.9.0, so the bare glob would
    # auto-select an unreviewed local commit as the rollback target. An explicit
    # PREV_VERSION= is still honoured verbatim, which is how the drill runs.
    PREV_VERSION="$(git tag --list 'v*' --sort=-version:refname \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
        | grep -v "^${VERSION}$" | head -1 || true)"
fi

# The api container publishes no host port — only caddy does (:80/:443). So the
# default probe target is the public host over HTTPS, not localhost:8000.
if [[ -n "${BASE_URL_REQUESTED}" ]]; then
    BASE_URL="${BASE_URL_REQUESTED}"
elif [[ -n "${PUBLIC_HOST}" ]]; then
    BASE_URL="https://${PUBLIC_HOST}"
else
    BASE_URL=""
fi
PRODUCT_AREA="${PRODUCT_AREA:-codex}"
# Must be a seeded ExactTerm row for PRODUCT_AREA (see probe 7).
EXACT_TERM="${EXACT_TERM:---model}"

echo "    version under test : ${VERSION:-<unset>}"
echo "    rollback target    : ${PREV_VERSION:-<none found>}"
echo "    probing            : ${BASE_URL:-<unset>}"

if [[ "${DRY_RUN}" == "1" ]]; then
    cat <<EOF
==> --dry-run: would run
      0. guard: clean tree, VERSION is a real tag, env is not a stub
      1. backup.sh                                   (safety net)
      2. git checkout ${VERSION:-<tag>} && VERSION=${VERSION:-<tag>} refresh.sh
      3. functional verify against ${BASE_URL:-<base-url>}
      4. drill A (data): backup.sh -> stop api/worker -> restore.sh -> re-verify
      5. drill B (code): rollback.sh ${PREV_VERSION:-<skipped>} + re-verify,
         but ONLY if ${PREV_VERSION:-<prev>} ships every migration ${VERSION:-<tag>} does;
         otherwise assert rollback.sh REFUSES and fail unless --data-rollback-only
      6. git checkout ${VERSION:-<tag>} && refresh.sh (roll forward) + re-verify
EOF
    exit 0
fi

# ── dry-run tool checks ────────────────────────────────────────────────────
# Below the --dry-run exit on purpose; see the note at the top of preflight.
command -v docker >/dev/null || die "docker not found on PATH"
command -v curl   >/dev/null || die "curl not found on PATH"

# ── Real-run guards. These apply only when we will MUTATE production. ──────
# --verify-only deploys nothing, so requiring a tagged release, a clean tree
# and a non-stub prod .env would be wrong there: its whole purpose is to probe
# an already-running stack (typically local or staging) from a work branch.
RETURN_REF=""
if [[ "${VERIFY_ONLY}" == "0" ]]; then
    [[ -n "${VERSION}" ]] || die "VERSION is unset and HEAD is not tagged; pass VERSION=vX.Y.Z"
    git rev-parse -q --verify "refs/tags/${VERSION}" >/dev/null \
        || die "VERSION='${VERSION}' is not an existing git tag (the gate deploys a tagged release, not a branch)"
    [[ -n "${BASE_URL}" ]] || die "BASE_URL is unset and CITEVYN_PUBLIC_HOST is empty in ${COMPOSE_DIR}/.env"
    [[ -n "${DEMO_KEY}" ]] || die "CITEVYN_DEMO_API_KEY is unset in ${COMPOSE_DIR}/.env"

    # A dirty tree must be caught BEFORE we redeploy production — otherwise we
    # ship uncommitted local edits and only discover it when the rollback
    # drill refuses.
    if [[ -n "$(git status --porcelain)" ]]; then
        git status --short >&2
        die "working tree is dirty; commit or stash before running the live gate"
    fi

    if [[ "${SKIP_ROLLBACK}" == "0" ]]; then
        [[ -n "${PREV_VERSION}" ]] \
            || die "no previous v* tag for the rollback drill; pass PREV_VERSION= or --skip-rollback-drill"
        # Must be checked HERE. Downstream, a non-existent ref lists no
        # migrations at all, which the drill-B check would read as "a different
        # migration generation" — reporting a typo'd tag as a migration boundary
        # and silently narrowing the gate.
        git rev-parse -q --verify "refs/tags/${PREV_VERSION}" >/dev/null \
            || die "PREV_VERSION='${PREV_VERSION}' is not an existing git tag"
    fi

    RETURN_REF="$(git rev-parse --abbrev-ref HEAD)"
    [[ "${RETURN_REF}" == "HEAD" ]] && RETURN_REF="$(git rev-parse HEAD)"

    # shellcheck source=infra/docker/scripts/_env_guard.sh
    source "${COMPOSE_DIR}/scripts/_env_guard.sh" "${COMPOSE_DIR}" \
        || die "env guard refused: refusing to run the live gate against a stub .env"
fi

# ── Provider-side budget (#153 Layer 5). FREE: a metadata read, no inference. ──
# A release must not proceed on an exhausted key — the stack would deploy, boot,
# pass /health, and then fail every actual question with a provider 402. The
# exit codes are distinguished deliberately: 1 means "checked, and it is low"
# (fatal), 2 means "could not check" (a warning, not a reason to block a deploy
# that may not even use OpenRouter).
if [[ "${VERIFY_ONLY}" == "0" ]]; then
    echo "==> [1b/6] provider-side budget check"
    "${REPO_ROOT}/scripts/check_budget.sh"
    case $? in
        0) record PASS "provider key has budget remaining" ;;
        2) echo "    [WARN] provider budget could not be checked (no key found)" >&2 ;;
        *) record FAIL "provider key budget" "below threshold or unreadable"
           die "refusing to deploy on an exhausted provider key (see docs/COST_CONTROLS.md §0)" ;;
    esac
fi

# shellcheck disable=SC2206  # deliberate word-splitting of operator-supplied flags
CURL=(curl --silent --show-error --max-time 30 ${CURL_OPTS:-})

# Auth headers are fed via --config on STDIN so the keys never appear in argv
# (visible to any user on the host via `ps aux`).
curl_demo() {  # curl_demo <curl-args...>  — adds demo bearer auth
    printf 'header = "Authorization: Bearer %s"\n' "${DEMO_KEY}" \
        | "${CURL[@]}" --config - "$@"
}

# ── Probe helpers ──────────────────────────────────────────────────────────
http_code_noauth() {  # http_code_noauth <method> <path>
    "${CURL[@]}" -o /dev/null -w '%{http_code}' -X "$1" "${BASE_URL}$2"
}

api_post() {  # api_post <path> <json> -> body on stdout
    curl_demo -X POST -H 'content-type: application/json' \
        --data "$2" "${BASE_URL}$1"
}

new_session() {  # -> session id on stdout (empty on failure)
    # The response key is `session_id`, NOT `id` (sessions.py returns
    # {request_id, session_id, expires_at}). Matching `"id"` would require a
    # quote immediately before `id`, which neither `"session_id"` nor
    # `"request_id"` contains — so it would never match and every dependent
    # probe would be skipped on a perfectly healthy stack.
    api_post /v1/sessions '{}' \
        | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
}

# ── The functional verify suite ────────────────────────────────────────────
verify_suite() {
    local phase="$1" code body

    echo "  -- verify (${phase}) --"

    # 1. Liveness.
    code="$(http_code_noauth GET /health)"
    [[ "${code}" == "200" ]] && record PASS "${phase}: GET /health" \
        || record FAIL "${phase}: GET /health" "http ${code}"

    # 2. Dependencies (db + redis reachable).
    code="$(http_code_noauth GET /health/dependencies)"
    [[ "${code}" == "200" ]] && record PASS "${phase}: GET /health/dependencies" \
        || record FAIL "${phase}: GET /health/dependencies" "http ${code}"

    # 3. Index health. A cold stack with NO index still returns 200 with
    #    status="pre_index" and active_index=null, so assert status=="ready"
    #    explicitly — merely finding the KEY would pass on an unindexed deploy.
    body="$("${CURL[@]}" "${BASE_URL}/health/index" 2>/dev/null)"
    if grep -q '"status"[[:space:]]*:[[:space:]]*"ready"' <<<"${body}"; then
        record PASS "${phase}: /health/index status=ready"
    else
        record FAIL "${phase}: /health/index status=ready" "got: ${body:0:140}"
    fi

    # 3b. Vector arm. Top-level `status` only answers "is there an active
    #     index" — it stays "ready" when the vector arm is DEAD (the #97 failure
    #     mode: NULL embeddings / embedder mismatch). The lexical arm alone can
    #     still satisfy the citation probe below, so without this assertion a
    #     half-broken retrieval stack passes the whole gate.
    if grep -q '"healthy"[[:space:]]*:[[:space:]]*true' <<<"${body}"; then
        record PASS "${phase}: /health/index vector arm healthy"
    else
        record FAIL "${phase}: /health/index vector arm healthy" "got: ${body:0:180}"
    fi

    # 4. Session creation.
    local session_id
    session_id="$(new_session)"
    if [[ -n "${session_id}" ]]; then
        record PASS "${phase}: POST /v1/sessions"
    else
        record FAIL "${phase}: POST /v1/sessions" "no session id returned"
        # Plain echo, not `record`: the FAIL above already fires, and counting a
        # non-probe would make this phase's pass/fail totals asymmetric with the
        # other phases in the summary.
        echo "    (probes 5-8 skipped for this phase — no session)" >&2
        return
    fi

    # 5. GROUNDED + CITED answer for an in-corpus question (§10 blocker 3).
    #    The `"citations":[{` shape correctly rejects an empty `"citations":[]`.
    body="$(api_post "/v1/sessions/${session_id}/messages" \
        '{"message":"How do I install the Codex CLI?"}')"
    if grep -q '"citations"[[:space:]]*:[[:space:]]*\[[[:space:]]*{' <<<"${body}"; then
        record PASS "${phase}: in-corpus question returns a CITED answer"
    else
        record FAIL "${phase}: in-corpus question returns a CITED answer" \
            "no citations in: ${body:0:160}"
    fi

    # 6. Refusal for an out-of-corpus question (§10 blocker 5 / guardrail).
    #    MUST assert `"no_answer": true` — every success response also CONTAINS
    #    the key as `"no_answer": false`, so a key-presence grep would pass on a
    #    hallucinated answer, i.e. exactly the regression this probe exists for.
    #    Uses a FRESH session so conversation memory cannot contextualise this
    #    question against the preceding Codex turn.
    local refusal_session
    refusal_session="$(new_session)"
    if [[ -z "${refusal_session}" ]]; then
        record FAIL "${phase}: out-of-corpus question is refused" "could not open a session"
    else
        body="$(api_post "/v1/sessions/${refusal_session}/messages" \
            '{"message":"What is the best laptop to buy in 2026?"}')"
        if grep -q '"no_answer"[[:space:]]*:[[:space:]]*true' <<<"${body}"; then
            record PASS "${phase}: out-of-corpus question is refused"
        else
            record FAIL "${phase}: out-of-corpus question is refused" \
                "expected no_answer=true, got: ${body:0:160}"
        fi
    fi

    # 7. Exact lookup (§10 blocker 4). `product_area` is REQUIRED; the response
    #    envelope key is `hits` (not `results`).
    #    The term MUST be a real `ExactTerm` row: exact_lookup matches on strict
    #    equality against ExactTerm.term_text, not substring over chunk text. The
    #    seed defines `--model` (codex) and `CLAUDE_API_RATE_LIMIT` (claude_api);
    #    a term that only appears inside chunk prose returns zero hits on a
    #    correctly-seeded stack.
    body="$(curl_demo -X POST -H 'content-type: application/json' \
        --data "{\"term\":\"${EXACT_TERM}\",\"product_area\":\"${PRODUCT_AREA}\"}" \
        "${BASE_URL}/v1/search/exact")"
    if grep -q '"hits"[[:space:]]*:[[:space:]]*\[[[:space:]]*{' <<<"${body}"; then
        record PASS "${phase}: exact lookup returns a hit"
    else
        record FAIL "${phase}: exact lookup returns a hit" "got: ${body:0:160}"
    fi

    # 8. Admin endpoints are protected (§10 blocker 7). `/v1/admin/index_versions`
    #    is a REAL route — probing a non-existent path would 404 before the auth
    #    dependency runs and would never actually exercise the guard.
    code="$(http_code_noauth GET /v1/admin/index_versions)"
    if [[ "${code}" == "401" || "${code}" == "403" ]]; then
        record PASS "${phase}: admin endpoint rejects an unauthenticated call (${code})"
    else
        record FAIL "${phase}: admin endpoint rejects an unauthenticated call" \
            "expected 401/403, got ${code}"
    fi
}

# The data-recovery drill and its crash-safety machinery live in a sourceable
# helper so tests/shell/ can exercise them with stubbed docker/backup/restore.
# This is the only code here that deliberately stops production, so it is the
# code most worth testing — see _drill_lib.sh for the contract it upholds.
# shellcheck source=infra/docker/scripts/_drill_lib.sh
source "${COMPOSE_DIR}/scripts/_drill_lib.sh"
install_drill_traps

deploy_at() {  # deploy_at <tag> — check out that tag's tree, then build+deploy it
    local tag="$1"
    # refresh.sh builds from the WORKING TREE, so the checkout is what actually
    # decides which code ships. Without it we would label the current branch
    # with a release tag.
    git checkout --quiet "${tag}" || return 1
    VERSION="${tag}" "${COMPOSE_DIR}/scripts/refresh.sh"
}

# ── --verify-only: run ONLY the probe suite against BASE_URL. ──────────────
# Mutates nothing: no backup, no deploy, no checkout, no rollback. This exists
# so the probe logic — the part most likely to be wrong, and the part static
# review cannot fully validate — can be exercised against a real running stack
# (local or staging) WITHOUT a production release. Use it to smoke-test the
# gate itself before trusting it on a real cut.
#
# MUST stay below the verify_suite definition: an earlier revision placed this
# block above it, so `verify_suite` was undefined, ZERO probes ran, FAIL_COUNT
# stayed 0 and the script printed "all probes passed" and exited 0 — a false
# green in the very tool built to prevent false greens. Hence also the
# zero-probe guard below.
if [[ "${VERIFY_ONLY}" == "1" ]]; then
    [[ -n "${BASE_URL}" ]] || die "--verify-only needs BASE_URL (or CITEVYN_PUBLIC_HOST)"
    [[ -n "${DEMO_KEY}" ]] || die "--verify-only needs CITEVYN_DEMO_API_KEY"
    echo "==> --verify-only: probing ${BASE_URL} (no deploy, no rollback)"
    verify_suite "verify-only"
    echo
    echo "════════════════════════════════════════════════════════════════"
    echo " verify-only summary — ${BASE_URL}"
    echo "════════════════════════════════════════════════════════════════"
    if [[ "${#RESULTS[@]}" -gt 0 ]]; then
        for row in "${RESULTS[@]}"; do
            IFS='|' read -r status name detail <<<"${row}"
            printf ' %-6s %s%s\n' "[${status}]" "${name}" "${detail:+ — ${detail}}"
        done
    fi
    echo "────────────────────────────────────────────────────────────────"
    echo " passed: ${PASS_COUNT}   failed: ${FAIL_COUNT}"
    # A run that recorded NOTHING is a broken harness, not a pass.
    if [[ "${#RESULTS[@]}" -eq 0 ]]; then
        echo " RESULT: ✗ no probes executed — the harness is broken, not the stack."
        exit 1
    fi
    if [[ "${FAIL_COUNT}" -gt 0 ]]; then
        echo " RESULT: ✗ probes FAILED"
        exit 1
    fi
    echo " RESULT: ✓ all probes passed (NOTE: this is not the release gate —"
    echo "         it proves the probes, not the deploy/rollback path)."
    exit 0
fi

# ── 2. Safety backup ───────────────────────────────────────────────────────
echo "==> [2/6] safety backup before touching the live stack"
if "${COMPOSE_DIR}/scripts/backup.sh"; then
    record PASS "pre-deploy backup"
else
    record FAIL "pre-deploy backup" "backup.sh failed"
    die "refusing to deploy without a restorable backup"
fi

# ── 3. Deploy the target version ───────────────────────────────────────────
echo "==> [3/6] deploying ${VERSION}"
if deploy_at "${VERSION}"; then
    record PASS "deploy ${VERSION}"
else
    record FAIL "deploy ${VERSION}" "checkout or refresh.sh failed"
    echo "FATAL: deploy failed; the stack may be mid-roll. Investigate: make logs" >&2
    exit 1
fi

# ── 4. Verify the deployment ───────────────────────────────────────────────
echo "==> [4/6] verifying the deployed release"
verify_suite "deployed ${VERSION}"

# ── 5/6. Rollback drills + roll forward ────────────────────────────────────
ENDED_ON="${VERSION}"
DATA_ROLLBACK_PROVEN=0
CODE_ROLLBACK_PROVEN=0
if [[ "${SKIP_ROLLBACK}" == "1" ]]; then
    echo "==> [5/6] rollback drills SKIPPED (--skip-rollback-drill)"
    echo "    NOTE: RELEASE_PLAN §10 blocker 9 is NOT satisfied by this run." >&2
else
    echo "==> [5/6] rollback drills"

    # ── Drill A: data-recovery. Always runnable, and it is the only rollback
    #    that works across a forward-only migration boundary.
    if data_restore_drill; then
        record PASS "drill A: backup -> pg_restore -> stack healthy"
        DATA_ROLLBACK_PROVEN=1
        verify_suite "post-restore ${VERSION}"
    else
        record FAIL "drill A: backup -> pg_restore -> stack healthy" \
            "the documented §4.2 recovery path does not work"
    fi

    # ── Drill B: code rollback to PREV_VERSION. Only attempted when it CAN
    #    succeed. `migrations_missing_at` compares the two TREES; a non-empty
    #    result means the live DB is stamped at a revision PREV_VERSION does not
    #    ship, so `alembic upgrade head` at that tag cannot resolve the graph.
    # shellcheck source=infra/docker/scripts/_migration_gen.sh
    source "${COMPOSE_DIR}/scripts/_migration_gen.sh"
    _prev_missing="$(migrations_missing_at "${PREV_VERSION}" "${VERSION}" | tr '\n' ' ')"

    if [[ -n "${_prev_missing}" ]]; then
        echo "  -- drill B: code rollback NOT POSSIBLE to ${PREV_VERSION} --"
        echo "     ${PREV_VERSION} is missing applied migration(s): ${_prev_missing}"
        # What CAN be proven here is the fail-fast contract (#195): the incident
        # tool must refuse before it touches production, not die inside an
        # alembic container half-way through. --dry-run mutates nothing.
        #
        # Assert the refusal TEXT, not just a non-zero exit. Exit-status-only
        # would record a PASS for any failure whatsoever — rollback.sh missing
        # (127), not executable (126), a syntax error, _migration_gen.sh failing
        # to source, an unknown-flag exit 2. Delete the migration guard entirely
        # and replace it with a bare `exit 1` and an exit-status-only check still
        # reports the fail-fast contract as proven. So we require both the
        # refusal sentence AND at least one of the specific missing revisions,
        # which is what proves the guard reasoned about migrations at all.
        # --base-ref is REQUIRED here: step 2 checked out VERSION, so we are on a
        # detached HEAD and rollback.sh refuses to infer the deployed tree from
        # it. We can name it, because we just deployed it.
        _refusal="$( "${COMPOSE_DIR}/scripts/rollback.sh" "${PREV_VERSION}" --base-ref "${VERSION}" --dry-run 2>&1 )" && _refused_rc=0 || _refused_rc=$?
        _first_missing="${_prev_missing%% *}"
        if [[ "${_refused_rc}" == "0" ]]; then
            record FAIL "rollback.sh refuses a cross-migration target" \
                "it did NOT refuse — it would die mid-deploy inside a container"
        elif ! printf '%s' "${_refusal}" | grep -qF "cannot roll back to"; then
            record FAIL "rollback.sh refuses a cross-migration target" \
                "it exited ${_refused_rc} but not with the migration refusal; it may be failing for an unrelated reason"
        elif ! printf '%s' "${_refusal}" | grep -qF "${_first_missing}"; then
            record FAIL "rollback.sh refuses a cross-migration target" \
                "it refused but never named the missing revision ${_first_missing}"
        else
            record PASS "rollback.sh refuses a cross-migration target (fail-fast)"
        fi

        if [[ "${DATA_ROLLBACK_ONLY}" == "1" ]]; then
            # Explicitly narrowed scope. Not a pass for drill B — it is recorded
            # nowhere as a pass, and the summary says so in full.
            echo "     [NOT PROVEN] code rollback to ${PREV_VERSION} was not exercised" >&2
            echo "                  (--data-rollback-only). Blocker 9 stays PARTIAL." >&2
        else
            record FAIL "drill B: code rollback to ${PREV_VERSION}" \
                "target is a different migration generation; pass PREV_VERSION=<same-generation tag>, or --data-rollback-only to gate the data path alone"
        fi
    else
        echo "  -- drill B: code rollback -> ${PREV_VERSION} (same migration generation) --"
        # Pessimistic BEFORE the mutation, not optimistic after it. rollback.sh
        # checks out the target and rebuilds; if refresh.sh dies part-way the
        # stack is mid-roll and belongs to neither release. Leaving ENDED_ON at
        # VERSION through that window is what suppressed the loud prod-state
        # warning on exactly the paths that needed it.
        ENDED_ON="unknown (interrupted mid-rollback)"
        if "${COMPOSE_DIR}/scripts/rollback.sh" "${PREV_VERSION}" --base-ref "${VERSION}"; then
            record PASS "drill B: rollback to ${PREV_VERSION}"
            ENDED_ON="${PREV_VERSION}"
            verify_suite "rolled-back ${PREV_VERSION}"
            CODE_ROLLBACK_PROVEN=1
        else
            record FAIL "drill B: rollback to ${PREV_VERSION}" "rollback.sh failed"
        fi

        echo "==> [6/6] rolling forward to ${VERSION}"
        # Hard-fail on a bad checkout. Falling back to another ref here would
        # redeploy the PREVIOUS release while reporting the roll-forward as green.
        ENDED_ON="unknown (interrupted mid-roll-forward)"
        if deploy_at "${VERSION}"; then
            record PASS "roll forward to ${VERSION}"
            ENDED_ON="${VERSION}"
            verify_suite "restored ${VERSION}"
        else
            record FAIL "roll forward to ${VERSION}" "checkout or refresh.sh failed"
            CODE_ROLLBACK_PROVEN=0
        fi
    fi
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════"
echo " deploy-verify summary — version ${VERSION}"
echo "════════════════════════════════════════════════════════════════"
# "${RESULTS[@]}" on an empty array trips `set -u` on bash 3.2; guard the count.
if [[ "${#RESULTS[@]}" -gt 0 ]]; then
    for row in "${RESULTS[@]}"; do
        IFS='|' read -r status name detail <<<"${row}"
        printf ' %-6s %s%s\n' "[${status}]" "${name}" "${detail:+ — ${detail}}"
    done
fi
echo "────────────────────────────────────────────────────────────────"
echo " passed: ${PASS_COUNT}   failed: ${FAIL_COUNT}"

# Production state is reported UNCONDITIONALLY, and from a real probe rather
# than from a variable that tracks intent. This block used to fire only when
# ENDED_ON != VERSION, so a drill that stopped the api and never restarted it
# ended the run with production down and NOT ONE LINE saying so (#195).
_api_state="$(docker inspect --format '{{.State.Status}}/{{.State.Health.Status}}' citevyn-api 2>/dev/null || echo 'absent/unknown')"
echo "────────────────────────────────────────────────────────────────"
case "${_api_state}" in
    running/healthy)
        echo " production: api container UP and healthy, serving ${ENDED_ON}" ;;
    *)
        echo
        echo " ****************************************************************"
        echo " ** WARNING: production is NOT serving. citevyn-api is"
        echo " **          ${_api_state} (status/health)."
        echo " ** Recover with:"
        echo " **     cd ${COMPOSE_DIR} && docker compose --profile prod up -d api"
        echo " ****************************************************************" ;;
esac

if [[ "${ENDED_ON}" != "${VERSION}" ]]; then
    echo
    echo " ****************************************************************"
    echo " ** WARNING: production is running ${ENDED_ON}, NOT ${VERSION}."
    echo " ** The roll-forward did not complete. Re-run:"
    echo " **     make rollback TAG=${VERSION}"
    echo " ****************************************************************"
fi

echo " (git: you are on a detached HEAD; return with: git checkout ${RETURN_REF})"

if [[ "${FAIL_COUNT}" -gt 0 ]]; then
    echo " RESULT: ✗ GATE FAILED — do not tag/announce this release."
    exit 1
fi

if [[ "${SKIP_ROLLBACK}" == "1" ]]; then
    echo " RESULT: ✓ verify passed, but the rollback drills were skipped."
    echo "         RELEASE_PLAN §10 blocker 9 remains OPEN."
    exit 0
fi

# Never claim more than was actually exercised. The whole point of #195 is that
# this gate previously reported "rollback proven" for a path that could not run.
echo " rollback coverage:"
if [[ "${DATA_ROLLBACK_PROVEN}" == "1" ]]; then
    echo "   ✓ data-recovery rollback (RUNBOOK §4.2) — PROVEN end to end"
else
    echo "   ✗ data-recovery rollback (RUNBOOK §4.2) — NOT proven"
fi
if [[ "${CODE_ROLLBACK_PROVEN}" == "1" ]]; then
    echo "   ✓ code rollback to ${PREV_VERSION} + roll forward — PROVEN end to end"
else
    echo "   ✗ code rollback to ${PREV_VERSION} — NOT proven (different migration"
    echo "     generation; a code-only rollback across that boundary is impossible)"
fi

if [[ "${DATA_ROLLBACK_PROVEN}" == "1" && "${CODE_ROLLBACK_PROVEN}" == "1" ]]; then
    echo " RESULT: ✓ GATE PASSED — deploy verified and BOTH rollback paths proven."
    echo "         RELEASE_PLAN §10 blocker 9 satisfied on $(date -u '+%Y-%m-%dT%H:%M:%SZ')."
    exit 0
fi

echo " RESULT: ✓ GATE PASSED (NARROWED) — deploy verified and the data-recovery"
echo "         rollback proven. Code rollback to ${PREV_VERSION} was NOT exercised."
echo "         RELEASE_PLAN §10 blocker 9 is PARTIAL, not closed."
