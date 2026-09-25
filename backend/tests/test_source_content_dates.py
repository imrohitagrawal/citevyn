"""The "docs as of" date for each shipped source (ADR-0005 §6), kept honest.

Every source under ``app/worker/sources`` is an original, paraphrased summary of
the official page, written into this repo by hand. It is not fetched live. So the
one honest "docs as of" date is when OUR copy was last updated, and that is what
``SourceSpec.content_as_of`` records. The ingest run's clock is not it: every
deploy re-ingests, so the run date would say "today" about July's content.

``content_sha256`` pins the date to the exact text it describes. Editing a source
file without updating both fields turns ``test_every_source_date_matches_its_text``
red, so the date cannot drift away from the content silently.

WHAT THIS CANNOT SEE: a date moved forward with no change to the text. Nothing in
the file says when it was last checked against upstream, and ``git log`` is not
usable in CI (a shallow checkout sees one commit). Moving a date without an edit
is a deliberate, reviewable change to ``allowlist.py``; this guard does not stop it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.worker.allowlist import MVP_SOURCES

BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("spec", MVP_SOURCES, ids=lambda s: s.name)
def test_every_source_date_matches_its_text(spec) -> None:  # type: ignore[no-untyped-def]
    """Turns red if: a source file changes and its content_as_of/content_sha256
    are not updated with it (or a source has none)."""
    assert spec.content_as_of is not None, f"{spec.name}: no content_as_of"
    assert spec.content_sha256, f"{spec.name}: no content_sha256"
    text = (BACKEND / spec.location).read_bytes()
    actual = hashlib.sha256(text).hexdigest()
    assert actual == spec.content_sha256, (
        f"{spec.location} changed. Set content_as_of to today's date (the day this copy "
        f"was updated) and content_sha256 to {actual}."
    )


@pytest.mark.parametrize("spec", MVP_SOURCES, ids=lambda s: s.name)
def test_no_source_is_dated_in_the_future(spec) -> None:  # type: ignore[no-untyped-def]
    """Turns red if: a date is set ahead of today, which would overstate freshness."""
    assert spec.content_as_of is not None
    assert spec.content_as_of <= datetime.now(UTC).date()


def test_the_allowlist_is_not_empty() -> None:
    """Partner: the parametrised checks above would pass vacuously on no sources."""
    assert len(MVP_SOURCES) >= 6
    assert all(isinstance(s.content_as_of, date) for s in MVP_SOURCES)
