"""Verify the initial Alembic migration runs cleanly against SQLite.

This test invokes Alembic programmatically against an in-memory
SQLite database so the migration is exercised in CI without a Postgres
server. The set of tables created must match ``docs/DATA_MODEL.md``.
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.command import downgrade as alembic_downgrade
from alembic.command import upgrade as alembic_upgrade
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import DefaultClause, Engine, MetaData, UniqueConstraint, create_engine, inspect
from sqlalchemy.exc import IntegrityError, SAWarning
from sqlalchemy.types import TypeEngine

import app.models as app_models
from app.models import Base

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
    # 0007's columns must all be present at head; later migrations (0013)
    # add to this table, so this is a subset check, not equality.
    assert {"auth_session_id", "secret_hash", "user_id", "created_at", "expires_at"} <= columns

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


def test_migration_0013_magic_link_verified_at_round_trips(alembic_config: AlembicConfig) -> None:
    """0013 (``auth_sessions.magic_link_verified_at``, ADR-0004 PR 15 / #293)
    adds one nullable column and the downgrade removes it -- in batch mode,
    so SQLite can drop a column the way Postgres does. RED if the column is
    missing at head, non-nullable (every password/OAuth/anonymous session
    inserts NULL), or survives the downgrade."""
    alembic_upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        columns = {
            row[1]: row
            for row in connection.exec_driver_sql("PRAGMA table_info(auth_sessions)").all()
        }
    assert "magic_link_verified_at" in columns
    assert columns["magic_link_verified_at"][3] == 0, "must be nullable"

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (user_id, role, created_at, email, password_hash) "
            "VALUES ('usr_su', 'demo_user', CURRENT_TIMESTAMP, 'su@example.com', NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO auth_sessions (auth_session_id, secret_hash, user_id, created_at, "
            "expires_at) VALUES ('44444444-4444-4444-4444-444444444444', 'h', 'usr_su', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    with engine.connect() as connection:
        stamped = connection.exec_driver_sql(
            "SELECT magic_link_verified_at FROM auth_sessions WHERE user_id = 'usr_su'"
        ).scalar_one()
    assert stamped is None, "a session inserted without the stamp reads back NULL"

    alembic_downgrade(alembic_config, "0012")
    with engine.connect() as connection:
        columns_after = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(auth_sessions)").all()
        }
        rows = connection.exec_driver_sql("SELECT count(*) FROM auth_sessions").scalar_one()
    assert "magic_link_verified_at" not in columns_after
    assert rows == 1, "batch downgrade must preserve existing rows"


def test_migration_0012_magic_link_tokens_round_trips(alembic_config: AlembicConfig) -> None:
    """0012 (``magic_link_tokens``, ADR-0004 PR 14) upgrades and downgrades cleanly.

    Additive-only, same shape as 0011's ``oauth_nonces``. Beyond the DDL
    round trip, two things are PROVEN rather than read off the DDL:

    1. the ``user_id`` index exists (every link request deletes by user);
    2. ``user_id`` really is ``ON DELETE CASCADE``: a user is deleted with a
       live token and the token is gone afterwards. A migration that wrote
       ``RESTRICT`` would pass a column inventory and fail only here.
       (SQLite with ``PRAGMA foreign_keys = ON`` -- the Postgres proof is in
       ``test_pg_integration.py``, see #286.)
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
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(magic_link_tokens)").all()
        }
        indexes = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA index_list(magic_link_tokens)").all()
        }
    assert "magic_link_tokens" in tables
    assert columns == {"token_id", "secret_hash", "user_id", "created_at", "expires_at"}
    assert "ix_magic_link_tokens_user_id" in indexes

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.exec_driver_sql(
            "INSERT INTO users (user_id, role, created_at, email, password_hash) "
            "VALUES ('usr_ml', 'demo_user', CURRENT_TIMESTAMP, 'ml@example.com', NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO magic_link_tokens "
            "(token_id, secret_hash, user_id, created_at, expires_at) "
            "VALUES ('33333333-3333-3333-3333-333333333333', 'h', 'usr_ml', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.exec_driver_sql("DELETE FROM users WHERE user_id = 'usr_ml'")
    with engine.connect() as connection:
        remaining = connection.exec_driver_sql(
            "SELECT token_id FROM magic_link_tokens WHERE user_id = 'usr_ml'"
        ).all()
    assert remaining == [], "magic_link_tokens.user_id must be ON DELETE CASCADE"

    alembic_downgrade(alembic_config, "0011")
    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).all()
        }
    assert "magic_link_tokens" not in tables
    assert "oauth_nonces" in tables
    assert tables >= EXPECTED_TABLES


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


