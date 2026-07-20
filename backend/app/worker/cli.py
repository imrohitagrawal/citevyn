"""Console-script entry point for the ingestion worker.

Usage:

    citevyn-worker run                       # ingest all MVP sources
    citevyn-worker run --source codex        # ingest one source
    citevyn-worker run --index-version v-test
    citevyn-worker list-sources

The CLI is intentionally thin: it builds a single
:class:`IngestionRunner` and drives it through one source
or all of them. Polling, retries, and parallel workers are
deliberately out of scope for the MVP — the admin
``POST /v1/admin/index_versions/{version}/promote`` is
the only way to ship a build, and that gate is gated
behind the admin API key.

:func:`build_runner`, :func:`content_version_hash` and :func:`drive` are
PUBLIC (they used to be underscore-private). ``db/seed/seed_catalog.py``
now seeds the demo/bootstrap catalog by running this very pipeline over
:data:`MVP_SOURCES` instead of carrying its own hand-written copy of the
corpus (#178), and it must not fork the fingerprint or the
publish-the-hash-only-after-a-clean-full-run ordering — sharing the
functions is what makes that impossible.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
from collections.abc import Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.db import get_sessionmaker
from app.core.logging import configure_logging
from app.embeddings import (
    DocumentEmbedder,
    NullEmbedder,
    build_embedder,
    metered_embedder,
    validate_embedder_provider,
)
from app.worker.allowlist import MVP_SOURCES, SourceSpec, get_source, list_source_names
from app.worker.fetchers import FetchError, build_fetcher
from app.worker.runner import (
    IngestionRunner,
    RunResult,
    advance_source_version_hash,
    ensure_index_version,
)

logger = logging.getLogger(__name__)

# Folded into the corpus fingerprint in place of a source that cannot be read,
# so one unfetchable spec degrades the hash instead of aborting the whole run.
_UNFETCHABLE_SENTINEL = "\x00<unfetchable>"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging()
    if args.command == "list-sources":
        return _cmd_list_sources()
    if args.command == "run":
        return _cmd_run(args)
    parser.print_help()
    return 1


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _cmd_list_sources() -> int:
    """Print the MVP source list and exit 0."""
    for name in list_source_names():
        spec = get_source(name)
        print(f"{spec.name}\t{spec.product_area}\t{spec.fetcher}\t{spec.location}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Ingest all sources (or the one named by ``--source``)."""
    settings = get_settings()
    runner = build_runner(settings, index_version=args.index_version)
    sessionmaker = get_sessionmaker()
    sources = _resolve_sources(args.source)
    return asyncio.run(drive(runner, sessionmaker, sources, args.index_version))


async def drive(
    runner: IngestionRunner,
    sessionmaker: async_sessionmaker[AsyncSession],
    sources: list[SourceSpec],
    index_version: str,
) -> int:
    """Drive ``runner`` through ``sources`` and report."""
    failed = 0
    async with sessionmaker() as session:
        # One IndexVersion row for the whole run. Idempotent
        # — a re-run for the same ``index_version`` returns
        # the existing row. On a re-ingest this deliberately
        # leaves ``source_version_hash`` alone; see the
        # ``advance_source_version_hash`` call below.
        await ensure_index_version(
            session,
            index_version=index_version,
            source_version_hash=runner.source_version_hash,
            embedding_provider=runner.embedding_provider,
            embedding_model=runner.embedding_model,
            embedding_dim=runner.embedding_dim,
        )
        await session.commit()

    for source in sources:
        async with sessionmaker() as session:
            result = await runner.run(session, source=source)
        _print_result(result)
        if result.status.value == "failed":
            failed += 1

    # Publish the new corpus fingerprint only once the corpus actually matches
    # it: every source ingested, and the whole corpus ingested. The fingerprint
    # spans all of MVP_SOURCES, so a ``--source`` subset run cannot vouch for
    # the sources it skipped — advancing there would claim the untouched docs
    # were rebuilt too. Publishing early (or on a partial run) caches answers
    # built from the OLD chunks under the NEW key, and a retry re-hashes the
    # same files, so nothing would evict them until the TTL.
    full_corpus = {s.name for s in sources} == {s.name for s in MVP_SOURCES}
    if failed == 0 and full_corpus:
        async with sessionmaker() as session:
            await advance_source_version_hash(
                session,
                index_version=index_version,
                source_version_hash=runner.source_version_hash,
            )
            await session.commit()
    elif failed == 0:
        logger.info(
            "source_version_hash_not_advanced_partial_run",
            extra={"index_version": index_version, "ingested": sorted(s.name for s in sources)},
        )

    return 0 if failed == 0 else 2


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="citevyn-worker",
        description="Ingestion worker (Slice 8 step 6).",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list-sources", help="Print the MVP source list.")

    run = sub.add_parser("run", help="Ingest one source or all sources.")
    run.add_argument(
        "--source",
        default=None,
        help="Ingest one source by name (default: all MVP sources).",
    )
    run.add_argument(
        "--index-version",
        default="v-local",
        help="IndexVersion key to write to (default: v-local).",
    )
    return parser


