"""Shared pytest fixtures and helpers for the backend test suite.

This module was simplified to a minimal surface during a lint pass;
it still exposes :func:`seed_catalog` (and a few ``session`` /
``seeded_session`` fixtures) because the Slice 7 HTTP route tests
import :func:`seed_catalog` directly and the retrieval tests depend
on the session-scoped async DB fixtures. Keeping the helpers here
means individual test files do not have to re-seed the same catalog.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import db as db_module
from app.core.config import get_settings
from app.embeddings.factory import EmbedderIdentity
from app.embeddings.protocol import Embedder
from app.main import create_app
from app.models import (
    Base,
    Chunk,
    Document,
    DocumentStatus,
    ExactTerm,
    IndexStatus,
    IndexVersion,
    TermType,
)


@pytest.fixture(autouse=True, scope="session")
def _default_database_url() -> Generator[None, None, None]:
    """Point the test run at an in-memory SQLite database.

    The production default is a Postgres URL which is not available
    in the hermetic CI environment. The fixture sets the URL once
    per test session (before any test runs and before
    ``get_settings.cache_clear()`` can rebind it).
    """
    import os

    os.environ.setdefault("CITEVYN_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    yield


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Generator[None, None, None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Yield a :class:`TestClient` whose lifespan has fully started.

    The :func:`_lifespan` body runs :func:`validate_llm_provider`
    and the Settings model_validators on app startup. We enter
    the TestClient as a context manager so those hooks fire and
    a future test that exercises the prod path is covered.

    The cached async engine is reset before the app is built so a
    test that previously used the ``session`` fixture (with a
    per-test temp-file engine) doesn't leak a closed engine into
    the app's first request. We also reset the rate-limiter and
    redis-client caches — same rationale: a previous test may
    have left a closed fakeredis or in-process limiter behind.
    """
    db_module.reset_engine()
    # Reset process-wide limiter + redis client + LLM factory
    # caches so a previous test's closed handle doesn't leak into
    # this app. (The LLM factory singleton in particular survived
    # between tests until this was added — a test that ran
    # ``get_llm_client`` with one Settings would have handed the
    # next test a stale client built from a different Settings.)
    import app.core.rate_limit as rate_limit
    import app.core.redis_client as redis_client
    from app.llm import factory as llm_factory

    rate_limit.reset_limiter()
    redis_client.reset_redis_client()
    llm_factory.reset_llm_client()
    with TestClient(create_app()) as app:
        yield app


# ---------------------------------------------------------------------------
# Async DB fixtures + seed helper
# ---------------------------------------------------------------------------