# ── #455: the migrated schema must match the ORM models ─────────────────────
#
# The hermetic suite builds its schema with ``Base.metadata.create_all``, NOT
# alembic, and CI's ``alembic upgrade head`` step only proves the migrations
# RUN -- it never looks at the models. So a model column, index or constraint
# with no migration passed the whole suite and the CI upgrade step, and the
# first place it would fail is a real deploy. These tests are the comparison.
# They replace ``test_migrated_documents_table_matches_the_orm_model``, which
# compared only the column NAMES of one table. Every drift it could see shows
# up here as an add_column / remove_column item (checked by deleting
# ``Document.title``: both went red); this also sees the other tables, types,
# nullability, indexes, FKs and server defaults.
#
# The diff is not empty today, and the items that are left are listed below,
# each one keyed exactly (op + table + name/columns + unique flag), never by a
# loose string match, so a NEW item can never hide behind an old one.
#
# WHAT THE COMPARISON CANNOT SEE (it runs on SQLite, the only engine the
# hermetic suite has):
#   - anything a migration does only on Postgres: 0002's native ENUM types and
#     0004's pgvector column and HNSW index sit behind a dialect check, so on
#     SQLite they never run. A Postgres variant of this test does not exist.
#   - CHECK constraints: alembic's autogenerate does not compare them. None
#     exist in the models or migrations today.
#   - server defaults: compared, and a ``modify_default`` key carries both
#     rendered values, so a changed default on an allowlisted column is a NEW
#     key, not the old entry. Only "the migration has one, the model has none"
#     occurs today; how alembic compares two different non-empty defaults on
#     SQLite was not measured.
#   - an UNNAMED unique constraint: compare_metadata does not report one that
#     only a migration creates. ``test_migrated_uniqueness_matches_the_orm_models``
#     below compares uniqueness directly, because that is the permissive
#     direction this guard exists for.
# Types (compare_type) and nullability ARE compared, and on SQLite they add no
# noise: zero modify_type / modify_nullable items at 57d8d4f. Changing
# ``User.email`` to String(256), or to nullable=False, turns the test red.

DiffKey = tuple[object, ...]

# PERMISSIVE DIVERGENCE: the hermetic schema is LOOSER than production. See the
# comment on the entry below.
USERS_EMAIL_UNIQUE_INDEX: DiffKey = ("remove_index", "users", "ix_users_email", ("email",), True)

