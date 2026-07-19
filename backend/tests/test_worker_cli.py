"""Tests for :mod:`app.worker.cli` helpers.

Focus: ``_content_version_hash`` — the fingerprint that makes the answer cache
self-invalidate when a source doc is edited. Previously the CLI stamped a static
``settings.source_version_hash`` on every ingest, so editing a doc and re-ingesting
left the cache serving the OLD answer (the cache key includes source_version_hash).

The full chain has three links, each pinned somewhere:

1. content edit → new fingerprint (here),
2. new fingerprint → stamped onto the ``IndexVersion`` row even on an in-place
   re-ingest (``test_worker_runner.py::test_ensure_index_version_refreshes_*``),
3. changed hash → cache miss (``test_cache_invalidation.py``).

Link 2 is the one that made the fix a no-op before: ``ensure_index_version``
only wrote the hash on INSERT, and the shipped worker image always re-ingests
into the same default ``--index-version v-local``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.worker.allowlist import MVP_SOURCES, SourceSpec
from app.worker.cli import _build_runner, _content_version_hash
from app.worker.fetchers import FetchError, LocalFetcher

# ---------------------------------------------------------------------------
# Fingerprint properties
# ---------------------------------------------------------------------------


def test_hash_is_sha256_prefixed_and_deterministic() -> None:
    h1 = _content_version_hash(MVP_SOURCES)
    h2 = _content_version_hash(MVP_SOURCES)
    assert h1.startswith("sha256:")
    assert len(h1) == len("sha256:") + 64  # sha256 hex digest
    assert h1 == h2


def test_hash_is_order_independent() -> None:
    """Sources are hashed in a stable name-sorted order — input order must not matter."""
    assert _content_version_hash(MVP_SOURCES) == _content_version_hash(list(reversed(MVP_SOURCES)))


def test_hash_changes_when_a_source_edit_changes() -> None:
    """The core property: an edit to a source's CONTENT changes the fingerprint.

    A changed fingerprint changes the answer-cache key, so a re-ingest after a doc
    edit invalidates stale cached answers instead of serving them.
    """
    one = MVP_SOURCES[:1]
    before = _content_version_hash(one, fetch=lambda s: "original content")
    after = _content_version_hash(one, fetch=lambda s: "EDITED content")
    assert before != after


def test_hash_reflects_the_source_set() -> None:
    """Adding/removing a source changes the corpus fingerprint too."""
    assert _content_version_hash(MVP_SOURCES) != _content_version_hash(MVP_SOURCES[:2])


def test_hash_changes_when_a_source_is_renamed() -> None:
    """The name is mixed in, so a rename is a corpus change even at identical bytes."""
    spec = MVP_SOURCES[0]
    renamed = SourceSpec(
        name=spec.name + "_renamed",
        product_area=spec.product_area,
        title=spec.title,
        fetcher=spec.fetcher,
        location=spec.location,
    )
    same_bytes = "identical content"
    assert _content_version_hash([spec], fetch=lambda s: same_bytes) != _content_version_hash(
        [renamed], fetch=lambda s: same_bytes
    )


def test_field_framing_prevents_a_boundary_collision() -> None:
    """Name/content are separated, so shifting the boundary must change the hash.

    Without the ``\\x1f`` field separator, a source named ``ab`` with content ``c``
    and one named ``a`` with content ``bc`` would hash identically.
    """

    def _spec(name: str) -> SourceSpec:
        return SourceSpec(name=name, product_area="p", title="t", fetcher="local", location="l")

    left = _content_version_hash([_spec("ab")], fetch=lambda s: "c")
    right = _content_version_hash([_spec("a")], fetch=lambda s: "bc")
    assert left != right


# ---------------------------------------------------------------------------
# Real fetch path
# ---------------------------------------------------------------------------


def test_default_fetch_reads_the_real_source_files() -> None:
    """The no-``fetch`` path goes through the real ``build_fetcher`` → ``LocalFetcher``.

    Pins the wiring: hashing something other than the file bytes (e.g. the
    ``location`` string) would still satisfy every property test above.
    """
    assert _content_version_hash(MVP_SOURCES) == _content_version_hash(
        MVP_SOURCES, fetch=LocalFetcher().fetch
    )


def test_editing_a_real_source_file_changes_the_hash(tmp_path: Path) -> None:
    """End-to-end over the filesystem: edit a doc on disk, get a different hash."""
    (tmp_path / "sources").mkdir()
    doc = tmp_path / "sources" / "thing.md"
    doc.write_text("# Thing\n\nOriginal body.\n", encoding="utf-8")
    spec = SourceSpec(
        name="thing",
        product_area="thing",
        title="Thing",
        fetcher="local",
        location="sources/thing.md",
    )
    read = LocalFetcher(root=tmp_path).fetch

    before = _content_version_hash([spec], fetch=read)
    doc.write_text("# Thing\n\nBroadened body.\n", encoding="utf-8")
    after = _content_version_hash([spec], fetch=read)
    assert before != after


def test_an_unfetchable_source_degrades_instead_of_aborting_the_run() -> None:
    """One bad source must not kill the whole ingest at runner-build time.

    The fingerprint spans the entire corpus, so without the ``FetchError`` guard a
    single missing fixture — or the first ``fetcher="http"`` source, which
    :class:`HttpFetcher` always rejects — would raise here and prevent even
    ``run --source <a healthy one>`` from writing a single job row.
    """

    def _explode(spec: SourceSpec) -> str:
        raise FetchError(f"cannot read {spec.name}")

    digest = _content_version_hash(MVP_SOURCES[:2], fetch=_explode)
    assert digest.startswith("sha256:")
    # Degrading is not the same as ignoring: a readable corpus still differs.
    assert digest != _content_version_hash(MVP_SOURCES[:2], fetch=lambda s: "readable")


# ---------------------------------------------------------------------------
# Wiring: the runner actually receives the content hash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index_version", ["v-local", "v-candidate"])
def test_build_runner_stamps_the_content_hash(index_version: str) -> None:
    """``_build_runner`` must hand the runner the CONTENT hash.

    This is the behaviour change itself: before, a static setting was stamped on
    every ingest. Without this test, reverting the one line in ``_build_runner``
    leaves every other test in this file green.
    """
    runner = _build_runner(Settings(embedding_provider="stub"), index_version=index_version)
    assert runner.source_version_hash == _content_version_hash(MVP_SOURCES)
    assert runner.source_version_hash.startswith("sha256:")
