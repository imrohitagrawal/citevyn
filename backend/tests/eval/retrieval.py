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
from collections.abc import AsyncGenerator, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.guardrails.domain import classify_domain
from app.models import Base
from app.retrieval.hybrid import HybridRetriever
from app.routing.intent import classify_intent
from tests.conftest import seed_catalog

from .cases import EvalCase


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

    def _answerable(self) -> list[RetrievalOutcome]:
        return [o for o in self.outcomes if o.kind != "refusal"]

    @staticmethod
    def _rate(hits: int, total: int) -> float:
        # An empty pool returns 1.0 (0 hits / 0 cases is vacuously "no misses").
        # This is safe ONLY because the callers guard non-emptiness elsewhere:
        # the pytest gate asserts coverage (>=20 cases, every area + kind), and
        # ``runner.gate_failures`` fails on ``answerable_total == 0`` / a missing
        # ``literal`` bucket. Do not lean on this rate alone as a gate.
        return (hits / total) if total else 1.0

    def hit_rate(self, kind: str | None = None) -> float:
        """Fraction of answerable cases (optionally of one kind) that hit."""
        pool = [o for o in self._answerable() if kind is None or o.kind == kind]
        return self._rate(sum(o.hit for o in pool), len(pool))

    @property
    def overall_hit_rate(self) -> float:
        return self.hit_rate()

    @property
    def refusal_leaks(self) -> int:
        return sum(o.leaked for o in self.outcomes if o.kind == "refusal")

    def as_dict(self) -> dict[str, object]:
        answerable = self._answerable()
        kinds = sorted({o.kind for o in answerable})
        refusals = [o for o in self.outcomes if o.kind == "refusal"]
        return {
            "answerable_total": len(answerable),
            "answerable_hits": sum(o.hit for o in answerable),
            "overall_hit_rate": self.overall_hit_rate,
            "hit_rate_by_kind": {k: self.hit_rate(k) for k in kinds},
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


async def _retrieve_sources(
    session: AsyncSession, case: EvalCase, *, settings: Settings
) -> tuple[str, ...]:
    """Run the live retrieval path for one case; return the top-k source names."""
    domain = classify_domain(case.question)
    intent = classify_intent(case.question, domain)
    result = await HybridRetriever(session, embedder=None).retrieve(
        case.question,
        product_area=domain.value,
        intent=intent,
        limit=settings.retrieval_max_candidates,
        top_k=settings.retrieval_top_k,
    )
    return tuple(hit.source_name for hit in result.hits)


async def evaluate_retrieval(
    cases: Sequence[EvalCase], *, settings: Settings | None = None
) -> RetrievalReport:
    """Compute the retrieval hit-rate report over ``cases`` (hermetic)."""
    settings = settings or get_settings()
    outcomes: list[RetrievalOutcome] = []
    async with seeded_session() as session:
        for case in cases:
            sources = await _retrieve_sources(session, case, settings=settings)
            domain = classify_domain(case.question)
            if case.is_refusal:
                leaked = len(sources) > 0
                hit = False
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
