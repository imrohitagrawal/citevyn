"""Service layer for :class:`app.models.evaluation.EvaluationRun`.

Read-only today. The runner that creates :class:`EvaluationRun`
rows is the worker (Step 6); the admin surface only lists and
inspects what the worker has already produced.

Design notes
------------
* The list endpoint always sorts by ``started_at`` descending so
  the most recent run is on top — that's the run an SRE will
  open first when triaging "did the last build regress?".
* The detail endpoint returns the row as-is. The ``metrics`` and
  ``failure_summary`` columns are ``JSON``-typed on the model;
  we hand them back through Pydantic rather than reshaping,
  since the worker (which produces them) is the only writer and
  the shape is its contract.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import EvaluationRun


async def list_runs(
    session: AsyncSession,
    *,
    index_version: str | None = None,
    limit: int = 50,
) -> list[EvaluationRun]:
    """Return the most recent evaluation runs, newest first.

    ``limit`` is clamped to a sensible maximum (200) so a stale
    dashboard query can't drag the whole history. The
    ``index_version`` filter is exact-match.
    """
    capped_limit = max(1, min(limit, 200))
    stmt = select(EvaluationRun)
    if index_version is not None:
        stmt = stmt.where(EvaluationRun.index_version == index_version)
    stmt = stmt.order_by(EvaluationRun.started_at.desc()).limit(capped_limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
) -> EvaluationRun | None:
    """Return the run keyed by ``run_id`` or ``None``."""
    return await session.get(EvaluationRun, run_id)


def summarize_run(
    run: EvaluationRun,
) -> dict[str, Any]:
    """Return a flat dict suitable for the admin list response.

    The detail endpoint serialises the row directly through
    Pydantic; the list endpoint flattens the most-asked
    metrics (``metrics.cases_total`` / ``metrics.cases_passed``)
    into a single ``summary`` block so a list view can render
    "12/15 passed" without re-parsing the JSON blob.
    """
    metrics: dict[str, Any] = dict(run.metrics or {})
    return {
        "run_id": str(run.run_id),
        "suite_name": run.suite_name,
        "index_version": run.index_version,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "status": run.status.value,
        "summary": {
            "cases_total": metrics.get("cases_total"),
            "cases_passed": metrics.get("cases_passed"),
            "cases_failed": metrics.get("cases_failed"),
        },
    }


__all__ = [
    "get_run",
    "list_runs",
    "summarize_run",
]
