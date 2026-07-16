"""Distractor corpus + context precision/recall — PR B of #125.

The clean 5-chunk conftest corpus has ONE chunk per product area, so retrieval never
has to *choose* between competing chunks — precision/recall are trivially 1.0 and the
rank metric (:mod:`tests.eval.retrieval`) is only exercised on the 2 global paraphrases.
This module adds a dedicated, eval-only **distractor corpus** so ``top_k`` is forced to
select among many candidates, making context recall/precision a real signal.

Isolation (see the two adversarial plan reviews for #125):

* The distractor corpus is seeded by :func:`seed_eval_distractors` — a SEPARATE function,
  NEVER ``conftest.seed_catalog`` — into its OWN throwaway product area (``eval_grafana``)
  under its OWN active ``IndexVersion``. The locked hermetic run (SQLite ``seed_catalog``)
  and the locked judged ``--postgres`` run (clean corpus) are byte-for-byte unchanged.
* :func:`postgres_distractor_session` carries the SAME safety rails as
  :func:`tests.eval.retrieval.postgres_session`: refuses production / a non-Postgres URL /
  a stub embedder / a NON-EMPTY catalog, seeds with ``commit=False``, and rolls back on
  every exit path → zero residue. Run it SERIALLY against the same dedicated/empty DB the
  judged pass uses — never concurrently (both share one ``CITEVYN_DATABASE_URL``).
* Retrieval is measured **vector-only** (:class:`~app.retrieval.vector.VectorRetriever`
  directly, not the hybrid path): the hybrid keyword ILIKE arm scores a flat 0.5 and would
  confound the ranking into a keyword tautology. Vector-only measures the cosine ranking the
  metric actually claims.
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
from app.embeddings.factory import EmbedderIdentity, build_embedder, configured_embedder_identity
from app.embeddings.protocol import Embedder
from app.models import (
    Chunk,
    Document,
    DocumentStatus,
    IndexStatus,
    IndexVersion,
)
from app.retrieval.vector import VectorRetriever

from .cases import EvalCase, load_cases
from .paths import DISTRACTOR_GOLDEN_PATH
from .retrieval import PostgresEvalError, _chunk_key_map

# The one throwaway product area the whole distractor corpus lives in. A query scoped to it
# (``VectorRetriever.retrieve(product_area=DISTRACTOR_AREA)``) competes the 2 gold chunks
# against every distractor. It is NOT one of the 5 real areas, so it can never collide with a
# locked case or the one-chunk-per-area guard on ``seed_catalog``.
DISTRACTOR_AREA = "eval_grafana"

# The multi-relevant GOLD source: one document, two DISTINGUISHABLE chunks. Its stable keys
# (PR A's ``"{source_name}#{chunk_order}"``) are ``eval_grafana#0`` / ``eval_grafana#1``.
_GOLD_SOURCE = "eval_grafana"
_GOLD_CHUNKS: tuple[tuple[str, str], ...] = (
    (
        "Dashboards and panels",
        "Build a Grafana dashboard by adding panels; each panel visualizes a query from a "
        "data source. Arrange the panels on the dashboard grid, resize them, and save the "
        "dashboard so a team can open the same view.",
    ),
    (
        "Alerting rules",
        "Create a Grafana alerting rule that evaluates a query on a schedule and fires "
        "notifications to a contact point when the value breaches the threshold you set. "
        "Group and route firing alerts by labels.",
    ),
)

# Within-area DISTRACTORS: plausible Grafana-adjacent subtopics, each its own source with a
# single chunk, so ``top_k=6`` over the ~18-chunk area forces real selection.
#
# The last TWO are deliberate LEXICAL HARD NEGATIVES (adversarial PR review): they SHARE the
# gold vocabulary (``panels``/``dashboards``; ``alert``/``notifications``) but are NOT the
# answer — reusing a saved panel is not "how to build/arrange a dashboard", and silencing
# alerts is not "how to create an alerting rule". A metric whose distractors are all disjoint
# subtopics would pass under any non-broken embedder (it could only detect a dead arm, which
# hit-rate already catches); these near-misses give precision@|gold| the teeth to catch a
# SUBTLE ranking regression — the whole reason the distractor corpus exists.
_DISTRACTORS: tuple[tuple[str, str, str], ...] = (
    (
        "eval_grafana_datasources",
        "Data sources",
        "Add a data source to connect to Prometheus, InfluxDB, or a SQL database before querying.",
    ),
    (
        "eval_grafana_plugins",
        "Plugins",
        "Install plugins from the catalog to add new panel types, data sources, and apps.",
    ),
    (
        "eval_grafana_ldap",
        "LDAP auth",
        "Configure LDAP authentication so users sign in with their existing directory credentials.",
    ),
    (
        "eval_grafana_provisioning",
        "Provisioning",
        "Provision data sources and folders as code with YAML files loaded at startup.",
    ),
    (
        "eval_grafana_variables",
        "Template variables",
        "Use template variables to make a view dynamic with dropdown selectors for hosts and jobs.",
    ),
    (
        "eval_grafana_annotations",
        "Annotations",
        "Add annotations to mark events such as deploys directly on the time axis of a graph.",
    ),
    (
        "eval_grafana_transformations",
        "Transformations",
        "Apply transformations to join, filter, and reshape query results before display.",
    ),
    (
        "eval_grafana_explore",
        "Explore mode",
        "Use Explore mode for ad-hoc querying and troubleshooting without building anything.",
    ),
    (
        "eval_grafana_reporting",
        "Reporting",
        "Schedule PDF reports of a view to be emailed to stakeholders on a cadence.",
    ),
    (
        "eval_grafana_teams",
        "Teams and permissions",
        "Organize users into teams and assign folder permissions to control who can access what.",
    ),
    (
        "eval_grafana_rendering",
        "Image rendering",
        "Enable the image renderer to export panels as PNG images for reports and messages.",
    ),
    (
        "eval_grafana_apikeys",
        "Service accounts",
        "Create service accounts and tokens to authenticate programmatic access to the HTTP API.",
    ),
    (
        "eval_grafana_ha",
        "High availability",
        "Run in high-availability mode with a shared database and a load balancer across replicas.",
    ),
    (
        "eval_grafana_backup",
        "Backup and restore",
        "Back up by exporting the database and provisioning files so you can restore later.",
    ),
    # --- lexical HARD NEGATIVES (share gold vocabulary, are NOT the answer) ---
    (
        "eval_grafana_panel_library",
        "Panel library",
        "Reuse a saved panel across dashboards from a shared library instead of rebuilding it.",
    ),
    (
        "eval_grafana_silences",
        "Alert silences",
        "Silence or mute alert notifications during a maintenance window so they do not fire.",
    ),
)


def _chunk_key(source_name: str, chunk_order: int) -> str:
    """The stable chunk key (PR A scheme) — kept in ONE place so seed + golden never drift."""
    return f"{source_name}#{chunk_order}"


def seeded_chunk_keys() -> set[str]:
    """The exact set of stable chunk keys :func:`seed_eval_distractors` produces.

    Lets a hermetic test assert every golden ``gold_chunks`` key references a real seeded
    chunk (a typo'd key would silently score recall 0), without needing Postgres.
    """
    keys = {_chunk_key(_GOLD_SOURCE, order) for order in range(len(_GOLD_CHUNKS))}
    keys |= {_chunk_key(source, 0) for source, _heading, _text in _DISTRACTORS}
    return keys


async def seed_eval_distractors(
    session: AsyncSession,
    *,
    embedder: Embedder,
    embedder_identity: EmbedderIdentity | None = None,
    index_version: str,
    commit: bool = False,
) -> dict[str, list[object]]:
    """Seed the self-contained distractor corpus into ``session`` and return the rows.

    Mirrors ``conftest.seed_catalog``'s structure (one active ``IndexVersion``, active
    documents, embedded chunks) but is a DISTINCT function so it can never perturb the locked
    corpus. All chunks share ``product_area == DISTRACTOR_AREA`` so a scoped vector query
    competes them together. Exactly ONE active ``IndexVersion`` is created (asserted by the
    caller) so the vector arm's provenance resolution is unambiguous. ``commit=False`` (the
    default) flushes without committing so the caller owns the rollback.
    """
    now = datetime.now(UTC)
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

    # (source_name, chunk_order, heading, text) rows: the 2-chunk gold source + 1-chunk
    # distractors. Distinct (source_name, chunk_order) pairs → distinct stable keys (asserted).
    specs: list[tuple[str, int, str, str]] = [
        (_GOLD_SOURCE, order, heading, text) for order, (heading, text) in enumerate(_GOLD_CHUNKS)
    ]
    specs += [(source, 0, heading, text) for source, heading, text in _DISTRACTORS]

    docs: list[Document] = []
    chunks: list[Chunk] = []
    for source_name, chunk_order, heading, text in specs:
        doc = Document(
            document_id=uuid.uuid4(),
            index_version=index_version,
            source_name=source_name,
            product_area=DISTRACTOR_AREA,
            source_url=f"/eval/{source_name}",
            title=f"{source_name} ({heading})",
            content_checksum=f"sha256:{source_name}-chunk-{chunk_order}",
            last_fetched_at=now,
            last_indexed_at=now,
            status=DocumentStatus.active,
        )
        session.add(doc)
        await session.flush()
        chunk = Chunk(
            chunk_id=uuid.uuid4(),
            document_id=doc.document_id,
            product_area=DISTRACTOR_AREA,
            section_path=heading,
            heading=heading,
            parent_heading=None,
            chunk_text=text,
            context_summary=text[:120],
            exact_terms=[],
            chunk_order=chunk_order,
            content_checksum=f"sha256:{source_name}-chunk-{chunk_order}",
        )
        session.add(chunk)
        await session.flush()
        docs.append(doc)
        chunks.append(chunk)

    # Distinctness invariant (adversarial review): a duplicate key would let a distractor be
    # mis-scored as gold and silently corrupt the metric. Fail loudly if it ever happens.
    keys = [_chunk_key(s, o) for s, o, _h, _t in specs]
    if len(keys) != len(set(keys)):
        raise ValueError(f"seed_eval_distractors produced duplicate chunk keys: {keys}")

    # Embed every chunk in one batched call so the vector arm has something to rank.
    vectors = await embedder.embed_documents([c.chunk_text for c in chunks])
    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk.embedding = vector
    await session.flush()

    if commit:
        await session.commit()
    return {"docs": docs, "chunks": chunks}


@contextlib.asynccontextmanager
async def postgres_distractor_session(
    settings: Settings,
) -> AsyncGenerator[tuple[AsyncSession, Embedder], None]:
    """Yield ``(session, embedder)`` over a REAL Postgres+pgvector distractor catalog (opt-in).

    Same safety rails as :func:`tests.eval.retrieval.postgres_session` — refuses production, a
    non-Postgres URL, a stub embedder, or a non-empty catalog; seeds ``commit=False`` and rolls
    back on EVERY exit path (normal, error, cancel) for zero residue. Builds its OWN embedder
    via :func:`build_embedder` (never the process-wide ``get_embedder`` singleton) so a leaked
    stub can't be reused against an openrouter-stamped index.
    """
    if settings.environment == "production":
        raise PostgresEvalError("the distractor eval must not run against production")
    if not settings.database_url.startswith(("postgresql", "postgres")):
        raise PostgresEvalError(
            f"CITEVYN_DATABASE_URL must be a Postgres URL for the distractor eval; "
            f"got {settings.database_url.split(':', 1)[0]!r}"
        )
    embedder = build_embedder(settings)
    from app.embeddings.stub import StubEmbedder

    if isinstance(embedder, StubEmbedder):
        raise PostgresEvalError(
            "the distractor eval requires a REAL embedder — set CITEVYN_EMBEDDING_PROVIDER="
            "openrouter (+ model and key). Refusing to emit a fabricated ranking under the stub."
        )
    identity = configured_embedder_identity(settings)
    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            existing = await session.scalar(select(func.count()).select_from(Chunk))
            if existing:
                raise PostgresEvalError(
                    f"the target Postgres catalog is not empty ({existing} chunk(s)). Run the "
                    "distractor eval against a migrated-but-empty/dedicated DB, serially with "
                    "the judged pass (both share one CITEVYN_DATABASE_URL)."
                )
            try:
                await seed_eval_distractors(
                    session,
                    embedder=embedder,
                    embedder_identity=identity,
                    index_version=f"eval-distractor-{uuid.uuid4().hex[:8]}",
                    commit=False,
                )
                # Exactly one active index → unambiguous provenance for the vector arm.
                active = await session.scalar(
                    select(func.count())
                    .select_from(IndexVersion)
                    .where(IndexVersion.status == IndexStatus.active)
                )
                if active != 1:
                    raise PostgresEvalError(
                        f"distractor seed left {active} active IndexVersion rows; expected 1"
                    )
                yield session, embedder
            finally:
                await session.rollback()
    finally:
        aclose = getattr(embedder, "aclose", None)
        if callable(aclose):
            await aclose()
        await engine.dispose()


@dataclasses.dataclass(frozen=True)
class DistractorOutcome:
    """Per-case context-retrieval result over the distractor corpus."""

    case_id: str
    gold_chunks: tuple[str, ...]
    retrieved_chunk_keys: tuple[str, ...]
    # Cosine similarity scores aligned 1:1 with ``retrieved_chunk_keys`` (rank order). Kept so
    # the report can surface the gold-vs-distractor MARGIN — a shrinking margin is an early
    # warning that a pinned 1.0 gate is about to flip (adversarial PR review). ``()`` in the
    # hand-built metric-math unit tests, which don't exercise scores.
    retrieved_scores: tuple[float, ...] = ()

    @property
    def recall_at_k(self) -> float:
        """Fraction of gold chunks present anywhere in the top-k retrieved keys."""
        gold = set(self.gold_chunks)
        return len(gold & set(self.retrieved_chunk_keys)) / len(gold) if gold else 1.0

    @property
    def precision_at_gold(self) -> float:
        """Fraction of the top-|gold| retrieved keys that ARE gold (precision@2 for 2 gold).

        Rank-strict: a distractor breaking into the top ``|gold|`` positions lowers it, so it
        catches a ranking regression the forgiving recall@k would miss.
        """
        n = len(self.gold_chunks)
        if not n:
            return 1.0
        gold = set(self.gold_chunks)
        return len([k for k in self.retrieved_chunk_keys[:n] if k in gold]) / n

    @property
    def gold_margin(self) -> float | None:
        """Min retrieved-gold score − max retrieved-distractor score, over the top-k.

        The cosine headroom by which the gold beats its nearest competitor (the lexical hard
        negatives are the intended competitors). Positive = gold ranks above every distractor
        in the top-k; a shrinking value warns the pinned gate is nearing a flip. ``None`` when
        the top-k has no gold or no distractor (margin undefined)."""
        if len(self.retrieved_scores) != len(self.retrieved_chunk_keys):
            return None
        gold = set(self.gold_chunks)
        gold_scores = [
            s
            for k, s in zip(self.retrieved_chunk_keys, self.retrieved_scores, strict=True)
            if k in gold
        ]
        distractor_scores = [
            s
            for k, s in zip(self.retrieved_chunk_keys, self.retrieved_scores, strict=True)
            if k not in gold
        ]
        if not gold_scores or not distractor_scores:
            return None
        return min(gold_scores) - max(distractor_scores)

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "gold_chunks": list(self.gold_chunks),
            "retrieved_chunk_keys": list(self.retrieved_chunk_keys),
            "retrieved_scores": [round(s, 4) for s in self.retrieved_scores],
            "recall_at_k": self.recall_at_k,
            "precision_at_gold": self.precision_at_gold,
            "gold_margin": self.gold_margin,
        }


@dataclasses.dataclass(frozen=True)
class DistractorReport:
    """Aggregate context precision/recall over the distractor cases. Kept SEPARATE from
    :class:`tests.eval.retrieval.RetrievalReport` so the locked report shape / gate is
    untouched (adversarial review guardrail (d))."""

    outcomes: tuple[DistractorOutcome, ...]

    @property
    def mean_recall_at_k(self) -> float:
        return (
            sum(o.recall_at_k for o in self.outcomes) / len(self.outcomes) if self.outcomes else 1.0
        )

    @property
    def min_recall_at_k(self) -> float:
        return min((o.recall_at_k for o in self.outcomes), default=1.0)

    @property
    def mean_precision_at_gold(self) -> float:
        return (
            sum(o.precision_at_gold for o in self.outcomes) / len(self.outcomes)
            if self.outcomes
            else 1.0
        )

    @property
    def min_gold_margin(self) -> float | None:
        """The smallest gold-vs-distractor cosine margin across cases (early-warning signal)."""
        margins = [o.gold_margin for o in self.outcomes if o.gold_margin is not None]
        return min(margins) if margins else None

    def as_dict(self) -> dict[str, object]:
        return {
            "cases": len(self.outcomes),
            "mean_recall_at_k": self.mean_recall_at_k,
            "min_recall_at_k": self.min_recall_at_k,
            "mean_precision_at_gold": self.mean_precision_at_gold,
            "min_gold_margin": self.min_gold_margin,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


async def evaluate_distractors(
    cases: Sequence[EvalCase] | None = None,
    *,
    settings: Settings | None = None,
) -> DistractorReport:
    """Run VECTOR-ONLY scoped retrieval over the distractor corpus and score context
    recall/precision per case. Opt-in / Postgres-only (the vector arm is dead on SQLite).

    Each case's ``question`` is retrieved with :class:`VectorRetriever` scoped to
    ``DISTRACTOR_AREA`` — NOT the hybrid path (whose keyword arm would confound the ranking)
    and NOT ``classify_domain`` routing (which would send a fictional-product query to the
    margin-gated global arm). The ordered hit keys are compared to the case's ``gold_chunks``.
    """
    settings = settings or get_settings()
    cases = list(cases) if cases is not None else load_cases(DISTRACTOR_GOLDEN_PATH)
    outcomes: list[DistractorOutcome] = []
    async with postgres_distractor_session(settings) as (session, embedder):
        key_map = await _chunk_key_map(session)
        # active_index_version=None → no index filter (mirrors the main eval); the scoped
        # product_area restricts candidates to the distractor area. No global confidence gate
        # fires because product_area is not None.
        vector = VectorRetriever(session, active_index_version=None, embedder=embedder)
        for case in cases:
            hits = await vector.retrieve(
                case.question,
                product_area=DISTRACTOR_AREA,
                limit=settings.retrieval_max_candidates,
            )
            top_k = hits[: settings.retrieval_top_k]
            try:
                retrieved_keys = tuple(key_map[str(h.chunk_id)] for h in top_k)
            except KeyError as exc:
                raise RuntimeError(
                    f"distractor eval: retrieved chunk_id {exc.args[0]} on case {case.id!r} is "
                    "absent from the chunk identity map"
                ) from exc
            outcomes.append(
                DistractorOutcome(
                    case_id=case.id,
                    gold_chunks=case.gold_chunks,
                    retrieved_chunk_keys=retrieved_keys,
                    retrieved_scores=tuple(float(h.score) for h in top_k),
                )
            )
    return DistractorReport(outcomes=tuple(outcomes))


def distractor_gate_failures(report: DistractorReport) -> list[str]:
    """Return the reasons the distractor eval should fail the build (empty = pass).

    Guards degenerate inputs (no cases) as well as low numbers — a golden file that parsed to
    zero cases must FAIL, not sail through on the empty-pool 1.0 convention.
    """
    from .thresholds import MIN_DISTRACTOR_PRECISION_AT_GOLD, MIN_DISTRACTOR_RECALL_AT_K

    failures: list[str] = []
    if not report.outcomes:
        return ["distractor golden set is empty (zero cases)"]
    for o in report.outcomes:
        if o.recall_at_k < MIN_DISTRACTOR_RECALL_AT_K:
            failures.append(
                f"{o.case_id}: recall@k {o.recall_at_k:.3f} < {MIN_DISTRACTOR_RECALL_AT_K} "
                f"(gold {list(o.gold_chunks)} not all in top-k {list(o.retrieved_chunk_keys)})"
            )
        if o.precision_at_gold < MIN_DISTRACTOR_PRECISION_AT_GOLD:
            failures.append(
                f"{o.case_id}: precision@|gold| {o.precision_at_gold:.3f} < "
                f"{MIN_DISTRACTOR_PRECISION_AT_GOLD} (a distractor outranks a gold chunk)"
            )
    return failures


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI entry point
    """CLI for the opt-in distractor eval: ``python -m tests.eval.distractors``.

    Postgres-only by nature (the vector arm is dead on SQLite), so there is no hermetic mode.
    Run it SERIALLY against the same dedicated/empty DB the judged pass uses.
    """
    import argparse
    import asyncio
    import json

    parser = argparse.ArgumentParser(
        prog="eval-distractors",
        description="Context precision/recall over the eval distractor corpus (#125).",
    )
    parser.add_argument("--report", type=str, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    report = asyncio.run(evaluate_distractors())
    summary = report.as_dict()
    if args.report:
        with open(args.report, "w") as fh:
            json.dump(summary, fh, indent=2, default=str)
    if not args.quiet:
        mgm = summary["min_gold_margin"]
        margin_str = f"{mgm:.4f}" if mgm is not None else "n/a"
        print("Distractor context retrieval (vector-only, scoped):")
        print(
            f"  cases {summary['cases']}; mean recall@k {summary['mean_recall_at_k']:.3f}; "
            f"min recall@k {summary['min_recall_at_k']:.3f}; "
            f"mean precision@|gold| {summary['mean_precision_at_gold']:.3f}; "
            f"min gold margin {margin_str}"
        )
        for o in summary["outcomes"]:
            m = o["gold_margin"]
            m_str = f"{m:.4f}" if m is not None else "n/a"
            print(
                f"    {o['case_id']}: recall@k {o['recall_at_k']:.3f} "
                f"precision@|gold| {o['precision_at_gold']:.3f} margin {m_str}"
            )
            print(f"      gold {o['gold_chunks']} top-k {o['retrieved_chunk_keys']}")
    failures = distractor_gate_failures(report)
    if failures:
        print("\nDISTRACTOR EVAL GATE FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    if not args.quiet:
        print("\nDistractor eval gate passed.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
