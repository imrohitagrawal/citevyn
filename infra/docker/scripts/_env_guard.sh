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

# ``^...=dev-only-change-me\r?$`` matches LF-terminated lines
# by default and CRLF-terminated ones (a .env edited on Windows
# or written via a CRLF-emitting pipeline) optionally. Without
# the ``\r?`` allowance, CRLF .env files would silently bypass
# the stub guard.
if grep -qE '^POSTGRES_PASSWORD=dev-only-change-me\r?$' "${_GUARD_COMPOSE_DIR}/.env" \
   || grep -qE '^CITEVYN_ADMIN_API_KEY=dev-only-change-me\r?$' "${_GUARD_COMPOSE_DIR}/.env"; then
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
# leak into the caller's environment. The shell scripts
# (deploy.sh / refresh.sh / backup.sh) delegate everything to
# ``docker compose``, which reads .env on its own — they never
# need CITEVYN_ACME_EMAIL in their own shell. The ``make
# restore`` target sources .env later in a controlled ``set
# -a`` block, so leaking it here would be redundant at best
# and confusing at worst.
#
# The value is trimmed before comparison: a CRLF-terminated
# .env (e.g. edited on Windows) would otherwise retain a ``\r``
# and slip past the equality check, registering a literal
# ``dev@local.invalid\r`` (or any other value plus ``\r``)
# with Let's Encrypt.
if ! (
    set -a
    # shellcheck source=/dev/null
    . "${_GUARD_COMPOSE_DIR}/.env"
    set +a
    _acme_email="${CITEVYN_ACME_EMAIL:-}"
    # Strip trailing CR / LF / space / tab. Parameter expansion
    # does this in two steps: the inner ``${var%%[$'\r\n ']}``
    # strips one trailing whitespace char; we loop because the
    # value could end with multiple of them (e.g. CRLF plus a
    # space from a careless ``echo value >> .env``).
    while [[ "${_acme_email}" =~ [[:space:]]$ ]]; do
        _acme_email="${_acme_email%%[[:space:]]}"
    done
    if [[ -z "${_acme_email}" ]]; then
        echo "error: CITEVYN_ACME_EMAIL is not set in .env." >&2
        echo "       This is the email Let's Encrypt uses to" >&2
        echo "       notify the operator about expiring certs." >&2
        exit 1
    fi
    if [[ "${_acme_email}" == "dev@local.invalid" ]]; then
        echo "error: CITEVYN_ACME_EMAIL is still the dev-time default" >&2
        echo "       (dev@local.invalid). Set a real operator email" >&2
        echo "       in .env before running a prod entry point." >&2
        exit 1
    fi
); then
    return 1 2>/dev/null || exit 1
fi
