"""Tests for :mod:`app.worker.cli` helpers.

Focus: ``_content_version_hash`` — the fingerprint that makes the answer cache
self-invalidate when a source doc is edited. Previously the CLI stamped a static
``settings.source_version_hash`` on every ingest, so editing a doc and re-ingesting
left the cache serving the OLD answer (the cache key includes source_version_hash).
``test_cache_invalidation.py`` already proves the cache invalidates when that hash
changes; these tests prove the hash actually changes with content.
"""

from __future__ import annotations

from app.worker.allowlist import MVP_SOURCES
from app.worker.cli import _content_version_hash


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


def test_hash_is_stable_when_content_is_unchanged() -> None:
    """Same content ⇒ same hash, so re-ingesting identical content REUSES the cache
    (e.g. a config-only embedder swap must not needlessly bust every cached answer)."""
    one = MVP_SOURCES[:1]
    a = _content_version_hash(one, fetch=lambda s: "same content")
    b = _content_version_hash(one, fetch=lambda s: "same content")
    assert a == b


def test_hash_reflects_the_source_set() -> None:
    """Adding/removing a source changes the corpus fingerprint too."""
    assert _content_version_hash(MVP_SOURCES) != _content_version_hash(MVP_SOURCES[:2])
