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
# The frontend's offline matcher (knowledgeBase.ts::matchCitevynMeta) MIRRORS
# this pattern via frontend/src/lib/citevynAliases.ts — same canonical branch,
# same alias list, same identifier guards (#84 item 4). It is a hand-kept copy
# because the demo path never reaches this module, so any edit below must be
# made there too; both sides pin the alias list in a test, so a one-sided edit
# fails rather than drifting silently.
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
#
# NOTE: the canonical spelling is deliberately NOT in this list. It is matched by its
# own un-guarded branch below, so the identifier guards — which exist for the
# lower-confidence ALIASES — cannot narrow the pre-existing literal-name behaviour.
_CITEVYN_ALIASES: tuple[str, ...] = (
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

# An alias inside a hostname, URL, email, ticket id or filename is an IDENTIFIER the
# user is asking about, not the product name — rewriting "sitewin.example.com" to
# "CiteVyn.example.com" corrupts the very string the question is about.
#
# The two guards are deliberately SYMMETRIC: an alias can be the trailing segment of
# an identifier ("docs.sitewin") or the leading one ("sitewin@example.com",
# "SITEWIN-1234", "sitewin:8080", "sitewin/main", "sitewin==1.2.3"). Guarding only one
# side leaves the other open, which is what a review round caught.
#
# The AFTER guard rejects when a run of identifier punctuation leads to another word
# character. Sentence-final "sitewin." still matches: the "." is followed by a space or
# end of input, so no word character follows.
#
# These apply to the ALIASES ONLY. The canonical "citevyn" keeps its original
# un-guarded ``\bcitevyn\b`` match so this change cannot narrow behaviour that already
# worked ("is citevyn.com free?", "anti-citevyn rant" — both routed to citevyn before).
_IDENTIFIER_GUARD_BEFORE = r"(?<![\w./@:=-])"
_IDENTIFIER_GUARD_AFTER = r"(?![\w./@:=-]*\w)"

_CITEVYN_RE = re.compile(
    # Branch 1 — the canonical spelling, byte-for-byte as it was before aliases
    # existed. Un-guarded on purpose (see _IDENTIFIER_GUARD_* above).
    r"\bcitevyn\b"
    # Branch 2 — the speech-to-text aliases, identifier-guarded on both sides.
    r"|" + _IDENTIFIER_GUARD_BEFORE + r"\b(?:" + "|".join(_CITEVYN_ALIASES) + r")\b"
    r"" + _IDENTIFIER_GUARD_AFTER,
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


# --- Ambiguous two-word aliases (#84 follow-up) ----------------------------
#
# "site win" / "cite win" / "sight win" are what dictation produces for "CiteVyn"
# most often, and they are ALSO two ordinary English words. They are deliberately
# absent from :data:`_CITEVYN_ALIASES` above: three adversarial rounds established
# that no surrounding-token rule separates them from ordinary English — "may the
# best site win!" broke the last attempt.
#
# They are exposed here as a SEPARATE, opt-in surface so the orchestrator can run a
# real intent check over the whole utterance (which is what the reviews prescribed)
# before treating one as the product. NOTHING in this module routes on them:
# :func:`classify_domain` and :func:`canonicalize_product_name` are unaffected, so the
# guardrail stays pure, deterministic, and as conservative as it was.
_CITEVYN_AMBIGUOUS_RE = re.compile(r"\b(?:cite|site|sight)\s+win\b", re.IGNORECASE)


def contains_ambiguous_citevyn_alias(text: str) -> bool:
    """True when ``text`` contains a two-word CiteVyn homophone.

    A cheap, deterministic PREFILTER — it says only "this is worth asking about", never
    "this is CiteVyn". Ordinary English trips it constantly by design ("did the site
    win?"), which is exactly why the caller must disambiguate before acting.
    """
    return bool(text) and bool(_CITEVYN_AMBIGUOUS_RE.search(text))


def canonicalize_ambiguous_alias(text: str) -> str:
    """Rewrite two-word CiteVyn homophones to the canonical spelling.

    Call ONLY after an intent check has confirmed the utterance is about the product —
    unguarded, this turns "may the best site win!" into "may the best CiteVyn!".
    """
    if not text:
        return text
    return _CITEVYN_AMBIGUOUS_RE.sub(CANONICAL_PRODUCT_NAME, text)


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
