"""Measure search on the 44 shipped passages, with recorded vectors and no model (#601).

The seam, :func:`shipped_corpus_session`, ingests the real sources through the real
runner inside one transaction and rolls it back afterwards, so exact terms and chunking
are the shipped ones and the catalog is left empty. Query and chunk vectors come from the
recording (:mod:`tests.eval.recorded`), which is what lets this run in free CI.

The question path mirrors :func:`tests.eval.retrieval._retrieve_sources`, with one known
difference, labelled in the report: a follow-up's history is concatenated, where
production rewrites it with a model. Follow-ups are excluded from decision A.

Decision A is the number that picks the next retrieval work: precision@1 over answerable,
single-relevant, non-follow-up golden questions. Everything else is reported beside it.
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import json
import pathlib
import tomllib
import uuid
from collections.abc import AsyncGenerator, Generator, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.answer.memory import build_contextual_query
from app.core.config import Settings, get_settings
from app.embeddings.factory import EmbedderIdentity
from app.embeddings.protocol import Embedder
from app.guardrails.domain import (
    canonicalize_product_name,
    canonicalize_self_reference,
    classify_domain,
    classify_domains,
    is_unsupported,
)
from app.models import Chunk, Document, IndexVersion
from app.models.enums import DocumentStatus, IndexStatus, JobStatus
from app.retrieval import hybrid as hybrid_module
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.types import RetrievalResult, VectorDegrade
from app.routing.intent import Intent, classify_intent, should_skip_retrieval
from app.worker.allowlist import MVP_SOURCES
from app.worker.chunker import chunk_document
from app.worker.cli import content_version_hash
from app.worker.fetchers import LocalFetcher
from app.worker.parser import parse_markdown
from app.worker.runner import IngestionRunner, ensure_index_version

from .cases import load_cases
from .paths import GOLDEN_PATH, REPO_ROOT, SHIPPED_QUESTIONS_PATH
from .recorded import (
    RECORD_COMMAND,
    MissingRecordedVector,
    RecordedEmbedder,
    RecordingMismatch,
    ShippedEvalError,
    check_chunk_vectors,
    identity_from_header,
    load_chunk_vectors,
    load_question_vectors,
    sha256_text,
)

__all__ = [
    "RECORD_COMMAND",
    "MissingRecordedVector",
    "RecordedEmbedder",
    "RecordingMismatch",
    "ShippedCase",
    "ShippedEvalError",
    "ShippedLabelError",
    "ShippedOutcome",
    "all_shipped_cases",
    "arm_label",
    "build_report",
    "check_production_identity",
    "check_chunk_vectors",
    "evaluate_cases",
    "identity_from_header",
    "known_question_texts",
    "load_chunk_vectors",
    "load_golden_labels",
    "load_question_vectors",
    "load_shipped_questions",
    "production_embedder_identity",
    "render_summary",
    "retrieve_routed",
    "route_case",
    "shipped_chunk_texts",
    "shipped_corpus_session",
    "shipped_heading_keys",
    "write_report",
]

ROUTE_SCOPED = "scoped"
ROUTE_GLOBAL = "global_vector_only"
ROUTE_MULTI = "multi_product"
_RECALL_DEPTHS = (1, 3, 6)
_EXCLUSIONS = ("follow_up", "multi_relevant", "no_single_gold", "refusal", "coverage_set")


class ShippedLabelError(ValueError):
    """A label file that breaks the rules; the message names the row or key."""


# --- Chunks and labels --------------------------------------------------------------


def shipped_chunk_texts() -> dict[str, str]:
    """``source#heading`` to exact chunk text, from the real parser and chunker."""
    fetcher = LocalFetcher()
    texts: dict[str, str] = {}
    for spec in MVP_SOURCES:
        for draft in chunk_document(parse_markdown(fetcher.fetch(spec)), source=spec):
            key = f"{spec.name}#{draft.heading}"
            if key in texts:
                raise ShippedEvalError(f"two shipped chunks share the key {key!r}")
            texts[key] = draft.text
    return texts


