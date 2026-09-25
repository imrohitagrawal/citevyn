"""Source allowlist for the ingestion worker.

The MVP source list lives in code so the worker is hermetic
and reproducible — there is no separate ``sources.yaml`` for
the worker to read at boot. The lock between this list and the
corpus actually ingested is the ``source_version_hash`` column
on :class:`IndexVersion`, which the worker derives from the
bytes of these source docs (see
:func:`app.worker.cli.content_version_hash`). Editing any doc
below therefore changes the hash on the next run, which changes
the answer-cache key and retires stale cached answers. There is
no constant for an operator to bump — the content *is* the
version.

Design notes
------------
* ``SourceSpec`` is a frozen dataclass — the allowlist is
  compile-time fixed, not runtime-mutable.
* The MVP sources match the demo catalog in
  :func:`tests.conftest.seed_catalog` so a freshly-built
  ``candidate`` index contains the same docs the demo runs
  against. The number of them is deliberately NOT written
  down here — it is ``len(MVP_SOURCES)``. This line used to
  state a count, and that count stayed wrong from the day a
  further source shipped until #420 removed it. Do not put
  one back: ``test_worker_allowlist.py`` rejects any count
  in this file's prose that contradicts ``MVP_SOURCES``,
  and it cannot tell a live claim from a quoted one.
* The single source of truth is :data:`MVP_SOURCES`. Tests
  must reference this list (not redefine it) so a future
  source addition only changes one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class SourceSpec:
    """One allowed ingestion source.

    ``name`` is the short identifier used by the
    :class:`IngestionJob.source_name` column and the admin
    list endpoint. ``product_area`` is the product the doc
    belongs to (used for exact-term lookup scoping).
    ``fetcher`` is the implementation key — ``"local"`` for
    the test fixtures and ``"http"`` for production URLs.
    The CLI switches on it in :mod:`app.worker.cli`.
    """

    name: str
    product_area: str
    title: str
    fetcher: str
    location: str
    # Official upstream URL this source paraphrases. Stamped onto
    # :class:`Document.source_url` so citations resolve to a real source. The
    # source docs under ``app/worker/sources`` are original, license-clean
    # summaries — not verbatim copies — of the page at this URL (see
    # docs/ADR/0003-embeddings-provider.md). They live under ``app/`` (not
    # ``tests/``) so they ship inside the prod worker image and ``run`` can
    # ingest them (#92). Defaults to "" so ad-hoc ``SourceSpec`` constructions
    # (tests) need not supply it.
    source_url: str = ""
    # "Docs as of" (ADR-0005 §6): the day this repo's copy of the source was last
    # updated, and the SHA-256 of that exact text. The sources are hand-written
    # summaries shipped in the repo, not fetched live, so the ingest run's clock
    # would say "today" about months-old content; this date is the honest one.
    # tests/test_source_content_dates.py fails when the file changes and these two
    # are not updated with it. None for ad-hoc specs (tests), which then carry no
    # date rather than an invented one.
    content_as_of: date | None = None
    content_sha256: str | None = None


# ---------------------------------------------------------------------------
# MVP source list
# ---------------------------------------------------------------------------
#
# Keep in lock-step with the demo catalog used by
# :func:`tests.conftest.seed_catalog`. Each entry, and why it is here:
#
#   Claude API Reference       product doc
#   Claude Code Reference      product doc
#   Codex Reference            product doc
#   Gemini API Reference       product doc
#   About CiteVyn              so questions about CiteVyn itself
#                              (Pro/membership/coverage/trust) flow through the
#                              normal retrieval + citation path instead of being
#                              refused off-domain (#49)
#   AI Concepts and Glossary   so conceptual questions ("what is an LLM?")
#                              answer instead of refusing (#112 follow-up)
#
# This enumeration is not decoration: ``test_worker_allowlist.py`` asserts every
# shipped ``SourceSpec.title`` appears in it, so adding a source without adding a
# line here turns the suite red. It carries no count, because the prose count is
# exactly what went stale before (#420) — the count is ``len(MVP_SOURCES)``.
# Adding a source is a deliberate operation; an SRE adds it here AND
# seeds the test fixture so the demo catalog stays consistent.

MVP_SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        name="claude_api",
        product_area="claude_api",
        title="Claude API Reference",
        fetcher="local",
        location="app/worker/sources/claude_api.md",
        source_url="https://docs.anthropic.com/en/api/overview",
        content_as_of=date(2026, 7, 18),
        content_sha256="4480285f11b4ad1694dd2ca3623b51075b8a410d777d7c0773d4dee9500e33cf",
    ),
    SourceSpec(
        name="claude_code",
        product_area="claude_code",
        title="Claude Code Reference",
        fetcher="local",
        location="app/worker/sources/claude_code.md",
        source_url="https://docs.anthropic.com/en/docs/claude-code/overview",
        content_as_of=date(2026, 7, 19),
        content_sha256="5ad05394cad5122cdada82bbb3a900908f510ec228ebe43cb4d0187ccd4ab11c",
    ),
    SourceSpec(
        name="codex",
        product_area="codex",
        title="Codex Reference",
        fetcher="local",
        location="app/worker/sources/codex.md",
        source_url="https://developers.openai.com/codex/",
        content_as_of=date(2026, 7, 19),
        content_sha256="e7daf653f771c8687d68c8f39c6291302c1a94598811741afaa0b0b36b4c9b5b",
    ),
    SourceSpec(
        name="gemini_api",
        product_area="gemini_api",
        title="Gemini API Reference",
        fetcher="local",
        location="app/worker/sources/gemini_api.md",
        source_url="https://ai.google.dev/gemini-api/docs",
        content_as_of=date(2026, 7, 18),
        content_sha256="be6a4b89de4d23fc99c1a846cc649c5fad565092c4f2db51a5bc97ab9a03e663",
    ),
    SourceSpec(
        name="citevyn",
        product_area="citevyn",
        title="About CiteVyn",
        fetcher="local",
        location="app/worker/sources/citevyn.md",
        # CiteVyn describes itself — there is no external upstream doc, so the
        # citation points at CiteVyn's own about page. A RELATIVE path keeps it
        # host-agnostic and never references a domain we don't own (a hard-coded
        # citevyn.com/app could later be squatted). TODO(deploy): confirm the
        # final /about route once CiteVyn is hosted.
        source_url="/about",
        content_as_of=date(2026, 9, 1),
        content_sha256="9252d5784d81d2ab21a066d27f0d5565b7d00911b8440880987676544acdcbef",
    ),
    SourceSpec(
        # Cross-cutting AI concepts/glossary (#112 follow-up): lets CiteVyn answer
        # "what is an LLM?", "is Codex an LLM?", "what do the different models mean?" from a
        # grounded, cited source instead of refusing. Original plain-language explainer; the
        # citation points at CiteVyn's own /about (there is no single upstream doc for a
        # general glossary, and it must never reference a domain CiteVyn does not own).
        name="concepts",
        product_area="concepts",
        title="AI Concepts and Glossary",
        fetcher="local",
        location="app/worker/sources/concepts.md",
        source_url="/about",
        content_as_of=date(2026, 9, 25),
        content_sha256="9c60b7d3bda7c1f62e831489277adb36f528a53d1a89a522ad8ed9475dfd98ac",
    ),
)


def get_source(name: str) -> SourceSpec:
    """Return the source spec for ``name`` or raise :class:`KeyError`.

    The CLI uses this for ``citevyn-worker run --source codex``
    so a typo'd name fails fast with a clear error.
    """
    for spec in MVP_SOURCES:
        if spec.name == name:
            return spec
    raise KeyError(f"unknown source: {name!r}. Known: {[s.name for s in MVP_SOURCES]}")


def list_source_names() -> tuple[str, ...]:
    """Return the ordered list of source names."""
    return tuple(spec.name for spec in MVP_SOURCES)


__all__ = [
    "MVP_SOURCES",
    "SourceSpec",
    "get_source",
    "list_source_names",
]