# Every item compare_metadata reports at head, and why it is tolerated.
# "remove_index" = the migration creates the index, the model does not declare
# it. "add_fk" = the model declares the FK, the migration never created it.
# "modify_default" = the migration sets a server default the model lacks.
KNOWN_DRIFT: dict[DiffKey, str] = {
    # PERMISSIVE DIVERGENCE (#455) -- the one that ships bugs. Migration 0008
    # creates ``ix_users_email`` UNIQUE, but ``User.email`` has no ``unique=``
    # and no ``__table_args__``. So every hermetic test runs on a schema that
    # ACCEPTS two users with the same email, where production REJECTS the second.
    # A code path that relies on the database to refuse a duplicate email is
    # therefore untested. CLOSED BY: an owner-approved model change declaring the
    # unique index on ``User.email`` (a schema-definition change, which is why it
    # is not made here). When that lands, this entry goes stale, the stale-entry
    # test below fails, and this entry must be deleted.
    USERS_EMAIL_UNIQUE_INDEX: "PERMISSIVE DIVERGENCE: users.email unique only in production",
    # STRICTER IN THE HERMETIC SCHEMA (latent). The model declares this FK and
    # ``app.core.db`` turns SQLite FK enforcement on, but migration 0001 created
    # ``evaluation_runs`` without it, so production accepts a run pointing at a
    # missing index_version. Closed by a migration adding the FK.
    (
        "add_fk",
        "evaluation_runs",
        ("index_version",),
        ("index_versions.index_version",),
        "RESTRICT",
    ): "STRICTER in tests: FK exists in the model, not in production",
    # STRICTER IN THE HERMETIC SCHEMA (latent). Migration 0005 gives these
    # columns a server default; the model uses Python-side ``default=`` instead,
    # which every ORM insert applies. Only a raw SQL insert that omits the column
    # behaves differently: production fills it, the hermetic schema refuses it.
    ("modify_default", "provider_calls", "input_tokens", "'0'", None): (
        "STRICTER in tests: server default"
    ),
    ("modify_default", "provider_calls", "output_tokens", "'0'", None): (
        "STRICTER in tests: server default"
    ),
    ("modify_default", "provider_calls", "attempts", "'1'", None): (
        "STRICTER in tests: server default"
    ),
    ("modify_default", "provider_calls", "cost_usd", "'0'", None): (
        "STRICTER in tests: server default"
    ),
    ("modify_default", "provider_calls", "priced", "1", None): (
        "STRICTER in tests: server default"
    ),
    ("modify_default", "provider_calls", "tokens_estimated", "0", None): (
        "STRICTER in tests: server default"
    ),
    # HARMLESS: non-unique indexes the migrations create for query speed and the
    # models never declared. No row is accepted or refused differently; only
    # query plans differ. Closed by declaring ``index=True`` on the model column.
    ("remove_index", "audit_events", "ix_audit_events_timestamp", ("timestamp",), False): (
        "HARMLESS: non-unique index only in production"
    ),
    ("remove_index", "audit_events", "ix_audit_events_user_id", ("user_id",), False): (
        "HARMLESS: non-unique index only in production"
    ),
    ("remove_index", "chunks", "ix_chunks_product_area", ("product_area",), False): (
        "HARMLESS: non-unique index only in production"
    ),
    ("remove_index", "documents", "ix_documents_product_area", ("product_area",), False): (
        "HARMLESS: non-unique index only in production"
    ),
    ("remove_index", "documents", "ix_documents_source_name", ("source_name",), False): (
        "HARMLESS: non-unique index only in production"
    ),
    ("remove_index", "evaluation_runs", "ix_evaluation_runs_suite_name", ("suite_name",), False): (
        "HARMLESS: non-unique index only in production"
    ),
    ("remove_index", "ingestion_jobs", "ix_ingestion_jobs_status", ("status",), False): (
        "HARMLESS: non-unique index only in production"
    ),
    ("remove_index", "messages", "ix_messages_session_id", ("session_id",), False): (
        "HARMLESS: non-unique index only in production"
    ),
    (
        "remove_index",
        "retrieved_evidence",
        "ix_retrieved_evidence_message_id",
        ("message_id",),
        False,
    ): "HARMLESS: non-unique index only in production",
    ("remove_index", "user_identities", "ix_user_identities_user_id", ("user_id",), False): (
        "HARMLESS: non-unique index only in production"
    ),
}


