"""Seed the demo catalog by INGESTING the shipped corpus.

Idempotent: re-running replaces the chunks of each document rather than
appending. Run from the repository root with
``uv run python -m db.seed.seed_catalog`` after ``alembic upgrade head``
and ``uv run python -m db.seed.seed_users``.

There is deliberately NO hand-written copy of the corpus in this file
(#178). It used to carry its own ``_DOC_DEFS`` list that the docstring
asked contributors to "keep in lock-step with MVP_SOURCES and the conftest
seed" — and that is exactly the invariant that kept breaking: #170 added
Claude Code installation content to ``app/worker/sources/claude_code.md``
and mirrored it into the conftest fixture, but not here, so a fresh
``make demo`` stack still refused "How do I install Claude Code?". A
corpus correction that reaches only some of its copies is the #162 failure
class.

So this module now runs the REAL ingestion pipeline
(fetch → parse → chunk → exact-terms → embed → persist) over
:data:`app.worker.allowlist.MVP_SOURCES` — the same code path
``citevyn-worker run`` uses — into the ``v1`` index the demo/bootstrap
stack reads from. The markdown under ``backend/app/worker/sources/`` is
the single authoritative copy: editing it is the only way to change what
``make demo``, ``scripts/smoke.sh`` and ``deploy.sh`` serve.

Two consequences worth knowing:

* The sources are fetched with ``fetcher="local"`` off the filesystem, so
  seeding stays offline and hermetic — no network, and under the default
  stub embedder no API key and no cost.
* Chunk *content* now matches production exactly (title-prefixed contextual
  chunks, real ``source_url`` citations), and the row counts are whatever
  the chunker produces — not a fixed five.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

# Make the backend package importable when running as a script.
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings  # noqa: E402
from app.models import (  # noqa: E402
    Chunk,
    Document,
    ExactTerm,
    IndexStatus,
    IndexVersion,
)
from app.worker.allowlist import MVP_SOURCES  # noqa: E402
from app.worker.cli import build_runner, drive  # noqa: E402

# Package-relative so it resolves under BOTH layouts: repo-root
# ``python -m db.seed.seed_catalog`` (package ``db.seed``) and the deploy image's
# ``python -m seed.seed_catalog`` with ``PYTHONPATH=/db`` (package ``seed``, no
# top-level ``db``). An absolute ``from db.seed import ...`` breaks the latter.
from . import redact_database_url  # noqa: E402

INDEX_VERSION: str = "v1"


class SeedError(RuntimeError):
    """A source failed to ingest, so the demo catalog is incomplete.

    Raised instead of exiting quietly, because a half-seeded catalog answers
    *some* questions and refuses others — which reads as a retrieval bug at
    demo time rather than as a broken bootstrap.
    """


async def _retire_orphans(session: AsyncSession, index_version: str) -> int:
    """Delete documents in ``index_version`` that no source in the allowlist owns.

    The ingestion runner is idempotent on ``(source_name, index_version)``, so it
    only ever refreshes documents it recognises — anything else in the index it
    simply ignores. Two ways that leaves stale content live:

    * **Upgrading a database seeded before #178.** The old hand-written seeder
      wrote all five of its documents under ``source_name="docs.test"`` with
      ``https://docs.test/...`` URLs. The runner does not match those, so without
      this sweep they survive the re-seed and ``v1`` serves the real corpus AND
      the hand-written copy side by side — the exact drift this change exists to
      end, now with fabricated citation links attached.
    * **Removing a source from the allowlist.** Its documents would keep being
      retrieved and cited forever.

    Chunks and exact terms are deleted explicitly rather than left to
    ``ON DELETE CASCADE``: the hermetic SQLite engine does not enforce foreign
    keys by default, so relying on the cascade would make this a no-op in tests
    and a silent divergence between backends (the same reason
    ``IngestionRunner._delete_existing_chunks`` deletes explicitly).

    Only ``index_version`` is touched — other index versions belong to the
    operator's worker/promote flow.
    """
    known = {spec.name for spec in MVP_SOURCES}
    orphans = list(
        (
            await session.execute(
                select(Document).where(
                    Document.index_version == index_version,
                    Document.source_name.not_in(known),
                )
            )
        )
        .scalars()
        .all()
    )
    if not orphans:
        return 0
    ids = [doc.document_id for doc in orphans]
    await session.execute(delete(ExactTerm).where(ExactTerm.document_id.in_(ids)))
    await session.execute(delete(Chunk).where(Chunk.document_id.in_(ids)))
    await session.execute(delete(Document).where(Document.document_id.in_(ids)))
    await session.flush()
    return len(orphans)


async def _activate(session: AsyncSession, index_version: str) -> str:
    """Make ``index_version`` the active index, unless an operator owns that slot.

    The demo/bootstrap stack has no admin API call in its path, so something has
    to promote what it just built — the old hand-written seeder simply created
    ``v1`` with ``status=active``. We keep that, with one guard it did not have:
    if a DIFFERENT index version is already active (an operator ran the worker
    and promoted, the normal production flow), we leave it alone and park ``v1``
    as a candidate. Re-running the bootstrap seed must never silently yank the
    live index out from under a promoted build.

    Returns a short status string for the summary line.
    """
    row = await session.get(IndexVersion, index_version)
    if row is None:  # pragma: no cover - drive() creates it before we get here
        return "missing"
    if row.status is IndexStatus.active:
        return "already-active"
    other_active = await session.scalar(
        select(IndexVersion).where(
            IndexVersion.status == IndexStatus.active,
            IndexVersion.index_version != index_version,
        )
    )
    if other_active is not None:
        return "left-as-candidate (another index is active)"
    row.status = IndexStatus.active
    row.promoted_at = datetime.now(UTC)
    return "promoted"


async def _tally(session: AsyncSession, index_version: str) -> dict[str, int]:
    """Count what the seeded index actually holds (absolute, not deltas).

    Absolute counts rather than "+N inserted": a re-seed replaces chunks in
    place, so a delta of zero would be indistinguishable from "nothing landed".
    """
    documents = await session.scalar(
        select(func.count()).select_from(Document).where(Document.index_version == index_version)
    )
    chunks = await session.scalar(
        select(func.count())
        .select_from(Chunk)
        .join(Document, Document.document_id == Chunk.document_id)
        .where(Document.index_version == index_version)
    )
    exact_terms = await session.scalar(
        select(func.count())
        .select_from(ExactTerm)
        .join(Document, Document.document_id == ExactTerm.document_id)
        .where(Document.index_version == index_version)
    )
    return {
        "sources": len(MVP_SOURCES),
        "documents": documents or 0,
        "chunks": chunks or 0,
        "exact_terms": exact_terms or 0,
    }


async def seed(database_url: str) -> dict[str, int | str]:
    """Ingest the shipped corpus into ``database_url`` as index ``v1``.

    Returns a summary of what the index holds afterwards. Safe to re-run: the
    runner replaces a document's chunks instead of appending to them, and the
    ``source_version_hash`` is advanced only after every source ingested
    cleanly (see :func:`app.worker.cli.drive`) so the answer cache invalidates
    exactly when the corpus really changed.

    Documents in ``v1`` that no allowlisted source owns are retired first — see
    :func:`_retire_orphans` for why a re-seed would otherwise serve the corpus
    twice on any database bootstrapped before #178.

    Raises :class:`SeedError` if any source failed. ``v1`` is NOT activated in
    that case, and nothing is retired, so a broken corpus edit cannot go live
    through the bootstrap path — the previously active index keeps serving.
    """
    settings = get_settings()
    # Fails loud on a stub embedder in production / a wrong-dim config, exactly
    # as ``citevyn-worker run`` and the API startup guard do. The old seeder
    # skipped embedding entirely under a stub, which quietly produced a
    # vector-less index on a stack whose read path expects one.
    runner = build_runner(settings, index_version=INDEX_VERSION)
    engine = create_async_engine(database_url, future=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        exit_code = await drive(runner, sessionmaker, list(MVP_SOURCES), INDEX_VERSION)
        if exit_code != 0:
            raise SeedError(
                "one or more sources failed to ingest; the demo catalog is incomplete "
                f"and {INDEX_VERSION} was left unpromoted (see the ingestion job rows)"
            )
        async with sessionmaker() as session:
            retired = await _retire_orphans(session, INDEX_VERSION)
            status = await _activate(session, INDEX_VERSION)
            summary: dict[str, int | str] = dict(await _tally(session, INDEX_VERSION))
            await session.commit()
    finally:
        await engine.dispose()
    summary["retired_documents"] = retired
    summary["index_version"] = INDEX_VERSION
    summary["status"] = status
    return summary


def main() -> None:
    """CLI entry point: seed and print a one-line summary."""
    settings = get_settings()
    summary = asyncio.run(seed(settings.database_url))
    # Redact the password: this line lands in deploy.sh / CI logs (#93).
    print(
        f"Seeded catalog into {redact_database_url(settings.database_url)}: "
        f"sources={summary['sources']} "
        f"documents={summary['documents']} "
        f"chunks={summary['chunks']} "
        f"exact_terms={summary['exact_terms']} "
        f"retired_documents={summary['retired_documents']} "
        f"index_version={summary['index_version']} ({summary['status']})"
    )


if __name__ == "__main__":
    main()
