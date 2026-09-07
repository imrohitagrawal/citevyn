"""Async database engine, session factory, and health helpers.

Slice 2 introduces the persistence layer. Engine creation accepts a
``Settings`` instance and selects the driver from the URL scheme:

* ``postgresql+psycopg://`` — production (Phase 1 deployment target).
* ``sqlite+aiosqlite://`` — local fallback and the engine used by tests.

The engine is created lazily via :func:`get_engine` and reused across the
application lifetime. Sessions are obtained through :func:`get_session`,
which is a FastAPI dependency that yields a session and rolls back on
exception.

SQLite ships with foreign-key enforcement OFF by default, per connection.
This module turns it back on (see :func:`enable_sqlite_foreign_keys`) so
the SQLite dialect honours the same referential integrity Postgres does.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def is_sqlite_dbapi_connection(dbapi_connection: Any) -> bool:
    """Return True when ``dbapi_connection`` is a SQLite DBAPI handle.

    The check is a substring match on the connection class's *module*,
    which is the only dialect signal available inside a pool ``connect``
    event (the pool's connection record carries no dialect).

    The two shapes that occur in this codebase:

    * ``sqlite3`` — the stdlib synchronous driver (alembic, seed scripts).
    * ``sqlalchemy.dialects.sqlite.aiosqlite`` — the async adapter class
      SQLAlchemy wraps ``aiosqlite`` in. Note this is NOT ``sqlite3``:
      a ``startswith("sqlite3")`` test silently misses every async engine,
      which is every engine the app and the test suite actually use.

    A deliberately broad substring, because the failure modes are not
    symmetric: matching an unknown *future* SQLite driver costs nothing,
    while missing one silently restores the exact gap this closes. No
    non-SQLite driver in the dependency set has "sqlite" anywhere in its
    module path (psycopg is ``psycopg`` /
    ``sqlalchemy.dialects.postgresql.psycopg``).
    """
    return "sqlite" in type(dbapi_connection).__module__


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    """Turn on ``PRAGMA foreign_keys`` for every new SQLite connection.

    Registered against the ``Engine`` *class*, so it fires for every
    engine in the process — including the several test modules that call
    ``create_async_engine`` directly rather than going through
    :func:`build_engine`, and the worker/seed/alembic entry points. An
    engine-instance listener in :func:`build_engine` would cover none of
    those.

    SQLite scopes this pragma to the connection, so it has to be re-issued
    on each one; there is no database-level setting. On Postgres this is a
    single string comparison and no SQL.
    """
    if not is_sqlite_dbapi_connection(dbapi_connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    """Build dialect-specific engine kwargs.

    SQLite (used in tests) does not understand ``pool_size``; we only set
    pooling arguments for Postgres.
    """
    if settings.database_url.startswith("postgres"):
        return {
            "echo": settings.database_echo,
            "pool_size": settings.database_pool_size,
            "pool_pre_ping": True,
        }
    return {"echo": settings.database_echo}


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create a new async engine for the given settings.

    A fresh engine is created on every call. The application uses
    :func:`get_engine` which caches one for the lifetime of the process.
    """
    resolved = settings or get_settings()
    return create_async_engine(resolved.database_url, **_engine_kwargs(resolved))


def get_engine() -> AsyncEngine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating it on first use."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


def reset_engine() -> None:
    """Discard the cached engine and sessionmaker.

    Tests use this to swap between in-memory engines between cases.
    """
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a session and rolls back on error."""
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def ping_database() -> dict[str, Any]:
    """Run a trivial query and return a health summary.

    Returns a dict with ``status`` (``"healthy"`` / ``"unhealthy"``)
    and a numeric ``latency_ms``. The exception object itself never
    leaves this function. Logging the failure record happens at the
    call site (the route layer) so CodeQL's clear-text-logging flow
    analysis has no path from the except block to a logger.
    """
    engine = get_engine()
    started = time.perf_counter()
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
    except SQLAlchemyError:
        return {"status": "unhealthy", "latency_ms": _elapsed_ms(started)}
    except Exception:  # pragma: no cover - defensive
        return {"status": "unhealthy", "latency_ms": _elapsed_ms(started)}

    return {"status": "healthy", "latency_ms": _elapsed_ms(started)}


def _elapsed_ms(started: float) -> float:
    """Return wall-clock milliseconds since ``started`` (perf_counter)."""
    return round((time.perf_counter() - started) * 1000, 2)
