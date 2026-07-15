"""Tests for :mod:`app.worker.fetchers`."""

from __future__ import annotations

import pytest

from app.worker.allowlist import MVP_SOURCES, SourceSpec
from app.worker.fetchers import (
    FetchError,
    HttpFetcher,
    LocalFetcher,
    build_fetcher,
)


def _spec(location: str) -> SourceSpec:
    return SourceSpec(
        name="test",
        product_area="test",
        title="Test Source",
        fetcher="local",
        location=location,
    )


def test_local_fetcher_reads_existing_fixture() -> None:
    """The Claude API fixture is read and returned as a str."""
    fetcher = LocalFetcher()
    raw = fetcher.fetch(_spec("app/worker/sources/claude_api.md"))
    assert isinstance(raw, str)
    assert "Claude API Reference" in raw
    assert "Rate limits" in raw


def test_local_fetcher_missing_file_raises_fetch_error() -> None:
    """A missing file is a :class:`FetchError`, not an :class:`OSError`."""
    fetcher = LocalFetcher()
    with pytest.raises(FetchError) as exc_info:
        fetcher.fetch(_spec("app/worker/sources/does-not-exist.md"))
    assert "not found" in str(exc_info.value)


def test_build_fetcher_returns_local_for_local_spec() -> None:
    """``build_fetcher`` picks :class:`LocalFetcher` for ``fetcher='local'``."""
    fetcher = build_fetcher(_spec("app/worker/sources/claude_api.md"))
    assert isinstance(fetcher, LocalFetcher)


def test_build_fetcher_returns_http_for_http_spec() -> None:
    """``build_fetcher`` picks :class:`HttpFetcher` for ``fetcher='http'``."""
    spec = SourceSpec(
        name="remote",
        product_area="remote",
        title="Remote",
        fetcher="http",
        location="https://example.com/docs",
    )
    fetcher = build_fetcher(spec)
    assert isinstance(fetcher, HttpFetcher)


def test_all_mvp_sources_ship_under_the_package_and_fetch() -> None:
    """#92 regression: every MVP source is a package-shipped local file.

    The worker CLI defaults to ``LocalFetcher`` with the package root. Before #92
    the source docs lived under ``tests/fixtures/sources`` — NOT copied into the
    prod worker image — so ``citevyn-worker run`` failed on the first fetch in
    production. They now ship under ``app/worker/sources`` (inside ``app/``, which
    the Dockerfile copies). This asserts each MVP source is a ``local`` fetcher,
    resolves under ``app/worker/sources/``, and its ``LocalFetcher.fetch()``
    succeeds against the same package root the CLI uses — so a re-move or a
    forgotten file fails here rather than only in a prod deploy.
    """
    assert MVP_SOURCES, "MVP_SOURCES must not be empty"
    for spec in MVP_SOURCES:
        assert spec.fetcher == "local", f"{spec.name}: prod ingestion expects a local fetcher"
        assert spec.location.startswith("app/worker/sources/"), (
            f"{spec.name}: source must ship under the package (got {spec.location!r})"
        )
        text = build_fetcher(spec).fetch(spec)  # raises FetchError if unshipped/missing
        assert text.strip(), f"{spec.name}: shipped source is empty"


def test_http_fetcher_raises_in_mvp() -> None:
    """The MVP HttpFetcher is a placeholder — calling it raises."""
    spec = SourceSpec(
        name="remote",
        product_area="remote",
        title="Remote",
        fetcher="http",
        location="https://example.com/docs",
    )
    fetcher = HttpFetcher()
    with pytest.raises(FetchError) as exc_info:
        fetcher.fetch(spec)
    assert "not wired" in str(exc_info.value).lower()