async def seed_catalog(
    session: AsyncSession,
    *,
    index_version: str = "v1",
    embedder: Embedder | None = None,
    embedder_identity: EmbedderIdentity | None = None,
    commit: bool = True,
) -> dict[str, list[object]]:
    """Insert the demo catalog into ``session`` and return the inserted rows.

    The helper is exported as a plain function (not a fixture) so
    individual tests can call it against an existing session — the
    Slice 7 route tests own their own client + session and only need
    the seeding primitive.

    Returns a dict with ``docs``, ``chunks``, and ``exact_terms`` so
    callers can grab a row by attribute (e.g. ``catalog['docs'][0]``)
    without re-querying.

    ``embedder`` (optional): when provided, every chunk is embedded (one batched
    :meth:`Embedder.embed_documents` call) and the vector is stored on
    ``Chunk.embedding`` — reviving the semantic arm on a pgvector backend (#97).
    Default ``None`` leaves ``embedding`` NULL, preserving the hermetic SQLite
    behaviour (no network, no key) every existing caller relies on. A mid-batch
    embed failure propagates and, because the rows are only flushed (see
    ``commit``), leaves no partial/NULL-vector residue.

    ``embedder_identity`` (optional): the ``(provider, model, dim)`` provenance
    stamped onto the active :class:`IndexVersion`. It MUST equal the read path's
    ``configured_embedder_identity(settings)`` so the Tier-3 gate treats the index
    as query-compatible; leaving it ``None`` stamps nothing (the gate then reads
    "unknown provenance ⇒ allow").

    ``commit`` (default ``True``): when ``False`` the rows are flushed but not
    committed, so the caller owns the transaction (the Postgres eval seeds with
    ``commit=False`` and rolls back for zero residue).
    """
    now = datetime.now(UTC)
    doc_specs: list[dict[str, str]] = [
        {
            "product_area": "claude_api",
            "source_name": "claude_api",
            "title": "Claude API Reference",
            "source_url": "https://docs.example.com/claude-api",
            "chunk_heading": "Rate limits",
            "chunk_text": (
                "The Claude API enforces a default rate limit of 50 requests "
                "per minute. The CLAUDE_API_RATE_LIMIT environment variable "
                "can override this for self-serve customers."
            ),
        },
        {
            "product_area": "claude_code",
            "source_name": "claude_code",
            "title": "Claude Code Reference",
            "source_url": "https://docs.example.com/claude-code",
            "chunk_heading": "Permissions",
            # Mirrors the real shipped worker source (claude_code.md: Permissions +
            # Installation) so an install question — "How do I install Claude Code?" —
            # is answerable on the HERMETIC path (exact + keyword; the vector arm is off
            # on SQLite), the same way #87 enriched the codex/gemini fixtures. Without
            # this the hermetic run cannot see a shipped-corpus regression (#162).
            "chunk_text": (
                "Claude Code permissions are configured in the project's "
                "settings file. Use the allow/deny lists to gate tools and "
                "commands the assistant can run. Install Claude Code with the "
                "native installer by running 'curl -fsSL https://claude.ai/install.sh "
                "| bash' on macOS, Linux or WSL, or from npm with "
                "'npm install -g @anthropic-ai/claude-code', which needs Node.js v22 "
                "or later. Confirm the install with 'claude --version' and diagnose it "
                "with 'claude doctor'."
            ),
        },
        {
            "product_area": "codex",
            "source_name": "codex",
            "title": "Codex Reference",
            "source_url": "https://docs.example.com/codex",
            "chunk_heading": "CLI flags",
            # Mirrors the real shipped worker source (codex.md: Installation +
            # Authentication + CLI flags) so a source-named question with a
            # question word — "How do I install the Codex CLI?", "Does Codex
            # need an API key?" — is answerable on the HERMETIC path (exact +
            # keyword, vector arm off on SQLite), guarding #87 against regression.
            "chunk_text": (
                "The --model flag selects the model Codex uses for code "
                "generation. Run 'codex --help' for the full list of flags. "
                "Install the Codex CLI globally with npm ('npm install -g "
                "@openai/codex') or on macOS with Homebrew ('brew install codex'). "
                "Codex reads its credentials from the OPENAI_API_KEY environment "
                "variable, or a stored login created by signing in through the CLI, "
                "so it does need an API key (or a ChatGPT sign-in)."
            ),
        },
        {
            "product_area": "gemini_api",
            "source_name": "gemini_api",
            "title": "Gemini API Reference",
            "source_url": "https://docs.example.com/gemini",
            "chunk_heading": "Authentication",
            # Mirrors the real shipped worker source (gemini_api.md:
            # Authentication + Streaming responses) so "google gemini streaming
            # responses" is answerable on the HERMETIC path (keyword arm, vector
            # off on SQLite) — guards #87 against regression.
            # NOTE: keep this text free of low-value function words shared with
            # the paraphrase eval cases (e.g. "to") — the keyword arm's ≥2-token
            # floor would otherwise let a single content token ("gemini") + a
            # stray function word spuriously satisfy a semantic paraphrase and
            # trip ``test_paraphrase_baseline_is_dead`` (the false-literal guard).
            "chunk_text": (
                "Pass your Gemini API key in the x-goog-api-key header on "
                "every request. The Gemini CLI also accepts the key in a "
                "credentials file. The Gemini API also supports streaming "
                "responses: the streaming generate-content variant "
                "(streamGenerateContent) returns the answer as a sequence of "
                "partial chunks rather than one whole response."
            ),
        },
        {
            # About-CiteVyn source (#49): keeps the demo catalog in lock-step
            # with MVP_SOURCES so CiteVyn-meta questions ("What is CiteVyn
            # Pro?") retrieve + cite instead of being refused off-domain.
            # source_url is the host-agnostic relative /about (see allowlist).
            "product_area": "citevyn",
            "source_name": "citevyn",
            "title": "About CiteVyn",
            "source_url": "/about",
            "chunk_heading": "CiteVyn Pro and membership",
            "chunk_text": (
                "CiteVyn Pro is not live yet. CiteVyn is an MVP demo and "
                "everything is free to try. Pro is planned to add higher rate "
                "limits, exact lookups, saved history, and shareable answers."
            ),
        },
        {
            # AI concepts/glossary (#112 follow-up): keeps the demo catalog in
            # lock-step with MVP_SOURCES so conceptual questions ("what is an LLM?",
            # "is Codex an LLM?") retrieve + cite instead of being refused off-domain.
            "product_area": "concepts",
            "source_name": "concepts",
            "title": "AI Concepts and Glossary",
            "source_url": "/about",
            "chunk_heading": "What a large language model (LLM) is",
            "chunk_text": (
                "A large language model, or LLM, is an AI system trained on a large "
                "amount of text so it can understand a plain-language request and "
                "generate a useful text response. Claude, Claude Code, Codex, and "
                "Gemini are all LLM-based AI tools."
            ),
        },
    ]

    docs: list[Document] = []
    chunks: list[Chunk] = []
    exact_terms: list[ExactTerm] = []

    # Seed the active IndexVersion row so the active-sentinel
    # path in services/exact_lookup.py and /health/index can
    # resolve. The chunk-row join links on
    # ``Document.index_version == IndexVersion.index_version``,
    # so the same string has to exist in both tables.
    active_index = IndexVersion(
        index_version=index_version,
        status=IndexStatus.active,
        source_version_hash=f"sha256:{index_version}",
        embedding_provider=embedder_identity.provider if embedder_identity else None,
        embedding_model=embedder_identity.model if embedder_identity else None,
        embedding_dim=embedder_identity.dim if embedder_identity else None,
        created_at=now,
        promoted_at=now,
    )
    session.add(active_index)
    await session.flush()

    for spec in doc_specs:
        doc = Document(
            document_id=uuid.uuid4(),
            index_version=index_version,
            source_name=spec["source_name"],
            product_area=spec["product_area"],
            source_url=spec["source_url"],
            title=spec["title"],
            content_checksum=f"sha256:{spec['product_area']}",
            last_fetched_at=now,
            last_indexed_at=now,
            status=DocumentStatus.active,
        )
        session.add(doc)
        await session.flush()
        chunk = Chunk(
            chunk_id=uuid.uuid4(),
            document_id=doc.document_id,
            product_area=spec["product_area"],
            section_path=spec["chunk_heading"],
            heading=spec["chunk_heading"],
            parent_heading=None,
            chunk_text=spec["chunk_text"],
            context_summary=spec["chunk_text"][:120],
            exact_terms=[],
            chunk_order=0,
            content_checksum=f"sha256:{spec['product_area']}-chunk-0",
        )
        session.add(chunk)
        await session.flush()
        docs.append(doc)
        chunks.append(chunk)

    # Embed every chunk in one batched call when a real embedder is supplied, so
    # the vector arm has something to retrieve (#97). Done AFTER all chunks exist so
    # a single ``embed_documents`` covers the whole corpus; the vectors are paired
    # back by position (``strict=True`` catches any count drift). A failure here
    # propagates before any commit, so no chunk is left with a partial vector.
    if embedder is not None:
        vectors = await embedder.embed_documents([c.chunk_text for c in chunks])
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.embedding = vector
        await session.flush()

    # Two exact terms: one env var, one CLI flag. The product_areas
    # are matched to the docs above so retrieval tests can find them.
    for chunk in chunks:
        if chunk.product_area == "claude_api":
            session.add(
                ExactTerm(
                    term_id=uuid.uuid4(),
                    term_text="CLAUDE_API_RATE_LIMIT",
                    term_type=TermType.environment_variable,
                    product_area="claude_api",
                    document_id=chunk.document_id,
                    chunk_id=chunk.chunk_id,
                )
            )
        elif chunk.product_area == "codex":
            session.add(
                ExactTerm(
                    term_id=uuid.uuid4(),
                    term_text="--model",
                    term_type=TermType.flag,
                    product_area="codex",
                    document_id=chunk.document_id,
                    chunk_id=chunk.chunk_id,
                )
            )

    await session.flush()
    if commit:
        await session.commit()
    return {"docs": docs, "chunks": chunks, "exact_terms": exact_terms}


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an ``AsyncSession`` bound to a per-test in-memory SQLite engine.

    Each test gets its own engine + schema so transactions are
    isolated. The engine is disposed after the test to release the
    file handle on Windows (where ``tempfile``-held files cannot be
    reopened while open).
    """
    db_module.reset_engine()
    get_settings.cache_clear()
    # Pin the URL to a temp file (not ``:memory:``) so the fixture,
    # the test, and any background task share one connection.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{fh.name}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            yield session
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Yield a fresh ``AsyncSession`` against a per-test in-memory SQLite.

    Alias of :func:`session` kept for the model round-trip tests,
    which were written against the ``db_session`` name. The schema
    is created up front so the test never has to run migrations.
    """
    db_module.reset_engine()
    get_settings.cache_clear()
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{fh.name}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            yield session
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded_session(session: AsyncSession) -> AsyncSession:
    """Yield a session with the demo catalog already inserted."""
    await seed_catalog(session)
    await session.commit()
    return session