def _full_orm_metadata() -> MetaData:
    """``Base.metadata`` with EVERY module under ``app.models`` imported.

    A model registers its table only when its module is imported. Relying on
    ``app.models/__init__`` (or on whatever earlier tests happened to import)
    would let a model that is not listed there drop out of the comparison
    silently, so import every module in the package by walking it.
    """
    for module in pkgutil.iter_modules(app_models.__path__):
        importlib.import_module(f"{app_models.__name__}.{module.name}")
    return Base.metadata


def _diff_key(item: object) -> DiffKey:
    """Reduce one compare_metadata item to an exact, hashable key."""
    assert isinstance(item, tuple) and item, f"unexpected diff item shape: {item!r}"
    op = item[0]
    if op in ("add_index", "remove_index"):
        index = item[1]
        return (
            op,
            index.table.name,
            index.name,
            tuple(column.name for column in index.columns),
            bool(index.unique),
        )
    if op in ("add_fk", "remove_fk"):
        fk = item[1]
        return (
            op,
            fk.parent.name,
            tuple(fk.column_keys),
            tuple(element.target_fullname for element in fk.elements),
            fk.ondelete,
        )
    if op in ("add_table", "remove_table"):
        return (op, item[1].name)
    if op in ("add_column", "remove_column"):
        return (op, item[2], item[3].name)
    if op in ("add_constraint", "remove_constraint"):
        constraint = item[1]
        return (
            op,
            constraint.table.name,
            constraint.name,
            tuple(column.name for column in constraint.columns),
        )
    if op.startswith("modify_"):
        # (op, schema, table, column, existing_kw, existing, new): keep BOTH
        # values, so a changed default or type on an allowlisted column is a
        # new key rather than a match for the old entry.
        return (op, item[2], item[3], _render(item[5]), _render(item[6]))
    # Unknown shape: keep it, fully rendered, so it can never match an entry.
    return (op, repr(item))


def _render(value: object) -> object:
    """A stable, comparable form of a modify_* value (no object addresses)."""
    if isinstance(value, DefaultClause):
        arg = value.arg
        return str(getattr(arg, "text", arg))
    if isinstance(value, TypeEngine):
        return repr(value)
    return value


def _diff_keys(diff: list[object]) -> list[DiffKey]:
    """Flatten compare_metadata's output (modify_* ops arrive as lists) to keys."""
    keys: list[DiffKey] = []
    for entry in diff:
        for item in entry if isinstance(entry, list) else [entry]:
            keys.append(_diff_key(item))
    return keys


def _unexpected_and_stale(
    keys: list[DiffKey], allowlist: dict[DiffKey, str]
) -> tuple[list[DiffKey], list[DiffKey]]:
    """(items in the diff but not allowlisted, allowlisted items not in the diff)."""
    unexpected = [key for key in keys if key not in allowlist]
    stale = [key for key in allowlist if key not in keys]
    return unexpected, stale


@pytest.fixture
def migrated_schema_diff(alembic_config: AlembicConfig) -> list[DiffKey]:
    """compare_metadata between ``alembic upgrade head`` and the full ORM metadata."""
    alembic_upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    metadata = _full_orm_metadata()
    with engine.connect() as connection, warnings.catch_warnings():
        # ``index_versions`` and ``evaluation_runs`` FK each other, so
        # SQLAlchemy cannot topologically sort them and warns. The warning is
        # about sort order only; the FK items are still compared (the
        # ``add_fk`` entry above is exactly such an item).
        warnings.filterwarnings(
            "ignore", message="Cannot correctly sort tables", category=SAWarning
        )
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        diff = compare_metadata(context, metadata)
    return _diff_keys(diff)