def build_runner(
    settings: Settings, *, index_version: str, write_vectors: bool = True
) -> IngestionRunner:
    """Build the runner with the default fetcher + embedder.

    ``index_version`` is plumbed into the constructor (not defaulted
    here) so the CLI's ``--index-version`` flag is actually honored
    end-to-end. The runner uses it to (a) write the
    :class:`IndexVersion` row during ``ensure_index_version``, (b)
    stamp every :class:`Document` / :class:`Chunk` row it creates,
    and (c) satisfy the FK on ``documents``.

    ``write_vectors=False`` is the seam for the demo/bootstrap seeder
    (``db/seed/seed_catalog.py``) under the default **stub** provider: it swaps
    in a :class:`~app.embeddings.null.NullEmbedder` so every chunk is persisted
    with a NULL embedding, and stamps NO provenance on the
    :class:`IndexVersion`. The point is that hash-bucketed stub vectors are
    never *written*, not that they are cleaned up afterwards — ``drive`` commits
    each source as it goes and a re-seed runs against an already-active index,
    so any after-the-fact strip leaves a window in which a reader sees a vector
    arm that is enabled (matching ``stub`` stamp), ranking by SHA-256 distance,
    and reported ``healthy``. See :class:`~app.embeddings.null.NullEmbedder`.

    ``citevyn-worker run`` leaves this at ``True``: an operator building a
    throwaway stub index has asked for exactly that, explicitly.
    """
    # Fail fast on a bad embedding config (unknown provider, stub-in-prod, or a
    # dimension that does not match the pgvector column) so a standalone worker
    # cannot silently build a hash-only or wrong-dim index. Mirrors the API's
    # startup guard in app.main. Validated even when ``write_vectors=False``:
    # the bootstrap must still refuse a stub provider in production.
    validate_embedder_provider(settings)
    fetcher = build_fetcher(_pick_first_source())  # build default-root LocalFetcher
    # The provenance stamp is a property OF the embedder, so it is decided here
    # in one place rather than re-tested per field: an embedder that writes no
    # vectors stamps no provider. A ``stub`` stamp with no vectors behind it
    # would be a claim the index cannot honour, and it is exactly the stamp the
    # Tier-3 gate reads; ``None`` is the "unknown provenance ⇒ allow" state
    # (:func:`app.embeddings.factory.is_index_embedder_mismatch`), which leaves a
    # later real-embedder deploy free to re-stamp instead of being wedged into a
    # permanent mismatch degrade.
    #
    # Metering wraps the REAL embedder only, and is applied here as well as in
    # ``get_embedder``: the worker builds its OWN embedder (it never touches the
    # API's process-wide singleton), and ingest is the burstiest embedding spend
    # there is — a whole corpus in one run. Wrapping only the API path would
    # leave the §9 budget blind to exactly that.
    #
    # ``NullEmbedder`` is deliberately NOT wrapped: it issues no provider call,
    # so metering it would write rows for spend that never happened — the same
    # reason ``_metered`` refuses to wrap the stub LLM client.
    embedder: DocumentEmbedder
    if write_vectors:
        embedder = metered_embedder(build_embedder(settings), settings)
        provider, model = settings.embedding_provider, settings.embedding_model
    else:
        embedder = NullEmbedder(settings.embedding_dim)
        provider, model = None, None
    return IngestionRunner(
        fetcher=fetcher,
        embedder=embedder,
        # Derive the snapshot hash from the ACTUAL corpus content, not the static
        # ``settings.source_version_hash``. The answer-cache key includes this hash
        # (see ``cache.build_cache_key``), so a constant value meant a doc edit +
        # re-ingest left the cache serving the OLD answer until someone manually
        # bumped the constant or the TTL expired. Hashing real content makes any
        # edit change the hash → the cache key changes → stale answers invalidate
        # on the next ingest, automatically.
        source_version_hash=content_version_hash(MVP_SOURCES),
        index_version=index_version,
        embedding_provider=provider,
        embedding_model=model,
    )


