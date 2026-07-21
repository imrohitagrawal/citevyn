"""Tests for the slice 8 step 5 admin services.

These are pure service-layer tests — no FastAPI, no auth header.
The route layer (and its auth dependency) is exercised in
:mod:`tests.test_admin_routes`.

The session is the per-test ``session`` fixture from
:mod:`tests.conftest`, which is an in-memory SQLite engine
seeded with the demo catalog. The seed inserts a single
:class:`IndexVersion` with ``status=active`` so the
promote-flow tests have something to demote.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.documents import Document
from app.models.enums import (
    DocumentStatus,
    IndexStatus,
    JobStage,
    JobStatus,
)
from app.models.evaluation import EvaluationRun, EvaluationStatus
from app.models.index_versions import IndexVersion
from app.models.ingestion_jobs import IngestionJob
from app.services import evaluations as evaluation_service
from app.services import index_versions as index_version_service
from app.services import ingestion_jobs as ingestion_job_service
from tests.conftest import seed_catalog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _make_candidate(
    session: AsyncSession,
    *,
    index_version: str,
    source_hash: str = "sha256:candidate",
) -> IndexVersion:
    """Insert a candidate index version and return it."""
    row = IndexVersion(
        index_version=index_version,
        status=IndexStatus.candidate,
        source_version_hash=source_hash,
        created_at=datetime.now(UTC),
        promoted_at=None,
    )
    session.add(row)
    await session.flush()
    return row


async def _make_run(
    session: AsyncSession,
    *,
    index_version: str,
    status: EvaluationStatus = EvaluationStatus.passed,
    metrics: dict[str, object] | None = None,
    started_at: datetime | None = None,
) -> EvaluationRun:
    """Insert an evaluation run for ``index_version`` and return it.

    The default ``metrics`` blob is the admin-API shape
    (``cases_total``/``cases_passed``) and scores 15/15, i.e. a pass rate
    of 1.0 — enough to clear the #210 promotion gate. Tests that care
    about the gate itself pass their own blob and ``started_at``.
    """
    now = started_at or datetime.now(UTC)
    row = EvaluationRun(
        suite_name="golden_v1",
        index_version=index_version,
        started_at=now,
        completed_at=None if status is EvaluationStatus.running else now,
        status=status,
        metrics=(
            {"cases_total": 15, "cases_passed": 15, "cases_failed": 0}
            if metrics is None
            else metrics
        ),
        failure_summary={},
    )
    session.add(row)
    await session.flush()
    return row


async def _make_job(
    session: AsyncSession,
    *,
    source_name: str = "claude_api",
    status: JobStatus = JobStatus.completed,
    stage: JobStage = JobStage.indexing,
    error_type: str | None = None,
) -> IngestionJob:
    """Insert an ingestion job and return it."""
    now = datetime.now(UTC)
    row = IngestionJob(
        source_name=source_name,
        status=status,
        stage=stage,
        started_at=now,
        completed_at=now,
        error_type=error_type,
        error_message="boom" if error_type else None,
        retryable=error_type is not None,
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# index_versions service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_versions_returns_all_unfiltered(session: AsyncSession) -> None:
    """``list_versions()`` with no filter returns every row."""
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_candidate(session, index_version="v3")

    rows = await index_version_service.list_versions(session)
    versions = {r.index_version for r in rows}
    # seed inserts "v1" as active
    assert versions == {"v1", "v2", "v3"}


@pytest.mark.asyncio
async def test_list_versions_filters_by_status(session: AsyncSession) -> None:
    """``list_versions(status=candidate)`` returns only candidates."""
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_candidate(session, index_version="v3")

    rows = await index_version_service.list_versions(session, status=IndexStatus.candidate)
    versions = {r.index_version for r in rows}
    assert versions == {"v2", "v3"}


@pytest.mark.asyncio
async def test_list_versions_sorted_by_created_at_asc(session: AsyncSession) -> None:
    """Newer rows appear later in the list (stable for pagination)."""
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_candidate(session, index_version="v3")

    rows = await index_version_service.list_versions(session)
    # SQLite datetime resolution is 1s — give each row a unique timestamp
    # by re-stamping them in known order. We don't need exact equality,
    # only the stable order.
    assert [r.index_version for r in rows] == sorted(r.index_version for r in rows)


@pytest.mark.asyncio
async def test_get_version_returns_row(session: AsyncSession) -> None:
    await seed_catalog(session)
    row = await index_version_service.get_version(session, index_version="v1")
    assert row is not None
    assert row.status is IndexStatus.active


@pytest.mark.asyncio
async def test_get_version_missing_returns_none(session: AsyncSession) -> None:
    await seed_catalog(session)
    row = await index_version_service.get_version(session, index_version="missing")
    assert row is None


@pytest.mark.asyncio
async def test_count_documents_for_version_zero_when_none_attached(
    session: AsyncSession,
) -> None:
    """A version with no documents counts as zero, not None."""
    await seed_catalog(session)
    count = await index_version_service.count_documents_for_version(session, index_version="v1")
    # The seed's documents are tagged with index_version="v1", so this
    # asserts the query path is exercised (count > 0 from the seed).
    assert count >= 1


@pytest.mark.asyncio
async def test_count_documents_for_version_isolates_per_version(
    session: AsyncSession,
) -> None:
    """Documents for a different version are not counted."""
    await seed_catalog(session)
    other = await _make_candidate(session, index_version="v2")

    # Insert a document for v2 so the query is non-trivial.
    doc = Document(
        index_version=other.index_version,
        source_name="v2-src",
        product_area="claude_api",
        source_url="https://example.com/v2",
        title="v2 doc",
        identity_checksum="a" * 64,
        last_fetched_at=datetime.now(UTC),
        status=DocumentStatus.active,
    )
    session.add(doc)
    await session.flush()

    v1_count = await index_version_service.count_documents_for_version(session, index_version="v1")
    v2_count = await index_version_service.count_documents_for_version(session, index_version="v2")
    assert v1_count >= 1
    assert v2_count == 1


@pytest.mark.asyncio
async def test_promote_version_demotes_current_active(
    session: AsyncSession,
) -> None:
    """Promoting a candidate moves the current active to previous_good."""
    await seed_catalog(session)
    candidate = await _make_candidate(session, index_version="v2")
    # The promotion gate (#210) needs evidence; ``_make_run`` writes a
    # 15/15 passing run. This test is about the demotion mechanics, so we
    # give it a clean pass rather than forcing past the gate.
    await _make_run(session, index_version="v2")

    updated = await index_version_service.promote_version(
        session,
        index_version=candidate.index_version,
        admin_user_id="admin",
        request_id="req-1",
    )
    await session.commit()
    await session.refresh(updated)

    assert updated.status is IndexStatus.active
    assert updated.promoted_at is not None

    # Current active is now previous_good.
    v1 = await index_version_service.get_version(session, index_version="v1")
    assert v1 is not None
    assert v1.status is IndexStatus.previous_good


@pytest.mark.asyncio
async def test_promote_version_recovers_from_dual_active_state(
    session: AsyncSession,
) -> None:
    """A drifted database with >1 active row is repaired, not crashed on.

    Regression: ``promote_version`` used ``scalar_one_or_none()`` to find the
    row to demote, so two ``active`` rows raised ``MultipleResultsFound`` and
    surfaced as an opaque HTTP 500. Because promotion is the only API that can
    repair index state, that made the database unrecoverable through the API.
    Found on a live stack, where seeding plus repeated local ingests had left
    two rows marked ``active``.
    """
    await seed_catalog(session)

    # Drift the database: a SECOND row is active alongside the seeded ``v1``.
    stale_active = await _make_candidate(session, index_version="v-stale")
    stale_active.status = IndexStatus.active
    await session.flush()

    candidate = await _make_candidate(session, index_version="v2")
    await _make_run(session, index_version="v2")  # satisfy the #210 gate

    updated = await index_version_service.promote_version(
        session,
        index_version=candidate.index_version,
        admin_user_id="admin",
        request_id="req-dual",
    )
    await session.commit()
    await session.refresh(updated)

    # The promotion succeeds and leaves exactly one active row.
    assert updated.status is IndexStatus.active
    for demoted in ("v1", "v-stale"):
        row = await index_version_service.get_version(session, index_version=demoted)
        assert row is not None
        assert row.status is IndexStatus.previous_good, f"{demoted} should be demoted"


@pytest.mark.asyncio
async def test_promote_version_writes_audit_event(session: AsyncSession) -> None:
    """A successful promote appends one ``promote_index`` audit row."""
    from app.models.audit_events import AuditEvent
    from app.models.enums import AuditAction

    await seed_catalog(session)
    candidate = await _make_candidate(session, index_version="v2")
    run = await _make_run(session, index_version="v2")  # satisfy the #210 gate

    await index_version_service.promote_version(
        session,
        index_version=candidate.index_version,
        admin_user_id="admin-actor",
        request_id="req-audit",
    )
    await session.commit()

    rows = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == AuditAction.promote_index)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    audit = rows[0]
    assert audit.resource_id == candidate.index_version
    assert audit.user_id == "admin-actor"
    assert audit.metadata_.get("request_id") == "req-audit"
    # A clean promote is evidenced as loudly as a forced one (#210).
    assert audit.metadata_.get("force") is False
    assert audit.metadata_.get("measured_pass_rate") == 1.0
    assert audit.metadata_.get("threshold") == 0.95
    assert audit.metadata_.get("evaluation_run_id") == str(run.run_id)


@pytest.mark.asyncio
async def test_promote_version_is_idempotent(session: AsyncSession) -> None:
    """Promoting the already-active row is a no-op (no audit row)."""
    from app.models.audit_events import AuditEvent
    from app.models.enums import AuditAction

    await seed_catalog(session)
    # "v1" is the seeded active row.
    updated = await index_version_service.promote_version(
        session,
        index_version="v1",
        admin_user_id="admin",
        request_id="req-noop",
    )
    await session.commit()
    await session.refresh(updated)
    assert updated.status is IndexStatus.active

    # No new audit row.
    rows = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == AuditAction.promote_index)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_promote_version_raises_when_target_missing(
    session: AsyncSession,
) -> None:
    """A missing target raises :class:`IndexVersionNotFound`."""
    await seed_catalog(session)
    with pytest.raises(index_version_service.IndexVersionNotFound) as exc_info:
        await index_version_service.promote_version(
            session,
            index_version="does-not-exist",
            admin_user_id="admin",
            request_id="req-404",
        )
    assert exc_info.value.index_version == "does-not-exist"


# ---------------------------------------------------------------------------
# promote_version — evaluation gate (#210)
#
# The threshold under test is the shipped default, 0.95. These tests
# deliberately do not override it (except the one that proves it IS the
# setting being read), so they also assert that the default has not
# drifted out from under the deploy runbook.
# ---------------------------------------------------------------------------


async def _promote_candidate(
    session: AsyncSession,
    *,
    index_version: str = "v2",
    force: bool = False,
) -> IndexVersion:
    """Promote ``index_version`` with the service's default arguments."""
    return await index_version_service.promote_version(
        session,
        index_version=index_version,
        admin_user_id="admin",
        request_id="req-gate",
        force=force,
    )


