"""SQLite foreign-key enforcement is on, and it actually rejects things (#286).

SQLite ships with ``PRAGMA foreign_keys`` OFF, per connection, and nothing in
this codebase turned it on until #286. Every child row inserted against a
parent that did not exist was silently accepted across ~1900 tests, while
real Postgres -- the production dialect -- always rejects it. That hid a real
production bug in ``Orchestrator._ensure_user`` (and, before it,
``auth_sessions._mint_principal``).

Every assertion here is about what a real connection REPORTS or what a real
INSERT DOES. Deliberately nothing greps ``app/core/db.py`` for the pragma
string: a source-text check would pass just as happily against a hook that
is registered on the wrong event, never fires, or writes to a cursor it
immediately discards.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.db import (
    build_engine,
    enable_sqlite_foreign_keys,
    is_sqlite_dbapi_connection,
)
from app.models import Base, Message, MessageRole, User

pytestmark = pytest.mark.asyncio


_SQLITE_URL = "sqlite+aiosqlite:///:memory:"


async def _pragma(engine) -> int:
    async with engine.connect() as connection:
        return (await connection.execute(text("PRAGMA foreign_keys"))).scalar()


# ---------------------------------------------------------------------------
# 1. The pragma is actually on, on both engine-construction paths
# ---------------------------------------------------------------------------


async def test_build_engine_reports_foreign_keys_on() -> None:
    """The app's own engine factory. RED if the hook is dropped or misfires."""
    engine = build_engine(Settings(_env_file=None, database_url=_SQLITE_URL))
    try:
        assert await _pragma(engine) == 1
    finally:
        await engine.dispose()


async def test_a_directly_constructed_engine_reports_foreign_keys_on() -> None:
    """The shape ~30 test modules use: ``create_async_engine`` with no help
    from ``build_engine``.

    This is the whole reason the listener is registered on the ``Engine``
    CLASS in ``app/core/db.py`` rather than on the instance inside
    ``build_engine``. RED if someone "tidies" it into ``build_engine``: this
    engine would come back 0 while the one above still reported 1.
    """
    engine = create_async_engine(_SQLITE_URL)
    try:
        assert await _pragma(engine) == 1
    finally:
        await engine.dispose()


async def test_every_connection_gets_the_pragma_not_just_the_first(tmp_path) -> None:
    """SQLite scopes the pragma to a connection; there is no database-level
    setting. RED if the hook is moved to ``first_connect``, which fires once
    per engine and leaves every LATER connection unprotected.

    The connections are held open SIMULTANEOUSLY and the engine is
    file-backed on purpose. Opening them one at a time returns the same
    pooled DBAPI handle, so no second ``connect`` event fires and the test
    would pass against the broken hook -- verified: an earlier sequential
    version of this test did exactly that and let the ``first_connect``
    mutant through.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}")
    try:
        async with engine.connect() as first, engine.connect() as second:
            assert first.sync_connection is not second.sync_connection
            reported = [
                (await first.execute(text("PRAGMA foreign_keys"))).scalar(),
                (await second.execute(text("PRAGMA foreign_keys"))).scalar(),
            ]
        assert reported == [1, 1]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 2. It rejects a real violating write -- and accepts the valid one
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fk_session():
    """A session on a throwaway SQLite engine with the full schema."""
    engine = create_async_engine(_SQLITE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _message(session_id: uuid.UUID) -> Message:
    return Message(
        session_id=session_id,
        role=MessageRole.user,
        content="hello",
        created_at=datetime.now(UTC),
    )


async def test_an_insert_naming_a_missing_parent_is_rejected(
    fk_session: AsyncSession,
) -> None:
    """``messages.session_id`` -> ``sessions.session_id``, with no such row.

    This is the assertion that matters: a pragma that reads back as 1 but
    does not bite would still pass every check above. RED the moment
    enforcement is off -- which is precisely how this suite behaved for its
    entire life before #286.
    """
    fk_session.add(_message(uuid.uuid4()))
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        await fk_session.flush()


async def test_the_same_insert_succeeds_once_the_parent_exists(
    fk_session: AsyncSession,
) -> None:
    """The partner to the test above.

    Without this, "the insert raised" is satisfiable by a schema typo, a
    broken fixture, or a NOT NULL violation dressed up as a foreign key --
    the failure would not be evidence of anything. Same row, same code path,
    only the parent chain differs.
    """
    from tests.conftest import seed_chat_session

    session_id = uuid.uuid4()
    await seed_chat_session(fk_session, session_id)
    message = _message(session_id)
    fk_session.add(message)
    await fk_session.flush()  # no raise

    stored = await fk_session.get(Message, message.message_id)
    assert stored is not None and stored.session_id == session_id


async def test_a_cascade_delete_actually_cascades(fk_session: AsyncSession) -> None:
    """``sessions.user_id`` is declared ``ON DELETE CASCADE``.

    With enforcement off, SQLite ignored the clause entirely -- so a test
    could delete a user and assert its sessions SURVIVED, and pass. One in
    ``test_magic_link_routes.py`` did exactly that. RED if the pragma is off:
    the session row would still be there.
    """
    from tests.conftest import seed_chat_session

    session_id = uuid.uuid4()
    await seed_chat_session(fk_session, session_id, user_id="cascade_me")
    await fk_session.commit()

    user = await fk_session.get(User, "cascade_me")
    assert user is not None
    await fk_session.delete(user)
    await fk_session.commit()

    from app.models import Session as SessionModel

    assert await fk_session.get(SessionModel, session_id) is None


# ---------------------------------------------------------------------------
# 3. The dialect predicate
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Stands in for a DBAPI connection from a given driver module."""


