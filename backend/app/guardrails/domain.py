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
# happens to mention. The match is word-bounded, so "mycitevynapp" reaches
# a product/unsupported path rather than the meta domain — the stricter,
# more correct behavior for a live query.
#
# The frontend's offline matcher (knowledgeBase.ts::matchCitevynMeta) still
# uses a looser bare-substring "citevyn" check and so recognizes NEITHER the
# word boundary NOR the aliases below. That path only runs in demo/offline
# mode — in live mode every question goes to this guardrail — so it is a
# cosmetic divergence rather than a live defect, tracked on #84 item 4.
# --- CiteVyn name recognition (#84 item 1) ---------------------------------
#
# The owner dictates questions, and speech-to-text reliably mangles "CiteVyn"
# into "sitewin", "citevin" and friends. Those questions used to hit the generic
# refusal even though the About-CiteVyn source is indexed and could answer them
# — a RECOGNITION gap, not a corpus gap.
#
# The design is shaped by one asymmetry: this guardrail ROUTES. A false positive
# does not merely fail — it produces a confidently-WRONG, confidently-CITED
# answer sourced from the CiteVyn docs, and (via canonicalization) rewrites the
# user's text on the way. A miss only makes the user rephrase.
#
# So the ONLY aliases here are single tokens that are not words in any language
# a user of this tool is likely to type. That rule is doing real work, and it was
# learned the hard way over two adversarial review rounds:
#
# ROUND 1 — "site win"/"cite win" were admitted with a BLOCKLIST of non-product
# readings ("not followed by a metric noun, not preceded by a possessive"). It
# broke five ways: "site win data" and "site win trend" (nouns not on the list),
# "site win % is up" ("%" can never match a list ending in \b), "site win-rate"
# (a hyphen dodged the guard's \s+), and "did the site win the award?" (win as a
# VERB — a reading the blocklist never modelled).
#
# ROUND 2 — replaced with a fail-closed ALLOWLIST (determiner guard + a closed
# set of product-context followers). It broke too, because Python's fixed-width
# lookbehind can only inspect the token IMMEDIATELY before the alias, so one
# adjective walks straight through:
#
#     "may the best site win!"            -> citevyn   (a common English idiom!)
#     "did Bob's site win?"               -> citevyn
#     "the recent site win cost us the deal" -> citevyn
#     "congrats on the huge site win!"    -> citevyn
#
# CONCLUSION: a phrase built from two ordinary English words cannot be
# disambiguated from ordinary English by surrounding-token rules. Both attempts
# failed against reviewers who simply wrote normal sentences. So "site win",
# "cite win" and "sight win" are NOT recognized — a deliberate, tested MISS. A
# user who says "site win" and gets the refusal can type "sitewin", which works.
# Reinstating them needs real disambiguation (an intent classifier over the whole
# utterance), not another regex guard.
#
# The separated "*vin" forms ("cite vin", "site vin") are out for the same
# reason: VIN is an ordinary English noun (Vehicle Identification Number) and
# "vin" is French for wine, so "please cite VIN and mileage" was being rewritten
# to "please CiteVyn and mileage". The single-token spellings ("citevin",
# "sitevin") stay — those are not words.
#
# Deliberately NOT phonetic/fuzzy either: an edit-distance or Metaphone tier
# widens exactly the class of false positive that costs the most, to buy
# manglings this list does not cover. Extend the list instead — it is greppable,
# each entry is a deliberate choice, and every entry has a test.
_CITEVYN_ALIASES: tuple[str, ...] = (
    "citevyn",
    "citevin",
    "citewin",
    "sitevyn",
    "sitevin",
    "sitewin",
    "sightvyn",
    "sightvin",
    "sightwin",
    # "vyn" is not a word in any language a user of this tool is likely to type,
    # so the separated spellings are safe here in a way "* vin" is not.
    r"cite[ \t-]vyn",
    r"site[ \t-]vyn",
    r"sight[ \t-]vyn",
)

# An alias inside a hostname, URL or filename is an IDENTIFIER the user is asking
# about, not the product name — rewriting "sitewin.example.com" to
# "CiteVyn.example.com" corrupts the very string the question is about. Reject a
# match preceded by a URL/path character, or followed by "." + a word character
# (a domain or extension). A sentence-final "sitewin." is followed by "." + space
# or end, so it still matches.
_IDENTIFIER_GUARD_BEFORE = r"(?<![\w./@-])"
_IDENTIFIER_GUARD_AFTER = r"(?!\.\w)"

_CITEVYN_RE = re.compile(
    _IDENTIFIER_GUARD_BEFORE
    + r"\b(?:"
    + "|".join(_CITEVYN_ALIASES)
    + r")\b"
    + _IDENTIFIER_GUARD_AFTER,
    re.IGNORECASE,
)

_PATTERNS: tuple[tuple[Domain, re.Pattern[str]], ...] = (
    (Domain.citevyn, _CITEVYN_RE),
    (Domain.claude_code, re.compile(r"\bclaude[\s-]+code\b", re.IGNORECASE)),
    (Domain.claude_api, re.compile(r"\bclaude[\s-]+api\b", re.IGNORECASE)),
    (Domain.gemini_api, re.compile(r"\bgemini(?:[\s-]+api)?\b", re.IGNORECASE)),
    (Domain.codex, re.compile(r"\bcodex\b", re.IGNORECASE)),
    (Domain.claude_api, re.compile(r"\bclaude\b", re.IGNORECASE)),
)


#: The canonical product spelling every recognized alias is rewritten to.
CANONICAL_PRODUCT_NAME = "CiteVyn"


def canonicalize_product_name(question: str) -> str:
    """Rewrite recognized CiteVyn aliases to the canonical spelling.

    Routing the alias is necessary but NOT sufficient. "what is sitewin?" routes to
    ``citevyn`` once the classifier knows the alias, but its only content word is the
    mangled token itself — which appears nowhere in the corpus — so both retrieval arms
    come back empty and the user still gets a refusal. Rewriting the alias to "CiteVyn"
    is what lets the indexed About-CiteVyn chunks actually match.

    Applies ONLY to the retrieval/generation query. The original utterance is what gets
    persisted as the user's message, so the transcript still shows what they typed.

    Uses the SAME pattern as :func:`classify_domain`, so this can never rewrite text the
    classifier would not also have routed to ``citevyn``. That shared pattern is why the
    alias list is restricted to single non-word tokens: a rewrite is destructive, and
    "may the best site win!" becoming "may the best CiteVyn!" corrupts the query on its
    way to the LLM. Identifiers are excluded too — "sitewin.example.com" is the string the
    user is asking about, not a mention of the product.
    """
    if not question:
        return question
    return _CITEVYN_RE.sub(CANONICAL_PRODUCT_NAME, question)


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
