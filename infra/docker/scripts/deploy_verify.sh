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
#   • deploy the target version onto the live stack
#   • liveness + dependency + index health
#   • FUNCTIONAL proof against the running deployment:
#       - a grounded, CITED answer for an in-corpus question   (§10 blocker 3)
#       - a refusal for an out-of-corpus question              (§10 blocker 5)
#       - exact lookup returns a hit                           (§10 blocker 4)
#       - admin endpoints reject an unauthenticated call       (§10 blocker 7)
#   • a real ROLLBACK DRILL to the previous tag + re-verify    (§10 blocker 9)
#   • roll forward to the target again + re-verify
#
# SCOPE — what this gate does NOT cover (deliberately):
#   It is NOT the full demo/release regression suite. The 50-case golden suite
#   (`make golden`), the judged answer-quality eval (`make eval`), Playwright UI
#   e2e (`make e2e`), lint/typecheck/unit tests (`make ci`) and the docs/hygiene
#   items all live in docs/DEMO_CHECKLIST.md and run BEFORE the cut, mostly in
#   CI against a hermetic stack. This script answers a narrower question:
#   "is the thing we just deployed actually serving correct answers, and can we
#   get back?" A deploy that boots but cannot answer is a FAILED deploy — which
#   is why the functional probes above are in scope even though the exhaustive
#   quality suites are not.
#
# Usage:
#   make deploy-verify                              # target=current tag, prev=previous tag
#   VERSION=v0.10.0 PREV_VERSION=v0.9.0 make deploy-verify
#   ./scripts/deploy_verify.sh --skip-rollback-drill  # deploy + verify only
#   ./scripts/deploy_verify.sh --dry-run              # print the plan, change nothing
#
# Env:
#   VERSION        release to deploy+verify   (default: tag at HEAD, else "dev")
#   PREV_VERSION   rollback drill target      (default: previous v* tag)
#   BASE_URL       where to probe             (default: https://<CITEVYN_PUBLIC_HOST> or http://localhost:8000)
#   CURL_OPTS      extra curl flags           (e.g. "-k" for a self-signed cert)
#
# Exit codes: 0 = gate PASSED, non-zero = gate FAILED (details in the summary).
# ────────────────────────────────────────────────────────────────────────────
set -uo pipefail   # NOTE: not -e; probes are allowed to fail so we can report them all.

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

SKIP_ROLLBACK=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-rollback-drill) SKIP_ROLLBACK=1; shift ;;
        --dry-run)             DRY_RUN=1; shift ;;
        -h|--help)             sed -n '2,46p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "error: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

cd "${REPO_ROOT}"

# ── Result tracking ────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

record() {  # record <PASS|FAIL> <name> [detail]
    local status="$1" name="$2" detail="${3:-}"
    if [[ "${status}" == "PASS" ]]; then
        PASS_COUNT=$((PASS_COUNT + 1))
        echo "    [PASS] ${name}"
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        echo "    [FAIL] ${name}${detail:+ — ${detail}}" >&2
    fi
    RESULTS+=("${status}|${name}|${detail}")
}

die() { echo "error: $*" >&2; exit 1; }

# ── Preflight ──────────────────────────────────────────────────────────────
echo "==> [1/6] preflight"

command -v docker >/dev/null || die "docker not found on PATH"
command -v curl   >/dev/null || die "curl not found on PATH"
[[ -f "${COMPOSE_DIR}/.env" ]] || die "${COMPOSE_DIR}/.env not found; copy prod.env.example first"

# Resolve the version under test and the rollback target.
VERSION="${VERSION:-$(git describe --tags --exact-match 2>/dev/null || echo dev)}"
if [[ -z "${PREV_VERSION:-}" ]]; then
    PREV_VERSION="$(git tag --list 'v*' --sort=-version:refname \
        | grep -v "^${VERSION}$" | head -1 || true)"
fi

# Read the public host + API keys from the prod env WITHOUT echoing secrets.
# shellcheck disable=SC1091
set -a; . "${COMPOSE_DIR}/.env"; set +a
BASE_URL="${BASE_URL:-${CITEVYN_PUBLIC_URL:-http://localhost:8000}}"
DEMO_KEY="${CITEVYN_DEMO_API_KEY:-}"
ADMIN_KEY="${CITEVYN_ADMIN_API_KEY:-}"

echo "    version under test : ${VERSION}"
echo "    rollback target    : ${PREV_VERSION:-<none found>}"
echo "    probing            : ${BASE_URL}"
if [[ "${SKIP_ROLLBACK}" == "0" && -z "${PREV_VERSION}" && "${DRY_RUN}" == "0" ]]; then
    die "no previous v* tag found for the rollback drill; pass PREV_VERSION= or --skip-rollback-drill"
