#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# deploy.sh — first-time / cold-start bring-up of the production stack.
#
# Use ``refresh.sh`` for subsequent updates. Use this script the very
# first time you wire up a host, or when you need to re-create the
# database volume (e.g. after a clean-up).
#
# What it does:
#   1. Starts Postgres + Redis (uses pre-built images; run `docker compose
#      --profile prod build api worker` first if they are missing).
#   2. Waits for Postgres to accept connections.
#   3. Runs the full alembic migration chain.
#   4. Seeds the initial admin user + the demo knowledge catalog (idempotent).
#   5. Brings up the api + caddy stack. The worker is a one-shot ingest job
#      (restart: no), NOT started here — run it on demand with
#      `docker compose --profile prod run --rm worker`.
#   6. Waits until the api is healthy and caddy is running.
#
# Usage:
#   VERSION=v1.2.3 ./scripts/deploy.sh
#
# Prereqs:
#   - .env exists with all required values
#   - DNS for CITEVYN_PUBLIC_HOST points at this host's public IP
#   - ports 80 and 443 are reachable from the internet
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${COMPOSE_DIR}"

if [[ ! -f .env ]]; then
    echo "error: .env not found at ${COMPOSE_DIR}/.env" >&2
    exit 1
fi
# Reuse the dev-stub guard shared by deploy / refresh / backup
# so the entry points cannot drift out of sync. The guard
# needs the compose dir as $1 (see _env_guard.sh).
# shellcheck source=infra/docker/scripts/_env_guard.sh
source "${COMPOSE_DIR}/scripts/_env_guard.sh" "${COMPOSE_DIR}"

VERSION="${VERSION:-dev}"
export VERSION

echo "==> deploy.sh: bringing up the database + cache layer"
docker compose up -d db redis

echo "==> waiting for Postgres to accept connections (max 60s)"
for _ in $(seq 1 30); do
    if docker compose exec -T db pg_isready -U citevyn -d citevyn >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

echo "==> running alembic migrations"
docker compose \
    --profile prod \
    run \
    --rm \
    --no-deps \
    api \
    python -m alembic \
        --config /db/alembic.ini \
    upgrade head
# NB: invoke via ``python -m alembic`` — the ``alembic`` console script
# in the runtime image carries a builder-stage shebang
# (``#!/build/backend/.venv/bin/python``) that does not exist at runtime,
# so a bare ``alembic`` exec fails with "no such file or directory".
# No ``--sqlalchemy-url`` is passed (it is also not a valid alembic
# 1.18 CLI option): ``db/env.py`` reads ``CITEVYN_DATABASE_URL`` from
# the container env (compose ``env_file``) whenever ``alembic.ini``
# still holds its ``sqlite:///placeholder.db`` default. If that var is
# unset, env.py falls back to the ``config.py`` localhost default
# (``postgres@localhost``, unreachable from the api container) and
# fails opaquely — so ``_env_guard.sh`` fails fast on a missing
# ``CITEVYN_DATABASE_URL`` before we get here.

echo "==> seeding the initial admin user (idempotent)"
# The seed module lives at ``db/seed/seed_users.py`` (not under
# ``app/``). The api image copies ``db/`` to ``/db`` and sets
# ``PYTHONPATH=/db``, so ``/db`` itself is the package root on
# ``sys.path`` — the module resolves as ``seed.seed_users`` (NOT
# ``db.seed.seed_users``, which would need ``/`` on the path).
# ``seed_users.seed()`` is idempotent: existing rows are left
# untouched, so re-running this script on a populated database
# is a no-op.
docker compose \
    --profile prod \
    run \
    --rm \
    --no-deps \
    api \
    python -m seed.seed_users