@pytest.mark.asyncio
async def test_promote_version_allows_pass_rate_above_threshold(
    session: AsyncSession,
) -> None:
    """A run comfortably above the gate promotes."""
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_run(session, index_version="v2", metrics={"pass_rate": 0.99})

    updated = await _promote_candidate(session)
    assert updated.status is IndexStatus.active


@pytest.mark.asyncio
async def test_promote_version_is_gated_when_force_is_not_passed_at_all(
    session: AsyncSession,
) -> None:
    """The ``force`` parameter DEFAULTS to off.

    Called deliberately without a ``force`` kwarg, rather than through
    ``_promote_candidate``, because the thing under test is the default in
    the signature. The route always passes ``force=`` explicitly, so nothing
    on the HTTP surface pins it; without this test the default was held in
    place only by an assertion inside an audit-content test, and flipping it
    to ``True`` would un-gate every direct service caller (worker, CLI, any
    future promote path) with a fully green suite.
    """
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")

    with pytest.raises(index_version_service.IndexPromotionBlocked):
        await index_version_service.promote_version(
            session,
            index_version="v2",
            admin_user_id="admin",
            request_id="req-gate",
        )


@pytest.mark.asyncio
async def test_promote_version_allows_pass_rate_exactly_at_threshold(
    session: AsyncSession,
) -> None:
    """A rate EQUAL to the threshold promotes.

    The gate is ``rate >= threshold``. Written as ``>`` it would reject a
    candidate that measured exactly the configured minimum — the classic
    off-by-one that makes a 0.95 gate refuse a 0.95 index, and one that no
    "clearly passing" or "clearly failing" fixture can catch.
    """
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_run(session, index_version="v2", metrics={"pass_rate": 0.95})

    updated = await _promote_candidate(session)
    assert updated.status is IndexStatus.active