fi

if [[ "${DRY_RUN}" == "1" ]]; then
    cat <<EOF
==> --dry-run: would run
      1. backup.sh                            (safety net)
      2. VERSION=${VERSION} refresh.sh        (deploy target)
      3. functional verify against ${BASE_URL}
      4. rollback.sh ${PREV_VERSION:-<skipped>} + re-verify
      5. VERSION=${VERSION} refresh.sh        (roll forward) + re-verify
EOF
    exit 0
fi

# Secrets are only required for a REAL run — checked after --dry-run so the plan
# stays previewable on a box that has no production credentials.
[[ -n "${DEMO_KEY}" ]] || die "CITEVYN_DEMO_API_KEY is unset in ${COMPOSE_DIR}/.env"

# shellcheck source=infra/docker/scripts/_env_guard.sh
source "${COMPOSE_DIR}/scripts/_env_guard.sh" "${COMPOSE_DIR}" \
    || die "env guard refused: refusing to run the live gate against a stub .env"

CURL=(curl --silent --show-error --max-time 30 ${CURL_OPTS:-})

# ── Probe helpers ──────────────────────────────────────────────────────────
http_code() {  # http_code <method> <path> [data] [extra-header...]
    local method="$1" path="$2" data="${3:-}"; shift 3 || shift 2
    if [[ -n "${data}" ]]; then
        "${CURL[@]}" -o /dev/null -w '%{http_code}' -X "${method}" \
            -H 'content-type: application/json' "$@" \
            --data "${data}" "${BASE_URL}${path}"
    else
        "${CURL[@]}" -o /dev/null -w '%{http_code}' -X "${method}" "$@" "${BASE_URL}${path}"
    fi
}

api_post() {  # api_post <path> <json> -> body on stdout
    "${CURL[@]}" -X POST -H 'content-type: application/json' \
        -H "x-api-key: ${DEMO_KEY}" --data "$2" "${BASE_URL}$1"
}

# ── The functional verify suite ────────────────────────────────────────────
# $1 = a label so the summary distinguishes the pre/post-rollback runs.
verify_suite() {
    local phase="$1"
    echo "  -- verify (${phase}) --"

    # 1. Liveness.
    local code
    code="$(http_code GET /health)"
    [[ "${code}" == "200" ]] && record PASS "${phase}: GET /health" \
        || record FAIL "${phase}: GET /health" "http ${code}"

    # 2. Dependencies (db + redis reachable).
    code="$(http_code GET /health/dependencies)"
    [[ "${code}" == "200" ]] && record PASS "${phase}: GET /health/dependencies" \
        || record FAIL "${phase}: GET /health/dependencies" "http ${code}"

    # 3. Index health — a deployed stack with a dead vector arm still "boots".
    local index_body
    index_body="$("${CURL[@]}" "${BASE_URL}/health/index" 2>/dev/null)"
    if grep -q '"active_index"\|"vector_arm"\|"index_version"' <<<"${index_body}"; then
        record PASS "${phase}: GET /health/index reports an active index"
    else
        record FAIL "${phase}: GET /health/index" "unexpected body: ${index_body:0:120}"
    fi

    # 4. Session creation.
    local session_body session_id
    session_body="$(api_post /v1/sessions '{}')"
    session_id="$(sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<<"${session_body}" | head -1)"
    if [[ -n "${session_id}" ]]; then
        record PASS "${phase}: POST /v1/sessions"
    else
        record FAIL "${phase}: POST /v1/sessions" "no session id in: ${session_body:0:120}"
        return  # everything below needs a session
    fi

    # 5. GROUNDED + CITED answer for an in-corpus question (§10 blocker 3).
    #    The corpus ships Claude Code / Codex / Gemini docs; this question is
    #    covered by the shipped sources and must answer WITH a citation.
    local ans
    ans="$(api_post "/v1/sessions/${session_id}/messages" \
        '{"content":"How do I install the Codex CLI?","channel":"chat"}')"
    if grep -q '"citations"[[:space:]]*:[[:space:]]*\[[[:space:]]*{' <<<"${ans}"; then
        record PASS "${phase}: in-corpus question returns a CITED answer"
    else
        record FAIL "${phase}: in-corpus question returns a CITED answer" \
            "no citations in: ${ans:0:160}"
    fi

    # 6. Refusal for an out-of-corpus question (§10 blocker 5 / guardrail).
    local refusal
    refusal="$(api_post "/v1/sessions/${session_id}/messages" \
        '{"content":"What is the best laptop to buy in 2026?","channel":"chat"}')"
    if grep -q '"no_answer"\|"unsupported"' <<<"${refusal}"; then
        record PASS "${phase}: out-of-corpus question is refused"
    else
        record FAIL "${phase}: out-of-corpus question is refused" \
            "expected a refusal, got: ${refusal:0:160}"
    fi

    # 7. Exact lookup (§10 blocker 4).
    local exact
    exact="$("${CURL[@]}" -X POST -H 'content-type: application/json' \
        -H "x-api-key: ${DEMO_KEY}" \
        --data '{"term":"OPENAI_API_KEY"}' "${BASE_URL}/v1/search/exact")"
    if grep -q '"results"[[:space:]]*:[[:space:]]*\[[[:space:]]*{' <<<"${exact}"; then
        record PASS "${phase}: exact lookup returns a hit"
    else
        record FAIL "${phase}: exact lookup returns a hit" "got: ${exact:0:160}"
    fi

    # 8. Admin endpoints are protected (§10 blocker 7). No key => must NOT be 200.
    code="$(http_code GET /v1/admin/sources)"
    if [[ "${code}" == "401" || "${code}" == "403" ]]; then
        record PASS "${phase}: admin endpoint rejects an unauthenticated call (${code})"
    else
        record FAIL "${phase}: admin endpoint rejects an unauthenticated call" \
            "expected 401/403, got ${code}"
    fi
}

