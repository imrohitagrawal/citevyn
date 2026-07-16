"""Slice 4 domain guardrail.

The guardrail classifies the incoming question into one of the four
supported product areas (``claude_api``, ``claude_code``, ``codex``,
``gemini_api``), the ``citevyn`` about-the-product domain (questions
about CiteVyn itself — Pro/membership/coverage/trust — answered from the
indexed "About CiteVyn" source), or marks it ``unsupported``. It runs
before any retrieval or LLM cost, so off-domain questions are refused
cheaply.

The classifier is a small, deterministic keyword + alias matcher. It
exists so the answer pipeline always has a domain to pass to the
retrievers; the seam is the single public function
:func:`classify_domain` and the rule can be swapped for an LLM-backed
classifier without changing the call site.
"""

from __future__ import annotations

import enum
import re


class Domain(enum.StrEnum):
    claude_api = "claude_api"
    claude_code = "claude_code"
    codex = "codex"
    gemini_api = "gemini_api"
    citevyn = "citevyn"
    unsupported = "unsupported"
    # A neutral, response-only domain. ``classify_domain`` never returns it;
    # the orchestrator stamps it on a bare-greeting reply so the greeting no
    # longer borrows ``unsupported`` (which would break the
    # ``domain == unsupported`` ⟺ ``unsupported == true`` invariant — #89).
    # Not a retrievable product area, so it is absent from ``ALLOWED_DOMAINS``
    # and ``is_unsupported`` returns ``False`` for it.
    general = "general"


ALLOWED_DOMAINS: frozenset[Domain] = frozenset(
    {
        Domain.claude_api,
        Domain.claude_code,
        Domain.codex,
        Domain.gemini_api,
        Domain.citevyn,
    }
)


# Patterns are ordered from most specific to least specific. The first
# match wins. Word boundaries prevent ``claude`` matching ``claude_code``
# twice and ``codex`` matching ``codex`` substrings inside other words.
#
# ``citevyn`` is checked FIRST: any question that names CiteVyn is a
# question about the product itself (Pro, coverage, "does CiteVyn support
# Gemini?"), so it must win over a product keyword the same sentence
# happens to mention. This is intentionally close to the frontend
# matcher's narrow "mentions CiteVyn" guard
# (knowledgeBase.ts::matchCitevynMeta), though the backend uses a word
# boundary (\bcitevyn\b) where the frontend uses a looser substring
# check — so "mycitevynapp" reaches a product/unsupported path here but
# would trip the frontend guard. The whole-word match is the stricter,
# more correct behavior for a live query.
_PATTERNS: tuple[tuple[Domain, re.Pattern[str]], ...] = (
    (Domain.citevyn, re.compile(r"\bcitevyn\b", re.IGNORECASE)),
    (Domain.claude_code, re.compile(r"\bclaude[\s-]+code\b", re.IGNORECASE)),
    (Domain.claude_api, re.compile(r"\bclaude[\s-]+api\b", re.IGNORECASE)),
    (Domain.gemini_api, re.compile(r"\bgemini(?:[\s-]+api)?\b", re.IGNORECASE)),
    (Domain.codex, re.compile(r"\bcodex\b", re.IGNORECASE)),
    (Domain.claude_api, re.compile(r"\bclaude\b", re.IGNORECASE)),
)


def classify_domain(question: str) -> Domain:
    """Return the resolved domain for ``question``.

    Empty or whitespace-only input returns :attr:`Domain.unsupported`.
    The classifier does not consult the database, the LLM, or the
    network — it is safe to call on every request.
    """
    if not question or not question.strip():
        return Domain.unsupported
    for domain, pattern in _PATTERNS:
        if pattern.search(question):
            return domain
    return Domain.unsupported


# Specific product patterns for multi-hop DETECTION only (:func:`classify_domains`).
# Excludes the ``citevyn`` meta-domain (its own short-circuit) and the generic
# ``\bclaude\b`` catch-all — the catch-all is a loose single-domain fallback, too
# weak to be a confident SECOND-product signal (a real cross-product question names
# its products specifically, e.g. "Claude API and Gemini"), and counting it would
# over-collect on a sentence that repeats "claude" after "claude code".
_MULTIHOP_PATTERNS: tuple[tuple[Domain, re.Pattern[str]], ...] = tuple(
    (d, p) for d, p in _PATTERNS if d is not Domain.citevyn and p.pattern != r"\bclaude\b"
)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def classify_domains(question: str) -> list[Domain]:
    """All DISTINCT product domains a question names — for multi-hop decomposition.

    A cross-product question ("compare the rate limits of the Claude API and
    Gemini") names two products; :func:`classify_domain` returns only the FIRST,
    so the retriever scopes to one area and the other product's answer is missed.
    This returns every named product area so the orchestrator can retrieve each.

    Rules (mirroring :func:`classify_domain`'s precedence):

    * **CiteVyn short-circuits** — a question that names CiteVyn is a question about
      the product itself (#49: "does CiteVyn support Gemini?" is about CiteVyn's
      coverage, not the Gemini API), so it returns ``[Domain.citevyn]`` regardless of
      any product keywords in the same sentence, and never triggers multi-hop.
    * Otherwise, collect distinct product domains from **non-overlapping** matches,
      most-specific pattern first: "claude code permissions" yields ``[claude_code]``,
      not ``[claude_code, claude_api]`` — the generic ``\\bclaude\\b`` catch-all is
      skipped where its match overlaps the already-matched "claude code" span.

    Deterministic, no I/O. Returns ``[]`` for empty/whitespace input.
    """
    if not question or not question.strip():
        return []
    for domain, pattern in _PATTERNS:
        if domain is Domain.citevyn and pattern.search(question):
            return [Domain.citevyn]
    matched_spans: list[tuple[int, int]] = []
    domains: list[Domain] = []
    for domain, pattern in _MULTIHOP_PATTERNS:
        for m in pattern.finditer(question):
            span = m.span()
            if any(_overlaps(span, s) for s in matched_spans):
                continue
            matched_spans.append(span)
            if domain not in domains:
                domains.append(domain)
    return domains


def is_unsupported(domain: Domain) -> bool:
    return domain is Domain.unsupported
