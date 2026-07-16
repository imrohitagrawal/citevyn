"""Hermetic retrieval hit-rate metric (Phase 0, issue #96).

Adopts the reference notebook's hit-rate: *does any top-k retrieved chunk
come from the expected source?* It measures the **live retrieval path** —
the same domain routing (:func:`classify_domain`), intent routing
(:func:`classify_intent`), and :class:`~app.retrieval.hybrid.HybridRetriever`
the orchestrator uses — against the conftest seed corpus, so the number
reflects what a user would actually get.

Fully hermetic: a temp-file SQLite engine seeded via
:func:`tests.conftest.seed_catalog`, no network and no LLM. On SQLite the
vector arm short-circuits to ``[]`` (no pgvector), so paraphrase cases that
depend on semantic recall miss — which is exactly the Phase 0 baseline this
harness exists to expose (#97).

The metric is intentionally split by :attr:`EvalCase.kind`:

* answerable cases (``literal`` / ``paraphrase``) score a **hit** when the
  expected ``source_name`` appears among the top-k hits;
* ``refusal`` cases score a **leak** when *any* chunk is retrieved — the
  correct outcome is an empty result.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.embeddings.factory import build_embedder, configured_embedder_identity
from app.embeddings.protocol import Embedder
from app.guardrails.domain import classify_domain, classify_domains, is_unsupported
from app.models import Base, Chunk, User, UserRole
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.types import VectorDegrade
from app.routing.intent import Intent, classify_intent
from tests.conftest import seed_catalog

from .cases import EvalCase


class PostgresEvalError(RuntimeError):
    """Raised when the opt-in Postgres eval mode is misconfigured or unsafe to run.

    Never a silent skip: the whole point of the Postgres mode is to produce a REAL
    semantic number, so a stub embedder, a missing key, a production target, or a
    non-empty catalog must fail loudly rather than quietly emit a hermetic-looking
    (fabricated) result.
    """


@dataclasses.dataclass(frozen=True)
class RetrievalOutcome:
    """Per-case retrieval result."""

    case_id: str
    area: str
    kind: str
    domain: str
    expected_source: str | None
    retrieved_sources: tuple[str, ...]
    # ``hit`` is meaningful for answerable cases; ``leaked`` for refusals.
    hit: bool
    leaked: bool

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class RetrievalReport:
    """Aggregate retrieval metrics over a case set."""

    outcomes: tuple[RetrievalOutcome, ...]

    # The "core" answerable kinds that make up the gated overall hit-rate.
    # ``multihop`` AND ``followup`` are deliberately EXCLUDED (each reported as its own
    # bucket). ``multihop`` needs the live vector arm to hit both areas, so it is
    # Postgres-only-provable and gated only on --postgres. ``followup`` resolves
    # deterministically once memory rewrites it (domain routing + keyword), so it hits
    # on hermetic SQLite too and is gated on the hermetic run — but it is a distinct
    # multi-turn concern, so it stays out of the single-turn literal+paraphrase overall.
    _CORE_KINDS = frozenset({"literal", "paraphrase"})

    def _answerable(self) -> list[RetrievalOutcome]:
        return [o for o in self.outcomes if o.kind != "refusal"]

    def _core(self) -> list[RetrievalOutcome]:
        return [o for o in self.outcomes if o.kind in self._CORE_KINDS]

    @staticmethod
    def _rate(hits: int, total: int) -> float:
        # An empty pool returns 1.0 (0 hits / 0 cases is vacuously "no misses").
        # Callers guard non-emptiness: the pytest gate asserts coverage and
        # ``runner.gate_failures`` fails on ``answerable_total == 0`` / a missing
        # ``literal`` bucket. Do not lean on this rate alone as a gate.
        return (hits / total) if total else 1.0

    def hit_rate(self, kind: str | None = None) -> float:
        """Fraction of answerable cases (optionally of one kind) that hit."""
        pool = [o for o in self._answerable() if kind is None or o.kind == kind]
        return self._rate(sum(o.hit for o in pool), len(pool))

    @property
    def overall_hit_rate(self) -> float:
        """Gated overall = core kinds (literal + paraphrase) only; excludes multihop."""
        core = self._core()
        return self._rate(sum(o.hit for o in core), len(core))

    @property
    def multihop_hit_rate(self) -> float:
        return self.hit_rate("multihop")

    @property
    def followup_hit_rate(self) -> float:
        return self.hit_rate("followup")

    @property
    def refusal_leaks(self) -> int:
        return sum(o.leaked for o in self.outcomes if o.kind == "refusal")

    def as_dict(self) -> dict[str, object]:
        core = self._core()
        answerable = self._answerable()
        kinds = sorted({o.kind for o in answerable})
        refusals = [o for o in self.outcomes if o.kind == "refusal"]
        multihop = [o for o in self.outcomes if o.kind == "multihop"]
        followup = [o for o in self.outcomes if o.kind == "followup"]
        return {
            "answerable_total": len(core),  # gated denominator = core kinds
            "answerable_hits": sum(o.hit for o in core),
            "overall_hit_rate": self.overall_hit_rate,
            "hit_rate_by_kind": {k: self.hit_rate(k) for k in kinds},
            "multihop_total": len(multihop),
            "multihop_hits": sum(o.hit for o in multihop),
            "multihop_hit_rate": self.multihop_hit_rate,
            "followup_total": len(followup),
            "followup_hits": sum(o.hit for o in followup),
            "followup_hit_rate": self.followup_hit_rate,
            "refusal_total": len(refusals),
            "refusal_leaks": self.refusal_leaks,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


@contextlib.asynccontextmanager
async def seeded_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an ``AsyncSession`` over a fresh temp-file SQLite seeded catalog.

    A temp *file* (not ``:memory:``) is used so the seeding session and the
    retrieval session share one database, mirroring the conftest ``session``
    fixture. The engine is disposed on exit. This does not touch the global
    ``app.core.db`` engine the pytest fixtures manage, so it is safe to call
    from a test, the CLI, or the runner alike.
    """
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        engine = create_async_engine(f"sqlite+aiosqlite:///{fh.name}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
            async with factory() as seed_session:
                await seed_catalog(seed_session)
            async with factory() as session:
                yield session
        finally:
            await engine.dispose()


@contextlib.asynccontextmanager
async def _sqlite_session_and_embedder() -> AsyncGenerator[
    tuple[AsyncSession, Embedder | None], None
]:
    """Adapt the hermetic :func:`seeded_session` to the ``(session, embedder)`` shape.

    The embedder is always ``None`` on SQLite — the vector arm short-circuits to
    ``[]`` there (no pgvector), which is the Phase-0 baseline this path measures.
    """
    async with seeded_session() as session:
        yield session, None


async def _seed_eval_users(session: AsyncSession) -> None:
    """Seed the ``demo_user``/``admin`` rows the judge's orchestrator needs.

    The judge drives the full orchestrator, which writes a ``sessions`` row FK'd to
    ``users``. On the hermetic SQLite path SQLite does not enforce foreign keys so
    this is a no-op there, but Postgres DOES — without these rows the first judged
    case raises a ForeignKeyViolation. Seeded into the same rolled-back transaction
    as the catalog, so it leaves no residue.
    """
    now = datetime.now(UTC)
    for user_id, role in (("demo_user", UserRole.demo_user), ("admin", UserRole.admin)):
        session.add(User(user_id=user_id, role=role, created_at=now))
    await session.flush()


@contextlib.asynccontextmanager
async def postgres_session(
    settings: Settings,
) -> AsyncGenerator[tuple[AsyncSession, Embedder], None]:
    """Yield ``(session, embedder)`` over a REAL Postgres+pgvector catalog (opt-in).

    Unlike :func:`seeded_session` (hermetic SQLite, dead vector arm), this seeds the
    conftest corpus WITH a real embedder into the configured Postgres database so
    the pgvector cosine arm actually runs — the only way to measure semantic recall.

    Safety (see the PR-plan review):

    * Refuses to run against ``environment == "production"`` or a stub embedder or a
      non-Postgres URL — a stub would silently emit a fabricated "semantic" number.
    * Refuses a database whose catalog is non-empty (a pre-existing active index or
      chunks), so it can never collide with or mutate a demo/prod index.
    * Seeds with ``commit=False`` under a UNIQUE per-run ``index_version`` and rolls
      back on EVERY exit path (normal, exception, cancellation) → **zero residue**.
    * Stamps the index provenance from ``configured_embedder_identity(settings)`` —
      the exact identity the read path compares against — so the Tier-3 gate treats
      the index as query-compatible (asserted ``VectorDegrade.none`` per case).

    The embedder is built via :func:`build_embedder` (NOT the process-wide
    ``get_embedder`` singleton) so a leaked stub from earlier in the process can
    never be reused with an openrouter-stamped index (a same-process space mismatch).
    """
    if settings.environment == "production":
        raise PostgresEvalError("the Postgres eval mode must not run against production")
    if not settings.database_url.startswith(("postgresql", "postgres")):
        raise PostgresEvalError(
            f"CITEVYN_DATABASE_URL must be a Postgres URL for --postgres eval; "
            f"got {settings.database_url.split(':', 1)[0]!r}"
        )
    embedder = build_embedder(settings)
    from app.embeddings.stub import StubEmbedder

    if isinstance(embedder, StubEmbedder):
        raise PostgresEvalError(
            "the Postgres eval mode requires a REAL embedder — set "
            "CITEVYN_EMBEDDING_PROVIDER=openrouter (+ CITEVYN_EMBEDDING_MODEL and key). "
            "Refusing to emit a fabricated semantic number under the stub embedder."
        )
    identity = configured_embedder_identity(settings)
    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            existing = await session.scalar(select(func.count()).select_from(Chunk))
            if existing:
                raise PostgresEvalError(
                    f"the target Postgres catalog is not empty ({existing} chunk(s)). "
                    "Run --postgres eval against a migrated-but-empty/dedicated DB so it "
                    "never mutates a demo/prod index (seed rolls back, but a pre-existing "
                    "active index would collide or double-activate)."
                )
            try:
                await seed_catalog(
                    session,
                    index_version=f"eval-pg-{uuid.uuid4().hex[:8]}",
                    embedder=embedder,
                    embedder_identity=identity,
                    commit=False,
                )
                await _seed_eval_users(session)
                yield session, embedder
            finally:
                # Undo the uncommitted seed on every path (normal, error, cancel).
                await session.rollback()
    finally:
        aclose = getattr(embedder, "aclose", None)
        if callable(aclose):
            await aclose()
        await engine.dispose()


async def _retrieve_sources(
    session: AsyncSession,
    case: EvalCase,
    *,
    settings: Settings,
    embedder: Embedder | None = None,
) -> tuple[tuple[str, ...], VectorDegrade]:
    """Run the live retrieval path for one case.

    Returns ``(top-k source names, vector_degrade)``. ``embedder`` is ``None`` on
    the hermetic SQLite path (vector arm dead by design) and the real embedder on
    the Postgres path, where its identity also gates the Tier-3 mismatch check.
    """
    domain = classify_domain(case.question)
    intent = classify_intent(case.question, domain)
    if is_unsupported(domain):
        intent = Intent.unsupported
    identity = configured_embedder_identity(settings) if embedder is not None else None
    retriever = HybridRetriever(
        session,
        embedder=embedder,
        embedder_identity=identity,
        global_confidence=(
            settings.retrieval_global_min_top_score,
            settings.retrieval_global_min_margin,
        ),
    )
    # Mirror the orchestrator's routing so the eval measures the SAME path the product
    # serves: multi-hop (>=2 named products) → retrieve each and merge (Phase 3);
    # else "answer when grounded" (Phase 2) sends an unsupported-routed question
    # through the global confidence-gated arm (product_area=None); else scoped.
    multi_domains = classify_domains(case.question)
    if len(multi_domains) >= 2:
        result = await retriever.retrieve_multi(
            case.question,
            product_areas=[d.value for d in multi_domains],
            intent=intent,
            limit=settings.retrieval_max_candidates,
            top_k=settings.retrieval_top_k,
        )
    else:
        answer_globally = settings.answer_when_grounded and intent is Intent.unsupported
        result = await retriever.retrieve(
            case.question,
            product_area=None if answer_globally else domain.value,
            intent=intent,
            limit=settings.retrieval_max_candidates,
            top_k=settings.retrieval_top_k,
        )
    return tuple(hit.source_name for hit in result.hits), result.vector_degrade


async def evaluate_retrieval(
    cases: Sequence[EvalCase], *, settings: Settings | None = None, postgres: bool = False
) -> RetrievalReport:
    """Compute the retrieval hit-rate report over ``cases``.

    ``postgres=False`` (default): hermetic SQLite, vector arm dead — the Phase-0
    baseline path. ``postgres=True``: opt-in real Postgres+pgvector with a real
    embedder (see :func:`postgres_session`); the only mode that measures semantic
    recall. In Postgres mode an answerable case whose vector arm degraded to a
    Tier-3 ``mismatch`` raises loudly (the index/query embedder identities diverged)
    rather than silently lowering the number.
    """
    settings = settings or get_settings()
    outcomes: list[RetrievalOutcome] = []
    session_cm = postgres_session(settings) if postgres else _sqlite_session_and_embedder()
    async with session_cm as (session, embedder):
        for case in cases:
            sources, degrade = await _retrieve_sources(
                session, case, settings=settings, embedder=embedder
            )
            if postgres and case.kind != "refusal" and degrade is VectorDegrade.mismatch:
                raise PostgresEvalError(
                    f"vector arm degraded to Tier-3 mismatch on case {case.id!r}: the "
                    "seeded index provenance and configured query embedder disagree."
                )
            domain = classify_domain(case.question)
            if case.is_refusal:
                leaked = len(sources) > 0
                hit = False
            elif case.expected_sources is not None:
                # Multi-hop: a HIT requires EVERY named product area to be retrieved.
                leaked = False
                hit = all(s in sources for s in case.expected_sources)
            else:
                leaked = False
                hit = case.expected_source in sources
            outcomes.append(
                RetrievalOutcome(
                    case_id=case.id,
                    area=case.area,
                    kind=case.kind,
                    domain=domain.value,
                    expected_source=case.expected_source,
                    retrieved_sources=sources,
                    hit=hit,
                    leaked=leaked,
                )
            )
    return RetrievalReport(outcomes=tuple(outcomes))
