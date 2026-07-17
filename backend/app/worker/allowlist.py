"""Source allowlist for the ingestion worker.

The MVP source list lives in code so the worker is hermetic
and reproducible — there is no separate ``sources.yaml`` for
the worker to read at boot. The lock between this list and
the actual production source feed is the ``source_version_hash``
column on :class:`IndexVersion`: when an external operator
publishes a new "official docs snapshot", they update
:meth:`SOURCE_VERSION_HASH` and the next worker run produces a
new ``IndexVersion(index_version=..., source_version_hash=...)``.

Design notes
------------
* ``SourceSpec`` is a frozen dataclass — the allowlist is
  compile-time fixed, not runtime-mutable.
* The five MVP sources match the demo catalog in
  :func:`tests.conftest.seed_catalog` so a freshly-built
  ``candidate`` index contains the same docs the demo runs
  against.
* The single source of truth is :data:`MVP_SOURCES`. Tests
  must reference this list (not redefine it) so a future
  source addition only changes one place.
"""

from __future__ import annotations

from dataclasses import dataclass


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


# ---------------------------------------------------------------------------
# MVP source list
# ---------------------------------------------------------------------------
#
# Keep in lock-step with the demo catalog used by
# :func:`tests.conftest.seed_catalog`. The five sources are the four
# product docs (Claude API, Claude Code, Codex, Gemini API) plus the
# "About CiteVyn" doc — so questions about CiteVyn itself
# (Pro/membership/coverage/trust) flow through the normal
# retrieval + citation path instead of being refused off-domain.
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
    ),
    SourceSpec(
        name="claude_code",
        product_area="claude_code",
        title="Claude Code Reference",
        fetcher="local",
        location="app/worker/sources/claude_code.md",
        source_url="https://docs.anthropic.com/en/docs/claude-code/overview",
    ),
    SourceSpec(
        name="codex",
        product_area="codex",
        title="Codex Reference",
        fetcher="local",
        location="app/worker/sources/codex.md",
        source_url="https://openai.com/codex/",
    ),
    SourceSpec(
        name="gemini_api",
        product_area="gemini_api",
        title="Gemini API Reference",
        fetcher="local",
        location="app/worker/sources/gemini_api.md",
        source_url="https://ai.google.dev/gemini-api/docs",
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