@pytest.mark.asyncio
async def test_promote_version_blocks_pass_rate_just_below_threshold(
    session: AsyncSession,
) -> None:
    """A rate a hair under the threshold refuses, and says both numbers."""
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    run = await _make_run(session, index_version="v2", metrics={"pass_rate": 0.94})

    with pytest.raises(index_version_service.IndexPromotionBlocked) as exc_info:
        await _promote_candidate(session)

    exc = exc_info.value
    assert exc.reason == "below_threshold"
    assert exc.measured_pass_rate == 0.94
    assert exc.threshold == 0.95
    assert exc.run_id == run.run_id
    # The operator has to decide whether to re-run the eval or to force;
    # "how far short" is the entire input to that decision.
    assert "0.94" in str(exc)
    assert "0.95" in str(exc)

    # Nothing moved: the seeded active index is still active.
    v1 = await index_version_service.get_version(session, index_version="v1")
    assert v1 is not None
    assert v1.status is IndexStatus.active


@pytest.mark.asyncio
async def test_promote_version_blocks_when_no_evaluation_run_exists(
    session: AsyncSession,
) -> None:
    """No run at all refuses. "Unevaluated" must not silently pass.

    This is the state production is always in — nothing in the deployed
    app writes ``EvaluationRun`` rows — so a gate that failed open here
    would be no gate at all.
    """
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")

    with pytest.raises(index_version_service.IndexPromotionBlocked) as exc_info:
        await _promote_candidate(session)

    exc = exc_info.value
    assert exc.reason == "no_evaluation_run"
    assert exc.measured_pass_rate is None
    assert exc.run_id is None
    assert "0.95" in str(exc)


