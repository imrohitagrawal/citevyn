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
#   - asserts CITEVYN_PUBLIC_HOST, CITEVYN_DATABASE_URL and a
#     non-stub CITEVYN_LLM_PROVIDER are set
#   - asserts the public client token passes the same weak-secret
#     test the app applies in production (non-empty, not the
#     published ``local-demo-key``, at least 16 chars) — see
#     Settings._is_weak_secret in backend/app/core/config.py.
#     Read from CITEVYN_PUBLIC_CLIENT_TOKEN, falling back to the
#     deprecated CITEVYN_DEMO_API_KEY (#430) only when the new name
#     is NOT SET AT ALL — the same rule pydantic's AliasChoices
#     applies, so the guard cannot bless a value the app would not
#     use. An empty CITEVYN_PUBLIC_CLIENT_TOKEN= is PRESENT and is
#     therefore rejected, exactly as Settings rejects it
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
#
# POSTGRES_PASSWORD also rejects ``citevyn`` — the repo-wide local
# dev credential the ``make db-up`` bootstrap now writes (and that
# DB_URL / smoke.sh / config.py / CI all use). It is never a valid
# prod secret, so a prod deploy that left it in place must be refused
# just like the ``dev-only-change-me`` stub.
if grep -qE '^POSTGRES_PASSWORD=(dev-only-change-me|citevyn)\r?$' "${_GUARD_COMPOSE_DIR}/.env" \
   || grep -qE '^CITEVYN_ADMIN_API_KEY=dev-only-change-me\r?$' "${_GUARD_COMPOSE_DIR}/.env" \
   || grep -qE '^CITEVYN_ACME_EMAIL=dev-only-change-me\r?$' "${_GUARD_COMPOSE_DIR}/.env"; then
    echo "error: .env still has dev-only stub secrets from the 'make db-up' bootstrap." >&2
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
    # Normalize a sourced value the way docker compose's env-file parser does
    # before comparing: peel trailing CR/LF/space/tab, then strip a matched pair
    # of surrounding single/double quotes. Bash preserves the quote chars
    # literally when sourcing, so without this a QUOTED value slips past a naive
    # ``==`` (or the anchored greps above): docker compose would run
    # POSTGRES_PASSWORD="citevyn" / CITEVYN_ADMIN_API_KEY="dev-only-change-me"
    # with the weak/known value while the guard sees the quotes and waves it
    # through. The loop peels one trailing whitespace char at a time so a CRLF
    # plus stray spaces (``echo value >> .env``) is fully trimmed.
    _strip() {
        local v="$1"
        # BOTH ends, because the Python predicate this mirrors is
        # `value.strip()` (backend/app/core/config.py:54-55), which is
        # symmetric. Trailing-only was an ASYMMETRY BUG, not a simplification:
        # CITEVYN_DEMO_API_KEY="  local-demo-key" is 16 chars with two leading
        # spaces, so it is neither == 'local-demo-key' nor < 16 — the guard
        # passed it, and then Python stripped both ends, saw the publicly-known
        # default, and crash-looped the api. Which is exactly the failure this
        # guard exists to catch BEFORE the 60s health poll burns.
        while [[ "${v}" =~ [[:space:]]$ ]]; do v="${v%%[[:space:]]}"; done
        while [[ "${v}" =~ ^[[:space:]] ]]; do v="${v#?}"; done
        if [[ ${#v} -ge 2 ]]; then
            local f="${v:0:1}" l="${v: -1}"
            if [[ "${f}" == "'" && "${l}" == "'" ]] \
               || [[ "${f}" == '"' && "${l}" == '"' ]]; then
                # Peel the two quote chars separately. ``${v:1:-1}`` needs a
                # negative-length substring, which is bash 4.2+; on the bash 3.2
                # that ships with macOS it errors "substring expression < 0" and
                # yields the EMPTY string — so a correctly-quoted secret stripped
                # to nothing and the guard rejected a VALID production .env,
                # blocking every prod entry point on a Mac (#161). ``%?``/``#?``
                # are portable to both.
                v="${v%?}"
                v="${v#?}"
            fi
        fi
        printf '%s' "${v}"
    }
    # Quote/whitespace-aware re-check of the two secrets the greps above match
    # only literally. Same OR-over-fields contract, now bypass-resistant.
    _pw="$(_strip "${POSTGRES_PASSWORD:-}")"
    _admin="$(_strip "${CITEVYN_ADMIN_API_KEY:-}")"
    if [[ "${_pw}" == "dev-only-change-me" || "${_pw}" == "citevyn" \
          || "${_admin}" == "dev-only-change-me" ]]; then
        echo "error: .env still has dev-only stub secrets (POSTGRES_PASSWORD /" >&2
        echo "       CITEVYN_ADMIN_API_KEY) even after quote/whitespace" >&2
        echo "       normalization. Replace them with strong secrets and re-run." >&2
        exit 1
    fi
    _acme_email="$(_strip "${CITEVYN_ACME_EMAIL:-}")"
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
    # CITEVYN_PUBLIC_HOST backs the named Caddy site
    # ``{$CITEVYN_PUBLIC_HOST}`` (infra/docker/Caddyfile). An unset or
    # empty value makes Caddy adapt a site block with an empty address
    # ("site address cannot be empty"), so the caddy container exits
    # non-zero and restart-loops and :443 never serves. Require it
    # before any prod entry point starts caddy.
    _public_host="$(_strip "${CITEVYN_PUBLIC_HOST:-}")"
    if [[ -z "${_public_host}" ]]; then
        echo "error: CITEVYN_PUBLIC_HOST is not set in .env." >&2
        echo "       It is the public DNS name Caddy provisions a TLS" >&2
        echo "       certificate for and matches the :443 site against;" >&2
        echo "       an empty value crash-loops the caddy container." >&2
        exit 1
    fi
    # CITEVYN_DATABASE_URL is read from the container env by alembic
    # (db/env.py) and the app. deploy.sh / refresh.sh no longer pass it
    # on the CLI, so an unset value silently resolves to the config.py
    # localhost default (postgresql+psycopg://citevyn:citevyn@localhost)
    # and dies with an opaque "connection refused" inside the api
    # container (Postgres is at hostname ``db``, not localhost). Fail
    # fast with an actionable message instead.
    _database_url="$(_strip "${CITEVYN_DATABASE_URL:-}")"
    if [[ -z "${_database_url}" ]]; then
        echo "error: CITEVYN_DATABASE_URL is not set in .env." >&2
        echo "       Migrations and the app read it from the container" >&2
        echo "       env; without it alembic connects to the localhost" >&2
        echo "       default (no Postgres there) and fails opaquely." >&2
        exit 1
    fi
    # stub-LLM-in-production: compose pins CITEVYN_ENVIRONMENT=production
    # and defaults ``CITEVYN_LLM_PROVIDER=stub``; the settings guard
    # rejects the stub provider in production, so an api/worker started
    # with the default crash-loops and /health never passes. Require a
    # real provider so the failure is a clear pre-flight message rather
    # than a 60s health-timeout.
    _llm_provider="$(_strip "${CITEVYN_LLM_PROVIDER:-}")"
    if [[ -z "${_llm_provider}" || "${_llm_provider}" == "stub" ]]; then
        echo "error: CITEVYN_LLM_PROVIDER must be a real provider in prod" >&2
        echo "       (got '${_llm_provider:-<unset>}'). The compose stack" >&2
        echo "       pins CITEVYN_ENVIRONMENT=production, where the stub" >&2
        echo "       provider is rejected and the api crash-loops." >&2
        echo "       Set CITEVYN_LLM_PROVIDER=gemini (or anthropic)." >&2
        exit 1
    fi
    # The public client token is the bearer every public /api/v1 request
    # carries. ``infra/docker/prod.env.example`` ships it EMPTY, and
    # Settings._is_weak_secret (backend/app/core/config.py) rejects an
    # empty / default / short value once CITEVYN_ENVIRONMENT=production
    # — which compose pins. Without this block an operator who copied
    # the template and skipped the field passes the guard, deploy.sh
    # proceeds, and the api dies at boot on a pydantic error AFTER the
    # 60s health poll has burned, reporting only ``api health=unknown``.
    #
    # Mirror the Python predicate exactly: strip (quotes + whitespace at BOTH
    # ends, via _strip), lower-case, reject the publicly-known default, and
    # reject anything under 16 characters — which also covers the empty case.
    # The lower-casing matters: ``LOCAL-DEMO-KEY`` is as guessable as the
    # default.
    #
    # One helper for BOTH keys. CITEVYN_ADMIN_API_KEY had the identical gap and
    # was only ever compared against the Makefile bootstrap stub
    # (``dev-only-change-me``): empty, absent, and the PUBLISHED code default
    # ``local-admin-key`` (config.py:71) all passed the guard, and the admin key
    # is the one that can promote an index and read the budget.
    # Does the .env FILE declare this variable? (Not: is it set in this shell.)
    #
    # The container is fed by `env_file`, so a declaration in the FILE is the
    # only thing that reaches the app -- see the long note below. Matching
    # compose's own declaration shape:
    #
    #   * anchored at column 0 modulo leading whitespace, so a mention inside a
    #     comment or a value does not count;
    #   * `#` anywhere before the name disqualifies the line -- a commented-out
    #     `# CITEVYN_PUBLIC_CLIENT_TOKEN=` is documentation, not a declaration,
    #     and `infra/docker/prod.env.example` uses that form deliberately;
    #   * `export ` accepted, because a .env written for `source` still parses;
    #   * the trailing `=` is what makes the match exact, so
    #     `CITEVYN_PUBLIC_CLIENT_TOKEN_EXTRA=` can never satisfy a query for
    #     `CITEVYN_PUBLIC_CLIENT_TOKEN`. A CRLF file is handled because `=`
    #     precedes the `\r`.
    #
    # `grep -E` with the name interpolated is safe here: every caller passes a
    # literal CITEVYN_* identifier, and `[A-Za-z0-9_]` carries no regex meaning.
    _declared_in_env_file() {  # $1 = variable name -> 0 if the .env declares it
        grep -qE "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" \
            "${_GUARD_COMPOSE_DIR}/.env" 2>/dev/null
    }

    _assert_strong_key() {  # $1 = var name, $2 = published default, $3 = raw value
        local _name="$1" _default="$2" _val _lc
        _val="$(_strip "${3:-}")"
        _lc="$(printf '%s' "${_val}" | tr '[:upper:]' '[:lower:]')"
        if [[ -z "${_val}" ]]; then
            echo "error: ${_name} is not set in .env." >&2
            echo "       prod.env.example ships it empty and the api refuses" >&2
            echo "       to boot in production without a strong value." >&2
            echo "       Generate one with e.g. openssl rand -hex 32." >&2
            exit 1
        fi
        if [[ "${_lc}" == "${_default}" ]]; then
            echo "error: ${_name} is still the publicly-known default" >&2
            echo "       ('${_default}'). It is in the open-source repo, so" >&2
            echo "       anyone can use it against your deployment." >&2
            echo "       Generate one with e.g. openssl rand -hex 32." >&2
            exit 1
        fi
        if [[ ${#_val} -lt 16 ]]; then
            echo "error: ${_name} is shorter than the 16-character minimum" >&2
            echo "       the api enforces in production, so the api would" >&2
            echo "       crash-loop on a pydantic validation error." >&2
            echo "       Generate one with e.g. openssl rand -hex 32." >&2
            exit 1
        fi
    }
    # The bearer every public /api/v1 request carries (#430).
    #
    # Both spellings accepted, NEW ONE WINS, mirroring the AliasChoices order in
    # Settings.public_client_token. A guard that resolved them the other way round
    # would test a value the app is not going to use, and pass a deploy that then
    # crash-loops -- which is the exact failure this whole block exists to prevent.
    #
    # WHICH NAME SUPPLIES THE VALUE IS DECIDED FROM THE .env FILE ALONE.
    #
    # The root cause of two defects, one in each direction, found by reviewing
    # the first version of this block. This function SOURCES the .env into a
    # subshell that INHERITS the caller's environment, so `${VAR+set}` and
    # `${VAR:-}` both see a MERGE of (the file, the operator's shell). The api
    # container sees neither merge: `infra/docker/docker-compose.yml` passes this
    # value through `env_file` and mentions the variable nowhere else, so the ONLY
    # thing that reaches the app is what the FILE declares. Resolving against the
    # merge is wrong in both directions, and both were reproduced:
    #
    #   * FALSE REJECT -- .env holds a strong CITEVYN_DEMO_API_KEY, and the
    #     operator happens to have `CITEVYN_PUBLIC_CLIENT_TOKEN=` exported in
    #     their shell. The deploy is fine (the container gets the old name from
    #     the file) and the guard blocked it.
    #   * FALSE PASS -- .env declares NO token, but a strong one is exported in
    #     the shell. The guard passed; the container got nothing, fell back to
    #     the published `local-demo-key`, and production refuses to boot on it.
    #
    # `_declared_in_env_file` asks the file, which is the same source the
    # container reads, so the guard's verdict is about the deployment rather than
    # about the terminal it was typed in.
    #
    # SCOPE, STATED: only the client token is resolved this way. POSTGRES_PASSWORD
    # and CITEVYN_ADMIN_API_KEY above still read the merged view and so still
    # carry the FALSE PASS half of this bug. That is pre-existing, unchanged here,
    # and deliberately out of scope for a rename -- widening it touches every
    # prod-deploy path at once. Tracked in docs/BACKLOG.md under #430.
    #
    # PRESENCE, not non-emptiness, once the file is the subject.
    #
    # This is the whole finding of the #430 review round, reproduced by three
    # independent reviewers and then by hand. pydantic's ``AliasChoices`` selects
    # the first alias PRESENT in the environment, and an exported-but-empty
    # variable IS present: with ``CITEVYN_PUBLIC_CLIENT_TOKEN=`` and a strong
    # ``CITEVYN_DEMO_API_KEY=`` in the same .env, ``Settings()`` raises
    # ``string_too_short`` and never consults the old value at all.
    #
    # An `-n` test treats empty as absent, so it fell through to the strong old
    # value and reported PASS -- MEASURED: guard exit 0, `docker compose config`
    # injecting ``CITEVYN_PUBLIC_CLIENT_TOKEN: ""``, and ``Settings()`` refusing to
    # construct from that same environment. That is precisely the "guard green,
    # api dies at boot AFTER the 60s health poll has burned" failure this whole
    # block exists to pre-empt, and the ingredient ships: prod.env.example
    # declares the new name EMPTY, so an operator who copies the template and
    # pastes back only their old line lands exactly here.
    #
    # A DECLARED-BUT-EMPTY line is PRESENT, exactly as pydantic sees it:
    # ``AliasChoices`` selects the first alias present in the environment, and
    # ``CITEVYN_PUBLIC_CLIENT_TOKEN=`` reaches the container as an empty string,
    # which ``min_length=1`` rejects. So a declared-empty new name must NOT fall
    # through to the old one -- measured on the first draft, which did fall
    # through, reported PASS, and left the api crash-looping on
    # ``string_too_short`` after the 60s health poll had burned.
    #
    # The VALUE still comes from the SOURCED variable rather than from the raw
    # line, so quote peeling and CRLF trimming (``_strip`` below) keep working;
    # sourcing overwrites any inherited value for a name the file declares, so
    # for a declared name the two agree by construction.
    #
    # The NAME reported on failure is whichever line supplied the value, so the
    # remediation points at the line the operator has to edit -- except when the
    # effective value is EMPTY, where there is no line worth pointing at and they
    # need to be told which variable to SET. That is never the one being retired.
    # The emptiness test uses the STRIPPED value, so a whitespace-only
    # ``CITEVYN_DEMO_API_KEY="   "`` is reported as the new name too rather than
    # as "CITEVYN_DEMO_API_KEY is not set", which was both wrong words and the
    # wrong variable.
    if _declared_in_env_file CITEVYN_PUBLIC_CLIENT_TOKEN; then
        _token_name=CITEVYN_PUBLIC_CLIENT_TOKEN
        _token_value="${CITEVYN_PUBLIC_CLIENT_TOKEN:-}"
    elif _declared_in_env_file CITEVYN_DEMO_API_KEY; then
        _token_name=CITEVYN_DEMO_API_KEY
        _token_value="${CITEVYN_DEMO_API_KEY:-}"
    else
        _token_name=CITEVYN_PUBLIC_CLIENT_TOKEN
        _token_value=""
    fi
    if [[ -z "$(_strip "${_token_value}")" ]]; then
        _token_name=CITEVYN_PUBLIC_CLIENT_TOKEN
    fi
    _assert_strong_key "${_token_name}" local-demo-key "${_token_value}"
    # The key that can promote an index, read the budget, and inspect jobs.
    _assert_strong_key CITEVYN_ADMIN_API_KEY local-admin-key "${CITEVYN_ADMIN_API_KEY:-}"
); then
    return 1 2>/dev/null || exit 1
fi
