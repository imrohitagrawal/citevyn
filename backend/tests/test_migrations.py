"""Verify the initial Alembic migration runs cleanly against SQLite.

This test invokes Alembic programmatically against an in-memory
SQLite database so the migration is exercised in CI without a Postgres
server. The set of tables created must match ``docs/DATA_MODEL.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.command import downgrade as alembic_downgrade
from alembic.command import upgrade as alembic_upgrade
from alembic.config import Config as AlembicConfig
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
    assert (EXPECTED_TABLES - {"provider_calls"}) <= names


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
