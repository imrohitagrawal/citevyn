#!/usr/bin/env bash
# Image build + BOOT smoke (#82).
#
# `docker build` succeeds even when the runtime interpreter is misaligned or the
# console-script CMD is broken — those fail at `exec` time. So this script does
# not just build: it BOOTS the image(s) and asserts they actually serve/run.
#
#   * api    — boot under CITEVYN_ENVIRONMENT=local (stub providers, no DB needed
#              for liveness) and poll GET /health for 200 from inside the container
#              (same probe as the Dockerfile HEALTHCHECK; no host port required).
#   * worker — assert the interpreter execs the console CMD:
#              `python -m app.worker.cli list-sources` exits 0 (reads the static
#              MVP source list only — no DB, no network, no settings).
#
# Used by `make image-smoke` (local dev), the CI image-smoke job (PR gate), and
# the release.yml post-build gate (per matrix target, BEFORE publishing :latest),
# so the same boot assertions protect every path.
#
# Usage:
#   image_smoke.sh both   [API_IMAGE] [WORKER_IMAGE]   # default role
#   image_smoke.sh api     [API_IMAGE]                 # single role → its image is $2
#   image_smoke.sh worker  [WORKER_IMAGE]
#   (no args → role=both with default local tags)
#   When BUILD=0 the images are assumed already built (release/CI pass the exact
#   artifact to publish); otherwise the needed image(s) are built from source.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ROLE="${1:-both}"
BUILD="${BUILD:-1}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"  # seconds to wait for /health=200
API_CONTAINER="citevyn-api-smoke-$$"

case "${ROLE}" in
  both)
    API_IMAGE="${2:-citevyn/api:smoke}"
    WORKER_IMAGE="${3:-citevyn/worker:smoke}"
    ;;
  api)
    API_IMAGE="${2:-citevyn/api:smoke}"
    ;;
  worker)
    WORKER_IMAGE="${2:-citevyn/worker:smoke}"
    ;;
  *)
    echo "usage: image_smoke.sh [api|worker|both] [IMAGE...]" >&2
    exit 2
    ;;
esac

cleanup() { docker rm -f "${API_CONTAINER}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

log() { printf '\n=== %s ===\n' "$*"; }

smoke_api() {
  if [[ "${BUILD}" == "1" ]]; then
    log "Building ${API_IMAGE}"
    docker build -f "${REPO_ROOT}/infra/docker/Dockerfile.api" -t "${API_IMAGE}" "${REPO_ROOT}"
  fi
  log "Booting api (${API_IMAGE}) and polling GET /health"
  docker run -d --name "${API_CONTAINER}" -e CITEVYN_ENVIRONMENT=local "${API_IMAGE}" >/dev/null
  # Probe INSIDE the container with the image's own python — identical to the
  # Dockerfile HEALTHCHECK — so no host port mapping is needed and a broken
  # uvicorn/interpreter surfaces as a non-200 (or connection refused) here.
  local probe deadline
  probe='import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("http://localhost:8000/health",timeout=2).status==200 else 1)'
  deadline=$(( SECONDS + HEALTH_TIMEOUT ))
  until docker exec "${API_CONTAINER}" python -c "${probe}" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "FAIL: api did not report /health=200 within ${HEALTH_TIMEOUT}s" >&2
      docker logs "${API_CONTAINER}" >&2 2>&1 || true
      return 1
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "${API_CONTAINER}" 2>/dev/null || echo false)" != "true" ]]; then
      echo "FAIL: api container exited before serving /health" >&2
      docker logs "${API_CONTAINER}" >&2 2>&1 || true
      return 1
    fi
    sleep 2
  done
  echo "OK: api /health returned 200"
  cleanup
}

smoke_worker() {
  if [[ "${BUILD}" == "1" ]]; then
    log "Building ${WORKER_IMAGE}"
    docker build -f "${REPO_ROOT}/infra/docker/Dockerfile.worker" -t "${WORKER_IMAGE}" "${REPO_ROOT}"
  fi
  log "Booting worker (${WORKER_IMAGE}): python -m app.worker.cli list-sources"
  if ! docker run --rm "${WORKER_IMAGE}" python -m app.worker.cli list-sources; then
    echo "FAIL: worker 'python -m app.worker.cli list-sources' did not exit 0" >&2
    return 1
  fi
  echo "OK: worker list-sources exited 0"
}

if [[ "${ROLE}" == "api" || "${ROLE}" == "both" ]]; then
  smoke_api
fi
if [[ "${ROLE}" == "worker" || "${ROLE}" == "both" ]]; then
  smoke_worker
fi

log "image smoke passed (${ROLE})"
