#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# _migration_gen.sh — shared "is this rollback target a different MIGRATION
# GENERATION?" helper. Sourced by rollback.sh and deploy_verify.sh.
#
# WHY THIS EXISTS
#
# rollback.sh rolls back CODE by checking out an older tag and rebuilding. The
# live DATABASE is not touched, so it stays stamped at the newest applied
# alembic revision. If that revision's file does not exist in the target tag's
# db/versions/, alembic cannot even build the version graph and dies with
#
#     Can't locate revision identified by '0006'
#
# — inside a one-shot container, mid-deploy, after the stack has already been
# torn toward the old release. This is not a tuning problem: a code-only
# rollback ACROSS a migration boundary is impossible by construction. The only
# recovery is a database restore (RUNBOOK §4.2).
#
# So the question every rollback path must answer BEFORE it touches anything is
# "does the target tree still contain every migration that HEAD ships?".
#
# WHAT IT COMPARES — and what that is a proxy for
#
# It compares TREES (target's db/versions vs the base revision's), not the live
# database's alembic_version row. That is deliberate:
#   • it needs no docker, no database, no credentials, so it works in --dry-run,
#     on a laptop, and in tests;
#   • the base revision is the tree that is deployed, so its newest migration is
#     the revision the live DB is stamped at after a successful deploy.
# The proxy is CONSERVATIVE in the safe direction: it can refuse a rollback that
# would in fact have worked (a migration file that landed but was never applied
# because the deploy failed first), and the escape hatch for that case is the
# operator-supplied --allow-migration-mismatch. It cannot MISS a boundary that a
# normal deploy already crossed.
#
# Only MISSING files matter. A migration file that merely CHANGED content
# between the two trees still resolves by revision id, so it is not fatal and is
# not reported here.
# ────────────────────────────────────────────────────────────────────────────

# List the alembic revision files a git revision ships, one basename per line.
# Matches the NNNN_name.py convention every file in db/versions/ uses, which
# also excludes __init__.py / __pycache__ noise.
_mg_list_migrations() {  # $1 = git revision
    git ls-tree -r --name-only "$1" -- db/versions 2>/dev/null \
        | sed -n 's#^.*db/versions/##p' \
        | grep -E '^[0-9]+_.*\.py$' \
        | sort
}

# Print the migration files present at <base> but ABSENT from <target>, one per
# line. Empty output means "same migration generation — a code-only rollback is
# safe". Exit status is always 0; callers branch on the output, so a target with
# no migrations at all is reported as a boundary rather than as an error.
migrations_missing_at() {  # $1 = target revision, $2 = base revision (default HEAD)
    local _target="$1" _base="${2:-HEAD}" _have _m
    # One space-delimited line so the `case` glob below can test membership.
    # bash 3.2 has no associative arrays; this is the portable equivalent.
    # Revision filenames never contain spaces (NNNN_snake_case.py).
    _have=" $(_mg_list_migrations "${_target}" | tr '\n' ' ') "
    for _m in $(_mg_list_migrations "${_base}"); do
        case "${_have}" in
            *" ${_m} "*) ;;
            *) printf '%s\n' "${_m}" ;;
        esac
    done
}
