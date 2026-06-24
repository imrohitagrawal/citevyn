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
#   - asserts CITEVYN_ACME_EMAIL is set and is not the dev-time
#     default ``dev@local.invalid`` (the Caddy service uses that
#     address to register with Let's Encrypt, so a missing or
#     default value silently disables cert-expiry notifications
#     on the prod host)
#   - exits non-zero with a remediation message if any fails
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

# ACME email: a missing value or the docker-compose dev default
# would silently register Caddy with Let's Encrypt using
# dev@local.invalid, losing cert-expiry notifications. The
# guard refuses both so a real operator email is required before
# any prod entry point runs.
#
# We ``source`` the .env in a subshell so the variables don't
# leak into the caller's environment (deploy.sh / refresh.sh /
# backup.sh / the make restore target each set -a / . .env
# themselves in a controlled way later).
if ! (
    set -a
    # shellcheck source=/dev/null
    . "${_GUARD_COMPOSE_DIR}/.env"
    set +a
    if [[ -z "${CITEVYN_ACME_EMAIL:-}" ]]; then
        echo "error: CITEVYN_ACME_EMAIL is not set in .env." >&2
        echo "       This is the email Let's Encrypt uses to" >&2
        echo "       notify the operator about expiring certs." >&2
        exit 1
    fi
    if [[ "${CITEVYN_ACME_EMAIL}" == "dev@local.invalid" ]]; then
        echo "error: CITEVYN_ACME_EMAIL is still the dev-time default" >&2
        echo "       (dev@local.invalid). Set a real operator email" >&2
        echo "       in .env before running a prod entry point." >&2
        exit 1
    fi
); then
    return 1 2>/dev/null || exit 1
fi
