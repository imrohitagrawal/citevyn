#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# check_budget.sh — read the OpenRouter key's remaining balance (#153 Layer 5).
#
# This is the check that CAUGHT the problem it exists to prevent: a routine look
# at GET /api/v1/key showed the key at 96.6% consumed ($1.06 of $1.10), which no
# application-side layer could have known — the app can only see what IT spent,
# not what the key has spent in total.
#
# FREE. GET /api/v1/key is a metadata read: no model is invoked, no tokens are
# billed. Safe to run in a deploy gate, in CI, or in a loop.
#
# Usage:
#   make budget                       # warn below the default threshold
#   MIN_REMAINING_USD=5 make budget   # require at least $5 of headroom
#
# Exit codes:
#   0  key has at least MIN_REMAINING_USD remaining (or has no limit set)
#   1  key is below the threshold, or the API said something we cannot parse
#   2  misconfigured (no key available) — distinct so a gate can treat "cannot
#      check" differently from "checked and it is low"
# ────────────────────────────────────────────────────────────────────────────
set -uo pipefail

MIN_REMAINING_USD="${MIN_REMAINING_USD:-1}"
API="https://openrouter.ai/api/v1/key"

# Search order: the environment, then the PROD env file, then the dev one. A
# deploy gate runs against the prod stack, whose key lives in infra/docker/.env;
# a developer running `make budget` by hand has it in backend/.env. Checking only
# the latter would make the gate silently unable to check on exactly the host that
# matters.
KEY="${CITEVYN_OPENROUTER_API_KEY:-}"
for ENV_FILE in infra/docker/.env backend/.env; do
    [[ -n "${KEY}" ]] && break
    [[ -f "${ENV_FILE}" ]] || continue
    # Read in a SUBSHELL so the whole secret set is not exported into this shell
    # and inherited by every child process (same pattern as _env_guard.sh).
    KEY="$( set -a; . "${ENV_FILE}" >/dev/null 2>&1; set +a; printf '%s' "${CITEVYN_OPENROUTER_API_KEY:-}" )"
done

if [[ -z "${KEY}" ]]; then
    echo "budget: CITEVYN_OPENROUTER_API_KEY is not set (checked env, infra/docker/.env, backend/.env)." >&2
    echo "budget: cannot check the provider-side balance." >&2
    exit 2
fi

# --fail-with-body so a 401/429 is an error here rather than a body we misparse.
#
# The Authorization header is fed to curl via ``--config -`` on STDIN rather
# than ``-H``, so the key never appears in curl's argv — where any user on the
# host can read it out of ``ps aux`` (and where it lands in shell history and
# process accounting). Same pattern as ``curl_demo`` in
# infra/docker/scripts/deploy_verify.sh. ``set -o pipefail`` is already on, so
# ``$?`` after the pipeline reflects a curl failure even though curl is last.
RESPONSE="$(printf 'header = "Authorization: Bearer %s"\n' "${KEY}" \
    | curl --silent --show-error --max-time 20 --fail-with-body \
        --config - "${API}" 2>&1)"
CURL_RC=$?
if [[ ${CURL_RC} -ne 0 ]]; then
    # Never echo RESPONSE unfiltered — an error body can quote the request,
    # including the Authorization header on some proxies.
    echo "budget: could not reach ${API} (curl exit ${CURL_RC})." >&2
    exit 1
fi

# python3 rather than jq: jq is not a declared dependency of this repo, and a
# missing-tool failure in a deploy gate reads as "budget exhausted" to an
# operator skimming output. python3 is already required by the backend.
# The response is passed via the ENVIRONMENT, not stdin: `python3 -` already
# consumes stdin for the script itself, so a here-string would silently replace
# the program with the JSON.
BUDGET_RESPONSE="${RESPONSE}" python3 - "$MIN_REMAINING_USD" <<'PY'
import json, os, sys

threshold = float(sys.argv[1])
try:
    data = json.loads(os.environ["BUDGET_RESPONSE"])["data"]
except Exception as exc:  # noqa: BLE001
    print(f"budget: could not parse the key response: {exc}", file=sys.stderr)
    sys.exit(1)

usage = float(data.get("usage") or 0.0)
limit = data.get("limit")

if limit is None:
    # No provider-side cap at all. NOT a pass: COST_CONTROLS.md §0 calls the
    # provider cap the only layer app code cannot bypass, so its absence is the
    # single most important thing this check can report.
    print(f"budget: usage ${usage:.4f}, NO provider-side limit set.")
    print("budget: WARNING — §0 requires a DAILY per-key limit before going public.")
    sys.exit(1)

limit = float(limit)
remaining = limit - usage
pct = (usage / limit * 100) if limit else 0.0
print(f"budget: usage ${usage:.4f} of ${limit:.2f} ({pct:.1f}% used), ${remaining:.4f} remaining")

if remaining < threshold:
    print(f"budget: FAIL — below the ${threshold:.2f} threshold.", file=sys.stderr)
    sys.exit(1)
if pct >= 85:
    print("budget: WARNING — over 85% of the key limit consumed.", file=sys.stderr)
sys.exit(0)
PY
