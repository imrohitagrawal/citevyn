"""Service layer for :class:`app.models.index_versions.IndexVersion`.

Read paths (``list_versions``, ``get_version``) and the only write
path the HTTP surface exposes today: :func:`promote_version`.

Design notes
------------
* The route layer is the only place this is called from; the
  worker (Step 6) writes :class:`IndexVersion` rows directly via
  the ORM during a build.
* :func:`promote_version` is transactional. The release plan
  requires "last known good" is preserved, so a promotion moves
  the current ``active`` row to ``previous_good`` and the
  chosen candidate to ``active`` in the same transaction. A
  second caller racing with the first will lose (one wins, the
  other sees a clean state) — the function does not retry.
* Promote is idempotent on the same target. If ``index_version``
  is already ``active``, the call returns without writing and
  without raising.
* Promote is a privileged write. Every call writes one
  :class:`AuditEvent` row (``promote_index``) inside the same
  transaction so the audit log and the data move together.
* Promote is GATED on evaluation quality (#210). The candidate's
  newest *completed* :class:`EvaluationRun` must have measured a
  pass rate of at least
  :attr:`Settings.index_promotion_min_pass_rate`, or the call
  raises :class:`IndexPromotionBlocked`. The gate lives here, in
  the service, and not in the route, so that every caller of
  :func:`promote_version` is gated — a future worker or CLI
  promote path cannot accidentally route around it.
* The gate refuses when there is no usable evidence at all, not
  just when the evidence is bad: "unevaluated" is not "passing".
  Evidence is produced by ``citevyn-worker evaluate
  --index-version <candidate>``
  (:mod:`app.worker.promotion_eval`), which measures the
  CANDIDATE index against the shipped corpus and writes the
  :class:`EvaluationRun` row this gate reads (#216). Run it after
  ingesting and before promoting, and the gate decides on a
  measurement.
* ``force=True`` remains for the cases that genuinely have no
  evidence — a bootstrap, or an emergency rollback that cannot
  wait for a suite — and records the override, including the
  measured rate and the threshold, in the audit row, so a bypass
  is evidence rather than a hole. It is no longer the ordinary
  path; before #216 nothing wrote these rows at all, so every
  promote needed it, which is exactly how ``force`` becomes
  muscle memory.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal, assert_never

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.enums import AuditAction, EvaluationStatus, IndexStatus
from app.models.evaluation import EvaluationRun
from app.models.index_versions import IndexVersion
from app.services import audit as audit_service

# The ways the gate can refuse. A closed literal rather than a bare ``str``
# so that adding a reason without teaching the message builder about it is a
# type error instead of a silently wrong operator-facing message.
PromotionBlockedReason = Literal[
    "no_evaluation_run",
    "unusable_metrics",
    "below_threshold",
]

# "Completed" is not a member of :class:`EvaluationStatus` — the enum is
# ``running | passed | failed``. A ``running`` run is not evidence of
# anything yet, so the gate skips past it to the newest run that actually
# finished rather than treating it as a failure.
_COMPLETED_EVALUATION_STATUSES = (EvaluationStatus.passed, EvaluationStatus.failed)


async def list_versions(
    session: AsyncSession,
    *,
    status: IndexStatus | None = None,
) -> list[IndexVersion]:
    """Return all index versions, optionally filtered by ``status``.

    Sorted by ``created_at`` ascending so the seed row appears
    before any later candidates in the admin UI. ``None`` when no
    rows match — the route layer turns that into an empty list.
    """
    stmt = select(IndexVersion)
    if status is not None:
        stmt = stmt.where(IndexVersion.status == status)
    stmt = stmt.order_by(IndexVersion.created_at.asc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_version(
    session: AsyncSession,
    *,
    index_version: str,
) -> IndexVersion | None:
    """Return the row keyed by ``index_version`` or ``None``."""
    return await session.get(IndexVersion, index_version)


async def count_documents_for_version(
    session: AsyncSession,
    *,
    index_version: str,
) -> int:
    """Count documents attached to ``index_version``.

    Used by the admin detail endpoint to show "active is 12 docs
    / 348 chunks" without re-iterating the relationship. Lives
    here (not in the route) so the service is the single source
    of truth for what "index state" means.
    """
    from app.models.documents import Document

    stmt = select(func.count()).select_from(Document).where(Document.index_version == index_version)
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def _latest_completed_run(
    session: AsyncSession,
    *,
    index_version: str,
) -> EvaluationRun | None:
    """Return the newest *completed* evaluation run for ``index_version``.

    Deliberately NOT filtered by ``suite_name``: ANY completed run for the
    index counts as evidence. That is intentional — a future suite (a judged
    answer-quality pass, a latency budget) should gate promotion too, without
    needing this predicate widened. It does mean the gate trusts whatever
    producer wrote the row, so a new writer must be held to the same standard
    as :mod:`app.worker.promotion_eval`: measure the CANDIDATE index, and never
    persist a zero-case run as passing. Today that module is the only producer
    in the deployed application.

    Deliberately a fresh ``SELECT`` rather than a walk through
    :attr:`IndexVersion.evaluation_run`: that relationship is
    configured ``lazy="raise"`` on both sides, so touching the
    attribute raises instead of loading. It is also the wrong
    question — the column records the run an index was *built*
    with, whereas the gate wants the newest run that has been
    executed against the candidate, whoever attached it.

    ``started_at`` is the ordering key, with ``run_id`` as a
    deterministic tiebreak: several rows flushed in the same
    instant (which happens in tests, and in any batch runner)
    would otherwise make "newest wins" depend on row order.
    """
    stmt = (
        select(EvaluationRun)
        .where(
            EvaluationRun.index_version == index_version,
            EvaluationRun.status.in_(_COMPLETED_EVALUATION_STATUSES),
        )
        .order_by(EvaluationRun.started_at.desc(), EvaluationRun.run_id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


def _pass_rate_from_metrics(metrics: dict[str, Any] | None) -> float | None:
    """Read a pass rate out of an :attr:`EvaluationRun.metrics` blob.

    ``None`` means "this run cannot be used as evidence" — an
    empty blob, a non-numeric value, a rate outside ``[0, 1]``, or
    counts that cannot be divided. The caller must treat that as a
    refusal, not as a pass.

    Two shapes are accepted because the repository genuinely has
    two producers. ``backend/tests/golden/scoring.py`` emits a
    ``pass_rate`` float (its count keys are ``total``/``passed``),
    while the admin API's own summariser and every fixture use
    ``cases_passed``/``cases_total``. We read ``pass_rate`` first
    and fall back to the counts; we deliberately do NOT also read
    ``passed``/``total`` **to compute a rate**, because blending the
    two conventions is how a blob from one producer would be
    silently scored with the other's semantics.

    Reading a count key purely to DISQUALIFY a blob is a different
    matter, and is why ``total`` appears below. That use is
    asymmetric — it can only ever turn a pass into a refusal, never
    the reverse — so it cannot import the other producer's
    semantics into a promotion decision.

    Two rules here are load-bearing, and both fail safe:

    * **A zero-case run is not a perfect run.** ``scoring.py`` emits
      ``"pass_rate": (passed / total) if total else 1.0`` — so a
      suite that collected NO cases (bad glob, everything skipped,
      a renamed suite) scores a flawless 1.0 and is otherwise
      indistinguishable from 20/20. Promoting an index because its
      evaluation ran nothing is precisely the failure #210 exists to
      prevent, so a stated case count of zero makes the blob
      unusable no matter what ``pass_rate`` claims.
    * **A corrupt ``pass_rate`` poisons the whole blob.** If the key
      is present but is not a real number in ``[0, 1]``, we refuse
      rather than quietly falling back to the counts: a producer
      that emitted NaN for its headline metric has no claim on our
      trust for its secondary ones.
    """
    blob = metrics or {}

    if "pass_rate" in blob:
        raw = blob.get("pass_rate")
        # ``bool`` is an ``int`` subclass; ``True`` must not read as a rate of 1.0.
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return None
        rate = float(raw)
        # NaN fails both comparisons, which is the behaviour we want.
        if not (0.0 <= rate <= 1.0):
            return None
        # Both producers' count keys are consulted, because the blob
        # carrying ``pass_rate`` may use either convention.
        for count_key in ("cases_total", "total"):
            stated = blob.get(count_key)
            if isinstance(stated, (int, float)) and not isinstance(stated, bool) and stated <= 0:
                return None
        return rate

    total = blob.get("cases_total")
    passed = blob.get("cases_passed")
    if (
        isinstance(total, (int, float))
        and not isinstance(total, bool)
        and isinstance(passed, (int, float))
        and not isinstance(passed, bool)
        and total > 0
    ):
        derived = float(passed) / float(total)
        if 0.0 <= derived <= 1.0:
            return derived

    return None


async def measured_pass_rate(
    session: AsyncSession,
    *,
    index_version: str,
) -> float | None:
    """Return the pass rate the promotion gate will measure, or ``None``.

    Public because the admin route reports the same number back to
    the operator on a successful promote. Sharing this function
    (rather than the route recomputing it) is what keeps the number
    on the response identical to the number the gate acted on.
    """
    run = await _latest_completed_run(session, index_version=index_version)
    if run is None:
        return None
    return _pass_rate_from_metrics(run.metrics)


def _format_rate(rate: float | None) -> str:
    """Render a rate for an operator-facing message."""
    return "unknown" if rate is None else f"{rate:.4g}"


async def promote_version(
    session: AsyncSession,
    *,
    index_version: str,
    admin_user_id: str,
    request_id: str,
    force: bool = False,
) -> IndexVersion:
    """Promote ``index_version`` to :data:`IndexStatus.active`.

    Steps inside a single transaction:

    1. Lock the target row (``SELECT ... FOR UPDATE``); 404 if
       not found.
    2. If the target is already ``active``, return it unchanged
       and skip steps 3-6 (idempotent on the same target).
    3. Apply the evaluation gate: resolve the candidate's newest
       COMPLETED :class:`EvaluationRun` and raise
       :class:`IndexPromotionBlocked` unless its measured pass
       rate is at least
       :attr:`Settings.index_promotion_min_pass_rate`. Missing
       evidence refuses too. ``force=True`` skips the refusal —
       but not the measurement, which is still audited.
    4. Move the current ``active`` row to ``previous_good``.
       If no row is currently ``active``, step 4 is a no-op.
    5. Mark the target ``active`` and stamp ``promoted_at`` with
       the current UTC instant.
    6. Append an ``AuditEvent`` row with action ``promote_index``
       so the change shows up in the audit log with the same
       request id the caller has on their HTTP response. The
       ``extra`` blob carries ``force``, ``measured_pass_rate``,
       ``threshold`` and ``evaluation_run_id`` on BOTH paths, so
       a clean promote is evidenced just as loudly as a forced
       one.

    The gate is step 3 and not step 2 on purpose: re-promoting the
    already-active index must stay a no-op, and a gate above the
    early return would turn that harmless retry into a 409.

    Do NOT read that ordering as "the dual-active repair is
    ungated" — an earlier draft of this docstring did, and it was
    wrong. The repair for a drifted database is the demotion loop
    below, which runs only when the target is a DIFFERENT version;
    it therefore sits under the gate like any other promotion.
    Re-promoting the row that is already active returns early and
    demotes nothing. Converging a dual-active database therefore
    needs evidence for the version being converged ON — run
    ``citevyn-worker evaluate --index-version <target>`` first
    (#216) — or the audited ``force`` override when the drift has
    to be repaired faster than a suite can run.
    ``docs/DEPLOY_FLY.md`` §4.3 covers both.

    Returns the (now-active) target row. The caller is
    responsible for committing the session.
    """
    now = datetime.now(UTC)

    target = (
        await session.execute(
            select(IndexVersion)
            .where(IndexVersion.index_version == index_version)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise _IndexVersionNotFound(index_version)

    if target.status is IndexStatus.active:
        # Idempotent: target is already active. Do not write an
        # audit row — the state didn't change.
        return target

    # --- Evaluation gate (#210) -------------------------------------------
    threshold = get_settings().index_promotion_min_pass_rate
    latest_run = await _latest_completed_run(session, index_version=index_version)
    run_id = None if latest_run is None else latest_run.run_id
    rate = None if latest_run is None else _pass_rate_from_metrics(latest_run.metrics)

    if not force:
        if latest_run is None:
            raise _IndexPromotionBlocked(
                index_version,
                reason="no_evaluation_run",
                measured_pass_rate=None,
                threshold=threshold,
                run_id=None,
            )
        if rate is None:
            raise _IndexPromotionBlocked(
                index_version,
                reason="unusable_metrics",
                measured_pass_rate=None,
                threshold=threshold,
                run_id=run_id,
            )
        # ``>=`` and not ``>``: a candidate that measures exactly the
        # configured minimum has met the bar. Getting this wrong is the
        # classic off-by-one that makes a 0.95 gate reject a 0.95 index.
        if rate < threshold:
            raise _IndexPromotionBlocked(
                index_version,
                reason="below_threshold",
                measured_pass_rate=rate,
                threshold=threshold,
                run_id=run_id,
            )

    # Find the current active row(s) and demote them. We do not take a
    # row-level lock on the demotion candidate; the promotion is a
    # single-step read+update.
    #
    # ``active`` is logically a singleton, but nothing in the schema
    # ENFORCES that, and a database that has drifted into a dual-active
    # state really happens (seed + repeated local ingests will do it).
    # This used to be ``scalar_one_or_none()``, which raised
    # ``MultipleResultsFound`` on >1 active row and surfaced as an opaque
    # HTTP 500 — with promotion being the only API that can repair index
    # state, that made a drifted database UNRECOVERABLE through the API.
    # Demote every active row instead: correct for the normal
    # single-row case and self-healing for the drifted one.
    current_active_rows = (
        (
            await session.execute(
                select(IndexVersion)
                .where(IndexVersion.status == IndexStatus.active)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for current_active in current_active_rows:
        if current_active.index_version != target.index_version:
            current_active.status = IndexStatus.previous_good

    target.status = IndexStatus.active
    target.promoted_at = now

    await audit_service.record_admin_action(
        session,
        admin_user_id=admin_user_id,
        action=AuditAction.promote_index,
        resource_type="index_version",
        resource_id=target.index_version,
        extra={
            "request_id": request_id,
            "promoted_at": now.isoformat(),
            # The gate's own reading, recorded whether or not it was
            # overridden. ``run_id`` is stringified because the audit
            # ``metadata`` column is generic JSON and ``json.dumps``
            # cannot serialise a ``uuid.UUID``.
            "force": force,
            "measured_pass_rate": rate,
            "threshold": threshold,
            "evaluation_run_id": None if run_id is None else str(run_id),
        },
    )
    return target


class _IndexVersionNotFound(Exception):
    """Raised by :func:`promote_version` when the target row is missing.

    Not the standard :class:`APIErrorCode`-shaped envelope — the
    route layer catches this and turns it into a 404.
    """

    def __init__(self, index_version: str) -> None:
        self.index_version = index_version
        super().__init__(f"index_version not found: {index_version}")


class _IndexPromotionBlocked(Exception):
    """Raised by :func:`promote_version` when the evaluation gate refuses.

    Not the standard :class:`APIErrorCode`-shaped envelope — the
    route layer catches this and turns it into a 409
    ``promotion_blocked``.

    The message names BOTH numbers (measured and required) because
    the operator reading it is deciding whether to re-run the
    evaluation or to re-issue the promote with ``force=true``, and
    "how far short did it fall" is the whole input to that call.
    ``reason`` distinguishes the three ways the gate refuses:
    ``no_evaluation_run``, ``unusable_metrics``, ``below_threshold``.
    It is typed as a :data:`PromotionBlockedReason` literal, and the
    message builder below matches on every member with no catch-all
    ``else``. Both are deliberate: a bare ``str`` plus a trailing
    ``else`` is how a fourth reason would silently inherit the
    ``below_threshold`` prose and ship an operator-facing message
    that contradicts ``details.reason`` with every test still green
    (the ``_NO_ANSWER_REASONS`` incident, in a new costume). Adding
    a member to the literal without handling it here is a pyright
    error, which is the point.
    """

    def __init__(
        self,
        index_version: str,
        *,
        reason: PromotionBlockedReason,
        measured_pass_rate: float | None,
        threshold: float,
        run_id: uuid.UUID | None,
    ) -> None:
        self.index_version = index_version
        self.reason: PromotionBlockedReason = reason
        self.measured_pass_rate = measured_pass_rate
        self.threshold = threshold
        self.run_id = run_id

        detail: str
        if reason == "no_evaluation_run":
            detail = "no completed evaluation run"
        elif reason == "unusable_metrics":
            detail = f"evaluation run {run_id} reported no readable pass_rate"
        elif reason == "below_threshold":
            detail = "pass rate below the promotion gate"
        else:
            assert_never(reason)
        super().__init__(
            f"index_version {index_version} not promoted: {detail} "
            f"(measured pass_rate {_format_rate(measured_pass_rate)}, "
            f"required >= {_format_rate(threshold)})"
        )


__all__ = [
    "IndexPromotionBlocked",
    "IndexVersionNotFound",
    "PromotionBlockedReason",
    "count_documents_for_version",
    "get_version",
    "list_versions",
    "measured_pass_rate",
    "promote_version",
]


# Re-export the private errors under their public names so callers
# don't have to reach for the underscore prefix.
IndexVersionNotFound = _IndexVersionNotFound
IndexPromotionBlocked = _IndexPromotionBlocked