def shipped_heading_keys() -> dict[str, str]:
    """``source#heading`` to content checksum, the identity a recorded chunk is held to."""
    return {key: sha256_text(text) for key, text in shipped_chunk_texts().items()}


def production_embedder_identity() -> EmbedderIdentity:
    """Production's query embedder: ``fly.toml``'s environment over the Settings defaults."""
    env = tomllib.loads((REPO_ROOT / "fly.toml").read_text(encoding="utf-8")).get("env", {})

    def value(field: str) -> Any:
        return env.get(f"CITEVYN_{field.upper()}", Settings.model_fields[field].default)

    return EmbedderIdentity(
        provider=value("embedding_provider"),
        model=value("embedding_model"),
        dim=int(value("embedding_dim")),
    )


def check_production_identity(*headers: Mapping[str, Any]) -> None:
    """Every recording header must name production's provider, model and dimension."""
    production = production_embedder_identity()
    for header in headers:
        recorded = identity_from_header(header)
        if recorded != production:
            raise ShippedEvalError(
                f"a recording was made with {tuple(recorded)}; production embeds with "
                f"{tuple(production)}. Re-record with: {RECORD_COMMAND}"
            )


@dataclasses.dataclass(frozen=True)
class ShippedCase:
    id: str
    question: str
    origin: str
    kind: str | None
    coverage_kind: str | None
    shipped_gold: tuple[str, ...]
    follow_up: bool = False
    expected_empty: bool = False
    facet: str | None = None
    planned_for: str | None = None
    history: tuple[str, ...] = ()


def _check_gold(case_id: str, gold: Iterable[str], heading_keys: Mapping[str, str]) -> None:
    for key in gold:
        if key not in heading_keys:
            raise ShippedLabelError(f"case {case_id!r}: {key!r} is not a shipped heading")


def load_golden_labels(path: pathlib.Path, heading_keys: Mapping[str, str]) -> list[ShippedCase]:
    """The golden cases with their shipped labels. An unreviewed answerable row fails."""
    try:
        golden = load_cases(path)
    except ValueError as exc:
        raise ShippedLabelError(str(exc)) from exc
    cases: list[ShippedCase] = []
    for row in golden:
        if row.shipped_gold is None and not row.is_refusal:
            raise ShippedLabelError(f"answerable case {row.id!r} has no shipped_gold")
        gold = row.shipped_gold or ()
        _check_gold(row.id, gold, heading_keys)
        cases.append(
            ShippedCase(
                id=row.id,
                question=row.question,
                origin="golden",
                kind=row.kind,
                coverage_kind=None,
                shipped_gold=gold,
                follow_up=row.kind == "followup" or bool(row.history),
                history=row.history,
            )
        )
    return cases


def load_shipped_questions(
    path: pathlib.Path, heading_keys: Mapping[str, str]
) -> list[ShippedCase]:
    """The coverage questions. Every row must say what it expects."""
    cases: list[ShippedCase] = []
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ShippedLabelError(f"{path.name}:{lineno}: invalid JSON") from exc
        absent = [k for k in ("id", "question", "coverage_kind", "shipped_gold") if k not in row]
        if absent:
            raise ShippedLabelError(f"{path.name}:{lineno}: missing {absent}")
        case_id = row["id"]
        if case_id in seen:
            raise ShippedLabelError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        gold = tuple(row["shipped_gold"])
        expected_empty = bool(row.get("expected_empty", False))
        if expected_empty and gold:
            raise ShippedLabelError(f"case {case_id!r} is expected empty but carries gold")
        if not expected_empty and not gold:
            raise ShippedLabelError(f"case {case_id!r} carries no gold and is not expected empty")
        _check_gold(case_id, gold, heading_keys)
        history = tuple(row.get("history") or ())
        cases.append(
            ShippedCase(
                id=case_id,
                question=row["question"],
                origin="coverage",
                kind=None,
                coverage_kind=row["coverage_kind"],
                shipped_gold=gold,
                follow_up=bool(history),
                expected_empty=expected_empty,
                facet=row.get("facet"),
                planned_for=row.get("planned_for"),
                history=history,
            )
        )
    return cases