def content_version_hash(
    sources: Sequence[SourceSpec],
    *,
    fetch: Callable[[SourceSpec], str] | None = None,
) -> str:
    """Return ``sha256:<hex>`` over the actual content of every source.

    Hashes each source's fetched text (in a stable name-sorted order, with the
    source name mixed in so a rename also changes the hash) so the result is a
    deterministic fingerprint of the whole corpus. This is what makes the answer
    cache self-invalidate when a source doc is edited — see the call site. Hashing
    the full ``MVP_SOURCES`` (not just the ``--source`` subset) keeps the fingerprint
    corpus-wide, so a single-source re-ingest still reflects the true snapshot.

    A source that cannot be fetched contributes a stable sentinel instead of
    aborting the run. Hashing spans the whole corpus, so without this a single
    unreadable spec — a missing local fixture, or the first ``fetcher="http"``
    source, which :class:`HttpFetcher` always rejects in the MVP — would raise
    here at runner-build time and kill ``run`` before a single
    :class:`IngestionJob` row existed, even for ``--source <a healthy one>``.
    Degrading keeps the pre-existing behaviour: the bad source still fails, but
    as its own job row with ``error_type="FetchError"``, and the other sources
    still ingest.

    ``fetch`` is injectable for testing; it defaults to the real per-source fetcher.
    """

    def _default_read(spec: SourceSpec) -> str:
        return build_fetcher(spec).fetch(spec)

    read: Callable[[SourceSpec], str] = fetch if fetch is not None else _default_read
    digest = hashlib.sha256()
    for spec in sorted(sources, key=lambda s: s.name):
        try:
            text = read(spec)
        except FetchError as exc:
            logger.warning(
                "source_version_hash_source_unfetchable",
                extra={"source": spec.name, "error": str(exc)},
            )
            text = _UNFETCHABLE_SENTINEL
        digest.update(spec.name.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(text.encode("utf-8"))
        digest.update(b"\x1e")
    return f"sha256:{digest.hexdigest()}"


def _pick_first_source() -> SourceSpec:
    """Return the first :class:`SourceSpec` (a placeholder for fetcher build)."""
    return MVP_SOURCES[0]


def _resolve_sources(name: str | None) -> list[SourceSpec]:
    """Return the source list — either one source by name or all of them."""
    if name is None:
        return list(MVP_SOURCES)
    return [get_source(name)]


def _print_result(result: RunResult) -> None:
    """Print a one-line summary of ``result`` to stderr."""
    if result.status.value == "completed":
        logger.info(
            "ingestion completed source=%s chunks=%d terms=%d",
            result.source_name,
            result.chunk_count,
            result.term_count,
        )
    else:
        logger.error(
            "ingestion failed source=%s error_type=%s message=%s",
            result.source_name,
            result.error_type,
            result.error_message,
        )


if __name__ == "__main__":
    raise SystemExit(main())
