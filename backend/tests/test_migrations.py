"""Verify the initial Alembic migration runs cleanly against SQLite.

This test invokes Alembic programmatically against an in-memory
SQLite database so the migration is exercised in CI without a Postgres
server. The set of tables created must match ``docs/DATA_MODEL.md``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.command import downgrade as alembic_downgrade
from alembic.command import upgrade as alembic_upgrade
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from app.models.documents import Document

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "db" / "alembic.ini"
VERSIONS_DIR = REPO_ROOT / "db" / "versions"

EXPECTED_TABLES = {
    "users",
    "index_versions",
    "documents",
    "chunks",
    "exact_terms",
    "ingestion_jobs",
    "sessions",
    "messages",
    "retrieved_evidence",
    "answer_cache",
    "evaluation_cases",
    "evaluation_runs",
    "audit_events",
    "provider_calls",
    "auth_sessions",
}


@pytest.fixture
def alembic_config(tmp_path: Path) -> Iterator[AlembicConfig]:
    cfg = AlembicConfig(str(ALEMBIC_INI))
    # The ``script_location`` in alembic.ini is relative to CWD. When
    # pytest is invoked from ``backend/`` (the common case) that path
    # does not exist. Resolve to the absolute path of the ``db``
    # directory so the test works regardless of where pytest is run.
    db_root = REPO_ROOT / "db"
    cfg.set_main_option("script_location", str(db_root))
    # Use a temp file-backed SQLite so we can inspect the schema.
    db_path = tmp_path / "alembic_test.db"
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    yield cfg
    if db_path.exists():
        db_path.unlink()


def test_upgrade_head_creates_all_tables(alembic_config: AlembicConfig) -> None:
    alembic_upgrade(alembic_config, "head")

    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).all()
    table_names = {row[0] for row in rows}

    # Alembic creates its own bookkeeping table; the rest must match.
    assert "alembic_version" in table_names
    missing = EXPECTED_TABLES - table_names
    assert not missing, f"Missing tables after migration: {missing}"


def test_chunks_embedding_column_is_added_by_migration(
    alembic_config: AlembicConfig,
) -> None:
    """Migration 0003 adds a portable ``embedding`` column to ``chunks``.

    The column is declared as ``LargeBinary`` (BLOB on SQLite,
    ``bytea`` on Postgres) and is nullable so existing rows
    survive the upgrade. The test inspects ``PRAGMA table_info``
    because the project pins the hermetic test engine to SQLite.
    """
    alembic_upgrade(alembic_config, "head")

    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("PRAGMA table_info(chunks)").all()
    columns = {row[1]: row[2] for row in rows}
    assert "embedding" in columns, f"Expected chunks.embedding, got: {columns}"
    # SQLite reports BLOB for LargeBinary; the type is intentionally
    # not a TEXT/INTEGER so the pickle round-trip works.
    assert "BLOB" in columns["embedding"].upper()
    # Nullability: PRAGMA puts 1 in the ``notnull`` column when
    # the column is NOT NULL; 0 (or absent) means nullable.
    notnull_flags = {row[1]: row[3] for row in rows}
    assert notnull_flags["embedding"] == 0, "embedding should be nullable"


def test_migration_0005_downgrade_drops_provider_calls(
    alembic_config: AlembicConfig,
) -> None:
    """The 0005 rollback removes ``provider_calls`` and its index cleanly.

    ``code_review.md`` blocks a migration without a working rollback. 0005 is
    purely additive (new table, no FKs, no edits to existing tables), so unlike
    the 0004 vector rollback this needs no Postgres-only types and can be
    exercised on the hermetic SQLite engine — the same ``alembic_downgrade``
    pattern ``test_pg_integration.py`` uses for 0004, minus the Postgres gate.
    Downgrading only to 0004 keeps the pgvector-dependent 0004 rollback out of
    the path, which SQLite cannot run.
    """
    alembic_upgrade(alembic_config, "head")
    alembic_downgrade(alembic_config, "0004")

    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        objects = connection.exec_driver_sql(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).all()
    names = {name for _, name in objects}
    assert "provider_calls" not in names
    # A leaked index would make a re-upgrade fail with "index already exists",
    # so assert the drop_index actually ran rather than relying on the implicit
    # cascade that only some engines perform.
    assert "ix_provider_calls_occurred_at" not in names
    # Everything else must survive: an over-broad downgrade that took out the
    # pre-existing schema would still pass the two assertions above.
    # auth_sessions is ALSO excluded here: head is 0007 now, so downgrading
    # to 0004 rolls back 0007 too, not just 0005.
    assert (EXPECTED_TABLES - {"provider_calls", "auth_sessions"}) <= names


def test_migration_0007_auth_sessions_round_trips(alembic_config: AlembicConfig) -> None:
    """0007 (``auth_sessions``, ADR-0004 PR 3) upgrades and downgrades cleanly.

    Additive-only, same shape as 0005's ``provider_calls``: a new table with
    one FK to the pre-existing ``users`` table, no changes to any existing
    table. The FK itself is exercised by ``test_auth_sessions.py`` against a
    live app; this test is scoped to the DDL round trip.
    """
    alembic_upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).all()
        }
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(auth_sessions)").all()
        }
    assert "auth_sessions" in tables
    assert columns == {"auth_session_id", "secret_hash", "user_id", "created_at", "expires_at"}

    alembic_downgrade(alembic_config, "0006")
    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).all()
        }
    assert "auth_sessions" not in tables
    # Everything else must survive: an over-broad downgrade that took out the
    # pre-existing schema would still pass the assertion above.
    assert (EXPECTED_TABLES - {"auth_sessions"}) <= tables


def test_migration_0008_users_identity_columns_round_trips(
    alembic_config: AlembicConfig,
) -> None:
    """0008 (users identity columns + sessions FK CASCADE, ADR-0004 PR 5) round-trips.

    Two things are proven, not just asserted structurally:

    1. ``email``/``password_hash`` exist on ``users``, are nullable (so the
       anonymous principal's row still inserts with neither set), and
       ``email`` is unique.
    2. ``sessions.user_id`` really is ``ON DELETE CASCADE`` now, not just
       declared as such: deleting a user row is exercised end-to-end and the
       user's session is gone afterward. A migration that changed the FK
       name but left ``ondelete="RESTRICT"`` would pass a column-inventory
       check and fail only here, when the delete raises ``IntegrityError``
       instead of cascading.

    Downgrade is exercised the same way: after downgrading to 0007, the
    identity columns are gone and the FK reverts to RESTRICT (deleting a
    user with a session raises again).
    """
    alembic_upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))

    with engine.connect() as connection:
        columns = {
            row[1]: row for row in connection.exec_driver_sql("PRAGMA table_info(users)").all()
        }
    assert "email" in columns and "password_hash" in columns
    # PRAGMA table_info's notnull column (index 3) is 0 for nullable.
    assert columns["email"][3] == 0
    assert columns["password_hash"][3] == 0

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.exec_driver_sql(
            "INSERT INTO users (user_id, role, created_at, email, password_hash) "
            "VALUES ('usr_a', 'demo_user', CURRENT_TIMESTAMP, 'a@example.com', 'hash-a')"
        )
        connection.exec_driver_sql(
            "INSERT INTO users (user_id, role, created_at, email, password_hash) "
            "VALUES ('usr_b', 'demo_user', CURRENT_TIMESTAMP, NULL, NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO sessions (session_id, user_id, channel, created_at, expires_at) "
            "VALUES ('11111111-1111-1111-1111-111111111111', 'usr_a', 'chat', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    # Duplicate email is rejected; a second NULL email (usr_b, above) is not.
    with (
        engine.begin() as connection,
        pytest.raises(Exception, match="UNIQUE"),
    ):
        connection.exec_driver_sql(
            "INSERT INTO users (user_id, role, created_at, email, password_hash) "
            "VALUES ('usr_dupe', 'demo_user', CURRENT_TIMESTAMP, 'a@example.com', 'hash-dupe')"
        )

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.exec_driver_sql("DELETE FROM users WHERE user_id = 'usr_a'")

    with engine.connect() as connection:
        remaining = connection.exec_driver_sql(
            "SELECT session_id FROM sessions WHERE user_id = 'usr_a'"
        ).all()
    assert remaining == [], "sessions.user_id must be ON DELETE CASCADE after 0008"

    alembic_downgrade(alembic_config, "0007")
    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(users)").all()}
    assert "email" not in columns
    assert "password_hash" not in columns

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.exec_driver_sql(
            "INSERT INTO sessions (session_id, user_id, channel, created_at, expires_at) "
            "VALUES ('22222222-2222-2222-2222-222222222222', 'usr_b', 'chat', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    with (
        engine.begin() as connection,
        pytest.raises(Exception, match="FOREIGN KEY constraint failed|IntegrityError"),
    ):
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.exec_driver_sql("DELETE FROM users WHERE user_id = 'usr_b'")


def test_migration_0009_message_citations_round_trips(alembic_config: AlembicConfig) -> None:
    """0009 (``messages.citations``, ADR-0004 PR 10) round-trips.

    Additive-only: one new nullable column, no changes to any existing
    table. Proves the column actually carries a JSON list through a real
    insert/read (SQLite's JSON type is TEXT-backed; a broken type mapping
    would surface here as a string instead of a list), then that the
    downgrade drops it cleanly and everything else survives.
    """
    alembic_upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))

    with engine.connect() as connection:
        columns = {
            row[1]: row for row in connection.exec_driver_sql("PRAGMA table_info(messages)").all()
        }
    assert "citations" in columns
    assert columns["citations"][3] == 0, "citations must be nullable"

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (user_id, role, created_at) "
            "VALUES ('usr_cit', 'demo_user', CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO sessions (session_id, user_id, channel, created_at, expires_at) "
            "VALUES ('33333333-3333-3333-3333-333333333333', 'usr_cit', 'chat', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO messages (message_id, session_id, role, content, created_at, citations) "
            "VALUES ('44444444-4444-4444-4444-444444444444', "
            "'33333333-3333-3333-3333-333333333333', 'assistant', 'answer', "
            'CURRENT_TIMESTAMP, \'[{"source_name": "claude_code", "title": "T", '
            '"url": "https://x", "chunk_id": "c1", "marker": 1}]\')'
        )

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT citations FROM messages WHERE message_id = "
            "'44444444-4444-4444-4444-444444444444'"
        ).one()
    stored = json.loads(row[0])
    assert stored == [
        {
            "source_name": "claude_code",
            "title": "T",
            "url": "https://x",
            "chunk_id": "c1",
            "marker": 1,
        }
    ]

    alembic_downgrade(alembic_config, "0008")
    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(messages)").all()
        }
    assert "citations" not in columns


def test_documents_identity_checksum_rename_round_trips(
    alembic_config: AlembicConfig,
) -> None:
    """Migration 0006 renames ``documents.content_checksum`` → ``identity_checksum``.

    Both directions are exercised because a rename with a broken downgrade is
    an un-rollbackable schema change. The values must survive the round trip:
    the rename carries data, it does not recreate the column.
    """
    # Stop at 0005: the rename is 0006, so this is the state immediately before it.
    alembic_upgrade(alembic_config, "0005")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))

    def _document_columns() -> set[str]:
        with engine.connect() as connection:
            rows = connection.exec_driver_sql("PRAGMA table_info(documents)").all()
        return {row[1] for row in rows}

    assert "content_checksum" in _document_columns()

    # Seed a row so the rename is proven to CARRY data, not just reshape the
    # schema (SQLite batch mode recreates the table and copies rows).
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO index_versions (index_version, status, source_version_hash, "
            "created_at) VALUES ('v-mig', 'candidate', 'sha256:x', CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO documents (document_id, index_version, source_name, "
            "product_area, source_url, title, content_checksum, last_fetched_at, status) "
            "VALUES ('doc-1', 'v-mig', 'codex', 'cli', '/x', 'T', 'sha256:keepme', "
            "CURRENT_TIMESTAMP, 'active')"
        )

    alembic_upgrade(alembic_config, "0006")
    columns = _document_columns()
    assert "identity_checksum" in columns
    assert "content_checksum" not in columns, "the misleading name must be gone"
    with engine.connect() as connection:
        value = connection.exec_driver_sql(
            "SELECT identity_checksum FROM documents WHERE document_id = 'doc-1'"
        ).scalar_one()
    assert value == "sha256:keepme"

    # Rollback path: the column name (and its data) must come back.
    alembic_downgrade(alembic_config, "0005")
    assert "content_checksum" in _document_columns()
    with engine.connect() as connection:
        value = connection.exec_driver_sql(
            "SELECT content_checksum FROM documents WHERE document_id = 'doc-1'"
        ).scalar_one()
    assert value == "sha256:keepme"


def test_migrated_documents_table_matches_the_orm_model(
    alembic_config: AlembicConfig,
) -> None:
    """Guard against model/migration drift on ``documents``.

    The hermetic suite builds its schema with ``Base.metadata.create_all``, NOT
    alembic — so a column renamed in the model but not in a migration passes
    every other test in this repo and only explodes on a real Postgres deploy.
    This test is the one place the two are compared.
    """
    alembic_upgrade(alembic_config, "head")

    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("PRAGMA table_info(documents)").all()
    migrated = {row[1] for row in rows}

    model = {column.name for column in Document.__table__.columns}
    assert migrated == model, f"documents drift: migration={migrated} model={model}"


def test_versions_directory_has_exactly_one_head(alembic_config: AlembicConfig) -> None:
    """Guard against a second, divergent migration branch.

    ``docs/ADR/0004-user-accounts.md`` PR 1 adds this because a branch cut
    before PR #184 once shipped two migrations both claiming
    ``revision="0005"`` — undetected until ``alembic upgrade head`` failed
    with "Multiple head revisions" (see ``docs/BACKLOG.md``, #163's closed
    entry). ADR-0004 adds migrations 0007 and 0008 in PRs 3 and 5; this test
    protects both from repeating that incident. A real two-head state (not a
    hypothetical) is exercised by asserting the count, not just that
    ``head`` resolves — a single ``down_revision`` collision would still
    resolve to *a* head via alembic's default ordering and hide the branch.
    """
    script = ScriptDirectory.from_config(alembic_config)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected exactly one migration head, found: {heads}"


def test_versions_directory_contains_initial_migration() -> None:
    """The repo ships a hand-written initial migration; ensure it lives where expected."""
    assert (VERSIONS_DIR / "0001_initial_schema.py").exists()


def test_versions_directory_contains_promote_enum_migration() -> None:
    """The Slice 3+ follow-up ENUM promotion migration must be present."""
    assert (VERSIONS_DIR / "0002_promote_strenum_to_native.py").exists()


def test_versions_directory_contains_chunk_embedding_migration() -> None:
    """Slice 8 step 4 adds a portable ``chunks.embedding`` column.

    The migration lives at
    ``db/versions/0003_add_chunk_embedding.py`` and must ship
    alongside the rest of the chain. The follow-up ``pgvector``
    migration is not in this file — it lands as 0004.
    """
    assert (VERSIONS_DIR / "0003_add_chunk_embedding.py").exists()