def all_shipped_cases() -> list[ShippedCase]:
    """Every golden and coverage question, each id unique across both sets."""
    headings = shipped_heading_keys()
    cases = [
        *load_golden_labels(GOLDEN_PATH, headings),
        *load_shipped_questions(SHIPPED_QUESTIONS_PATH, headings),
    ]
    repeated = [key for key, n in collections.Counter(c.id for c in cases).items() if n > 1]
    if repeated:
        raise ShippedLabelError(f"case ids used in both sets: {sorted(repeated)}")
    return cases


# --- The question path --------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RoutedQuestion:
    """What the product would search for this question, and which route it takes."""

    query: str
    route: str
    routed_domain: str
    intent: Intent
    areas: tuple[str, ...]
    # ``clarify`` or ``refused`` when production answers without retrieving at all.
    production_skip: str | None = None


def route_case(case: ShippedCase, settings: Settings) -> RoutedQuestion:
    """Keep in step with ``tests.eval.retrieval._retrieve_sources`` and ``Orchestrator.ask``."""
    query = canonicalize_self_reference(case.question)
    if case.history and settings.conversation_memory:
        # ``build_contextual_query`` wants prior turns most recent first.
        priors = list(reversed(case.history))[: settings.memory_recent_turns]
        query = build_contextual_query(query, priors)
    query = canonicalize_product_name(query)
    domain = classify_domain(query)
    intent = classify_intent(query, domain)
    if is_unsupported(domain):
        intent = Intent.unsupported
    answer_globally = settings.answer_when_grounded and intent is Intent.unsupported
    named = [d.value for d in classify_domains(query)]
    if len(named) >= 2:
        route, areas = ROUTE_MULTI, tuple(named)
    elif answer_globally:
        route, areas = ROUTE_GLOBAL, ()
    else:
        route, areas = ROUTE_SCOPED, (domain.value,)
    skip = None
    if intent is Intent.unsupported and not answer_globally:
        skip = "refused"
    elif should_skip_retrieval(intent) and not answer_globally:
        skip = "clarify"
    return RoutedQuestion(
        query=query,
        route=route,
        routed_domain=domain.value,
        intent=intent,
        areas=areas,
        production_skip=skip,
    )


class _NoHits:
    """An arm switched off: built like a retriever, it finds nothing."""

    def __init__(self, *_: object, **__: object) -> None:
        pass

    async def retrieve(self, *_: object, **__: object) -> list[Any]:
        return []


@contextlib.contextmanager
def _arms_off(arms: frozenset[str]) -> Generator[None]:
    """Switch the exact or keyword arm off for one retrieval, then restore it."""
    names = {"exact": "ExactRetriever", "keyword": "KeywordRetriever"}
    saved = {arm: getattr(hybrid_module, names[arm]) for arm in arms if arm in names}
    try:
        for arm in saved:
            setattr(hybrid_module, names[arm], _NoHits)
        yield
    finally:
        for arm, original in saved.items():
            setattr(hybrid_module, names[arm], original)


async def retrieve_routed(
    session: AsyncSession,
    routed: RoutedQuestion,
    *,
    embedder: Embedder,
    identity: EmbedderIdentity | None,
    settings: Settings,
    arms_off: frozenset[str] = frozenset(),
) -> RetrievalResult:
    """The real retriever over the routed question, optionally with arms switched off."""
    retriever = HybridRetriever(
        session,
        embedder=None if "vector" in arms_off else embedder,
        embedder_identity=identity,
        global_confidence=(
            settings.retrieval_global_min_top_score,
            settings.retrieval_global_min_margin,
        ),
    )
    with _arms_off(arms_off):
        if routed.route == ROUTE_MULTI:
            return await retriever.retrieve_multi(
                routed.query,
                product_areas=list(routed.areas),
                intent=routed.intent,
                limit=settings.retrieval_max_candidates,
                top_k=settings.retrieval_top_k,
            )
        return await retriever.retrieve(
            routed.query,
            product_area=routed.areas[0] if routed.areas else None,
            intent=routed.intent,
            limit=settings.retrieval_max_candidates,
            top_k=settings.retrieval_top_k,
        )


