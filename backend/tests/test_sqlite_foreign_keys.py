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
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

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


def _import_probe(names: Sequence[str]) -> tuple[bool, list[str]]:
    """Import every name in ``names`` in ONE clean interpreter.

    Returns ``(app.core.db ended up in sys.modules, the names that imported)``.

    Names that are not modules are skipped rather than fatal: an AST walk over
    a ``from x import y`` cannot tell a submodule from an attribute, and only
    the submodule case can load anything. The caller is handed the list that
    DID import so it can refuse to draw a conclusion from a probe where
    nothing resolved -- otherwise a renamed module would turn this guard
    vacuous while it stayed green.
    """
    import json
    import subprocess
    import sys

    backend = Path(__file__).resolve().parents[1]
    script = (
        "import sys, json, importlib\n"
        f"names = {list(names)!r}\n"
        "imported = []\n"
        "for name in names:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except ModuleNotFoundError:\n"
        "        continue\n"
        "    imported.append(name)\n"
        "print(json.dumps({'loaded': 'app.core.db' in sys.modules, "
        "'imported': imported}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend.parent,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(backend), "PATH": "/usr/bin:/bin"},
        check=True,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    return payload["loaded"], payload["imported"]


def _loads_app_core_db(module: str) -> bool:
    """Whether importing ``module`` registers the pragma listener."""
    loaded, imported = _import_probe([module])
    assert imported == [module], f"{module!r} did not import at all; probe is meaningless"
    return loaded


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

    Its partner is ``test_the_entry_points_that_write_child_rows_load_the_hook``
    above: together they prove ``_loads_app_core_db`` discriminates at all,
    rather than returning False for everything.
    """
    assert _loads_app_core_db("db.seed.seed_users") is False


def _modules_imported_by(path: Path) -> list[str]:
    """Every module name ``path`` imports, read from its AST.

    ``db/env.py`` cannot be imported directly (it calls ``alembic.context``
    at module scope and dies with ``AttributeError`` outside an alembic run),
    so its imports are parsed instead of executed.
    """
    import ast

    tree = ast.parse(path.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            # ``from app.core import db`` imports ``app.core.db``; a plain
            # ``from app.core.config import X`` imports ``app.core.config``.
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return sorted(set(names))


async def test_alembic_does_not_load_the_hook_and_still_needs_not_to() -> None:
    """Alembic's exclusion is deliberate, and both halves of that are checked.

    Half one: nothing ``db/env.py`` imports drags in ``app.core.db``. This
    RESOLVES each import in a clean interpreter rather than grepping the file
    for a string. A string check is not a guard here -- a skeptic demonstrated
    two ways past one: ``from app.core import db as db_module`` (the exact
    idiom ``tests/conftest.py`` uses) never spells ``app.core.db``, and
    ``from app.worker.cli import ...`` pulls the hook in transitively with no
    mention of it at all. Both left the string check green while the hook was
    loaded. It also went RED on a comment that merely said the words.

    Half two: the REASON for the exclusion is still real, because a bare
    absence assertion proves nothing on its own. ``op.batch_alter_table`` is
    still in use; on SQLite that is create-new-table / copy-rows / drop-old /
    rename, and SQLite's own documentation says to run that with foreign keys
    OFF, since its intermediate states are legitimately inconsistent. Matching
    ``op.batch_alter_table(`` rather than the bare name keeps a passing
    mention in a comment from satisfying it.

    RED if any import of ``db/env.py`` starts loading ``app.core.db``, by any
    spelling or transitively; red the other way if every batch migration
    disappears, at which point the exclusion is an unexamined gap rather than
    a decision.
    """
    db_dir = Path(__file__).resolve().parents[2] / "db"
    env_py = db_dir / "env.py"
    assert env_py.is_file(), f"alembic env not found at {env_py}"

    candidates = _modules_imported_by(env_py)
    assert candidates, f"parsed no imports at all from {env_py}; the AST walk is broken"
    app_candidates = [name for name in candidates if name.split(".")[0] in {"app", "db"}]
    assert app_candidates, (
        "db/env.py imports nothing from app/ or db/ any more, so this guard "
        "resolves nothing; re-check what it is meant to cover"
    )

    loaded, imported = _import_probe(app_candidates)
    assert imported, (
        f"none of {app_candidates} imported, so the probe proves nothing -- a "
        "renamed module would leave this guard green while enforcement changed"
    )
    assert not loaded, (
        f"importing what db/env.py imports ({imported}) now loads app.core.db, "
        "so alembic runs with foreign keys ON -- that breaks "
        "op.batch_alter_table on SQLite. Either revert the import or update "
        "app/core/db.py's documented boundary."
    )

    versions = db_dir / "versions"
    assert versions.is_dir(), f"migration directory not found at {versions}"
    using_batch = sorted(
        p.name for p in versions.glob("*.py") if "op.batch_alter_table(" in p.read_text()
    )
    assert using_batch, "no migration uses batch_alter_table; revisit the alembic exclusion"


async def test_the_alembic_import_guard_resolves_imports_it_cannot_see_textually() -> None:
    """Partner: the guard above asserts an ABSENCE, so prove it can find one.

    Feeds ``_modules_imported_by`` + ``_loads_app_core_db`` a synthetic module
    that imports ``app.worker.cli`` -- which loads ``app.core.db``
    transitively, without the string ``app.core.db`` appearing anywhere. If
    this comes back empty, the guard above is vacuous and would stay green
    through exactly the change it exists to catch.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe_env.py"
        probe.write_text("from app.worker.cli import build_runner\n")
        candidates = _modules_imported_by(probe)

    assert "app.worker.cli" in candidates, candidates
    assert "app.core.db" not in "".join(candidates), (
        "the probe is supposed to hide the string; it did not, so this test "
        "no longer proves the guard sees past textual matching"
    )

    loaded, imported = _import_probe(candidates)
    assert imported, "the probe imported nothing; it proves nothing"
    assert loaded, "the guard's resolver failed to notice a transitive load of app.core.db"


# ---------------------------------------------------------------------------
# 5. The seed helpers refuse to hand back a row in the wrong state
# ---------------------------------------------------------------------------


async def test_seeding_a_user_twice_with_a_different_role_raises(
    fk_session: AsyncSession,
) -> None:
    """``seed_user`` is idempotent but not silent.

    A quiet no-op would let a test believe it was acting as an admin while
    the row said ``demo_user`` -- invisible wrongness of exactly the kind
    this file exists to end. RED if the conflict check is dropped: the second
    call returns and the role silently stays ``demo_user``.
    """
    from app.models import UserRole
    from tests.conftest import seed_user

    await seed_user(fk_session, "dual_role", role=UserRole.demo_user)
    with pytest.raises(AssertionError, match="already seeded with role"):
        await seed_user(fk_session, "dual_role", role=UserRole.admin)


async def test_seeding_a_user_twice_with_the_same_role_is_a_quiet_no_op(
    fk_session: AsyncSession,
) -> None:
    """Partner to the test above.

    Without it, "it raises" is also satisfied by a helper that raises on
    EVERY repeat call -- which would break every fixture that legitimately
    seeds the same id twice, and would then be caught only by the full suite
    rather than here.

    The third call passes no role at all: that is the shape
    ``seed_chat_session`` uses, and it must stay a no-op against an admin row
    or an admin can never own a chat session.
    """
    from app.models import UserRole
    from tests.conftest import seed_user

    await seed_user(fk_session, "same_role", role=UserRole.admin)
    await seed_user(fk_session, "same_role", role=UserRole.admin)  # no raise
    await seed_user(fk_session, "same_role")  # status-agnostic, also no raise

    stored = await fk_session.get(User, "same_role")
    assert stored is not None and stored.role is UserRole.admin


async def test_requesting_an_index_status_that_conflicts_raises(
    fk_session: AsyncSession,
) -> None:
    """``seed_index_version`` polices only a status the caller ASKED for.

    ``seed_evidence_chunks`` calls it with no status, because it just needs
    the foreign-key parent to exist. A caller that explicitly wants ``active``
    must not silently receive the ``candidate`` row that call created and then
    assert against the wrong branch. RED if the conflict check is dropped.
    """
    from app.models import IndexStatus
    from tests.conftest import seed_index_version

    await seed_index_version(fk_session, "idx_conflict")  # implicit candidate
    with pytest.raises(AssertionError, match="already seeded as"):
        await seed_index_version(fk_session, "idx_conflict", status=IndexStatus.active)


async def test_not_asking_for_a_status_accepts_whatever_row_exists(
    fk_session: AsyncSession,
) -> None:
    """The partner, and the reason the status argument is optional at all.

    A status-agnostic call must stay a no-op against an existing row whatever
    its state. Without this, the check above is also satisfied by a helper
    that raises on any repeat call -- which breaks ``seed_evidence_chunks``
    on every test that seeded an active index first. Not hypothetical: the
    first version of this guard did exactly that and turned 93 tests red.
    """
    from app.models import IndexStatus, IndexVersion
    from tests.conftest import seed_index_version

    await seed_index_version(fk_session, "idx_active", status=IndexStatus.active)
    await seed_index_version(fk_session, "idx_active")  # no raise

    stored = await fk_session.get(IndexVersion, "idx_active")
    assert stored is not None and stored.status is IndexStatus.active