@pytest.mark.asyncio
async def test_promote_version_ignores_a_still_running_evaluation(
    session: AsyncSession,
) -> None:
    """A ``running`` run is not evidence, so it cannot satisfy the gate."""
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_run(
        session,
        index_version="v2",
        status=EvaluationStatus.running,
        metrics={"pass_rate": 1.0},
    )

    with pytest.raises(index_version_service.IndexPromotionBlocked) as exc_info:
        await _promote_candidate(session)

    # Not "unusable metrics" — the run is skipped entirely, so from the
    # gate's point of view there is no completed run at all.
    assert exc_info.value.reason == "no_evaluation_run"


@pytest.mark.asyncio
async def test_promote_version_skips_running_run_to_an_older_completed_one(
    session: AsyncSession,
) -> None:
    """Newest COMPLETED wins — not newest-then-check.

    A build that is mid-evaluation must not mask the last finished
    verdict, in either direction.
    """
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    base = datetime(2026, 7, 1, tzinfo=UTC)
    await _make_run(
        session,
        index_version="v2",
        status=EvaluationStatus.passed,
        metrics={"pass_rate": 1.0},
        started_at=base,
    )
    await _make_run(
        session,
        index_version="v2",
        status=EvaluationStatus.running,
        metrics={"pass_rate": 0.0},
        started_at=base.replace(day=2),
    )

    updated = await _promote_candidate(session)
    assert updated.status is IndexStatus.active