echo "==> seeding the demo knowledge catalog (idempotent)"
# Without this step the production DB has zero chunks on cold start and
# every retrieval arm returns [], so the orchestrator serves no_answer
# for every question (Issue 2 / F0). ``seed_catalog.seed()`` is
# idempotent — existing ``v1`` rows are left untouched — so re-running
# on a populated database is a no-op. Operators ingest richer content
# via the worker (``refresh.sh`` + admin promote) once the stack is up.
# Resolves as ``seed.seed_catalog`` for the same reason as the admin
# seed above (``/db`` is the package root on ``PYTHONPATH``).
docker compose \
    --profile prod \
    run \
    --rm \
    --no-deps \
    api \
    python -m seed.seed_catalog

echo "==> verifying the admin row landed"
# Sanity-check that the seed actually wrote a row (the script
# exits non-zero if the seed module's commit failed silently).
ADMIN_COUNT=$(docker compose exec -T db psql -U citevyn -d citevyn -tAc \
    "SELECT count(*) FROM users WHERE user_id = 'admin';")
if [[ "${ADMIN_COUNT}" != "1" ]]; then
    echo "error: seed step did not create the admin user (count=${ADMIN_COUNT})" >&2
    echo "       inspect: docker compose exec db psql -U citevyn -d citevyn -c '\\dt'" >&2
    exit 1
fi

echo "==> bringing up the long-running services (api + caddy)"
# The worker is intentionally NOT started here. It is a one-shot ingest
# job (compose ``worker`` service: CMD ``... run``, ``restart: "no"``),
# not a resident service — so it never needs a health probe and cannot
# crash-loop. Ingestion is an explicit operator step run once the
# worker's source fetcher is configured for this deployment:
#
#     docker compose --profile prod run --rm worker
#
# It writes a *candidate* IndexVersion that an admin then promotes; the
# live demo answers from the seeded ``v1`` active index until then, so
# the deploy does not depend on ingestion. (The default CLI fetcher
# reads local test fixtures that are not shipped in the runtime image —
# a prod HTTP fetcher + source config is tracked as a follow-up.)
docker compose --profile prod up -d api caddy

echo "==> waiting for the api to become healthy + caddy to be running (max 60s)"
# Poll the api container's OWN health status (its compose healthcheck
# hits http://localhost:8000/health *inside* the container), NOT
# http://localhost/health: the :80 Caddy site 301-redirects every
# non-ACME path to HTTPS, and ``curl --fail`` (no ``-L``) treats a 3xx
# as success — so a crash-looping api would be reported healthy
# (the pre-existing false-green gate this replaces).
#
# Also require caddy to be RUNNING (not ``restarting``/``exited``). Caddy
# has no healthcheck and the api's own probe bypasses it, so without this
# a Caddyfile that fails to adapt at runtime (bad CITEVYN_PUBLIC_HOST,
# ACME/permission issue, a future Caddyfile edit) would crash-loop while
# the deploy still reported green and :80/:443 served nothing.
for _ in $(seq 1 30); do
    _api_health="$(docker inspect --format '{{.State.Health.Status}}' citevyn-api 2>/dev/null || true)"
    _caddy_state="$(docker inspect --format '{{.State.Status}}' citevyn-caddy 2>/dev/null || true)"
    if [[ "${_api_health}" == "healthy" && "${_caddy_state}" == "running" ]]; then
        echo "==> api healthy, caddy running"
        echo "==> Caddy provisions the TLS certificate for the configured"
        echo "    CITEVYN_PUBLIC_HOST eagerly at startup via the ACME HTTP-01"
        echo "    challenge on :80; tail the caddy logs to confirm issuance"
        echo "    once DNS for that host resolves here and :80/:443 are open."
        echo "==> tail logs with: docker compose --profile prod logs -f"
        exit 0
    fi
    sleep 2
done

echo "error: stack did not come up within 60s" >&2
echo "       api health=${_api_health:-unknown}, caddy state=${_caddy_state:-unknown}" >&2
echo "       inspect with: docker compose --profile prod logs api caddy" >&2
exit 1