def known_question_texts() -> set[str]:
    """Every exact text the recording must cover: the query each case really embeds."""
    settings = get_settings()
    return {route_case(case, settings).query for case in all_shipped_cases()}


# --- The seam -----------------------------------------------------------------------


async def _ingest_shipped_corpus(
    session: AsyncSession, embedder: RecordedEmbedder, *, index_version: str
) -> None:
    """The real runner over every source, then promotion, all inside the caller's transaction."""
    index_identity = embedder.index_identity
    corpus_hash = content_version_hash(MVP_SOURCES)
    row = await ensure_index_version(
        session,
        index_version=index_version,
        source_version_hash=corpus_hash,
        embedding_provider=index_identity.provider,
        embedding_model=index_identity.model,
        embedding_dim=index_identity.dim,
    )
    runner = IngestionRunner(
        fetcher=LocalFetcher(),
        embedder=embedder,
        source_version_hash=corpus_hash,
        index_version=index_version,
        embedding_provider=index_identity.provider,
        embedding_model=index_identity.model,
    )
    for spec in MVP_SOURCES:
        result = await runner.run(session, source=spec)
        if result.status is not JobStatus.completed:
            raise ShippedEvalError(
                f"ingesting {spec.name!r} did not complete: "
                f"{result.error_type}: {result.error_message}"
            )
    # A candidate index has no readable stamp, which would leave the identity gate open.
    row.status = IndexStatus.active
    row.promoted_at = datetime.now(UTC)
    await session.flush()


@contextlib.asynccontextmanager
async def shipped_corpus_session(
    settings: Settings,
    query_embedder: RecordedEmbedder,
    query_identity: EmbedderIdentity,
) -> AsyncGenerator[tuple[AsyncSession, RecordedEmbedder], None]:
    """Yield ``(session, embedder)`` over the real shipped corpus, then leave no trace.

    Every refusal comes before any connection. The session joins an outer transaction
    with savepoints, so the runner's commits only release savepoints and the rollback
    removes every row. ``expire_on_commit=False`` is required: without it the runner
    reads expired attributes after a commit and every source fails.
    """
    if settings.environment == "production":
        raise ShippedEvalError("the shipped-corpus eval must not run against production")
    if not settings.database_url.startswith(("postgresql", "postgres")):
        raise ShippedEvalError("the shipped-corpus eval needs a Postgres database")
    if None in query_identity:
        raise ShippedEvalError(f"the query stamp {tuple(query_identity)} is incomplete")
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            session = AsyncSession(
                bind=conn,
                join_transaction_mode="create_savepoint",
                expire_on_commit=False,
                autoflush=False,
            )
            try:
                for model in (Chunk, IndexVersion):
                    existing = await session.scalar(select(func.count()).select_from(model))
                    if existing:
                        raise ShippedEvalError(
                            f"the catalog is not empty ({existing} {model.__tablename__} rows)"
                        )
                await _ingest_shipped_corpus(
                    session, query_embedder, index_version=f"shipped-{uuid.uuid4().hex[:8]}"
                )
                yield session, query_embedder
            finally:
                await session.close()
                await outer.rollback()
    finally:
        await engine.dispose()


# --- Evaluation ---------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ShippedOutcome:
    case_id: str
    origin: str
    kind: str | None
    coverage_kind: str | None
    gold: tuple[str, ...]
    retrieved: tuple[str, ...]
    route: str | None
    winning_arm: str | None
    routed_domain: str
    degrade: VectorDegrade
    follow_up: bool
    # Which arms, removed one at a time, change the rank-1 passage, and the gold rank
    # with only the vector arm or only the keyword arm on.
    rank1_changed_by: tuple[str, ...] = ()
    rank_vector_only: int | None = None
    rank_keyword_only: int | None = None
    production_skip: str | None = None


