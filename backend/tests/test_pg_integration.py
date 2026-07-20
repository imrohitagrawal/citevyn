"""Postgres integration tests (opt-in via the ``postgres`` marker).

These tests run ``alembic upgrade head`` against a real Postgres
database and exercise a few round-trips to prove the migration
behaves on Postgres (real UUID columns, native ENUMs after
migration 0002, JSON/JSONB columns). They are skipped by default so
``uv run pytest`` keeps working hermetically with in-memory SQLite.

Enable with either of:

    export CITEVYN_PG_TEST_URL=postgresql+psycopg://user:pass@host:5432/citevyn_pg_test
    uv run pytest -m postgres -v

    # …or set the same URL in the environment / .env via the
    # ``CITEVYN_PG_TEST_URL`` variable documented in ``.env.example``.

The test never modifies the default ``public`` schema. Each test
isolates itself with a unique ``citevyn_test_<rand>`` schema and
drops it on teardown, so the URL may safely point at a shared
development database.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.command import downgrade as alembic_downgrade
from alembic.command import upgrade as alembic_upgrade
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text

from app.core.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "db" / "alembic.ini"

# Mark every test in this file with the ``postgres`` marker. The
# ``skipif`` is intentionally evaluated at collection time so a
# developer running ``uv run pytest -m postgres --collect-only`` sees
# the explicit skip reason without spinning up the test fixtures.
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("CITEVYN_PG_TEST_URL") is None and get_settings().pg_test_url is None,
        reason=(
            "Set CITEVYN_PG_TEST_URL (or Settings.pg_test_url) to a "
            "postgresql+psycopg:// URL to enable Postgres integration tests."
        ),
    ),
]


def _pg_url() -> str:
    url = os.environ.get("CITEVYN_PG_TEST_URL") or get_settings().pg_test_url
    assert url is not None, "Postgres URL is required (guarded by skipif)"
    return url


def _pg_url_with_schema(schema: str) -> str:
    """Build a Postgres URL whose ``search_path`` is ``<schema>,public``.

    Alembic opens its own connection from the URL we hand it; setting
    ``search_path`` on the fixture's connection does not propagate.
    libpq accepts ``options=-c search_path=<schema>`` as a connection
    parameter, which psycopg applies on every new connection. The
    schema name comes from ``pg_schema`` and is URL-safe (``[a-z0-9_]``).

    ``public`` is appended (second, so the isolated schema still wins for
    unqualified CREATEs) because the ``pgvector`` extension is installed
    database-wide and its ``vector`` type / ``vector_cosine_ops`` opclass live in
    whichever schema first created it. In CI, ``alembic upgrade head`` runs against
    ``public`` before this per-schema test suite, so the type lives in ``public``;
    without ``public`` on the path, ``ALTER TABLE ... vector(1536)`` fails with
    ``type "vector" does not exist``. Production is unaffected (it runs in
    ``public``, which is always on the default path).
    """
    base = _pg_url()
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}options=-c search_path={schema},public"


def _alembic_config_for_schema(schema: str) -> AlembicConfig:
    """Build an Alembic config isolated to ``schema``.

    Sets the connection ``search_path`` to ``<schema>,public`` (so the shared
    pgvector ``vector`` type resolves) and pins alembic's ``version_table_schema``
    to ``schema`` (so its ``alembic_version`` lookup does not resolve to a
    ``public`` copy via the search_path). See ``db/env.py``.
    """
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "db"))
    cfg.set_main_option("sqlalchemy.url", _pg_url_with_schema(schema))
    cfg.set_main_option("version_table_schema", schema)
    return cfg


@pytest.fixture
def alembic_pg_config(pg_schema: str) -> Iterator[AlembicConfig]:
    """Yield an Alembic config isolated to the per-test ``pg_schema``.

    Both the ``search_path`` and alembic's ``version_table_schema`` are already
    configured, so consuming tests just call ``alembic_upgrade(cfg, "head")``.
    """
    yield _alembic_config_for_schema(pg_schema)


@pytest.fixture
def pg_schema() -> Iterator[str]:
    """Create a unique Postgres schema, yield its name, then drop it.

    The fixture opens a short-lived AUTOCOMMIT connection so
    ``CREATE SCHEMA`` / ``DROP SCHEMA`` are not subject to the
    implicit transaction that ``engine.begin()`` opens. A fresh
    ``create_engine`` is used here because we need to set
    ``search_path`` on the same connection Alembic uses; Alembic
    builds its own engine from the ``sqlalchemy.url`` config option
    with ``NullPool``, so we set the path at the role level via
    ``ALTER ROLE … IN DATABASE`` is not portable — instead we set
    ``search_path`` on the current session and let Alembic inherit
    it because ``db/env.py`` opens a single connection per upgrade.
    """
    schema = f"citevyn_test_{uuid.uuid4().hex[:8]}"
    engine = create_engine(_pg_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f"CREATE SCHEMA {schema}"))
            conn.execute(text(f"SET search_path TO {schema}"))
        yield schema
    finally:
        # Open a fresh connection in case the yielded engine was
        # disposed or the connection is in a bad state.
        with engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        engine.dispose()


def test_alembic_upgrade_head_creates_all_tables(
    alembic_pg_config: AlembicConfig, pg_schema: str
) -> None:
    alembic_upgrade(alembic_pg_config, "head")

    engine = create_engine(_pg_url())
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET search_path TO {pg_schema}"))
            rows = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"
                ),
                {"schema": pg_schema},
            ).all()
    finally:
        engine.dispose()

    table_names = {row[0] for row in rows}
    assert "alembic_version" in table_names

    expected = {
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
    }
    missing = expected - table_names
    assert not missing, f"Missing tables after migration: {missing}"


def test_uuid_columns_are_native_uuid_on_postgres(
    alembic_pg_config: AlembicConfig, pg_schema: str
) -> None:
    """GUID columns must be real ``uuid`` on Postgres, not CHAR(36)."""
    alembic_upgrade(alembic_pg_config, "head")

    # Every GUID-typed column across the schema. Built from a tuple
    # so the SQL string stays short and lint-friendly.
    guid_columns = (
        "document_id",
        "chunk_id",
        "message_id",
        "term_id",
        "evidence_id",
        "event_id",
        "run_id",
        "case_id",
        "session_id",
        "job_id",
    )
    in_clause = ", ".join(f"'{c}'" for c in guid_columns)

    engine = create_engine(_pg_url())
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET search_path TO {pg_schema}"))
            rows = conn.execute(
                text(
                    "SELECT table_name, column_name, data_type "
                    "FROM information_schema.columns "
                    f"WHERE table_schema = :schema AND column_name IN ({in_clause})"
                ),
                {"schema": pg_schema},
            ).all()
    finally:
        engine.dispose()

    assert rows, "Expected GUID-typed columns to exist after migration"
    non_uuid = [(t, c) for t, c, dtype in rows if dtype != "uuid"]
    assert not non_uuid, f"Expected native uuid, found CHAR(36) on: {non_uuid}"


def test_round_trip_user_insert(alembic_pg_config: AlembicConfig, pg_schema: str) -> None:
    """Smoke-test the application ORM against real Postgres."""
    alembic_upgrade(alembic_pg_config, "head")

    engine = create_engine(_pg_url())
    try:
        with engine.begin() as conn:
            conn.execute(text(f"SET search_path TO {pg_schema}"))
            conn.execute(
                text("INSERT INTO users (user_id, role, created_at) VALUES (:uid, 'admin', now())"),
                {"uid": "demo_user"},
            )
        with engine.connect() as conn:
            conn.execute(text(f"SET search_path TO {pg_schema}"))
            row = conn.execute(
                text("SELECT user_id, role FROM users WHERE user_id = :uid"),
                {"uid": "demo_user"},
            ).first()
    finally:
        engine.dispose()

    assert row is not None
    assert row[0] == "demo_user"
    assert row[1] == "admin"


def test_chunks_embedding_is_pgvector_on_postgres(
    alembic_pg_config: AlembicConfig, pg_schema: str
) -> None:
    """After migration ``0004`` ``chunks.embedding`` is a pgvector ``vector``.

    Migration ``0003`` declared the column as ``bytea``; ``0004`` swaps it to
    ``vector(1536)`` on Postgres. ``information_schema`` reports vector columns as
    ``USER-DEFINED`` with ``udt_name = 'vector'``. The column stays nullable so
    rows without an embedding survive.
    """
    alembic_upgrade(alembic_pg_config, "head")

    engine = create_engine(_pg_url())
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET search_path TO {pg_schema}"))
            row = conn.execute(
                text(
                    "SELECT data_type, udt_name, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'chunks' "
                    "AND column_name = 'embedding'"
                ),
                {"schema": pg_schema},
            ).first()
    finally:
        engine.dispose()

    assert row is not None, "chunks.embedding column not found after migration"
    data_type, udt_name, is_nullable = row
    assert udt_name == "vector", f"expected pgvector 'vector', got udt_name={udt_name}"
    assert data_type == "USER-DEFINED", f"expected USER-DEFINED, got {data_type}"
    assert is_nullable == "YES", "embedding should be nullable"


def test_chunks_embedding_has_hnsw_index(alembic_pg_config: AlembicConfig, pg_schema: str) -> None:
    """Migration ``0004`` creates the HNSW cosine index used by retrieval."""
    alembic_upgrade(alembic_pg_config, "head")

    engine = create_engine(_pg_url())
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET search_path TO {pg_schema}"))
            row = conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = :schema AND indexname = 'ix_chunks_embedding_hnsw'"
                ),
                {"schema": pg_schema},
            ).first()
    finally:
        engine.dispose()

    assert row is not None, "HNSW index ix_chunks_embedding_hnsw not found"
    assert "hnsw" in row[0].lower()
    assert "vector_cosine_ops" in row[0].lower()


def test_index_versions_has_embedding_stamp_columns(
    alembic_pg_config: AlembicConfig, pg_schema: str
) -> None:
    """Migration ``0004`` adds the Tier 3 provenance stamp columns."""
    alembic_upgrade(alembic_pg_config, "head")

    engine = create_engine(_pg_url())
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET search_path TO {pg_schema}"))
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'index_versions'"
                ),
                {"schema": pg_schema},
            ).all()
    finally:
        engine.dispose()

    columns = {r[0] for r in rows}
    assert {"embedding_provider", "embedding_model", "embedding_dim"} <= columns


def test_migration_0004_downgrade_restores_bytea(
    alembic_pg_config: AlembicConfig, pg_schema: str
) -> None:
    """The 0004 rollback reverts the vector column to bytea and drops the stamp.

    ``code_review.md`` blocks a migration without a working rollback, so this
    exercises ``downgrade`` end-to-end: upgrade to head, downgrade to 0003, and
    assert the schema shape returned to bytea + no stamp columns.
    """
    alembic_upgrade(alembic_pg_config, "head")
    alembic_downgrade(alembic_pg_config, "0003")

    engine = create_engine(_pg_url())
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET search_path TO {pg_schema}"))
            emb = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'chunks' "
                    "AND column_name = 'embedding'"
                ),
                {"schema": pg_schema},
            ).first()
            stamp = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'index_versions' "
                    "AND column_name = 'embedding_provider'"
                ),
                {"schema": pg_schema},
            ).first()
    finally:
        engine.dispose()

    assert emb is not None and emb[0] == "bytea", "downgrade should restore bytea"
    assert stamp is None, "downgrade should drop the embedding_provider stamp column"


async def test_vector_retriever_returns_ranked_hits_on_postgres(pg_schema: str) -> None:
    """The pgvector read path returns real, ranked hits on Postgres (not []).

    This is #51's core "not []" proof, run WITHOUT a provider key: a deterministic
    stub embedder writes 1536-dim vectors into the real ``vector(1536)`` column,
    and :class:`VectorRetriever` runs a live ``<=>`` cosine query. The chunk whose
    text matches the query embeds to the same point, so it ranks first.
    """
    from datetime import UTC, datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import Settings
    from app.embeddings import build_embedder
    from app.models import Chunk, Document, DocumentStatus, IndexStatus, IndexVersion
    from app.retrieval.vector import VectorRetriever

    # Build the schema via alembic on a sync engine, then work async in-schema.
    alembic_upgrade(_alembic_config_for_schema(pg_schema), "head")

    settings = Settings()  # embedding_dim=1536, provider=stub
    embedder = build_embedder(settings)
    # The app URL scheme is already ``postgresql+psycopg`` (psycopg3), which has an
    # async driver, so the same URL drives ``create_async_engine`` directly.
    engine = create_async_engine(_pg_url_with_schema(pg_schema))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    texts = [
        "The --model flag selects the Claude model for a request.",
        "Pass your API key in the x-api-key header.",
        "Rate limits return HTTP 429 when exceeded.",
    ]
    query = texts[0]

    try:
        now = datetime.now(UTC)
        async with maker() as session:
            session.add(
                IndexVersion(
                    index_version="v-pgtest",
                    status=IndexStatus.active,
                    source_version_hash="sha256:pg-vec-test",
                    embedding_provider=settings.embedding_provider,
                    embedding_model=settings.embedding_model,
                    embedding_dim=settings.embedding_dim,
                    created_at=now,
                    promoted_at=now,
                )
            )
            await session.flush()
            doc = Document(
                index_version="v-pgtest",
                source_name="claude_api",
                product_area="claude_api",
                source_url="https://docs.anthropic.com/en/api/overview",
                title="Claude API Reference",
                identity_checksum="c" * 20,
                last_fetched_at=now,
                status=DocumentStatus.active,
            )
            session.add(doc)
            await session.flush()

            vectors = await embedder.embed_documents(texts)
            for order, (t, v) in enumerate(zip(texts, vectors, strict=True)):
                session.add(
                    Chunk(
                        document_id=doc.document_id,
                        product_area="claude_api",
                        section_path=f"s{order}",
                        heading=f"h{order}",
                        parent_heading=None,
                        chunk_text=t,
                        context_summary=t[:40],
                        exact_terms=[],
                        chunk_order=order,
                        content_checksum=f"chk{order}",
                        embedding=v,
                    )
                )
            await session.commit()

            retriever = VectorRetriever(session, active_index_version="v-pgtest", embedder=embedder)
            hits = await retriever.retrieve(query, product_area="claude_api", limit=3)

        # Proof it is NOT [] and that the pgvector ordering is real.
        assert len(hits) == 3, "vector retrieval returned no hits on Postgres"
        assert hits[0].chunk_text == query, "closest hit should be the matching chunk"
        # Scores are descending (distance ascending); the exact match is ~1.0.
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] > scores[-1]
    finally:
        await engine.dispose()
