#!/usr/bin/env bash
# End-to-end smoke test for the CiteVyn backend.
#
# Brings up Postgres via docker compose, applies migrations, seeds
# users + the demo catalog, starts uvicorn in the background, asserts
# GET /health reports "healthy", and tears down the stack. Exits 0 on
# success, non-zero on any failure.
#
# NB this comment used to claim the script also "posts a session and a
# question, asserts the response carries a grounded cited answer". It
# does not, and never did below the /health check -- the same
# phantom-claim defect as the endpoints fixed in #217. Asking is worth
# adding here; until it is, this comment describes what runs.
#
# Run from anywhere; the script resolves its own repo root.
# Requirements: docker, curl, jq, uv.
#
# Every backend step (migrate, both seeds, the server) loads the same env file
# through scripts/with_env.py, so the data is seeded and served with the same
# settings. The file is DEV_ENV_FILE when set (it must exist), else
# backend/.env when present; `make smoke DEV_ENV_FILE=path` passes it.

set -Eeuo pipefail

# --- configuration --------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/docker/docker-compose.yml"
DB_CONTAINER="citevyn-db"
DB_URL="${CITEVYN_DATABASE_URL:-postgresql+psycopg://citevyn:citevyn@localhost:5432/citevyn}"
BASE_URL="${CITEVYN_BASE_URL:-http://127.0.0.1:8000}"
DB_TIMEOUT=60
HTTP_TIMEOUT=30
UVICORN_LOG="$REPO_ROOT/.smoke-uvicorn.log"
UVICORN_PID=""

# Checked before anything starts, under `set -e`: a file named explicitly must
# exist; the default may be absent (one notice per step, from the wrapper).
WITH_ENV="$REPO_ROOT/scripts/with_env.py"
if [[ -n "${DEV_ENV_FILE:-}" ]]; then
  if [[ ! -f "$DEV_ENV_FILE" ]]; then
    printf 'DEV_ENV_FILE=%s does not exist; unset it to use backend/.env when present\n' \
      "$DEV_ENV_FILE" >&2
    exit 1
  fi
  ENV_OPTIONAL=""
  ENV_FILE="$(cd "$(dirname "$DEV_ENV_FILE")" && pwd)/$(basename "$DEV_ENV_FILE")"
else
  ENV_OPTIONAL="yes"
  ENV_FILE="$REPO_ROOT/backend/.env"
fi

# --- helpers --------------------------------------------------------------

log()  { printf '\033[1;34m→\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

cleanup() {
  # Always release the background uvicorn if we started one.
  if [[ -n "$UVICORN_PID" ]] && kill -0 "$UVICORN_PID" 2>/dev/null; then
    kill -TERM "$UVICORN_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$UVICORN_PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL "$UVICORN_PID" 2>/dev/null || true
    wait "$UVICORN_PID" 2>/dev/null || true
  fi
  # Always tear down the docker compose stack.
  docker compose -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true
  # Leave UVICORN_LOG and RESPONSE_FILE on disk for post-mortem.
}
trap cleanup EXIT INT TERM

wait_for_db() {
  local elapsed=0
  while (( elapsed < DB_TIMEOUT )); do
    if docker exec "$DB_CONTAINER" pg_isready -U citevyn -d citevyn >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  fail "Postgres did not become ready within ${DB_TIMEOUT}s"
}

wait_for_http() {
  local elapsed=0
  while (( elapsed < HTTP_TIMEOUT )); do
    if curl -sf -o /dev/null "$BASE_URL/health"; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  fail "uvicorn did not respond on $BASE_URL within ${HTTP_TIMEOUT}s"
}

# --- main -----------------------------------------------------------------

require_cmd docker
require_cmd curl
require_cmd jq
require_cmd uv

cd "$REPO_ROOT"

# Compose now requires POSTGRES_PASSWORD (the prod stack refuses
# to boot with an empty one). Smoke is a local one-shot, so we
# fall back to the same default the README documents — the
# docker-compose ``db`` service still accepts the plain-text
# credential on its private docker network.
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-citevyn}"
export CITEVYN_ADMIN_API_KEY="${CITEVYN_ADMIN_API_KEY:-smoke-admin-key}"

log "Bringing up Postgres (container: $DB_CONTAINER)…"
docker compose -f "$COMPOSE_FILE" up -d db >/dev/null
wait_for_db

log "Applying migrations to $DB_URL"
CITEVYN_DATABASE_URL="$DB_URL" uv run --project backend --no-env-file \
  python "$WITH_ENV" ${ENV_OPTIONAL:+--optional} "$ENV_FILE" -- alembic -c db/alembic.ini upgrade head

log "Seeding users"
CITEVYN_DATABASE_URL="$DB_URL" PYTHONPATH=.. uv run --project backend --no-env-file \
  python "$WITH_ENV" ${ENV_OPTIONAL:+--optional} "$ENV_FILE" -- python -m db.seed.seed_users

log "Seeding catalog"
CITEVYN_DATABASE_URL="$DB_URL" PYTHONPATH=.. uv run --project backend --no-env-file \
  python "$WITH_ENV" ${ENV_OPTIONAL:+--optional} "$ENV_FILE" -- python -m db.seed.seed_catalog

log "Starting uvicorn in background (logs: $UVICORN_LOG)"
(
  cd "$REPO_ROOT/backend"
  CITEVYN_DATABASE_URL="$DB_URL" \
    uv run --no-env-file python "$WITH_ENV" ${ENV_OPTIONAL:+--optional} "$ENV_FILE" -- \
      uvicorn app.main:app --host 127.0.0.1 --port 8000 \
      >"$UVICORN_LOG" 2>&1 &
  echo $! >"$REPO_ROOT/.smoke-uvicorn.pid"
)
UVICORN_PID="$(cat "$REPO_ROOT/.smoke-uvicorn.pid")"
rm -f "$REPO_ROOT/.smoke-uvicorn.pid"
wait_for_http

log "GET /health (anonymous)"
HEALTH=$(curl -sf "$BASE_URL/health") || { cat "$UVICORN_LOG" >&2; fail "GET /health failed"; }
[[ "$(printf '%s' "$HEALTH" | jq -er .status)" == "healthy" ]] || fail "/health did not return status=healthy: $HEALTH"

log "Smoke test PASSED"