@pytest.mark.asyncio
async def test_promote_version_uses_the_newest_completed_run(
    session: AsyncSession,
) -> None:
    """A newer FAILED run supersedes an older PASSED one."""
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    base = datetime(2026, 7, 1, tzinfo=UTC)
    await _make_run(
        session,
        index_version="v2",
        status=EvaluationStatus.passed,
        metrics={"pass_rate": 1.0},
        started_at=base,
    )
    newer = await _make_run(
        session,
        index_version="v2",
        status=EvaluationStatus.failed,
        metrics={"pass_rate": 0.4},
        started_at=base.replace(day=2),
    )

    with pytest.raises(index_version_service.IndexPromotionBlocked) as exc_info:
        await _promote_candidate(session)

    assert exc_info.value.run_id == newer.run_id
    assert exc_info.value.measured_pass_rate == 0.4


@pytest.mark.asyncio
async def test_promote_version_ignores_runs_for_a_different_index(
    session: AsyncSession,
) -> None:
    """Evidence for ``v3`` must not let ``v2`` through."""
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_candidate(session, index_version="v3", source_hash="sha256:v3")
    await _make_run(session, index_version="v3", metrics={"pass_rate": 1.0})

    with pytest.raises(index_version_service.IndexPromotionBlocked) as exc_info:
        await _promote_candidate(session)

    assert exc_info.value.reason == "no_evaluation_run"


@pytest.mark.asyncio
async def test_promote_version_derives_pass_rate_from_case_counts(
    session: AsyncSession,
) -> None:
    """No ``pass_rate`` key → derive it from ``cases_passed/cases_total``.

    The two producers of a metrics blob disagree: the golden scorer emits
    ``pass_rate``, while the admin API's summariser (and every fixture)
    writes the ``cases_*`` counts. Both must resolve.
    """
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_run(
        session,
        index_version="v2",
        metrics={"cases_total": 50, "cases_passed": 49, "cases_failed": 1},
    )

    updated = await _promote_candidate(session)  # 0.98 >= 0.95
    assert updated.status is IndexStatus.active


@pytest.mark.asyncio
async def test_promote_version_blocks_on_derived_rate_below_threshold(
    session: AsyncSession,
) -> None:
    """The derived rate is gated the same as an explicit one."""
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_run(
        session,
        index_version="v2",
        metrics={"cases_total": 50, "cases_passed": 40, "cases_failed": 10},
    )

    with pytest.raises(index_version_service.IndexPromotionBlocked) as exc_info:
        await _promote_candidate(session)

    assert exc_info.value.measured_pass_rate == pytest.approx(0.8)


