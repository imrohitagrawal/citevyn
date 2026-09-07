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
from collections.abc import AsyncIterator, Generator, Sequence
from datetime import UTC, datetime, timedelta

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
    User,
    UserRole,
)
from app.models import Session as SessionModel
from app.retrieval.types import EvidenceHit


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


# ---------------------------------------------------------------------------
# The hermetic corpus fixture
# ---------------------------------------------------------------------------
#
# Module-level (not a local inside ``seed_catalog``) so a drift guard can read it
# without seeding a database: ``test_corpus_single_source.py`` asserts every
# verbatim claim below is still present in the SHIPPED corpus under
# ``app/worker/sources/``. That corpus is authoritative; this is a deliberately
# abridged mirror of it, kept small and stable because the golden/eval suite is
# anchored to these exact chunks (#178).
_CORPUS_FIXTURE_SPECS: list[dict[str, str]] = [
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
            "CiteVyn Pro is not live yet. CiteVyn is currently an MVP demo "
            "and everything here is free to try, with or without an "
            "account. Pro is planned to add higher rate limits, exact "
            "lookups, and shareable answers."
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


def clear_magic_link_interval(email: str) -> None:
    """Step past the #301 magic-link minimum interval without waiting 60s.

    Several tests need SEVERAL link requests for one address to exercise something
    else entirely -- draining the HOURLY ceiling, proving one live token per user,
    checking each same-origin signal, proving the notice bucket is independent of
    the request bucket. The 1-per-60s interval bucket blocks all of that, and
    sleeping a minute per send is not a test.

    Clears ONLY the interval bucket, deliberately NOT the hourly one: several
    callers assert the hourly ceiling still trips, so a blanket ``reset_limiter()``
    would silently destroy exactly what they measure.

    Lives here rather than in one test module because two modules need it and the
    behaviour it bypasses is auth-adjacent -- two copies drifting apart is a real
    hazard. Reaching into ``_buckets`` is deliberate: the alternative is a
    production "disable" switch on a rate limiter, which is the footgun
    ``_effective_global_limit`` already warns about.
    """
    from app.core.config import get_settings
    from app.core.rate_limit import get_limiter, magic_link_interval_rate_key

    settings = get_settings()
    limiter = get_limiter(settings)
    buckets = getattr(limiter, "_buckets", None)
    if buckets is not None:  # in-process limiter only; these tests never use Redis
        buckets.pop(magic_link_interval_rate_key(email, settings), None)


def corpus_fixture_specs() -> list[dict[str, str]]:
    """Return a copy of the hermetic corpus fixture specs.

    A copy, so a caller that mutates a spec cannot poison the next test in the
    session.
    """
    return [dict(spec) for spec in _CORPUS_FIXTURE_SPECS]


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
    doc_specs = corpus_fixture_specs()

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
            identity_checksum=f"sha256:{spec['product_area']}",
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


async def seed_user(
    session: AsyncSession,
    user_id: str,
    *,
    role: UserRole = UserRole.demo_user,
) -> None:
    """Persist a ``users`` row if absent, in its own flush.

    ``audit_events.user_id`` and ``sessions.user_id`` are foreign keys onto
    ``users``, and no ORM ``relationship()`` connects those mapped classes.
    That has two consequences a test has to respect, both of which this
    helper handles (#286):

    * an actor id the test invents (``"user-123"``, ``"admin-1"``) has no
      parent row unless someone inserts one;
    * even adding the right ``User`` is not enough if it goes in the SAME
      flush as the child — SQLAlchemy's unit of work orders inserts by
      declared relationships, not by raw foreign-key columns, and has been
      observed to emit ``audit_events`` before ``users``. The dedicated
      flush here is what makes the order deterministic.

    In production these ids always name a real row: the admin actor is the
    constant ``ADMIN_USER_ID`` seeded by ``db/seed/seed_users.py``, and a
    chat actor comes from ``Orchestrator._ensure_user``.

    Idempotent, but LOUD about a conflict: asking for a role the existing row
    does not have raises rather than silently handing back the old one. A
    quiet no-op there would let a test believe it was running as an admin
    while the row said ``demo_user`` — the same shape of invisible wrongness
    this whole change exists to end.
    """
    existing = await session.get(User, user_id)
    if existing is not None:
        if existing.role is not role:
            raise AssertionError(
                f"user {user_id!r} was already seeded with role {existing.role}, "
                f"not {role}; seed it once with the role the test needs"
            )
        return
    session.add(User(user_id=user_id, role=role, created_at=datetime.now(UTC)))
    await session.flush()


async def seed_index_version(
    session: AsyncSession,
    index_version: str,
    *,
    status: IndexStatus | None = None,
) -> None:
    """Persist an ``index_versions`` row if absent, in its own flush.

    ``documents.index_version`` and ``evaluation_runs.index_version`` are
    foreign keys onto it.

    ``status=None`` (the default) means "I just need the parent to exist":
    an absent row is created as ``candidate`` — never ``active``, so a
    caller that only wants the foreign key satisfied cannot accidentally
    hand a test a second active index — and an existing row is accepted
    whatever state it is in.

    Passing an explicit ``status`` means "I need it in THIS state", and a
    row already seeded in a different one raises instead of quietly handing
    back the old one. A silent no-op there would let a test believe it was
    running against an active index while the row said ``candidate`` — the
    same shape of invisible wrongness this whole change exists to end.

    Same one-flush-per-parent rule as :func:`seed_user`, and here the
    ordering trap is sharper: ``IndexVersion.evaluation_run_id`` DOES have a
    ``relationship()`` back to ``EvaluationRun``, so a combined flush is
    ordered ``evaluation_runs`` first — exactly backwards for the other,
    relationship-less foreign key.
    """
    existing = await session.get(IndexVersion, index_version)
    if existing is not None:
        if status is not None and existing.status is not status:
            raise AssertionError(
                f"index version {index_version!r} was already seeded as "
                f"{existing.status}, not the requested {status}; seed it once "
                f"with the status the test needs"
            )
        return
    session.add(
        IndexVersion(
            index_version=index_version,
            status=status or IndexStatus.candidate,
            source_version_hash=f"sha256:{index_version}",
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def seed_chat_session(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    user_id: str = "demo_user",
    channel: str = "chat",
) -> None:
    """Persist the ``users`` + ``sessions`` rows a ``messages`` row needs.

    ``messages.session_id`` is a foreign key onto ``sessions``, and
    ``sessions.user_id`` onto ``users``. A test that inserts a bare
    ``Message`` against a freshly minted UUID names a session that does not
    exist — accepted only because SQLite foreign-key enforcement was off
    (#286). In production a message is always written against a session the
    route (or ``Orchestrator._ensure_user``) already created.

    Parent first, child second, each in its own flush: this codebase
    declares no ORM ``relationship()`` between ``User`` and ``Session``, so
    a single combined flush is not ordered by the foreign key — the exact
    trap that produced the ``_mint_principal`` and ``_ensure_user`` bugs.
    Existing rows are left alone, so repeat calls are safe.
    """
    now = datetime.now(UTC)
    await seed_user(session, user_id)
    if await session.get(SessionModel, session_id) is None:
        session.add(
            SessionModel(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        await session.flush()


async def seed_evidence_chunks(
    session: AsyncSession,
    hits: Sequence[EvidenceHit],
    *,
    index_version: str = "index_v1",
) -> None:
    """Persist the ``documents`` + ``chunks`` rows a fake ``EvidenceHit`` references.

    A test that hands the orchestrator a stub retriever gets hits whose
    ``chunk_id`` / ``document_id`` are freshly minted UUIDs naming nothing.
    The orchestrator then writes ``retrieved_evidence`` rows pointing at
    them, which violates ``retrieved_evidence.chunk_id -> chunks.chunk_id``.
    SQLite silently accepted that for the whole life of this suite (#286);
    Postgres never would.

    In production every ``EvidenceHit`` is built from a row the retriever
    SELECTed out of ``chunks``, so its parents exist by construction — the
    application code is right and only the fake is wrong. This helper
    restores that invariant for the fake.

    Rows already present are left alone, so it is safe to call more than
    once and safe to call after :func:`seed_catalog`. The ``index_versions``
    parent that ``documents.index_version`` requires is created as
    ``candidate``, never ``active`` and with ``promoted_at`` left NULL, so a
    test that deliberately runs with no active index
    (``test_ask_no_active_index_still_answers_status_only``) keeps proving
    exactly what it did before.
    """
    now = datetime.now(UTC)
    await seed_index_version(session, index_version)

    for order, hit in enumerate(hits):
        if await session.get(Document, hit.document_id) is None:
            session.add(
                Document(
                    document_id=hit.document_id,
                    index_version=index_version,
                    source_name=hit.source_name,
                    product_area=hit.product_area,
                    source_url=hit.source_url,
                    title=hit.document_title,
                    identity_checksum=f"sha256:{hit.document_id}",
                    last_fetched_at=now,
                    last_indexed_at=now,
                    status=DocumentStatus.active,
                )
            )
            await session.flush()
        if await session.get(Chunk, hit.chunk_id) is None:
            session.add(
                Chunk(
                    chunk_id=hit.chunk_id,
                    document_id=hit.document_id,
                    product_area=hit.product_area,
                    section_path=hit.section_path,
                    heading=hit.heading,
                    parent_heading=hit.parent_heading,
                    chunk_text=hit.chunk_text,
                    context_summary=hit.context_summary,
                    exact_terms=[],
                    chunk_order=order,
                    content_checksum=f"sha256:{hit.chunk_id}",
                )
            )
            await session.flush()


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