_ARMS = ("vector", "keyword", "exact")


def arm_label(changed_by: Iterable[str]) -> str:
    """``none``, the single arm whose removal changes rank 1, or ``both`` for several."""
    arms = tuple(changed_by)
    if not arms:
        return "none"
    return arms[0] if len(arms) == 1 else "both"


def _keys(result: RetrievalResult) -> tuple[str, ...]:
    return tuple(f"{hit.source_name}#{hit.heading}" for hit in result.hits)


def _gold_rank(gold: Sequence[str], retrieved: Sequence[str]) -> int | None:
    return next((i for i, key in enumerate(retrieved, start=1) if key in set(gold)), None)


async def _check_vector_arm_is_live(session: AsyncSession) -> None:
    """Every shipped chunk is active and embedded, or no number is honest."""
    expected = len(shipped_heading_keys())
    active = (
        select(func.count())
        .select_from(Chunk)
        .join(Document, Chunk.document_id == Document.document_id)
        .where(Document.status == DocumentStatus.active)
    )
    total = int(await session.scalar(active) or 0)
    embedded = int(await session.scalar(active.where(Chunk.embedding.is_not(None))) or 0)  # type: ignore[attr-defined]
    if total != expected or embedded != expected:
        raise ShippedEvalError(
            f"the vector arm is not fully live: {embedded} of {total} active chunks embedded, "
            f"expected {expected}"
        )


async def evaluate_cases(
    session: AsyncSession,
    embedder: Embedder,
    identity: EmbedderIdentity,
    cases: Sequence[ShippedCase],
    settings: Settings,
) -> list[ShippedOutcome]:
    """Run each case through the real retriever. ``identity`` drives the mismatch gate.

    Each case also runs with each arm removed in turn, so the report can say which arm
    really decided rank 1 rather than which arm happened to tag the hit.
    """
    await _check_vector_arm_is_live(session)
    outcomes: list[ShippedOutcome] = []
    for case in cases:
        routed = route_case(case, settings)

        async def run(off: frozenset[str], routed: RoutedQuestion = routed) -> RetrievalResult:
            return await retrieve_routed(
                session,
                routed,
                embedder=embedder,
                identity=identity,
                settings=settings,
                arms_off=off,
            )

        result = await run(frozenset())
        retrieved = _keys(result)
        top = retrieved[:1]
        changed_by: list[str] = []
        for arm in _ARMS:
            if _keys(await run(frozenset({arm})))[:1] != top:
                changed_by.append(arm)
        vector_only = _keys(await run(frozenset({"keyword", "exact"})))
        keyword_only = _keys(await run(frozenset({"vector", "exact"})))
        outcomes.append(
            ShippedOutcome(
                case_id=case.id,
                origin=case.origin,
                kind=case.kind,
                coverage_kind=case.coverage_kind,
                gold=case.shipped_gold,
                retrieved=retrieved,
                route=routed.route,
                winning_arm=arm_label(changed_by) if retrieved else None,
                routed_domain=routed.routed_domain,
                degrade=result.vector_degrade,
                follow_up=case.follow_up,
                rank1_changed_by=tuple(changed_by),
                rank_vector_only=_gold_rank(case.shipped_gold, vector_only),
                rank_keyword_only=_gold_rank(case.shipped_gold, keyword_only),
                production_skip=routed.production_skip,
            )
        )
    return outcomes


# --- The report ---------------------------------------------------------------------


def _rank(outcome: ShippedOutcome) -> int | None:
    gold = set(outcome.gold)
    return next((i for i, key in enumerate(outcome.retrieved, start=1) if key in gold), None)


def _recall_at(outcome: ShippedOutcome, depth: int) -> float:
    return len(set(outcome.gold) & set(outcome.retrieved[:depth])) / len(set(outcome.gold))


