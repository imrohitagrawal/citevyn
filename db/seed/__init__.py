"""Seed scripts for the CiteVyn database.

Idempotent scripts that insert a known-good baseline of rows that
local development and tests can rely on. Run after ``alembic upgrade
head``.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url

__all__ = ["redact_database_url"]


def redact_database_url(database_url: str) -> str:
    """Return a log-safe rendering of ``database_url`` with the password masked.

    The seed scripts print a one-line success summary that names the target
    database. ``CITEVYN_DATABASE_URL`` embeds the Postgres password, and these
    scripts run under ``deploy.sh`` / CI, so printing the raw URL leaks the
    credential into deploy and CI logs (#93, AGENTS.md "never log secrets").

    SQLAlchemy's :meth:`URL.render_as_string` masks only the password
    (``postgresql+psycopg://citevyn:***@db:5432/citevyn``), keeping the driver,
    user, host, and database name that make the log line useful. A URL that
    cannot be parsed is reported as ``<unparseable database url>`` rather than
    echoed verbatim, so a malformed value can never leak a password either.
    """
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 — never echo an unparseable URL (could hold a secret)
        return "<unparseable database url>"