@pytest.mark.parametrize(
    "metrics",
    [
        pytest.param({}, id="empty"),
        pytest.param({"pass_rate": "0.99"}, id="string-rate"),
        pytest.param({"pass_rate": None}, id="null-rate"),
        pytest.param({"pass_rate": True}, id="bool-rate"),
        pytest.param({"pass_rate": 1.5}, id="rate-above-one"),
        pytest.param({"pass_rate": -0.1}, id="negative-rate"),
        pytest.param({"cases_total": 0, "cases_passed": 0}, id="zero-cases"),
        pytest.param({"cases_passed": 10}, id="counts-missing-total"),
        pytest.param({"total": 10, "passed": 10}, id="scorer-count-keys-only"),
        # The zero-case fail-open. ``scoring.py`` scores an empty suite
        # ``1.0``, so these two blobs are what a golden run that collected
        # NOTHING looks like — a flawless rate over no evidence at all.
        pytest.param(
            {"pass_rate": 1.0, "total": 0, "passed": 0},
            id="zero-case-scorer-blob",
        ),
        pytest.param(
            {"pass_rate": 1.0, "cases_total": 0, "cases_passed": 0},
            id="zero-case-admin-blob",
        ),
        # A corrupt headline metric discredits the counts beside it rather
        # than quietly falling through to them.
        pytest.param(
            {"pass_rate": 42.0, "cases_total": 10, "cases_passed": 10},
            id="corrupt-rate-with-good-counts",
        ),
        pytest.param(
            {"pass_rate": float("nan"), "cases_total": 10, "cases_passed": 10},
            id="nan-rate-with-good-counts",
        ),
    ],
)
@pytest.mark.asyncio
async def test_promote_version_blocks_on_unusable_metrics(
    session: AsyncSession,
    metrics: dict[str, object],
) -> None:
    """A run we cannot score is not a pass.

    ``scorer-count-keys-only`` is deliberate: ``backend/tests/golden/
    scoring.py`` emits ``total``/``passed`` alongside ``pass_rate``, and we
    must NOT read those as the ``cases_*`` counts — blending the two
    conventions would score one producer's blob with the other's
    semantics.
    """
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_run(session, index_version="v2", metrics=metrics)

    with pytest.raises(index_version_service.IndexPromotionBlocked) as exc_info:
        await _promote_candidate(session)

    assert exc_info.value.reason == "unusable_metrics"
    assert exc_info.value.measured_pass_rate is None


