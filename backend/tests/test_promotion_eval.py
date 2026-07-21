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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import Base
from app.models.enums import EvaluationStatus, IndexStatus
from app.models.evaluation import EvaluationRun
from app.models.index_versions import IndexVersion
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
