"""Tests for the worker-side promotion evaluation runner (#216).

The centrepiece is :class:`TestPromotionGateIsLive` — the issue's definition of
done: a candidate index that GENUINELY measures below the threshold is refused
by :func:`app.services.index_versions.promote_version` with
``reason="below_threshold"`` and **no ``force``**, while one that measures at or
above it promotes cleanly. Everything else here defends a guard that, if it
broke, would let a fabricated pass rate promote an index silently.

The corpus under test is the REAL shipped one (``app/worker/sources/*.md``),
ingested through the production worker pipeline — not ``conftest.seed_catalog``.
That is the whole point of the module: an evaluation run that attests to a
corpus the candidate index does not contain certifies nothing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core import db as db_module
from app.core.config import get_settings
from app.embeddings.factory import EmbedderIdentity
from app.main import create_app
from app.models import Base
from app.models.enums import EvaluationStatus, IndexStatus
from app.models.evaluation import EvaluationRun
from app.models.index_versions import IndexVersion
from app.retrieval.types import RetrievalResult, VectorDegrade
from app.services import index_versions as index_service
from app.services.index_versions import IndexPromotionBlocked, _pass_rate_from_metrics
from app.worker import cli, promotion_eval
from app.worker.allowlist import MVP_SOURCES, list_source_names
from app.worker.cli import build_runner
from app.worker.promotion_eval import (
    DEFAULT_CASES_PATH,
    SUITE_NAME,
    PromotionCase,
    PromotionEvalError,
    evaluate_index,
    load_cases,
)
from app.worker.runner import ensure_index_version

CANDIDATE = "cand-216"


async def _ingest_real_corpus(session: AsyncSession, index_version: str) -> None:
    """Ingest the SHIPPED corpus into ``index_version`` via the production pipeline.

    ``write_vectors=False`` (the bootstrap seeder's seam) keeps this hermetic and
    free: no embedding provider is called, chunks land with NULL embeddings, and
    the vector arm is dead — which it is on SQLite anyway. The exact + keyword
    arms, which is what the shipped suite is scoped to, are fully live.
    """
    settings = get_settings()
    runner = build_runner(settings, index_version=index_version, write_vectors=False)
    await ensure_index_version(
        session,
        index_version=index_version,
        source_version_hash=runner.source_version_hash,
        embedding_provider=runner.embedding_provider,
        embedding_model=runner.embedding_model,
        embedding_dim=runner.embedding_dim,
    )
    await session.commit()
    for spec in MVP_SOURCES:
        result = await runner.run(session, source=spec)
        assert result.status.value == "completed", f"ingest failed for {spec.name}: {result}"
    await session.commit()


@pytest.fixture
async def candidate_session(session: AsyncSession) -> AsyncSession:
    """A session whose database holds a freshly-ingested CANDIDATE index."""
    await _ingest_real_corpus(session, CANDIDATE)
    return session


# ---------------------------------------------------------------------------
# The definition of done
# ---------------------------------------------------------------------------


class TestPromotionGateIsLive:
    """The gate must act on a MEASUREMENT, with nobody typing ``force``."""

    async def test_below_threshold_candidate_is_refused_without_force(
        self, candidate_session: AsyncSession
    ) -> None:
        # A suite the shipped corpus genuinely cannot satisfy: three of the four
        # cases ask for a source the retriever will not return for that question.
        suite = [
            PromotionCase("ok", "How do I install Claude Code?", "claude_code"),
            PromotionCase("bad1", "How do I install Claude Code?", "gemini_api"),
            PromotionCase("bad2", "What is the Claude API rate limit?", "codex"),
            PromotionCase("bad3", "Which products does CiteVyn cover?", "claude_api"),
        ]
        run = await evaluate_index(candidate_session, index_version=CANDIDATE, cases=suite)
        assert run.status is EvaluationStatus.failed
        assert run.metrics["pass_rate"] == pytest.approx(0.25)

        with pytest.raises(IndexPromotionBlocked) as excinfo:
            await index_service.promote_version(
                candidate_session,
                index_version=CANDIDATE,
                admin_user_id="admin",
                request_id="req-below",
                # NO force — this is the point of the whole issue.
            )
        assert excinfo.value.reason == "below_threshold"
        assert excinfo.value.measured_pass_rate == pytest.approx(0.25)

        await candidate_session.rollback()
        row = await candidate_session.get(IndexVersion, CANDIDATE)
        assert row is not None
        assert row.status is not IndexStatus.active

    async def test_passing_candidate_promotes_without_force(
        self, candidate_session: AsyncSession
    ) -> None:
        run = await evaluate_index(candidate_session, index_version=CANDIDATE)
        assert run.status is EvaluationStatus.passed, run.failure_summary
        assert run.metrics["pass_rate"] >= get_settings().index_promotion_min_pass_rate

        promoted = await index_service.promote_version(
            candidate_session,
            index_version=CANDIDATE,
            admin_user_id="admin",
            request_id="req-pass",
        )
        await candidate_session.commit()
        assert promoted.status is IndexStatus.active

    async def test_shipped_suite_measures_the_shipped_corpus_at_full_marks(
        self, candidate_session: AsyncSession
    ) -> None:
        """The suite is answerable from the real corpus with the vector arm DEAD.

        If this drops, either a source doc changed under the suite or retrieval
        regressed — both are exactly what the promotion gate is for. It is
        asserted at 1.0 rather than at the threshold so a partial regression is
        visible here before it is visible as a blocked deploy.
        """
        run = await evaluate_index(candidate_session, index_version=CANDIDATE)
        assert run.metrics["pass_rate"] == 1.0, run.failure_summary
        assert run.metrics["cases_total"] == len(load_cases())


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class TestZeroCaseSuite:
    async def test_zero_cases_raises_and_persists_no_passing_run(
        self, candidate_session: AsyncSession
    ) -> None:
        """A zero-case run scores a vacuous 1.0. It must never reach the database."""
        with pytest.raises(PromotionEvalError, match="zero cases"):
            await evaluate_index(candidate_session, index_version=CANDIDATE, cases=[])

        await candidate_session.rollback()
        total = await candidate_session.scalar(select(func.count()).select_from(EvaluationRun))
        assert total == 0

    async def test_the_gate_also_refuses_a_zero_case_run_that_somehow_landed(
        self, candidate_session: AsyncSession
    ) -> None:
        """Belt and braces: even a hand-written 0/0 blob is not evidence."""
        candidate_session.add(
            EvaluationRun(
                suite_name=SUITE_NAME,
                index_version=CANDIDATE,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                status=EvaluationStatus.passed,
                metrics={"pass_rate": 1.0, "cases_total": 0, "cases_passed": 0},
                failure_summary={},
            )
        )
        await candidate_session.commit()
        with pytest.raises(IndexPromotionBlocked) as excinfo:
            await index_service.promote_version(
                candidate_session,
                index_version=CANDIDATE,
                admin_user_id="admin",
                request_id="req-zero",
            )
        assert excinfo.value.reason == "unusable_metrics"


class TestIncompleteRunIsNotEvidence:
    async def test_running_row_is_written_before_the_cases_execute(
        self, candidate_session: AsyncSession
    ) -> None:
        """The terminal row is the SAME row that started ``running``.

        One row per run, not two: a second row would let a crashed run's
        ``running`` marker outlive a later successful one and confuse the
        newest-run lookup.
        """
        run = await evaluate_index(candidate_session, index_version=CANDIDATE)
        rows = (await candidate_session.execute(select(EvaluationRun))).scalars().all()
        assert len(rows) == 1
        assert rows[0].run_id == run.run_id
        assert rows[0].started_at is not None
        assert rows[0].completed_at is not None

    async def test_an_interrupted_run_leaves_a_durable_running_row(
        self, candidate_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kill the suite mid-flight; the ``running`` marker must SURVIVE.

        Two properties in one, and both are load-bearing:

        * the row is COMMITTED before the first case runs — asserted by
          rolling the session back (standing in for the process dying) and
          still finding it. A flushed-but-uncommitted row would vanish with
          exactly the crash it exists to record;
        * it is committed as ``running``, not as anything terminal — a crashed
          evaluation that had been written ``passed`` up front would promote an
          index it never finished measuring.
        """

        async def _explode(*args: object, **kwargs: object) -> tuple[str, ...]:
            raise RuntimeError("embedding provider exploded mid-suite")

        monkeypatch.setattr(promotion_eval, "_retrieve_sources", _explode)
        with pytest.raises(RuntimeError, match="exploded mid-suite"):
            await evaluate_index(candidate_session, index_version=CANDIDATE)

        await candidate_session.rollback()
        rows = (await candidate_session.execute(select(EvaluationRun))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status is EvaluationStatus.running
        assert rows[0].completed_at is None

    async def test_a_running_run_is_not_accepted_as_promotion_evidence(
        self, candidate_session: AsyncSession
    ) -> None:
        """An interrupted evaluation leaves a ``running`` row; it must not promote.

        The metrics blob is deliberately a perfect one — the refusal must come
        from the STATUS, not from unreadable metrics.
        """
        candidate_session.add(
            EvaluationRun(
                suite_name=SUITE_NAME,
                index_version=CANDIDATE,
                started_at=datetime.now(UTC),
                completed_at=None,
                status=EvaluationStatus.running,
                metrics={"pass_rate": 1.0, "cases_total": 15, "cases_passed": 15},
                failure_summary={},
            )
        )
        await candidate_session.commit()
        with pytest.raises(IndexPromotionBlocked) as excinfo:
            await index_service.promote_version(
                candidate_session,
                index_version=CANDIDATE,
                admin_user_id="admin",
                request_id="req-running",
            )
        assert excinfo.value.reason == "no_evaluation_run"

    async def test_a_newer_running_run_does_not_mask_an_older_failure(
        self, candidate_session: AsyncSession
    ) -> None:
        """A restarted (still ``running``) evaluation must not resurrect a promote."""
        now = datetime.now(UTC)
        candidate_session.add(
            EvaluationRun(
                suite_name=SUITE_NAME,
                index_version=CANDIDATE,
                started_at=now - timedelta(minutes=5),
                completed_at=now - timedelta(minutes=4),
                status=EvaluationStatus.failed,
                metrics={"pass_rate": 0.2, "cases_total": 15, "cases_passed": 3},
                failure_summary={},
            )
        )
        candidate_session.add(
            EvaluationRun(
                suite_name=SUITE_NAME,
                index_version=CANDIDATE,
                started_at=now,
                completed_at=None,
                status=EvaluationStatus.running,
                metrics={"pass_rate": 1.0, "cases_total": 15, "cases_passed": 15},
                failure_summary={},
            )
        )
        await candidate_session.commit()
        with pytest.raises(IndexPromotionBlocked) as excinfo:
            await index_service.promote_version(
                candidate_session,
                index_version=CANDIDATE,
                admin_user_id="admin",
                request_id="req-mask",
            )
        assert excinfo.value.reason == "below_threshold"


class TestLoadCases:
    def test_the_shipped_suite_loads(self) -> None:
        cases = load_cases()
        assert len(cases) >= 10
        assert len({c.id for c in cases}) == len(cases)

    def test_every_expected_source_is_a_real_shipped_source(self) -> None:
        """A typo'd source name would make a case unpassable — and the gate
        would read that as an index regression rather than a suite bug."""
        known = set(list_source_names())
        unknown = {c.expected_source for c in load_cases()} - known
        assert not unknown, f"suite references non-existent source(s): {sorted(unknown)}"

    def test_the_suite_covers_every_routable_product_area(self) -> None:
        """``concepts`` is excluded by design (see the module docstring): it has
        no domain in ``classify_domain`` and is reachable only via the global
        vector arm, so a case for it would gate promotion on provider health."""
        covered = {c.expected_source for c in load_cases()}
        assert covered == set(list_source_names()) - {"concepts"}

    def test_an_empty_file_is_rejected(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.jsonl"
        empty.write_text("\n  \n# just a comment\n", encoding="utf-8")
        with pytest.raises(PromotionEvalError, match="no cases"):
            load_cases(empty)

    def test_a_duplicate_id_is_rejected(self, tmp_path: Path) -> None:
        dupes = tmp_path / "dupes.jsonl"
        line = json.dumps({"id": "a", "question": "q?", "expected_source": "codex"})
        dupes.write_text(f"{line}\n{line}\n", encoding="utf-8")
        with pytest.raises(PromotionEvalError, match="duplicate case id"):
            load_cases(dupes)

    def test_a_missing_file_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PromotionEvalError, match="not found"):
            load_cases(tmp_path / "nope.jsonl")

    def test_a_malformed_line_is_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.jsonl"
        bad.write_text('{"id": "a", "question":\n', encoding="utf-8")
        with pytest.raises(PromotionEvalError, match="not valid JSON"):
            load_cases(bad)

    def test_a_missing_field_is_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.jsonl"
        bad.write_text(json.dumps({"id": "a", "question": "q?"}) + "\n", encoding="utf-8")
        with pytest.raises(PromotionEvalError, match="missing/empty field"):
            load_cases(bad)

    def test_the_suite_ships_inside_the_worker_image(self) -> None:
        """The cases file must live under ``backend/app`` and survive .dockerignore.

        ``Dockerfile.worker`` copies ``backend/app`` wholesale, so anything under
        it ships — unless ``.dockerignore`` strips it. ``*.md`` IS stripped
        (with an explicit re-include for the source corpus); ``*.jsonl`` is not
        mentioned at all, which is why the suite is JSONL and not Markdown.
        """
        repo_root = Path(__file__).resolve().parents[2]
        backend_app = repo_root / "backend" / "app"
        assert DEFAULT_CASES_PATH.is_file()
        assert DEFAULT_CASES_PATH.is_relative_to(backend_app)
        ignore = (repo_root / ".dockerignore").read_text(encoding="utf-8")
        assert "jsonl" not in ignore


class TestMetricsShape:
    """The blob must be in the shape the GATE reads — not the golden runner's."""

    async def test_metrics_use_the_keys_the_gate_consumes(
        self, candidate_session: AsyncSession
    ) -> None:
        run = await evaluate_index(candidate_session, index_version=CANDIDATE)
        metrics = run.metrics
        assert set(metrics) >= {"pass_rate", "cases_total", "cases_passed", "cases_failed"}
        # The trap: ``tests/golden/scoring.py`` emits ``total``/``passed``, which
        # the gate reads only to DISQUALIFY, never to compute a rate. A blob in
        # that shape with no ``pass_rate`` is unusable evidence.
        assert "total" not in metrics
        assert "passed" not in metrics
        assert metrics["cases_passed"] + metrics["cases_failed"] == metrics["cases_total"]
        assert _pass_rate_from_metrics(metrics) == pytest.approx(metrics["pass_rate"])

    def test_the_golden_runner_shape_would_NOT_be_readable_by_the_gate(self) -> None:
        """Proof the trap is real, so nobody 'simplifies' the keys back."""
        assert _pass_rate_from_metrics({"total": 15, "passed": 15}) is None

    async def test_a_failed_run_records_which_cases_missed(
        self, candidate_session: AsyncSession
    ) -> None:
        suite = [PromotionCase("bad", "How do I install Claude Code?", "gemini_api")]
        run = await evaluate_index(candidate_session, index_version=CANDIDATE, cases=suite)
        failures = run.failure_summary["failures"]
        assert [f["case_id"] for f in failures] == ["bad"]
        assert failures[0]["expected_source"] == "gemini_api"
        assert failures[0]["retrieved_sources"]


class TestCli:
    """``citevyn-worker evaluate`` — the exit code IS the promotion verdict."""

    def test_evaluate_is_a_registered_subcommand(self) -> None:
        args = cli._build_parser().parse_args(["evaluate", "--index-version", "v9"])
        assert args.command == "evaluate"
        assert args.index_version == "v9"

    def test_exit_code_is_zero_on_pass_and_non_zero_on_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exit code is what a deploy script gates on, so it is worth asserting.

        Deliberately a SYNC test over its own temp-file database rather than the shared
        ``session`` fixture. ``cli.main`` calls :func:`asyncio.run`, which cannot run
        inside an already-running loop, and the fixture's session is transaction-scoped
        so a second connection could not see its uncommitted rows anyway.
        """
        db_path = tmp_path / "cli.db"
        url = f"sqlite+aiosqlite:///{db_path}"

        async def _setup() -> None:
            engine = create_async_engine(url)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
                async with factory() as s:
                    await _ingest_real_corpus(s, CANDIDATE)
                    # A second index with NO documents: same code path, measured 0.0.
                    await ensure_index_version(
                        s,
                        index_version="cand-cli-empty",
                        source_version_hash="sha256:empty",
                        embedding_provider=None,
                        embedding_model=None,
                        embedding_dim=None,
                    )
                    await s.commit()
            finally:
                await engine.dispose()

        asyncio.run(_setup())
        # A factory, not a cached instance: ``cli.main`` opens a fresh event loop per
        # invocation, and an aiosqlite engine is bound to the loop that first used it.
        monkeypatch.setattr(
            cli,
            "get_sessionmaker",
            lambda: async_sessionmaker(
                create_async_engine(url), expire_on_commit=False, autoflush=False
            ),
        )

        assert cli.main(["evaluate", "--index-version", CANDIDATE]) == 0
        assert cli.main(["evaluate", "--index-version", "cand-cli-empty"]) != 0


class TestScoping:
    async def test_the_run_measures_the_candidate_and_not_the_active_index(
        self, candidate_session: AsyncSession
    ) -> None:
        """Evaluating an EMPTY candidate must score 0 even with a full active index.

        This is the difference between measuring the index we intend to ship and
        measuring the one we are trying to replace.
        """
        empty = "cand-empty"
        await ensure_index_version(
            candidate_session,
            index_version=empty,
            source_version_hash="sha256:empty",
            embedding_provider=None,
            embedding_model=None,
            embedding_dim=None,
        )
        await candidate_session.commit()
        run = await evaluate_index(candidate_session, index_version=empty)
        assert run.status is EvaluationStatus.failed
        assert run.metrics["pass_rate"] == 0.0
        assert run.index_version == empty


class TestUntrustworthyCandidateIsRefused:
    """Conditions that produce a NUMBER but not a verdict must raise, not score.

    Each would otherwise persist a run whose ``pass_rate`` is real arithmetic over
    meaningless inputs. They raise :class:`PromotionEvalError` (exit 1, "invocation
    unusable") rather than persisting ``failed`` (exit 2, which the runbooks tell an
    operator means "the candidate genuinely regressed"). Confusing the two is how a
    typo gets read as a corpus regression.
    """

    async def test_a_nonexistent_index_version_raises_instead_of_scoring_zero(
        self, candidate_session: AsyncSession
    ) -> None:
        """``evaluation_runs.index_version`` carries no DB-level FK, so a typo would
        otherwise persist an orphan run at 0.0 and exit 2."""
        with pytest.raises(PromotionEvalError, match="does not exist"):
            await evaluate_index(candidate_session, index_version="typo-not-an-index")

        await candidate_session.rollback()
        assert await candidate_session.scalar(select(func.count()).select_from(EvaluationRun)) == 0

    async def test_a_candidate_whose_stamp_mismatches_the_query_embedder_raises(
        self, candidate_session: AsyncSession
    ) -> None:
        """Found in adversarial review of #216.

        Retrieval's Tier-3 check resolves the stamp of the *active* index, not of the
        version being retrieved, so a candidate ingested under embedder X and
        evaluated under configured embedder Y reported its vector arm ENABLED and was
        measured on meaningless cosine distances. The suite still scored well (the
        keyword arm carries it) and the gate passed -- then the arm degraded the
        moment the index went live. Checking the CANDIDATE's own stamp closes this
        for the promotion path without touching the shared request path.
        """
        mismatched = "cand-mismatched"
        await ensure_index_version(
            candidate_session,
            index_version=mismatched,
            source_version_hash="sha256:m",
            embedding_provider="openrouter",
            embedding_model="text-embedding-3-small",
            embedding_dim=1536,
        )
        await candidate_session.commit()

        with pytest.raises(PromotionEvalError, match="was built by"):
            await evaluate_index(
                candidate_session,
                index_version=mismatched,
                embedder=object(),  # never called; the guard fires first
                embedder_identity=EmbedderIdentity(
                    provider="gemini", model="gemini-embedding-001", dim=1536
                ),
            )

    async def test_a_candidate_with_no_vectors_raises_when_an_embedder_is_configured(
        self, candidate_session: AsyncSession
    ) -> None:
        """The suite is satisfiable from the keyword arm ALONE, so an index built
        during an embedder outage would score full marks and promote, then serve
        production with a dead vector arm -- certified in a state it will never
        actually be served in.

        ``candidate_session`` ingests with ``write_vectors=False``, so every chunk has
        a NULL embedding: exactly that scenario.
        """
        with pytest.raises(PromotionEvalError, match="no embedded chunks"):
            await evaluate_index(
                candidate_session,
                index_version=CANDIDATE,
                embedder=object(),  # never called; the guard fires first
                embedder_identity=None,
            )

    async def test_with_no_embedder_a_null_vector_index_still_evaluates(
        self, candidate_session: AsyncSession
    ) -> None:
        """Control for the two guards above.

        Without an embedder the vector arm is dead by definition (and is on SQLite
        regardless), so NULL embeddings are the EXPECTED state and must not be
        refused. Without this, the guards could be 'always raise' and both tests
        above would still pass.
        """
        run = await evaluate_index(candidate_session, index_version=CANDIDATE)
        assert run.status is EvaluationStatus.passed


# ---------------------------------------------------------------------------
# #229 — the linkage the READ surface displays
# ---------------------------------------------------------------------------


async def _row_over_an_independent_connection(
    session: AsyncSession, index_version: str
) -> IndexVersion | None:
    """Read an :class:`IndexVersion` over a SECOND connection to the same database.

    Not ``session.get``, and not even ``rollback()``-then-``get``: the fixture
    session is ``expire_on_commit=False``, so a pointer that was assigned in
    Python and never committed still answers correctly out of its identity map,
    and every assertion below would pass against a fix that writes nothing to
    the database. A separate connection sees committed rows and nothing else,
    which is the property production depends on — the worker process writes the
    pointer, a different API process reads it.
    """
    factory = async_sessionmaker(session.bind, expire_on_commit=False, autoflush=False)
    async with factory() as fresh:
        return await fresh.get(IndexVersion, index_version)


#: A suite the shipped corpus genuinely cannot satisfy (1 of 4 cases hits).
_FAILING_SUITE = [
    PromotionCase("ok", "How do I install Claude Code?", "claude_code"),
    PromotionCase("bad1", "How do I install Claude Code?", "gemini_api"),
    PromotionCase("bad2", "What is the Claude API rate limit?", "codex"),
    PromotionCase("bad3", "Which products does CiteVyn cover?", "claude_api"),
]


class TestEvaluationRunLinkage:
    """``index_versions.evaluation_run_id`` must name the newest terminal run (#229).

    Declared in the model, in migration ``0001`` and in BOTH read surfaces since
    the first commit, and assigned by nothing — so ``/health/index`` reported
    ``null`` for an index that had just measured ``pass_rate=1.0``, and the
    display contradicted the gate. An operator reading that as "no evidence"
    reaches for ``?force=true``, which is the habit #216 exists to remove.
    """

    async def test_a_passing_run_is_linked_and_committed(
        self, candidate_session: AsyncSession
    ) -> None:
        before = await _row_over_an_independent_connection(candidate_session, CANDIDATE)
        assert before is not None
        assert before.evaluation_run_id is None, "precondition: nothing linked yet"
        original_status = before.status
        original_hash = before.source_version_hash
        original_created = before.created_at

        run = await evaluate_index(candidate_session, index_version=CANDIDATE)
        assert run.status is EvaluationStatus.passed, run.failure_summary

        after = await _row_over_an_independent_connection(candidate_session, CANDIDATE)
        assert after is not None
        assert after.evaluation_run_id == run.run_id
        # The linker must UPDATE one column, not re-materialise the row. A
        # ``session.merge`` of a transient ``IndexVersion`` would set the
        # pointer correctly and silently blank everything else.
        assert after.status is original_status
        assert after.source_version_hash == original_hash
        assert after.created_at == original_created

    async def test_a_failed_run_is_linked_too(self, candidate_session: AsyncSession) -> None:
        """Deliberate semantics: the pointer names the newest run, not a certificate.

        Linking only PASSING runs would make "evaluated and failed" look
        identical to "never evaluated" — #229 relocated, not fixed.
        """
        run = await evaluate_index(candidate_session, index_version=CANDIDATE, cases=_FAILING_SUITE)
        # Assert the suite really failed FIRST: if it silently started passing,
        # this test would degenerate into a duplicate of the one above and stop
        # guarding the passed-only mutation entirely.
        assert run.status is EvaluationStatus.failed
        assert run.metrics["pass_rate"] == pytest.approx(0.25)

        row = await _row_over_an_independent_connection(candidate_session, CANDIDATE)
        assert row is not None
        assert row.evaluation_run_id == run.run_id

    async def test_the_pointer_moves_to_the_newer_run(
        self, candidate_session: AsyncSession
    ) -> None:
        """Newest terminal run wins — a set-once pointer would freeze stale evidence."""
        first = await evaluate_index(
            candidate_session, index_version=CANDIDATE, cases=_FAILING_SUITE
        )
        second = await evaluate_index(candidate_session, index_version=CANDIDATE)
        assert first.run_id != second.run_id

        row = await _row_over_an_independent_connection(candidate_session, CANDIDATE)
        assert row is not None
        # Exact equality with the SECOND run, not merely "changed" or "not None":
        # a bug that pointed at some third id would satisfy both weaker checks.
        assert row.evaluation_run_id == second.run_id

    async def test_only_the_evaluated_index_is_stamped(
        self, candidate_session: AsyncSession
    ) -> None:
        """A display crediting index A with index B's passing run is WORSE than null.

        Two rows exist at once — the ingested candidate and an untouched active
        index. Evaluating the candidate must stamp the candidate and nothing else.
        """
        candidate_session.add(
            IndexVersion(
                index_version="other-index",
                status=IndexStatus.active,
                source_version_hash="sha256:other",
                created_at=datetime.now(UTC),
                promoted_at=datetime.now(UTC),
            )
        )
        await candidate_session.commit()

        run = await evaluate_index(candidate_session, index_version=CANDIDATE)

        evaluated = await _row_over_an_independent_connection(candidate_session, CANDIDATE)
        bystander = await _row_over_an_independent_connection(candidate_session, "other-index")
        assert evaluated is not None
        assert bystander is not None
        assert evaluated.evaluation_run_id == run.run_id
        assert bystander.evaluation_run_id is None

    async def test_an_interrupted_run_leaves_no_pointer(
        self, candidate_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pointer must never resolve to a ``running`` row.

        The gate skips ``running`` runs so a crashed evaluation cannot read as
        evidence; a pointer installed before the terminal status would smuggle
        exactly that back in through the display.
        """

        async def _explode(*args: object, **kwargs: object) -> tuple[str, ...]:
            raise RuntimeError("embedding provider exploded mid-suite")

        monkeypatch.setattr(promotion_eval, "_retrieve_sources", _explode)
        with pytest.raises(RuntimeError, match="exploded mid-suite"):
            await evaluate_index(candidate_session, index_version=CANDIDATE)

        row = await _row_over_an_independent_connection(candidate_session, CANDIDATE)
        assert row is not None
        assert row.evaluation_run_id is None
        # Partner to the null assertion above: the thing that is NOT linked has
        # to exist, or this test would pass just as happily with no run at all.
        await candidate_session.rollback()
        runs = (await candidate_session.execute(select(EvaluationRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status is EvaluationStatus.running

    async def test_the_linker_refuses_a_running_run(self, candidate_session: AsyncSession) -> None:
        """The guard itself, at the boundary that owns it."""
        run = EvaluationRun(
            suite_name=SUITE_NAME,
            index_version=CANDIDATE,
            started_at=datetime.now(UTC),
            status=EvaluationStatus.running,
            metrics={},
            failure_summary={},
        )
        candidate_session.add(run)
        await candidate_session.flush()
        with pytest.raises(index_service.EvaluationRunNotLinkable, match="not a terminal"):
            await index_service.link_evaluation_run(candidate_session, run=run)

    async def test_the_linker_refuses_an_unflushed_run_rather_than_nulling_the_pointer(
        self, candidate_session: AsyncSession
    ) -> None:
        """An unflushed run must not silently CLEAR a good pointer (#229 review).

        ``EvaluationRun.run_id`` is a Python-side ``default=uuid.uuid4`` that
        SQLAlchemy applies at FLUSH, so on a transient run it is ``None``.
        Assigning that would not raise — it would overwrite valid evidence with
        ``NULL`` and put the index straight back into the state this issue is
        about. Unreachable from ``evaluate_index`` (which commits the ``running``
        row first); guarded because the function is public and exported.
        """
        real = await evaluate_index(candidate_session, index_version=CANDIDATE)
        await candidate_session.commit()
        # Read the id out BEFORE the rollback below: rollback expires the
        # instance, and re-reading an expired attribute on an async session is
        # a lazy load, which raises ``MissingGreenlet`` rather than refreshing.
        real_run_id = real.run_id
        # Partner to the "unchanged" assertion below: there IS a pointer here to
        # destroy, so a guard that did nothing would be visible.
        linked = await _row_over_an_independent_connection(candidate_session, CANDIDATE)
        assert linked is not None
        assert linked.evaluation_run_id == real_run_id

        unflushed = EvaluationRun(
            suite_name=SUITE_NAME,
            index_version=CANDIDATE,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            status=EvaluationStatus.passed,
            metrics={"pass_rate": 1.0, "cases_total": 4, "cases_passed": 4},
            failure_summary={},
        )
        assert unflushed.run_id is None, "precondition: run_id is assigned at flush"

        with pytest.raises(index_service.EvaluationRunNotLinkable, match="unflushed"):
            await index_service.link_evaluation_run(candidate_session, run=unflushed)

        await candidate_session.rollback()
        after = await _row_over_an_independent_connection(candidate_session, CANDIDATE)
        assert after is not None
        assert after.evaluation_run_id == real_run_id

    async def test_the_linker_takes_its_target_from_the_run(
        self, candidate_session: AsyncSession
    ) -> None:
        """The index is derived from ``run.index_version``, never supplied.

        A caller cannot name the wrong index because a caller cannot name one
        at all — the cross-index leak is impossible by construction rather than
        by discipline.
        """
        candidate_session.add(
            IndexVersion(
                index_version="other-index",
                status=IndexStatus.candidate,
                source_version_hash="sha256:other",
                created_at=datetime.now(UTC),
                promoted_at=None,
            )
        )
        run = EvaluationRun(
            suite_name=SUITE_NAME,
            index_version="other-index",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            status=EvaluationStatus.passed,
            metrics={"pass_rate": 1.0, "cases_total": 4, "cases_passed": 4},
            failure_summary={},
        )
        candidate_session.add(run)
        await candidate_session.flush()

        stamped = await index_service.link_evaluation_run(candidate_session, run=run)
        assert stamped is not None
        assert stamped.index_version == "other-index"
        await candidate_session.commit()

        other = await _row_over_an_independent_connection(candidate_session, "other-index")
        candidate = await _row_over_an_independent_connection(candidate_session, CANDIDATE)
        assert other is not None
        assert candidate is not None
        assert other.evaluation_run_id == run.run_id
        assert candidate.evaluation_run_id is None

    async def test_a_run_naming_a_vanished_index_is_a_no_op(
        self, candidate_session: AsyncSession
    ) -> None:
        """No row to stamp is not an error — the run itself is still the evidence."""
        run = EvaluationRun(
            suite_name=SUITE_NAME,
            index_version="never-existed",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            status=EvaluationStatus.passed,
            metrics={"pass_rate": 1.0, "cases_total": 4, "cases_passed": 4},
            failure_summary={},
        )
        assert await index_service.link_evaluation_run(candidate_session, run=run) is None


class TestHealthIndexReportsTheEvidence:
    """The end-to-end reproduction from #229: evaluate, promote, read it back.

    Kept as a test because of how the bug was found — a command reported
    success and the resulting STATE contradicted it. So the assertion is on the
    state, read back through the endpoint an operator actually opens.
    """

    async def test_health_index_shows_the_run_after_an_evaluation(
        self, candidate_session: AsyncSession
    ) -> None:
        run = await evaluate_index(candidate_session, index_version=CANDIDATE)
        assert run.status is EvaluationStatus.passed, run.failure_summary

        # No ``force``: the gate has its evidence, which is #216's whole point.
        await index_service.promote_version(
            candidate_session,
            index_version=CANDIDATE,
            admin_user_id="admin",
            request_id="req-229",
        )
        await candidate_session.commit()

        app = create_app()

        async def _override():
            yield candidate_session

        app.dependency_overrides[db_module.get_session] = _override
        try:
            with TestClient(app) as client:
                body = client.get("/health/index").json()
        finally:
            app.dependency_overrides.clear()

        assert body["status"] == "ready"
        assert body["active_index"]["index_version"] == CANDIDATE
        # The bug: this read ``None`` while the gate above promoted on evidence.
        assert body["active_index"]["evaluation_run_id"] == str(run.run_id)

    async def test_admin_index_versions_shows_the_run_after_an_evaluation(
        self, candidate_session: AsyncSession
    ) -> None:
        """The second read surface, which duplicates the column read (#229)."""
        run = await evaluate_index(candidate_session, index_version=CANDIDATE)
        await candidate_session.commit()

        app = create_app()

        async def _override():
            yield candidate_session

        app.dependency_overrides[db_module.get_session] = _override
        try:
            with TestClient(app) as client:
                response = client.get(
                    "/v1/admin/index_versions",
                    headers={"X-Admin-API-Key": get_settings().admin_api_key},
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        rows = {r["index_version"]: r for r in response.json()["versions"]}
        assert rows[CANDIDATE]["evaluation_run_id"] == str(run.run_id)


# ---------------------------------------------------------------------------
# Pipeline-mirror parity (#300)
# ---------------------------------------------------------------------------
#
# ``promotion_eval._retrieve_sources`` is a HAND-KEPT COPY of the pre-routing half of
# ``Orchestrator.ask``, and its docstring promises the number "measures the system
# production actually serves". It is the third such copy (the others are ``ask`` itself
# and ``tests/eval/retrieval.py::_retrieve_sources``), and during #300's own development
# two of the three were missed at least once — the drift is silent by construction,
# because a copy that omits a step still runs and still returns a plausible number.
#
# These tests exist because a coverage run proved the point: the #300 line was EXECUTED
# by the existing suite (97% line coverage) while no test ASSERTED it, so deleting it
# left all 42 promotion tests green. Coverage is not assertion.


async def test_promotion_gate_mirrors_the_orchestrator_query_pipeline(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promotion gate must hand the retriever the query production would issue.

    BEHAVIOURAL, not a source grep. An earlier version of this test asserted on
    ``inspect.getsource`` and was replaced after review showed it was theatre: it
    PASSED a behaviour-breaking edit that kept both call names in order while
    discarding the result::

        _ignored = canonicalize_self_reference(case.question)   # still greps clean
        query = canonicalize_product_name(case.question)        # rewrite thrown away

    and it FAILED semantics-preserving refactors (renaming the local, extracting a
    helper). It measured the text, not the behaviour.

    This spies on the retriever and asserts the QUERY IT ACTUALLY RECEIVES, which is
    the thing that decides what the gate measures. Turns red if the #300 rewrite is
    dropped, discarded, or applied after alias canonicalization instead of before.
    """
    captured: list[str] = []

    class _SpyRetriever:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def retrieve(self, query: str, **kwargs: object) -> RetrievalResult:
            captured.append(query)
            return RetrievalResult(hits=[], vector_degrade=VectorDegrade.none)

        async def retrieve_multi(self, query: str, **kwargs: object) -> RetrievalResult:
            captured.append(query)
            return RetrievalResult(hits=[], vector_degrade=VectorDegrade.none)

    monkeypatch.setattr(promotion_eval, "HybridRetriever", _SpyRetriever)

    # (question the user types, query production issues for it)
    pairs = [
        ("who are you?", "What is CiteVyn?"),  # #300 self-reference
        ("hey, what can you do?", "What can CiteVyn do?"),  # ... with an opener
        (
            "Is sitewin free to use right now?",  # #84 alias, unaffected
            "Is CiteVyn free to use right now?",
        ),
        (
            "How do I install Claude Code?",  # ordinary, untouched
            "How do I install Claude Code?",
        ),
    ]
    for question, expected_query in pairs:
        captured.clear()
        await promotion_eval._retrieve_sources(
            session,
            PromotionCase(id="mirror", question=question, expected_source="citevyn"),
            index_version="v-test",
            settings=get_settings(),
            embedder=None,
            embedder_identity=None,
        )
        assert captured == [expected_query], (
            f"promotion gate issued {captured!r} for {question!r}; production issues "
            f"[{expected_query!r}]. The gate's docstring promises it mirrors "
            "Orchestrator.ask — a divergence here means the promotion number attests to "
            "a query production never sends."
        )


def test_promotion_gate_would_measure_a_self_referential_case_correctly() -> None:
    """A self-referential promotion case must be measured as production answers it.

    No shipped promotion case is self-referential today (asserted below, so this test
    says what it means rather than passing vacuously) — which is exactly why the gap
    was invisible. The point is that ADDING one must not silently under-measure: raw,
    "who are you?" retrieves nothing; rewritten, it reaches the About-CiteVyn source.
    """
    from app.guardrails.domain import Domain, canonicalize_self_reference, classify_domain

    cases = load_cases(DEFAULT_CASES_PATH)
    assert cases, "the promotion suite must not be empty"
    # Partner assertion: prove the "no case is affected today" claim rather than
    # assuming it, so this test cannot quietly become a tautology if one is added.
    affected = [c for c in cases if canonicalize_self_reference(c.question) != c.question]
    assert affected == [], (
        "a promotion case is now self-referential; that is fine, but re-check that the "
        f"gate measures it the way production serves it: {[c.id for c in affected]}"
    )

    # The behaviour the mirror buys, stated directly.
    assert classify_domain("who are you?") is Domain.unsupported
    assert classify_domain(canonicalize_self_reference("who are you?")) is Domain.citevyn
