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
# into "sitewin", "site win", "citevin" and friends. Those questions used to hit
# the generic refusal even though the About-CiteVyn source is indexed and could
# answer them — a RECOGNITION gap, not a corpus gap.
#
# The design is shaped by one asymmetry: this guardrail ROUTES. A false positive
# does not merely fail — it produces a confidently-WRONG, confidently-CITED
# answer sourced from the CiteVyn docs. A miss only makes the user rephrase. So
# the aliases are split by how safe they are to match:
#
# * UNAMBIGUOUS — no English word is spelled this way, either because the whole
#   alias is one invented token ("sitewin", "citevin") or because its second
#   part is not a word ("cite vyn", "site vin"). A word-bounded match carries no
#   realistic false-positive risk.
# * AMBIGUOUS — two ordinary English words ("site win", "cite win"). "What is
#   our site win rate?" and "did the site win the award?" are ordinary English,
#   not CiteVyn questions. These match only inside a positively product-shaped
#   frame; see :data:`_CITEVYN_AMBIGUOUS_RE`.
#
# The ambiguous tier FAILS CLOSED, and that polarity is the whole point. The
# first attempt BLOCKLISTED the non-product readings — "not followed by a metric
# noun, not preceded by a possessive" — and adversarial review broke it in five
# different ways within minutes:
#
#     "site win data for Q3"       -> citevyn   (noun not on the metric list)
#     "site win trend"             -> citevyn   (ditto)
#     "site win % is up"           -> citevyn   ("%" can never match a list that
#                                                ends in \b — it is not a word char)
#     "what was the site win-rate" -> citevyn   (hyphen dodged the "\s+" in the guard)
#     "did the site win the award?"-> citevyn   ("win" as a VERB — a reading the
#                                                blocklist never modelled at all)
#
# A blocklist of English continuations is unbounded by construction: every noun
# added to the list leaves the next one open. An ALLOWLIST is finite and
# auditable, so the ambiguous tier now requires an explicitly product-shaped
# context and rejects everything else.
#
# Deliberately NOT phonetic/fuzzy: an edit-distance or Metaphone tier cannot be
# bounded tightly enough here to be worth it. "site win" already sits one token
# away from ordinary English, so a fuzzy tier would widen exactly the class of
# false positive that costs the most, to buy manglings this list does not cover.
# Extend the lists instead — they are greppable, each entry is a deliberate
# choice, and every entry has a test.
_CITEVYN_UNAMBIGUOUS_ALIASES: tuple[str, ...] = (
    "citevyn",
    "citevin",
    "citewin",
    "sitevyn",
    "sitevin",
    "sitewin",
    "sightvyn",
    "sightvin",
    "sightwin",
    r"cite[\s-]vyn",
    r"cite[\s-]vin",
    r"site[\s-]vyn",
    r"site[\s-]vin",
    r"sight[\s-]vyn",
    r"sight[\s-]vin",
)

# Two ordinary English words. Separator is a single space only — a hyphen
# ("site win-rate") is overwhelmingly the compound-noun reading, never the name.
_CITEVYN_AMBIGUOUS_ALIASES: tuple[str, ...] = (
    r"cite\s+win",
    r"site\s+win",
    r"sight\s+win",
)

# A determiner immediately before the alias marks a noun phrase or a verb
# clause, never the product name: "THE site win rate", "did THE site win the
# award?", "A site win is great". Each lookbehind must be individually
# fixed-width, which is what Python's ``re`` requires.
_DETERMINERS = (
    "the",
    "a",
    "an",
    "our",
    "my",
    "your",
    "his",
    "her",
    "its",
    "their",
    "this",
    "that",
    "each",
    "every",
    "any",
    "no",
    "another",
)
_DETERMINER_GUARD = "".join(rf"(?<!\b{d}\s)" for d in _DETERMINERS)

# What may legitimately FOLLOW the product name. Anything not on this list —
# including any other noun, "%", or a hyphen — rejects the match. This is the
# fail-closed half: an unanticipated continuation is treated as ordinary
# English, so the guardrail errs toward a cheap miss rather than a costly
# false hit.
#
# A LOOKAHEAD, not part of the match, so canonicalization replaces only the
# alias itself: "site win pro" -> "CiteVyn pro", never "CiteVyn".
_PRODUCT_CONTEXT_FOLLOWER = (
    r"(?="
    r"[?!.,;:]"  # terminal punctuation — "what is site win?"
    r"|$"  # end of input — "tell me about site win"
    # ...or a word that only makes sense about the PRODUCT.
    r"|\s+(?:pro|free|paid|pricing|price|cost|costs|subscription|membership|demo"
    r"|cover|covers|covering|support|supports|index|indexes|source|sources"
    r"|cite|cites|citation|citations|accurate|accuracy|hallucinate|hallucinates"
    r"|trustworthy|trust|work|works|do|does|answer|answers|use|uses|used)\b"
    r")"
)

_CITEVYN_RE = re.compile(
    r"\b(?:"
    + "|".join(_CITEVYN_UNAMBIGUOUS_ALIASES)
    + r")\b"
    + r"|"
    + _DETERMINER_GUARD
    + r"\b(?:"
    + "|".join(_CITEVYN_AMBIGUOUS_ALIASES)
    + r")\b"
    + _PRODUCT_CONTEXT_FOLLOWER,
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

    Uses the same guarded pattern as :func:`classify_domain`, so an ordinary phrase that
    merely contains an ambiguous alias ("our site win rate") is left untouched — this can
    never rewrite text the classifier would not also have routed.
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