@pytest.mark.asyncio
async def test_promote_version_reads_the_threshold_from_settings(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bar is the setting, not a constant baked into the service."""
    from app.core.config import get_settings

    monkeypatch.setenv("CITEVYN_INDEX_PROMOTION_MIN_PASS_RATE", "0.5")
    get_settings.cache_clear()

    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_run(session, index_version="v2", metrics={"pass_rate": 0.6})

    # 0.6 would be refused at the 0.95 default; at 0.5 it promotes.
    updated = await _promote_candidate(session)
    assert updated.status is IndexStatus.active


@pytest.mark.asyncio
async def test_promote_version_force_promotes_without_any_run(
    session: AsyncSession,
) -> None:
    """``force=True`` is the documented way past an unevaluated index."""
    from app.models.audit_events import AuditEvent
    from app.models.enums import AuditAction

    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")

    updated = await _promote_candidate(session, force=True)
    await session.commit()
    assert updated.status is IndexStatus.active

    rows = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == AuditAction.promote_index)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    # The override is evidence, not a hole.
    assert rows[0].metadata_.get("force") is True
    assert rows[0].metadata_.get("threshold") == 0.95
    assert rows[0].metadata_.get("measured_pass_rate") is None
    assert rows[0].metadata_.get("evaluation_run_id") is None


@pytest.mark.asyncio
async def test_promote_version_force_records_the_rate_it_overrode(
    session: AsyncSession,
) -> None:
    """Forcing past a BAD run still records how bad it was."""
    from app.models.audit_events import AuditEvent
    from app.models.enums import AuditAction

    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    run = await _make_run(
        session,
        index_version="v2",
        status=EvaluationStatus.failed,
        metrics={"pass_rate": 0.3},
    )

    updated = await _promote_candidate(session, force=True)
    await session.commit()
    assert updated.status is IndexStatus.active

    audit = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == AuditAction.promote_index)
            )
        )
        .scalars()
        .one()
    )
    assert audit.metadata_.get("force") is True
    assert audit.metadata_.get("measured_pass_rate") == 0.3
    assert audit.metadata_.get("evaluation_run_id") == str(run.run_id)


@pytest.mark.asyncio
async def test_promote_version_idempotent_path_is_never_gated(
    session: AsyncSession,
) -> None:
    """Re-promoting the ACTIVE index is a no-op, gate or no gate.

    The seeded ``v1`` is active and has no evaluation run. Promotion is
    the only API that can repair a database drifted into a dual-active
    state, so a gate placed above the idempotent early return would make
    that repair impossible in exactly the environment (production) that
    has no runs.
    """
    await seed_catalog(session)

    updated = await index_version_service.promote_version(
        session,
        index_version="v1",
        admin_user_id="admin",
        request_id="req-noop-gate",
    )
    assert updated.status is IndexStatus.active


@pytest.mark.asyncio
async def test_measured_pass_rate_matches_what_the_gate_sees(
    session: AsyncSession,
) -> None:
    """The route reports the same number the gate acted on."""
    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    await _make_run(session, index_version="v2", metrics={"pass_rate": 0.42})

    assert await index_version_service.measured_pass_rate(session, index_version="v2") == 0.42
    assert await index_version_service.measured_pass_rate(session, index_version="v1") is None


# ---------------------------------------------------------------------------
# orchestrator._retrieve_active_index — dual-active guard (#58, Issue 2 / F1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_active_index_returns_empty_on_dual_active(
    session: AsyncSession,
) -> None:
    """Two ``active`` rows must NOT let the orchestrator silently pick one.

    Regression: ``_retrieve_active_index`` used to ``LIMIT 1`` and return
    the winner of an arbitrary deterministic sort. With a real database
    carrying two ``active`` rows (the demo ``v1`` seed + a worker-ingested
    ``v-local``), this hides a richer index and degrades every retrieval
    arm. The guard now returns ``("", "")`` and logs a WARNING so
    operators see the inconsistency and converge it via
    ``promote_version``.
    """
    from app.answer.orchestrator import _retrieve_active_index

    await seed_catalog(session)
    # ``seed_catalog`` already has ``v1`` active; force a second active
    # row to reproduce the dual-active index bug.
    await _make_candidate(session, index_version="v2")
    second = await index_version_service.get_version(session, index_version="v2")
    assert second is not None
    second.status = IndexStatus.active
    await session.flush()

    version, source_hash = await _retrieve_active_index(session)
    # Empty sentinel → caller converts to active_index_version=None and
    # retrieval falls back to a status-only filter rather than 500-ing.
    assert (version, source_hash) == ("", "")


@pytest.mark.asyncio
async def test_orchestrator_active_index_emits_warning_on_dual_active(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """The dual-active guard must log the ``orchestrator_multiple_active_indexes``
    WARNING so this regression is observable in production logs."""
    from app.answer.orchestrator import _retrieve_active_index

    await seed_catalog(session)
    await _make_candidate(session, index_version="v2")
    second = await index_version_service.get_version(session, index_version="v2")
    second.status = IndexStatus.active
    await session.flush()

    with caplog.at_level("WARNING", logger="citevyn.answer"):
        await _retrieve_active_index(session)

    assert any(
        "orchestrator_multiple_active_indexes" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_orchestrator_active_index_returns_row_on_single_active(
    session: AsyncSession,
) -> None:
    """Single-active is the happy path and must return the (version, hash)."""
    from app.answer.orchestrator import _retrieve_active_index

    await seed_catalog(session)
    version, source_hash = await _retrieve_active_index(session)
    assert version == "v1"
    assert source_hash == "sha256:v1"


# ---------------------------------------------------------------------------
# evaluations service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_returns_recent_first(session: AsyncSession) -> None:
    """``list_runs()`` sorts by ``started_at`` desc."""
    await seed_catalog(session)
    await _make_run(session, index_version="v1")
    await _make_run(session, index_version="v1")

    rows = await evaluation_service.list_runs(session)
    assert len(rows) == 2
    # Both started within the same second, so the order is whatever
    # SQLite hands back — but the request should not error and should
    # return exactly the rows we inserted.
    assert {str(r.run_id) for r in rows} == {str(r.run_id) for r in rows}


@pytest.mark.asyncio
async def test_list_runs_filters_by_index_version(session: AsyncSession) -> None:
    """``list_runs(index_version="v2")`` returns only runs for v2."""
    await seed_catalog(session)
    await _make_run(session, index_version="v1")
    await _make_run(session, index_version="v2")

    rows = await evaluation_service.list_runs(session, index_version="v2")
    assert all(r.index_version == "v2" for r in rows)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_list_runs_clamps_limit(session: AsyncSession) -> None:
    """``limit=0`` is clamped to 1; ``limit=10000`` to 200."""
    await seed_catalog(session)
    for i in range(3):
        await _make_run(session, index_version=f"v{i}")
    rows = await evaluation_service.list_runs(session, limit=10000)
    assert len(rows) == 3  # only 3 exist; clamp is a ceiling


@pytest.mark.asyncio
async def test_get_run_returns_row(session: AsyncSession) -> None:
    await seed_catalog(session)
    run = await _make_run(session, index_version="v1")
    fetched = await evaluation_service.get_run(session, run_id=run.run_id)
    assert fetched is not None
    assert fetched.run_id == run.run_id


@pytest.mark.asyncio
async def test_get_run_missing_returns_none(session: AsyncSession) -> None:
    await seed_catalog(session)
    fetched = await evaluation_service.get_run(session, run_id=uuid.uuid4())
    assert fetched is None


@pytest.mark.asyncio
async def test_summarize_run_flattens_metrics(session: AsyncSession) -> None:
    """``summarize_run`` flattens ``metrics.cases_total`` etc."""
    await seed_catalog(session)
    run = await _make_run(session, index_version="v1")
    summary = evaluation_service.summarize_run(run)
    assert summary["run_id"] == str(run.run_id)
    assert summary["index_version"] == "v1"
    assert summary["status"] == "passed"
    assert summary["summary"]["cases_total"] == 15
    assert summary["summary"]["cases_passed"] == 15


# ---------------------------------------------------------------------------
# ingestion_jobs service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_jobs_returns_all_unfiltered(session: AsyncSession) -> None:
    """``list_jobs()`` with no filter returns every job."""
    await _make_job(session, source_name="claude_api")
    await _make_job(session, source_name="codex")
    rows = await ingestion_job_service.list_jobs(session)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_list_jobs_filters_by_status(session: AsyncSession) -> None:
    await _make_job(session, source_name="claude_api", status=JobStatus.completed)
    await _make_job(session, source_name="claude_api", status=JobStatus.failed)
    rows = await ingestion_job_service.list_jobs(session, status=JobStatus.failed)
    assert len(rows) == 1
    assert rows[0].status is JobStatus.failed


@pytest.mark.asyncio
async def test_list_jobs_filters_by_source_name(session: AsyncSession) -> None:
    await _make_job(session, source_name="claude_api")
    await _make_job(session, source_name="codex")
    rows = await ingestion_job_service.list_jobs(session, source_name="codex")
    assert len(rows) == 1
    assert rows[0].source_name == "codex"


@pytest.mark.asyncio
async def test_list_jobs_combines_status_and_source_filters(
    session: AsyncSession,
) -> None:
    await _make_job(session, source_name="claude_api", status=JobStatus.completed)
    await _make_job(session, source_name="codex", status=JobStatus.failed)
    rows = await ingestion_job_service.list_jobs(
        session, status=JobStatus.failed, source_name="codex"
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_get_job_returns_row(session: AsyncSession) -> None:
    job = await _make_job(session)
    fetched = await ingestion_job_service.get_job(session, job_id=job.job_id)
    assert fetched is not None
    assert fetched.job_id == job.job_id


@pytest.mark.asyncio
async def test_get_job_missing_returns_none(session: AsyncSession) -> None:
    fetched = await ingestion_job_service.get_job(session, job_id=uuid.uuid4())
    assert fetched is None
