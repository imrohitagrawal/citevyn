# ────────────────────────────────────────────────────────────────────────────
# _env_guard.sh — refuse to run with the dev-only stub .env that
# ``make demo`` auto-generates on a fresh clone.
#
# Sourced by deploy.sh, refresh.sh, backup.sh, and the ``make
# restore`` Makefile target. Each caller must pass the compose
# directory (the directory that contains docker-compose.yml and
# .env) as the first argument:
#
#   source "${COMPOSE_DIR}/scripts/_env_guard.sh" "${COMPOSE_DIR}"
#
# We require the explicit argument instead of resolving
# BASH_SOURCE[0] because the Makefile recipe sources the
# helper from a single-line shell where BASH_SOURCE is empty
# in some shells, and a fallback to ``$(dirname "$0")`` is
# wrong when the script is sourced rather than executed.
#
# Behavior:
#   - asserts the .env file exists
#   - asserts POSTGRES_PASSWORD and CITEVYN_ADMIN_API_KEY are not
#     the dev-only stubs the Makefile bootstrap writes
#   - exits non-zero with a remediation message if either fails
# ────────────────────────────────────────────────────────────────────────────

if [[ -z "${1:-}" ]]; then
    echo "error: _env_guard.sh requires the compose dir as \$1" >&2
    return 1 2>/dev/null || exit 1
fi
_GUARD_COMPOSE_DIR="${1}"

if [[ ! -f "${_GUARD_COMPOSE_DIR}/.env" ]]; then
    echo "error: .env not found at ${_GUARD_COMPOSE_DIR}/.env" >&2
    echo "       copy prod.env.example to .env and fill in the values" >&2
    return 1 2>/dev/null || exit 1
fi

if grep -q '^POSTGRES_PASSWORD=dev-only-change-me$' "${_GUARD_COMPOSE_DIR}/.env" \
   || grep -q '^CITEVYN_ADMIN_API_KEY=dev-only-change-me$' "${_GUARD_COMPOSE_DIR}/.env"; then
    echo "error: .env still has dev-only stub secrets from 'make demo'." >&2
    echo "       Replace POSTGRES_PASSWORD and CITEVYN_ADMIN_API_KEY" >&2
    echo "       (e.g. openssl rand -hex 32) and re-run." >&2
    return 1 2>/dev/null || exit 1
fi
