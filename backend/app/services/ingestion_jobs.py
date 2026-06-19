"""Service layer for :class:`app.models.ingestion_jobs.IngestionJob`.

Read paths only. The worker (Step 6) creates and updates
:class:`IngestionJob` rows; the admin surface inspects them.

Design notes
------------
* The list endpoint sorts by ``started_at`` descending so the
  most recent job is on top.
* The ``status`` and ``source_name`` filters compose. Either
  may be ``None``; both being ``None`` returns the last 50
  jobs across the board (the "what just happened?" view).
* No retry / requeue API yet. The worker writes ``retryable``
  and ``error_message``; the admin UI exposes them in the
  detail view. Re-running a failed job will be a future
  ``POST /v1/admin/ingestion_jobs/{job_id}/retry`` endpoint.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import JobStatus
from app.models.ingestion_jobs import IngestionJob


async def list_jobs(
    session: AsyncSession,
    *,
    status: JobStatus | None = None,
    source_name: str | None = None,
    limit: int = 50,
) -> list[IngestionJob]:
    """Return the most recent ingestion jobs, newest first.

    ``limit`` is clamped to 200 so the admin UI doesn't load
    a six-month history by accident.
    """
    capped_limit = max(1, min(limit, 200))
    stmt = select(IngestionJob)
    if status is not None:
        stmt = stmt.where(IngestionJob.status == status)
    if source_name is not None:
        stmt = stmt.where(IngestionJob.source_name == source_name)
    stmt = stmt.order_by(IngestionJob.started_at.desc()).limit(capped_limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
) -> IngestionJob | None:
    """Return the job keyed by ``job_id`` or ``None``."""
    return await session.get(IngestionJob, job_id)


__all__ = [
    "get_job",
    "list_jobs",
]