def _connection_from_module(module: str) -> object:
    fake = type("_Conn", (_FakeConnection,), {})
    fake.__module__ = module
    return fake()


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        # The async adapter class SQLAlchemy wraps aiosqlite in -- what every
        # engine in this app and its test suite actually uses. It is NOT
        # ``sqlite3``, which is the trap a startswith() check falls into.
        ("sqlalchemy.dialects.sqlite.aiosqlite", True),
        ("sqlite3", True),  # the stdlib sync driver (alembic, seed scripts)
        # Postgres must NOT be matched: PRAGMA is not valid SQL there, so a
        # false positive would break every production connection on startup.
        ("psycopg", False),
        ("sqlalchemy.dialects.postgresql.psycopg", False),
        ("sqlalchemy.dialects.mysql.aiomysql", False),
    ],
)
async def test_the_sqlite_predicate_matches_only_sqlite(module: str, expected: bool) -> None:
    assert is_sqlite_dbapi_connection(_connection_from_module(module)) is expected


class _RecordingConnection:
    """A DBAPI connection that reports whether anyone asked it for a cursor."""

    def __init__(self) -> None:
        self.cursors_opened = 0
        self.executed: list[str] = []

    def cursor(self) -> _RecordingConnection:
        self.cursors_opened += 1
        return self

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def close(self) -> None:
        pass


def _recording_connection(module: str) -> _RecordingConnection:
    cls = type("_Rec", (_RecordingConnection,), {})
    cls.__module__ = module
    return cls()


async def test_the_hook_issues_the_pragma_for_a_sqlite_connection() -> None:
    """The hook's own behaviour, not the predicate's. Partner to the test
    below: without it, "no SQL was issued" is satisfiable by a hook that
    issues nothing to anyone."""
    connection = _recording_connection("sqlalchemy.dialects.sqlite.aiosqlite")
    enable_sqlite_foreign_keys(connection, object())
    assert connection.executed == ["PRAGMA foreign_keys=ON"]


async def test_the_hook_touches_nothing_on_a_postgres_connection() -> None:
    """``PRAGMA`` is not valid SQL on Postgres, and this listener is
    registered on the ``Engine`` CLASS, so it fires for the production
    engine's every connection too.

    RED if the ``is_sqlite_dbapi_connection`` guard clause is deleted: the
    hook would open a cursor on a psycopg connection and issue a statement
    Postgres rejects, breaking every production connection at checkout. The
    guard is the one thing keeping a global class-level listener safe, and
    nothing in the hermetic suite connects to Postgres to notice.
    """
    connection = _recording_connection("sqlalchemy.dialects.postgresql.psycopg")
    enable_sqlite_foreign_keys(connection, object())
    assert connection.cursors_opened == 0
    assert connection.executed == []


