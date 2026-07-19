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
#   • a real ROLLBACK DRILL to the previous tag + re-verify    (§10 blocker 9)
#   • roll forward to the target again + re-verify
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-rollback-drill) SKIP_ROLLBACK=1; shift ;;
        --dry-run)             DRY_RUN=1; shift ;;
        -h|--help)             sed -n '2,43p' "${BASH_SOURCE[0]}"; exit 0 ;;
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

command -v docker >/dev/null || die "docker not found on PATH"
command -v curl   >/dev/null || die "curl not found on PATH"
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
            v="${v:1:-1}"
        fi
    fi
    printf '%s' "${v}"
}
DEMO_KEY="$(read_env CITEVYN_DEMO_API_KEY)"
PUBLIC_HOST="$(read_env CITEVYN_PUBLIC_HOST)"

VERSION="${VERSION_REQUESTED:-$(git describe --tags --exact-match 2>/dev/null || echo '')}"
if [[ -z "${PREV_VERSION:-}" ]]; then
    PREV_VERSION="$(git tag --list 'v*' --sort=-version:refname \
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
      4. rollback.sh ${PREV_VERSION:-<skipped>} + re-verify
      5. git checkout ${VERSION:-<tag>} && refresh.sh (roll forward) + re-verify
EOF
    exit 0
fi

# ── Real-run guards. Everything below can mutate production. ───────────────
[[ -n "${VERSION}" ]] || die "VERSION is unset and HEAD is not tagged; pass VERSION=vX.Y.Z"
git rev-parse -q --verify "refs/tags/${VERSION}" >/dev/null \
    || die "VERSION='${VERSION}' is not an existing git tag (the gate deploys a tagged release, not a branch)"
[[ -n "${BASE_URL}" ]] || die "BASE_URL is unset and CITEVYN_PUBLIC_HOST is empty in ${COMPOSE_DIR}/.env"
[[ -n "${DEMO_KEY}" ]] || die "CITEVYN_DEMO_API_KEY is unset in ${COMPOSE_DIR}/.env"

# A dirty tree must be caught BEFORE we redeploy production — otherwise we ship
# uncommitted local edits and only discover it when the rollback drill refuses.
if [[ -n "$(git status --porcelain)" ]]; then
    git status --short >&2
    die "working tree is dirty; commit or stash before running the live gate"
fi

if [[ "${SKIP_ROLLBACK}" == "0" ]]; then
    [[ -n "${PREV_VERSION}" ]] \
        || die "no previous v* tag for the rollback drill; pass PREV_VERSION= or --skip-rollback-drill"
fi

RETURN_REF="$(git rev-parse --abbrev-ref HEAD)"
[[ "${RETURN_REF}" == "HEAD" ]] && RETURN_REF="$(git rev-parse HEAD)"

# shellcheck source=infra/docker/scripts/_env_guard.sh
source "${COMPOSE_DIR}/scripts/_env_guard.sh" "${COMPOSE_DIR}" \
    || die "env guard refused: refusing to run the live gate against a stub .env"

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

deploy_at() {  # deploy_at <tag> — check out that tag's tree, then build+deploy it
    local tag="$1"
    # refresh.sh builds from the WORKING TREE, so the checkout is what actually
    # decides which code ships. Without it we would label the current branch
    # with a release tag.
    git checkout --quiet "${tag}" || return 1
    VERSION="${tag}" "${COMPOSE_DIR}/scripts/refresh.sh"
}

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

# ── 5/6. Rollback drill + roll forward ─────────────────────────────────────
ENDED_ON="${VERSION}"
if [[ "${SKIP_ROLLBACK}" == "1" ]]; then
    echo "==> [5/6] rollback drill SKIPPED (--skip-rollback-drill)"
    echo "    NOTE: RELEASE_PLAN §10 blocker 9 is NOT satisfied by this run." >&2
else
    echo "==> [5/6] rollback drill -> ${PREV_VERSION}"
    if "${COMPOSE_DIR}/scripts/rollback.sh" "${PREV_VERSION}"; then
        record PASS "rollback to ${PREV_VERSION}"
        ENDED_ON="${PREV_VERSION}"
        verify_suite "rolled-back ${PREV_VERSION}"
    else
        record FAIL "rollback to ${PREV_VERSION}" "rollback.sh failed"
    fi

    echo "==> [6/6] rolling forward to ${VERSION}"
    # Hard-fail on a bad checkout. Falling back to another ref here would
    # redeploy the PREVIOUS release while reporting the roll-forward as green.
    if deploy_at "${VERSION}"; then
        record PASS "roll forward to ${VERSION}"
        ENDED_ON="${VERSION}"
        verify_suite "restored ${VERSION}"
    else
        record FAIL "roll forward to ${VERSION}" "checkout or refresh.sh failed"
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
    echo " RESULT: ✓ verify passed, but the rollback drill was skipped."
    echo "         RELEASE_PLAN §10 blocker 9 remains OPEN."
    exit 0
fi

echo " RESULT: ✓ GATE PASSED — deploy verified and rollback proven."
echo "         RELEASE_PLAN §10 blocker 9 satisfied on $(date -u '+%Y-%m-%dT%H:%M:%SZ')."
