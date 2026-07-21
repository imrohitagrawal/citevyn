"""Worker-side promotion evaluation: the thing that finally *writes* an
:class:`~app.models.evaluation.EvaluationRun` row in the deployed system (#216).

Why this exists
---------------
:func:`app.services.index_versions.promote_version` gates a promotion on the
candidate index's newest COMPLETED :class:`EvaluationRun`. Until this module
existed, **nothing in the deployed application wrote such a row** — the only
constructions in the repository were in tests — so every production promote hit
``reason: no_evaluation_run`` and needed the audited ``?force=true``. The gate
was an audit trail and a speed bump, not a live quality threshold, and ``force``
was on its way to becoming muscle memory.

Why NOT the golden runner
-------------------------
The obvious shortcut is to wire ``backend/tests/golden/runner.py`` in: it already
emits a ``pass_rate``. That shortcut is **wrong, and deliberately not taken.**
The golden suite measures :func:`tests.conftest.seed_catalog` — a hand-written
fixture with one abridged chunk per document — NOT the corpus the worker
actually ingested. An ``EvaluationRun`` produced from it would attest to a corpus
the candidate index does not contain. That is strictly WORSE than today's loud
refusal: it converts a visible block ("no evaluation run, decide explicitly")
into a silent false pass ("measured 1.0 — on some other corpus"). A gate that
certifies nothing while *looking* live is the failure mode #210 exists to
prevent.

So the suite here is scoped to the SHIPPED corpus (``app/worker/sources/*.md``)
and is executed against the CANDIDATE ``index_version`` — not the active one —
through the same retrieval path production serves.

What it measures
----------------
Retrieval hit-rate, the same metric ``tests/eval/retrieval.py`` uses: a case
scores a hit when its ``expected_source`` appears among the top-k source names
the retriever returns. Deliberately LLM-free — no judge, no generation, no paid
completion call. The promotion question is "does this index still put the right
document in front of the answerer?", and an index that fails that cannot be
rescued by a good generator.

Scope of the shipped suite
--------------------------
The cases cover the five *routable* product areas (``claude_api``,
``claude_code``, ``codex``, ``gemini_api``, ``citevyn``). ``concepts.md`` is
covered by no case, on purpose: :func:`app.guardrails.domain.classify_domain`
has no ``concepts`` domain, so a concepts question routes ``unsupported`` and is
reachable ONLY through the global "answer when grounded" vector arm. A case
whose outcome depends on the embedding provider being live and healthy would
make the promotion gate flap on a provider incident rather than on index
quality. Add concepts cases here the day the routing gives them a keyword arm.

Transaction model
-----------------
:func:`evaluate_index` COMMITS twice, on purpose:

1. the ``running`` row, before any case executes, so an interrupted run leaves a
   durable ``running`` row behind. The gate skips ``running`` rows when looking
   for evidence, so a crashed evaluation can never be mistaken for a pass;
2. the terminal ``passed``/``failed`` row with its metrics.

Both are real commits because the point is durability across a process death —
a flushed-but-uncommitted row would vanish with exactly the crash it is meant to
record.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.embeddings.factory import EmbedderIdentity
from app.embeddings.null import NullEmbedder
from app.embeddings.protocol import Embedder
from app.embeddings.stub import StubEmbedder
from app.guardrails.domain import (
    canonicalize_product_name,
    classify_domain,
    classify_domains,
    is_unsupported,
)
from app.models import Chunk, Document, IndexVersion
from app.models.enums import EvaluationStatus
from app.models.evaluation import EvaluationRun
from app.retrieval.hybrid import HybridRetriever
from app.routing.intent import Intent, classify_intent

logger = logging.getLogger(__name__)

#: ``EvaluationRun.suite_name`` for every row this module writes.
SUITE_NAME = "promotion"

#: The shipped suite. Lives under ``app/`` (not ``tests/``) for the same reason
#: the source corpus does: everything under ``backend/app`` is copied wholesale
#: into the worker image by ``infra/docker/Dockerfile.worker`` (``COPY
#: backend/app /app/app``), so package data ships with no build-system
#: configuration at all — the project is not pip-installed in the image, it is
#: copied. ``.dockerignore`` strips ``*.md`` but nothing strips ``*.jsonl``.
DEFAULT_CASES_PATH = Path(__file__).resolve().parent / "eval_cases" / "promotion.jsonl"


class PromotionEvalError(RuntimeError):
    """Raised when the promotion suite cannot produce a trustworthy result.

    Never a silent degrade. Every condition below would otherwise turn into a
    *fabricated* pass rate, and a fabricated pass rate promoted an index.
    """


@dataclasses.dataclass(frozen=True)
class PromotionCase:
    """One retrieval case: ask ``question``, expect ``expected_source`` in top-k."""

    id: str
    question: str
    expected_source: str


def load_cases(path: Path | None = None) -> list[PromotionCase]:
    """Load the promotion suite from a JSONL file.

    Raises :class:`PromotionEvalError` — loudly, never returning a shorter list —
    on a missing file, a malformed line, a missing field, an EMPTY suite, or a
    duplicate ``id``.

    The empty-file and duplicate-id refusals are the two that matter:

    * an empty suite scores ``0/0``, and the classic implementation of that is
      ``1.0`` (see ``tests/golden/scoring.py``), i.e. a flawless run that
      measured nothing;
    * a duplicate id silently double-weights one case and makes the report's
      per-case failure list ambiguous about which one failed.
    """
    source = path if path is not None else DEFAULT_CASES_PATH
    if not source.is_file():
        raise PromotionEvalError(f"promotion suite not found: {source}")

    cases: list[PromotionCase] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parsed: object = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PromotionEvalError(f"{source}:{lineno}: not valid JSON ({exc})") from exc
        if not isinstance(parsed, dict):
            raise PromotionEvalError(f"{source}:{lineno}: expected a JSON object")
        # ``json.loads`` is typed ``Any``; narrow once here so the field reads below are
        # checked rather than silently unknown (pyright runs in strict mode on ``app/``).
        blob: dict[str, object] = {str(k): v for k, v in parsed.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        missing = [k for k in ("id", "question", "expected_source") if not blob.get(k)]
        if missing:
            raise PromotionEvalError(f"{source}:{lineno}: missing/empty field(s) {missing}")
        case = PromotionCase(
            id=str(blob["id"]),
            question=str(blob["question"]),
            expected_source=str(blob["expected_source"]),
        )
        if case.id in seen:
            raise PromotionEvalError(f"{source}:{lineno}: duplicate case id {case.id!r}")
        seen.add(case.id)
        cases.append(case)

    if not cases:
        raise PromotionEvalError(
            f"promotion suite {source} contains no cases. Refusing to run: a zero-case "
            "suite scores a vacuous 1.0 and would promote an index on no evidence."
        )
    return cases


async def _retrieve_sources(
    session: AsyncSession,
    case: PromotionCase,
    *,
    index_version: str,
    settings: Settings,
    embedder: Embedder | None,
    embedder_identity: EmbedderIdentity | None,
) -> tuple[str, ...]:
    """Return the top-k source names for ``case`` against ``index_version``.

    Mirrors ``Orchestrator.ask``'s query pipeline — alias canonicalization,
    domain routing, intent routing, multi-hop fan-out, and the Phase-2
    "answer when grounded" global path — so the number measures the system
    production actually serves. It is scoped to the CANDIDATE
    ``index_version``: evaluating the active index would measure the thing we
    are trying to replace.

    Conversation memory is deliberately absent: every promotion case is
    single-turn by construction, so there is no history to resolve.
    """
    query = canonicalize_product_name(case.question)
    domain = classify_domain(query)
    intent = classify_intent(query, domain)
    if is_unsupported(domain):
        intent = Intent.unsupported

    retriever = HybridRetriever(
        session,
        active_index_version=index_version,
        embedder=embedder,
        embedder_identity=embedder_identity,
        global_confidence=(
            settings.retrieval_global_min_top_score,
            settings.retrieval_global_min_margin,
        ),
    )
    multi_domains = classify_domains(query)
    if len(multi_domains) >= 2:
        result = await retriever.retrieve_multi(
            query,
            product_areas=[d.value for d in multi_domains],
            intent=intent,
            limit=settings.retrieval_max_candidates,
            top_k=settings.retrieval_top_k,
        )
    else:
        answer_globally = settings.answer_when_grounded and intent is Intent.unsupported
        result = await retriever.retrieve(
            query,
            product_area=None if answer_globally else domain.value,
            intent=intent,
            limit=settings.retrieval_max_candidates,
            top_k=settings.retrieval_top_k,
        )
    return tuple(hit.source_name for hit in result.hits)


async def _assert_candidate_is_evaluable(
    session: AsyncSession,
    *,
    index_version: str,
    embedder: Embedder | None,
    embedder_identity: EmbedderIdentity | None,
) -> None:
    """Refuse to measure a candidate whose result could not be trusted.

    All three conditions below produce a NUMBER that looks like a verdict but is not
    one, so each raises :class:`PromotionEvalError` (exit 1, "the invocation is
    unusable") rather than persisting a ``failed`` run (exit 2, which the runbooks
    tell an operator means "the candidate genuinely regressed"). Confusing those two
    is how a typo gets read as a corpus regression.

    1. **The index does not exist.** ``evaluation_runs.index_version`` carries no
       database-level FK (only the reverse direction is constrained), so a typo would
       otherwise persist an orphan run scoring 0.0 and exit 2.

    2. **A real embedder is configured but the candidate has no vectors.** The suite
       is satisfiable from the exact+keyword arms alone, so an index built during an
       embedder outage — or with vectorization half-finished — would score full marks
       and promote, then serve production with a dead vector arm. The gate would have
       certified an index in a state it will never actually be served in.

    3. **The candidate's embedding provenance does not match the configured query
       embedder.** Found in adversarial review: retrieval's Tier-3 check resolves the
       stamp of the *active* index, not of the version being retrieved, so a
       mismatched candidate reports its vector arm ENABLED and is measured with
       meaningless cosine distances. Checking the candidate's own stamp here closes
       that for the promotion path without changing the shared request path — the
       retrieval-side scoping is filed separately.
    """
    row = await session.get(IndexVersion, index_version)
    if row is None:
        raise PromotionEvalError(
            f"index_version {index_version!r} does not exist. Refusing to evaluate: a "
            "run against a nonexistent index scores 0.0 and would be indistinguishable "
            "from a genuine regression."
        )
    # Checks 2 and 3 are about the VECTOR arm, so they apply only when a real embedding
    # provider is in play. Under stub/null (local `make demo`, the bootstrap seeder's
    # ``write_vectors=False`` path, and the whole hermetic test suite) a NULL-embedding
    # index is the CORRECT and expected state, and refusing it would break the very
    # flows that are supposed to work without an API key.
    if embedder is None or isinstance(embedder, StubEmbedder | NullEmbedder):
        return

    if embedder_identity is not None and (
        row.embedding_provider is not None
        and (
            row.embedding_provider,
            row.embedding_model,
            row.embedding_dim,
        )
        != (embedder_identity.provider, embedder_identity.model, embedder_identity.dim)
    ):
        raise PromotionEvalError(
            f"index_version {index_version!r} was built by "
            f"{row.embedding_provider}/{row.embedding_model}@{row.embedding_dim} but the "
            f"configured query embedder is {embedder_identity.provider}/"
            f"{embedder_identity.model}@{embedder_identity.dim}. Refusing to evaluate: the "
            "vector arm would be scored on meaningless cosine distances, and would be "
            "degraded once this index went live. Re-ingest under the configured embedder."
        )

    embedded = await session.scalar(
        select(func.count())
        .select_from(Chunk)
        .join(Document, Chunk.document_id == Document.document_id)
        .where(Document.index_version == index_version, Chunk.embedding.is_not(None))
    )
    if not embedded:
        raise PromotionEvalError(
            f"index_version {index_version!r} has no embedded chunks, but a real embedder "
            f"({embedder_identity.provider if embedder_identity else 'configured'}) is in "
            "use. Refusing to evaluate: the suite is satisfiable from the keyword arm "
            "alone, so this index would score full marks and then serve production with a "
            "dead vector arm. Re-run ingestion with vectors enabled."
        )


async def evaluate_index(
    session: AsyncSession,
    *,
    index_version: str,
    embedder: Embedder | None = None,
    embedder_identity: EmbedderIdentity | None = None,
    threshold: float | None = None,
    cases: Sequence[PromotionCase] | None = None,
    settings: Settings | None = None,
) -> EvaluationRun:
    """Run the promotion suite against ``index_version`` and persist the result.

    Returns the terminal :class:`EvaluationRun` — ``passed`` when the measured
    pass rate is ``>= threshold`` (default
    :attr:`Settings.index_promotion_min_pass_rate`, the SAME setting the gate
    reads, so "evaluated green" and "promotable" cannot drift apart), ``failed``
    otherwise.

    ``embedder``/``embedder_identity`` are optional. Without an embedder the
    vector arm is dead and the suite measures the exact+keyword arms only —
    which is what happens on SQLite regardless. Production passes the configured
    embedder and its identity so the Tier-3 provenance check applies.

    The metrics blob is written in the shape
    :func:`app.services.index_versions._pass_rate_from_metrics` actually
    consumes: ``pass_rate`` **plus** ``cases_total``/``cases_passed``/
    ``cases_failed``. It deliberately does NOT use the ``total``/``passed`` keys
    ``tests/golden/scoring.py`` emits — the gate reads ``total`` only to
    DISQUALIFY a blob, never to compute a rate, so a blob carrying only those
    keys and no ``pass_rate`` reads as unusable.

    Raises :class:`PromotionEvalError` on a zero-case suite, BEFORE any row is
    written. That guard is redundant with :func:`load_cases` and with the gate's
    own zero-``cases_total`` disqualification, and all three are kept: a
    zero-case run scoring a vacuous 1.0 is the single most dangerous thing this
    module could persist, and it must be impossible from every direction.
    """
    settings = settings or get_settings()
    await _assert_candidate_is_evaluable(
        session,
        index_version=index_version,
        embedder=embedder,
        embedder_identity=embedder_identity,
    )
    suite = list(cases) if cases is not None else load_cases()
    if not suite:
        raise PromotionEvalError(
            "refusing to evaluate index_version "
            f"{index_version!r} with zero cases: a zero-case suite scores a vacuous "
            "1.0 and must never be persisted as a passing run."
        )
    limit = settings.index_promotion_min_pass_rate if threshold is None else threshold

    run = EvaluationRun(
        suite_name=SUITE_NAME,
        index_version=index_version,
        started_at=datetime.now(UTC),
        status=EvaluationStatus.running,
        metrics={},
        failure_summary={},
    )
    session.add(run)
    # Commit the ``running`` row before the first case executes: if this process
    # dies mid-suite, the durable evidence of that is a ``running`` row, which
    # ``_latest_completed_run`` skips — an interrupted evaluation can never be
    # read as a pass.
    await session.commit()

    passed = 0
    failures: list[dict[str, object]] = []
    for case in suite:
        sources = await _retrieve_sources(
            session,
            case,
            index_version=index_version,
            settings=settings,
            embedder=embedder,
            embedder_identity=embedder_identity,
        )
        if case.expected_source in sources:
            passed += 1
        else:
            failures.append(
                {
                    "case_id": case.id,
                    "question": case.question,
                    "expected_source": case.expected_source,
                    "retrieved_sources": list(sources),
                }
            )

    total = len(suite)
    pass_rate = passed / total
    run.metrics = {
        "pass_rate": pass_rate,
        "cases_total": total,
        "cases_passed": passed,
        "cases_failed": total - passed,
        "threshold": limit,
        "suite": SUITE_NAME,
    }
    run.failure_summary = {"failures": failures}
    run.status = EvaluationStatus.passed if pass_rate >= limit else EvaluationStatus.failed
    run.completed_at = datetime.now(UTC)
    await session.commit()

    logger.info(
        "promotion_eval_complete",
        extra={
            "index_version": index_version,
            "status": run.status.value,
            "pass_rate": pass_rate,
            "cases_total": total,
            "threshold": limit,
        },
    )
    return run


__all__ = [
    "DEFAULT_CASES_PATH",
    "SUITE_NAME",
    "PromotionCase",
    "PromotionEvalError",
    "evaluate_index",
    "load_cases",
]