def test_migrated_schema_matches_the_orm_models(migrated_schema_diff: list[DiffKey]) -> None:
    """Every difference between the migrated schema and the models is listed.

    RED WHEN: a ``mapped_column`` (or index, FK, table, type, nullability,
    server default) is added to any model with no migration, or the reverse; or
    when any ``KNOWN_DRIFT`` entry is deleted while the drift is still there.
    """
    unexpected, _ = _unexpected_and_stale(migrated_schema_diff, KNOWN_DRIFT)
    assert not unexpected, (
        "the ORM models and the migrated schema disagree on items that are not in "
        f"KNOWN_DRIFT: {unexpected}. Write a migration for a model change (or the "
        "model change for a migration). Add an entry only for a divergence you mean "
        "to keep, and label which side is looser."
    )
    assert len(migrated_schema_diff) == len(set(migrated_schema_diff)), (
        f"two diff items reduced to the same key, so one could hide: {migrated_schema_diff}"
    )


def test_every_known_drift_entry_is_still_real(migrated_schema_diff: list[DiffKey]) -> None:
    """PARTNER: an allowlist entry whose drift is gone must be deleted.

    Without this, an entry outlives its fix and later excuses the same drift
    returning. RED WHEN: a migration or model change closes one of the listed
    divergences (for example the owner adds the unique index to ``User.email``)
    and the entry is left behind.
    """
    _, stale = _unexpected_and_stale(migrated_schema_diff, KNOWN_DRIFT)
    assert not stale, f"these KNOWN_DRIFT entries no longer occur and must be deleted: {stale}"


def test_the_orm_metadata_covers_every_migrated_table(alembic_config: AlembicConfig) -> None:
    """PARTNER: the comparison cannot pass because a model dropped out of it.

    compare_metadata only compares the tables it is given. RED WHEN: a table the
    migrations create has no model in the metadata the comparison uses (for
    example ``_full_orm_metadata`` stops importing the model modules).
    """
    alembic_upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        migrated = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).all()
        } - {"alembic_version"}
    modelled = set(_full_orm_metadata().tables)
    # Not vacuous: the migrated schema really has the documented tables.
    assert migrated >= EXPECTED_TABLES
    assert modelled == migrated, (
        f"only migrated: {sorted(migrated - modelled)}; "
        f"only modelled: {sorted(modelled - migrated)}"
    )


def test_the_drift_checker_reports_every_offending_item_not_just_the_first() -> None:
    """The comparison helper itself, on a fixture whose FIRST item is allowlisted.

    RED WHEN: ``_unexpected_and_stale`` stops at the first item, or reports an
    allowlisted item as unexpected.
    """
    allowed: DiffKey = ("remove_index", "t", "ix_ok", ("a",), False)
    new_one: DiffKey = ("add_column", "t", "b")
    new_two: DiffKey = ("remove_index", "t", "ix_ok", ("a",), True)  # same index, unique
    gone: DiffKey = ("add_fk", "t", ("c",), ("u.c",), None)
    unexpected, stale = _unexpected_and_stale(
        [allowed, new_one, new_two], {allowed: "fine", gone: "fixed since"}
    )
    assert unexpected == [new_one, new_two]
    assert stale == [gone]


def _insert_two_users_with_one_email(engine: Engine) -> int:
    """Insert two users sharing an email; return how many rows were stored.

    Raises ``IntegrityError`` when the schema enforces email uniqueness.
    """
    with engine.begin() as connection:
        for user_id in ("u-455-a", "u-455-b"):
            connection.exec_driver_sql(
                "INSERT INTO users (user_id, role, created_at, email) VALUES "
                f"('{user_id}', 'demo_user', CURRENT_TIMESTAMP, 'same@example.com')"
            )
    with engine.connect() as connection:
        return connection.exec_driver_sql(
            "SELECT COUNT(*) FROM users WHERE email = 'same@example.com'"
        ).scalar_one()


