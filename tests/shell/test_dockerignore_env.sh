#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# test_dockerignore_env.sh — no local env file reaches a docker build context.
#
# A docker pattern matches from the context root only (unlike .gitignore), so
# the old bare `.env` / `.env.*` rules excluded only the root files. Measured
# 2026-10-29 with placeholder files: BuildKit uploads only the Dockerfile's
# COPY paths, so of the nested files only frontend/.env.local was sent (the
# stage that bakes the browser bundle copies all of frontend/); the legacy
# builder sends the whole context, so backend/.env and infra/docker/.env went
# too. See docs/ENVIRONMENT_FILES.md.
#
# This harness builds with BuildKit and `COPY . /ctx`, which makes BuildKit
# send everything not ignored: the same set the legacy builder sends.
#
# The real .dockerignore is copied into a small scratch context of placeholder
# files and built with `FROM scratch` + `--output type=local`, so docker itself
# decides what is excluded: no image pull, no network, no emulation of its
# matching rules.
#
# Needs a docker daemon. Without one it SKIPS, except on a Linux CI runner,
# where it FAILS so the suite cannot go green by never running.
#
# Which change turns each case red:
#   absent — `**/` dropped from the `**/.env*` rule in .dockerignore, or the
#            rule narrowed so a backup-style name (.env~, .env-old, .envrc) passes
#   kept   — `!**/.env.example` dropped, or the build copied nothing at all
# ────────────────────────────────────────────────────────────────────────────
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"

FAILURES=0
pass() { echo "  ok   — $1"; }
fail() { echo "  FAIL — $1"; FAILURES=$((FAILURES + 1)); }

echo "test_dockerignore_env.sh"

if ! docker info >/dev/null 2>&1; then
    if [ "${CI:-}" = "true" ] && [ "$(uname -s)" = "Linux" ]; then
        fail "no docker daemon on a Linux CI runner — this suite must run there"
        exit 1
    fi
    echo "  SKIP — no docker daemon (runs on the Linux CI runner and any machine with Docker)"
    exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
CTX="${WORK}/ctx"
mkdir -p "${CTX}/backend/app" "${CTX}/infra/docker" "${CTX}/frontend"
cp "${REPO_ROOT}/.dockerignore" "${CTX}/.dockerignore"
printf 'FROM scratch\nCOPY . /ctx\n' > "${CTX}/Dockerfile"

SECRET_FILES=".env .envrc backend/.env backend/.env-old backend/.env~ infra/docker/.env infra/docker/.env.old-bak frontend/.env.local frontend/.env.production"
KEPT_FILES=".env.example frontend/.env.example backend/app/canary.py"
for f in ${SECRET_FILES} ${KEPT_FILES}; do echo "PLACEHOLDER=1" > "${CTX}/${f}"; done

if ! DOCKER_BUILDKIT=1 docker build -q --output "type=local,dest=${WORK}/out" "${CTX}" >"${WORK}/build.log" 2>&1; then
    fail "the scratch build itself failed: $(tail -3 "${WORK}/build.log")"
    exit 1
fi

for f in ${SECRET_FILES}; do
    if [ -e "${WORK}/out/ctx/${f}" ]; then fail "absent: ${f} reached the build context"; else pass "absent: ${f}"; fi
done
for f in ${KEPT_FILES}; do
    if [ -e "${WORK}/out/ctx/${f}" ]; then pass "kept: ${f}"; else fail "kept: ${f} is missing from the build context"; fi
done

if [ "${FAILURES}" -ne 0 ]; then
    echo "${FAILURES} case(s) failed"
    exit 1
fi
echo "all passed"