def _metrics(pool: Sequence[ShippedOutcome]) -> dict[str, Any]:
    """Rank metrics over cases that have gold. An empty pool has no number, never a 1.0."""
    if not pool:
        return {
            "n": 0,
            "precision_at_1": None,
            "mrr": None,
            **{f"recall_at_{d}": None for d in _RECALL_DEPTHS},
        }
    ranks = [_rank(o) for o in pool]
    return {
        "n": len(pool),
        "precision_at_1": sum(r == 1 for r in ranks) / len(pool),
        "mrr": sum(1.0 / r for r in ranks if r is not None) / len(pool),
        **{
            f"recall_at_{d}": sum(_recall_at(o, d) for o in pool) / len(pool)
            for d in _RECALL_DEPTHS
        },
    }


def _exclusion(outcome: ShippedOutcome) -> str | None:
    """Why a question is outside decision A, or ``None`` when it counts."""
    if outcome.origin != "golden":
        return "coverage_set"
    if outcome.kind == "refusal":
        return "refusal"
    if outcome.follow_up:
        return "follow_up"
    if not outcome.gold:
        return "no_single_gold"
    if len(outcome.gold) > 1:
        return "multi_relevant"
    return None


def _validate(outcome: ShippedOutcome) -> None:
    """A degraded or unlabelled question would make the report quietly wrong."""
    if outcome.degrade is not VectorDegrade.none:
        raise ShippedEvalError(
            f"case {outcome.case_id!r} ran degraded ({outcome.degrade.value}); no report"
        )
    if not outcome.route:
        raise ShippedEvalError(f"case {outcome.case_id!r} has no route")
    if outcome.retrieved and not outcome.winning_arm:
        raise ShippedEvalError(f"case {outcome.case_id!r} has hits but no winning arm")


def _group(outcomes: Sequence[ShippedOutcome], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[ShippedOutcome]] = collections.defaultdict(list)
    for outcome in outcomes:
        label = getattr(outcome, field)
        if label is not None and outcome.gold:
            groups[label].append(outcome)
    return {label: _metrics(pool) for label, pool in sorted(groups.items())}


def build_report(outcomes: Sequence[ShippedOutcome]) -> dict[str, Any]:
    if not outcomes:
        raise ShippedEvalError("no questions were evaluated, so there is no report")
    for outcome in outcomes:
        _validate(outcome)
    reasons = {o.case_id: _exclusion(o) for o in outcomes}
    pool = [o for o in outcomes if reasons[o.case_id] is None]
    refusals = [o for o in outcomes if o.kind == "refusal"]
    passages = collections.Counter(o.gold[0] for o in pool)
    non_vector = sum(1 for o in outcomes if set(o.rank1_changed_by) & {"keyword", "exact"})
    return {
        "decision_a": {
            **_metrics(pool),
            "pool": len(pool),
            "distinct_passages": len(passages),
            "largest_passage_share": (max(passages.values()) / len(pool) if pool else None),
            "total": len(outcomes),
            "excluded": {
                label: sum(1 for r in reasons.values() if r == label) for label in _EXCLUSIONS
            },
        },
        "by_kind": _group(outcomes, "kind"),
        "by_coverage_kind": _group(outcomes, "coverage_kind"),
        "refusals": {
            "total": len(refusals),
            "retrieved_something": sum(1 for o in refusals if o.retrieved),
        },
        "arm_attribution": {"non_vector_changed_rank_1": non_vector, "total": len(outcomes)},
        "skipped_in_production": dict(
            sorted(
                collections.Counter(
                    o.production_skip for o in outcomes if o.production_skip
                ).items()
            )
        ),
        "routes": dict(sorted(collections.Counter(str(o.route) for o in outcomes).items())),
        "winning_arms": dict(
            sorted(collections.Counter(str(o.winning_arm) for o in outcomes).items())
        ),
        "questions": [
            {
                "id": o.case_id,
                "origin": o.origin,
                "kind": o.kind,
                "coverage_kind": o.coverage_kind,
                "gold": list(o.gold),
                "retrieved": list(o.retrieved),
                "rank": _rank(o),
                "route": o.route,
                "winning_arm": o.winning_arm,
                "arm": arm_label(o.rank1_changed_by) if o.retrieved else None,
                "rank_vector_only": o.rank_vector_only,
                "rank_keyword_only": o.rank_keyword_only,
                "skipped_in_production": o.production_skip,
                "routed_domain": o.routed_domain,
                "follow_up": o.follow_up,
                "decision_a_exclusion": reasons[o.case_id],
            }
            for o in outcomes
        ],
    }