def test_users_email_uniqueness_matches_the_known_drift_entry(
    alembic_config: AlembicConfig, tmp_path: Path
) -> None:
    """The PERMISSIVE DIVERGENCE on ``users.email``, measured by behaviour.

    The migrated schema (production) must refuse a duplicate email. The
    hermetic ``create_all`` schema must do whatever ``KNOWN_DRIFT`` says it
    does: accept the duplicate while the entry is there, refuse it once the
    owner closes the divergence and the entry is deleted. So this pins the
    allowlist to reality in both states and never locks the defect in.

    RED WHEN: migration 0008's unique index is dropped (production would accept
    the duplicate), or ``User.email`` gains uniqueness while the entry remains.
    """
    alembic_upgrade(alembic_config, "head")
    migrated = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    # Matched on the message: any IntegrityError (a new NOT NULL column, say)
    # would otherwise pass this with the email uniqueness gone.
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: users\.email"):
        _insert_two_users_with_one_email(migrated)

    hermetic = create_engine(f"sqlite:///{tmp_path / 'create_all.db'}")
    _full_orm_metadata().create_all(hermetic)
    if USERS_EMAIL_UNIQUE_INDEX in KNOWN_DRIFT:
        assert _insert_two_users_with_one_email(hermetic) == 2, (
            "create_all now refuses a duplicate email, so the users.email divergence "
            "is closed: delete its KNOWN_DRIFT entry"
        )
    else:
        with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: users\.email"):
            _insert_two_users_with_one_email(hermetic)


def _unique_column_sets(
    tables: dict[str, set[tuple[str, ...]]],
) -> set[tuple[str, tuple[str, ...]]]:
    # Sorted: uniqueness over a column set does not depend on column order, so
    # a reordered composite constraint is the same uniqueness, not a drift.
    return {(table, tuple(sorted(columns))) for table, sets in tables.items() for columns in sets}


# The uniqueness only the migrated schema has, and why it is tolerated. Tied to
# the KNOWN_DRIFT entry above: the same PERMISSIVE DIVERGENCE, seen by the
# uniqueness comparison rather than by compare_metadata.
KNOWN_UNIQUENESS_DRIFT: set[tuple[str, tuple[str, ...]]] = (
    {("users", ("email",))} if USERS_EMAIL_UNIQUE_INDEX in KNOWN_DRIFT else set()
)


def test_migrated_uniqueness_matches_the_orm_models(alembic_config: AlembicConfig) -> None:
    """Every UNIQUE column set in production is UNIQUE in the hermetic schema.

    compare_metadata misses an unnamed unique constraint that only a migration
    creates, and that is exactly the looser-than-production case #455 is about:
    create_all would accept a duplicate that production refuses. So compare the
    unique column sets directly -- unique constraints and unique indexes as
    reflected from the migrated DB, against UniqueConstraint, unique Index and
    ``unique=True`` columns in the models. Primary keys are not included.

    RED WHEN: a migration makes a column set unique that no model declares
    unique (or the reverse), or when the ``users.email`` divergence is closed and
    its KNOWN_DRIFT entry is left behind.
    """
    alembic_upgrade(alembic_config, "head")
    inspector = inspect(create_engine(alembic_config.get_main_option("sqlalchemy.url")))
    migrated: dict[str, set[tuple[str, ...]]] = {}
    for table in inspector.get_table_names():
        sets = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)}
        sets |= {tuple(i["column_names"]) for i in inspector.get_indexes(table) if i["unique"]}
        migrated[table] = sets
    modelled: dict[str, set[tuple[str, ...]]] = {}
    for name, table in _full_orm_metadata().tables.items():
        sets = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        sets |= {
            tuple(column.name for column in index.columns)
            for index in table.indexes
            if index.unique
        }
        sets |= {(column.name,) for column in table.columns if column.unique}
        modelled[name] = sets

    only_migrated = _unique_column_sets(migrated) - _unique_column_sets(modelled)
    only_modelled = _unique_column_sets(modelled) - _unique_column_sets(migrated)
    # Not vacuous: reflection really found uniqueness on both sides, including a
    # composite constraint that both declare.
    assert ("user_identities", ("provider", "provider_account_id")) in _unique_column_sets(migrated)
    assert ("user_identities", ("provider", "provider_account_id")) in _unique_column_sets(modelled)
    assert only_migrated == KNOWN_UNIQUENESS_DRIFT, (
        f"UNIQUE only in production (hermetic tests accept duplicates it refuses): "
        f"{sorted(only_migrated - KNOWN_UNIQUENESS_DRIFT)}; listed but no longer "
        f"diverging: {sorted(KNOWN_UNIQUENESS_DRIFT - only_migrated)}"
    )
    assert not only_modelled, (
        f"UNIQUE only in the models, not in production: {sorted(only_modelled)}"
    )


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


