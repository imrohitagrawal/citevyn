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
# We require the explicit argument so the contract is uniform
# regardless of how the guard is invoked. deploy.sh /
# refresh.sh / backup.sh source it as ``source
# "${COMPOSE_DIR}/scripts/_env_guard.sh" "${COMPOSE_DIR}"``
# (BASH_SOURCE[0] resolves correctly there). The Makefile
# ``restore`` target uses ``( source … )`` which also resolves
# BASH_SOURCE[0] correctly. Passing the explicit arg keeps
# every caller identical and makes the guard's dependency on
# the compose dir obvious in the call site.
#
# Behavior:
#   - tightens .env file mode to 0600 (defense-in-depth: the
#     ``make db-up`` bootstrap and a manual ``cp
#     prod.env.example .env`` both produce a file with
#     secret values, and would otherwise land at umask 0644
#     on a multi-user host)
#   - asserts the .env file exists
#   - asserts POSTGRES_PASSWORD, CITEVYN_ADMIN_API_KEY, and
#     CITEVYN_ACME_EMAIL are not the dev-only stubs the Makefile
#     bootstrap writes
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

# Tighten file mode before doing any other inspection. ``chmod``
# is a no-op if the file is already 0600, so callers that have
# already tightened the file (e.g. ``make db-up`` after the
# bootstrap) pay no cost. The chmod also catches the manual
# ``cp prod.env.example .env`` path that the Makefile doesn't
# see.
#
# ``|| true`` because on some hosts / docker bind-mounts the
# chmod can fail (e.g. read-only mount); the rest of the guard
# still enforces the actual security invariants.
chmod 600 "${_GUARD_COMPOSE_DIR}/.env" 2>/dev/null || true

# ``^...=dev-only-change-me\r?$`` matches LF-terminated lines
# by default and CRLF-terminated ones (a .env edited on Windows
# or written via a CRLF-emitting pipeline) optionally. Without
# the ``\r?`` allowance, CRLF .env files would silently bypass
# the stub guard.
if grep -qE '^POSTGRES_PASSWORD=dev-only-change-me\r?$' "${_GUARD_COMPOSE_DIR}/.env" \
   || grep -qE '^CITEVYN_ADMIN_API_KEY=dev-only-change-me\r?$' "${_GUARD_COMPOSE_DIR}/.env" \
   || grep -qE '^CITEVYN_ACME_EMAIL=dev-only-change-me\r?$' "${_GUARD_COMPOSE_DIR}/.env"; then
    echo "error: .env still has dev-only stub secrets from 'make demo'." >&2
    echo "       Replace POSTGRES_PASSWORD, CITEVYN_ADMIN_API_KEY," >&2
    echo "       and CITEVYN_ACME_EMAIL" >&2
    echo "       (the first two via e.g. openssl rand -hex 32; the" >&2
    echo "       last with a real operator email inbox you monitor)" >&2
    echo "       and re-run." >&2
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
    # Source the .env in a child process so the operator
    # variables don't leak into the caller's environment. The
    # shell scripts (deploy.sh / refresh.sh / backup.sh)
    # delegate to ``docker compose`` and never need
    # CITEVYN_ACME_EMAIL in their own shell; ``make restore``
    # sources .env later in a controlled ``set -a`` block.
    set +e
    set -a
    # shellcheck source=/dev/null
    . "${_GUARD_COMPOSE_DIR}/.env"
    _source_rc=$?
    set +a
    set -u
    if [[ ${_source_rc} -ne 0 ]]; then
        echo "error: failed to source ${_GUARD_COMPOSE_DIR}/.env" >&2
        echo "       (bash exited with status ${_source_rc}). The .env" >&2
        echo "       file may have a syntax error, a stray command, or" >&2
        echo "       an unparseable value. Inspect it with" >&2
        echo "       ``bash -n ${_GUARD_COMPOSE_DIR}/.env`` or run" >&2
        echo "       ``set -x; . ${_GUARD_COMPOSE_DIR}/.env`` to see" >&2
        echo "       the failing line." >&2
        exit 1
    fi
    _acme_email="${CITEVYN_ACME_EMAIL:-}"
    # Strip trailing CR / LF / space / tab. The loop handles
    # pathological inputs (CRLF plus spaces from a careless
    # ``echo value >> .env``) by peeling one trailing whitespace
    # char at a time until none remain.
    while [[ "${_acme_email}" =~ [[:space:]]$ ]]; do
        _acme_email="${_acme_email%%[[:space:]]}"
    done
    # Strip a matched pair of leading/trailing single or double
    # quotes. Bash preserves the quote chars literally when the
    # value is sourced, so an operator who hand-wrote
    # ``CITEVYN_ACME_EMAIL='ops@example.com'`` would otherwise
    # have the quote characters baked into the value Let's
    # Encrypt receives. docker compose's env-file parser strips
    # one matched pair — we mirror that here.
    if [[ ${#_acme_email} -ge 2 ]]; then
        _first="${_acme_email:0:1}"
        _last="${_acme_email: -1}"
        if [[ "${_first}" == "'" && "${_last}" == "'" ]] \
           || [[ "${_first}" == '"' && "${_last}" == '"' ]]; then
            _acme_email="${_acme_email:1:-1}"
        fi
    fi
    if [[ -z "${_acme_email}" ]]; then
        echo "error: CITEVYN_ACME_EMAIL is not set in .env." >&2
        echo "       This is the email Let's Encrypt uses to notify" >&2
        echo "       the operator about expiring certs." >&2
        echo "       Fill in POSTGRES_PASSWORD, CITEVYN_ADMIN_API_KEY," >&2
        echo "       and CITEVYN_ACME_EMAIL in .env (see" >&2
        echo "       infra/docker/prod.env.example for the format)." >&2
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
