#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# refresh.sh — rebuild and restart the CiteVyn production stack.
#
# Rebuilds the API and worker images from the current source tree,
# runs alembic migrations against the live database, and rolls the
# containers. Zero-downtime swap is NOT a goal for the MVP — the
# compose file brings services up one at a time, so expect a brief
# period where :443 returns 502 while caddy waits for the new api
# container to come up.
#
# Usage:
#   ./scripts/refresh.sh                 # default: use VERSION=dev
#   VERSION=v1.2.3 ./scripts/refresh.sh  # explicit tag
#
# Prereqs:
#   - .env exists next to docker-compose.yml with all required vars
#   - the user running this script is in the docker group
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Resolve the compose file's directory regardless of CWD so this
# script works from cron / CI / interactive shells alike.
COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${COMPOSE_DIR}"

if [[ ! -f .env ]]; then
    echo "error: .env not found at ${COMPOSE_DIR}/.env" >&2
    echo "       copy prod.env.example to .env and fill in the values" >&2
    exit 1
fi
# Refuse to refresh with the dev-only stub that ``make demo``
# auto-generates; share the guard with deploy.sh so the entry
# points cannot drift out of sync.
# shellcheck source=infra/docker/scripts/_env_guard.sh
source "${COMPOSE_DIR}/scripts/_env_guard.sh" "${COMPOSE_DIR}"

# Pull VERSION from the environment or fall back to ``dev``.
VERSION="${VERSION:-dev}"
export VERSION

echo "==> refresh.sh: rebuilding images (VERSION=${VERSION})"
# NOTE: do not pass ``--no-cache`` here. The uv builder stage
# uses ``--mount=type=cache`` so the dependency layer is cached
# across refreshes; ``--no-cache`` would invalidate that and
# re-download every base image layer on every refresh, taking
# minutes instead of seconds. Set ``REFRESH_NUKE=1`` if you need
# to force a clean rebuild (e.g. after a base-image CVE bump).
if [[ "${REFRESH_NUKE:-0}" == "1" ]]; then
    echo "==> REFRESH_NUKE=1 set; forcing a clean rebuild"
    docker compose \
        --profile prod \
        build \
        --pull \
        --no-cache
else
    docker compose \
        --profile prod \
        build \
        --pull
fi

echo "==> running alembic migrations against the live database"
# Migrations run inside the API container so they share the
# application code (and uv-managed deps) with the running service.
# The --rm flag removes the temporary container when done.
docker compose \
    --profile prod \
    run \
    --rm \
    --no-deps \
    api \
    python -m alembic \
        --config /db/alembic.ini \
    upgrade head
# See deploy.sh: invoke via ``python -m alembic`` (the console script's
# builder-stage shebang is absent at runtime) and pass no
# ``--sqlalchemy-url`` (``db/env.py`` reads ``CITEVYN_DATABASE_URL`` from
# the container env; the flag is also invalid in alembic 1.18.x).

echo "==> rolling the long-running containers (api + caddy)"
# The worker is a one-shot ingest job (below), not a long-running
# service, so it is excluded from this ``up``.
docker compose \
    --profile prod \
    up \
    -d \
    --no-deps \
    api caddy

# Caddy auto-reloads its config on SIGHUP; explicit ``caddy reload``
# is only needed if we change the Caddyfile.
docker compose \
    --profile prod \
    exec \
    caddy \
    caddy reload --config /etc/caddy/Caddyfile

# The worker is not rolled here: it is a one-shot ingest job (see
# deploy.sh), not a resident service. Re-ingest explicitly when needed:
#     docker compose --profile prod run --rm worker
echo "==> waiting for the api to become healthy (max 60s)"
# Poll the api container's OWN health status, NOT http://localhost/health:
# the :80 Caddy site 301-redirects to HTTPS and ``curl --fail`` (no
# ``-L``) treats the 3xx as success, falsely reporting a crash-looping
# api as healthy. See deploy.sh.
for _ in $(seq 1 30); do
    _api_health="$(docker inspect --format '{{.State.Health.Status}}' citevyn-api 2>/dev/null || true)"
    if [[ "${_api_health}" == "healthy" ]]; then
        echo "==> api healthy"
        exit 0
    fi
    sleep 2
done

echo "error: api did not become healthy within 60s" >&2
echo "       inspect with: docker compose --profile prod logs api" >&2
exit 1