# ── #374: alembic must not disable the application's loggers ────────────────
#
# `db/env.py` calls `logging.config.fileConfig(...)`, whose `disable_existing_
# loggers` defaults to True. `db/alembic.ini`'s `[loggers] keys` names only
# `root,sqlalchemy,alembic`, so the default sets `disabled = True` on every
# `citevyn.*` logger ALREADY CREATED in the process -- mutating them in place,
# so re-fetching the logger does not undo it.
#
# That made 34 tests order-dependent: this module runs alembic IN-PROCESS, and
# every later test asserting on a captured `citevyn.*` record then saw an empty
# capture. Default file order hid it (this file sorts after most of them);
# reverse order exposed it.
#
# WHAT TURNS THESE RED: drop `disable_existing_loggers=False` from
# `db/env.py`'s fileConfig call. Verified by doing exactly that -- both fail.


def test_running_alembic_does_not_disable_the_apps_loggers(
    alembic_config: AlembicConfig,
) -> None:
    """The consumer-visible effect: the logger still emits after a migration."""
    logger = logging.getLogger("citevyn.request")
    # PARTNER for the assertion below. A logger that was ALREADY disabled would
    # make "still enabled" vacuous, and a typo'd name would create a fresh
    # enabled logger and pass no matter what env.py does.
    assert logger.disabled is False, "precondition: the logger starts enabled"
    assert "citevyn.request" in logging.root.manager.loggerDict, (
        "precondition: the logger must already EXIST in the manager, since "
        "fileConfig only disables loggers that exist when it runs"
    )

    alembic_upgrade(alembic_config, "head")

    assert logger.disabled is False, (
        "alembic disabled citevyn.request (#374). db/env.py's fileConfig call "
        "needs disable_existing_loggers=False, or every later test that reads a "
        "captured citevyn.* record sees an empty capture"
    )


@pytest.fixture(scope="module")
def _alembic_already_ran(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Run a migration BEFORE the probe test's ``caplog`` handler is installed.

    MODULE scope is the point. ``fileConfig`` rebuilds the ROOT logger's
    handlers too, which drops pytest's capture handler for the REMAINDER of the
    test that triggered it -- so running alembic inside the probe test would
    fail even with #374 fixed, for an unrelated reason, and would not be
    testing #374 at all. pytest re-installs that handler per test, so the
    handler effect is transient while ``disabled = True`` was not. Higher-scoped
    fixtures are set up before function-scoped ``caplog``, which reproduces the
    real shape: the migration ran in an EARLIER test.
    """
    db_path = tmp_path_factory.mktemp("alembic_logging") / "probe.db"
    config = AlembicConfig(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic_upgrade(config, "head")


def test_a_citevyn_log_record_still_reaches_a_handler_after_a_migration(
    _alembic_already_ran: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asserts the EMITTED record, not the ``disabled`` flag that causes it.

    ``disabled`` is the mechanism; an empty capture is what the 34 failing tests
    actually saw. Pinning only the flag would still pass if some other mechanism
    started swallowing records.
    """
    with caplog.at_level(logging.WARNING, logger="citevyn.request"):
        logging.getLogger("citevyn.request").warning("post_migration_probe")

    emitted = "".join(record.getMessage() for record in caplog.records)
    assert "post_migration_probe" in emitted, (
        f"a citevyn.request record did not reach the capture handler after a "
        f"migration ran in-process (#374); captured: {emitted!r}"
    )