async def test_the_predicate_agrees_with_the_connection_the_hook_really_sees() -> None:
    """The parametrised cases above use hand-written module strings, which go
    stale silently if SQLAlchemy ever relocates the adapter class.

    This captures the exact object the production listener is handed, from
    the same ``Engine`` "connect" event, and runs the real predicate over
    it. If a SQLAlchemy upgrade moves the class, this fails loudly instead
    of quietly turning enforcement back off everywhere.
    """
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    captured: list[object] = []

    def _capture(dbapi_connection: object, connection_record: object) -> None:
        captured.append(dbapi_connection)

    event.listen(Engine, "connect", _capture)
    engine = create_async_engine(_SQLITE_URL)
    try:
        async with engine.connect():
            pass
    finally:
        await engine.dispose()
        event.remove(Engine, "connect", _capture)

    assert captured, "the Engine connect event never fired"
    assert is_sqlite_dbapi_connection(captured[0]) is True


# ---------------------------------------------------------------------------
# 4. Which entry points the hook actually reaches
# ---------------------------------------------------------------------------


def _loads_app_core_db(module: str) -> bool:
    """Import ``module`` in a clean interpreter and report whether that pulled
    in ``app.core.db`` (and therefore registered the pragma listener)."""
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    script = (
        "import sys, importlib\n"
        f"importlib.import_module({module!r})\n"
        "print('app.core.db' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend.parent,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(backend), "PATH": "/usr/bin:/bin"},
        check=True,
    )
    return result.stdout.strip().splitlines()[-1] == "True"


@pytest.mark.parametrize("module", ["app.worker.cli", "db.seed.seed_catalog"])
async def test_the_entry_points_that_write_child_rows_load_the_hook(module: str) -> None:
    """The listener only exists in a process that imported ``app.core.db``.

    Registering on the ``Engine`` class is process-wide but not magic: an
    entry point that never imports this module gets no enforcement at all.
    These two build engines and write rows carrying foreign keys, so they
    must load it. RED if an import is refactored away and enforcement
    silently disappears for the ingest or seed path.
    """
    assert _loads_app_core_db(module) is True


async def test_the_documented_uncovered_entry_point_is_still_uncovered() -> None:
    """The other half of the boundary drawn in ``app/core/db.py``'s docstring.

    Without this, the COVERED list above is satisfiable by everything loading
    the hook, and the docstring's boundary would be prose nobody checks.
    ``seed_users`` writes only ``users`` rows, which carry no foreign keys of
    their own, so it has nothing to enforce -- if that ever changes, this
    fails and the docstring gets revisited instead of quietly going stale.

    ``db/env.py`` (alembic) belongs on this list too but cannot be imported
    outside an alembic run, so it is covered by the test below instead.
    """
    assert _loads_app_core_db("db.seed.seed_users") is False


async def test_batch_alter_table_migrations_still_exist() -> None:
    """The partner to the test above.

    That one asserts an ABSENCE -- alembic does not load the hook -- which
    proves nothing on its own unless the reason for the absence is real. Four
    migrations use ``op.batch_alter_table``, whose SQLite implementation is
    create-new-table / copy / drop / rename, and SQLite's own documentation
    says to run that with foreign keys OFF. If no migration used it any more,
    excluding alembic would be an unexamined gap rather than a decision, and
    this test says so.
    """
    from pathlib import Path

    versions = Path(__file__).resolve().parents[2] / "db" / "versions"
    assert versions.is_dir(), f"migration directory not found at {versions}"
    using_batch = sorted(
        p.name for p in versions.glob("*.py") if "batch_alter_table" in p.read_text()
    )
    assert using_batch, "no migration uses batch_alter_table; revisit the alembic exclusion"