def _rank_cell(rank: int | None) -> str:
    return "-" if rank is None else str(rank)


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _table(title: str, groups: Mapping[str, Mapping[str, Any]]) -> list[str]:
    lines = [
        f"### {title}",
        "",
        "| group | n | P@1 | MRR | R@1 | R@3 | R@6 |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, row in groups.items():
        values = [row["precision_at_1"], row["mrr"], *(row[f"recall_at_{d}"] for d in (1, 3, 6))]
        lines.append(f"| {label} | {row['n']} | " + " | ".join(map(_number, values)) + " |")
    return [*lines, ""]


def render_summary(report: Mapping[str, Any]) -> str:
    """The report as Markdown, for the job summary."""
    decision = report["decision_a"]
    lines = [
        "## Shipped-corpus search (recorded vectors, no model)",
        "",
        f"**Decision A**: precision@1 {_number(decision['precision_at_1'])}, "
        f"MRR {_number(decision['mrr'])}, recall@1 {_number(decision['recall_at_1'])}, "
        f"over a pool of {decision['pool']} of {decision['total']} questions.",
        "",
        f"The pool covers {decision['distinct_passages']} distinct passages; the largest "
        f"passage holds a share of {_number(decision['largest_passage_share'])}.",
        "",
        "Excluded: "
        + ", ".join(f"{label} {count}" for label, count in decision["excluded"].items())
        + ".",
        "Follow-ups are concatenated here and rewritten by a model in production.",
        "",
        *_table("By golden kind", report["by_kind"]),
        *_table("By coverage kind (reported separately)", report["by_coverage_kind"]),
        f"Refusals that retrieved something (informational): "
        f"{report['refusals']['retrieved_something']} of {report['refusals']['total']}.",
        "",
        f"Arms (each removed in turn): a non-vector arm changed rank 1: "
        f"{report['arm_attribution']['non_vector_changed_rank_1']} of "
        f"{report['arm_attribution']['total']}.",
        "Not retrieved for in production (reported, not excluded): "
        + (", ".join(f"{k} {v}" for k, v in report["skipped_in_production"].items()) or "none")
        + ".",
        "",
        "Routes: " + ", ".join(f"{k} {v}" for k, v in report["routes"].items()) + ".",
        "Winning arms: " + ", ".join(f"{k} {v}" for k, v in report["winning_arms"].items()) + ".",
        "",
        "### Per question",
        "",
        "| id | rank | vector only | keyword only | route | arm | domain | follow-up | skipped |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {row['id']} | {_rank_cell(row['rank'])} | {_rank_cell(row['rank_vector_only'])} "
        f"| {_rank_cell(row['rank_keyword_only'])} | {row['route']} "
        f"| {row['arm'] or '-'} | {row['routed_domain']} "
        f"| {'yes' if row['follow_up'] else 'no'} | {row['skipped_in_production'] or '-'} |"
        for row in report["questions"]
    )
    return "\n".join(lines) + "\n"


def write_report(
    report: Mapping[str, Any], json_path: pathlib.Path, summary_path: pathlib.Path | None
) -> None:
    """Write the JSON, and append to the job summary so earlier steps' output survives."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if summary_path is not None:
        with summary_path.open("a", encoding="utf-8") as summary:
            summary.write(render_summary(report))