# ── 2. Safety backup ───────────────────────────────────────────────────────
echo "==> [2/6] safety backup before touching the live stack"
if "${COMPOSE_DIR}/scripts/backup.sh"; then
    record PASS "pre-deploy backup"
else
    record FAIL "pre-deploy backup" "backup.sh failed — aborting"
    echo "FATAL: refusing to deploy without a restorable backup." >&2
    exit 1
fi

# ── 3. Deploy the target version ───────────────────────────────────────────
echo "==> [3/6] deploying ${VERSION}"
if VERSION="${VERSION}" "${COMPOSE_DIR}/scripts/refresh.sh"; then
    record PASS "deploy ${VERSION}"
else
    record FAIL "deploy ${VERSION}" "refresh.sh failed"
    echo "FATAL: deploy failed; stack may be mid-roll. Investigate with: make logs" >&2
    exit 1
fi

# ── 4. Verify the deployment ───────────────────────────────────────────────
echo "==> [4/6] verifying the deployed release"
verify_suite "deployed ${VERSION}"

# ── 5. Rollback drill ──────────────────────────────────────────────────────
if [[ "${SKIP_ROLLBACK}" == "1" ]]; then
    echo "==> [5/6] rollback drill SKIPPED (--skip-rollback-drill)"
    echo "    NOTE: RELEASE_PLAN §10 blocker 9 is NOT satisfied by this run." >&2
else
    echo "==> [5/6] rollback drill -> ${PREV_VERSION}"
    if "${COMPOSE_DIR}/scripts/rollback.sh" "${PREV_VERSION}"; then
        record PASS "rollback to ${PREV_VERSION}"
        verify_suite "rolled-back ${PREV_VERSION}"
    else
        record FAIL "rollback to ${PREV_VERSION}" "rollback.sh failed"
    fi

    # ── 6. Roll forward again ──────────────────────────────────────────────
    echo "==> [6/6] rolling forward to ${VERSION}"
    git checkout --quiet "${VERSION}" 2>/dev/null || git checkout --quiet main
    if VERSION="${VERSION}" "${COMPOSE_DIR}/scripts/refresh.sh"; then
        record PASS "roll forward to ${VERSION}"
        verify_suite "restored ${VERSION}"
    else
        record FAIL "roll forward to ${VERSION}" "refresh.sh failed — STACK MAY BE ON THE OLD RELEASE"
    fi
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════"
echo " deploy-verify summary — version ${VERSION}"
echo "════════════════════════════════════════════════════════════════"
# ${RESULTS[@]} on an empty array trips `set -u` on bash 3.2, so guard the count.
if [[ "${#RESULTS[@]}" -gt 0 ]]; then
    for row in "${RESULTS[@]}"; do
        IFS='|' read -r status name detail <<<"${row}"
        printf ' %-6s %s%s\n' "[${status}]" "${name}" "${detail:+ — ${detail}}"
    done
fi
echo "────────────────────────────────────────────────────────────────"
echo " passed: ${PASS_COUNT}   failed: ${FAIL_COUNT}"

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
