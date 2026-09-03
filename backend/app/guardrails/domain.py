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
# made there too.
#
# The mirror is BEHAVIOURAL, not textual: its regex source deliberately differs.
# JavaScript's `\w`/`\b` are ASCII-only where Python's are Unicode-aware (so the
# JS side spells the word class `\p{L}\p{N}_` under the `u` flag), and a
# lookbehind is below the frontend's browser baseline (Safari gained it in 16.4;
# Vite 6's default target is safari16.0), so the JS side writes the BEFORE guard
# as a consumed `(?:^|[^...])` alternation. What pins the two together is
# frontend/src/lib/citevynAliases.cases.json — one question/expected corpus that
# BOTH test suites run, plus the alias-list pin. A one-sided edit that changes an
# answer fails on the other side rather than drifting silently.
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


# --- Self-referential questions (#300) --------------------------------------
#
# A question addressed to the assistant in the SECOND PERSON ("who are you?",
# "what can you do?") is a question about CiteVyn — but it never says the word
# "CiteVyn", so :func:`classify_domain` routes it ``unsupported`` and the user
# gets the generic refusal while the indexed About-CiteVyn source sits there
# able to answer it. A RECOGNITION gap, exactly like the dictation aliases
# above, and fixed the same way: rewrite the query so the existing CiteVyn
# retrieval path picks it up.
#
# Why a regex is the right tool here (unlike "site win", #84, which needed a
# classifier): this is a CLOSED list of fixed phrasings, and every one of them
# is matched against the WHOLE message. There is no ambiguity to resolve — the
# anchors do all the work.
#
# The end anchor is load-bearing. "what do you know about the Claude API rate
# limits?" opens with a listed phrasing but carries a substantive tail, so it
# must NOT be rewritten (it already routes to ``claude_api`` correctly). Only
# whitespace and sentence-final punctuation may follow the phrase -- the tail is
# the character class ``[\s,.!?]*``, byte-for-byte the one ``_GREETING_RE`` uses
# for a bare greeting, for the same reason (a prefix match would swallow real
# questions) and in the same SHAPE. The shape matters independently: the first
# version of this pattern spelled the tail ``\s*[?.!]*\s*$``, two ``\s*`` around
# a quantifier that can match empty, which backtracks QUADRATICALLY -- 63 ms of
# blocked event loop for one 4000-char message (the API's cap) against ~0.5 us
# for a real question. A single character class cannot backtrack at all.
# In particular the issue's named negative, "who are the Codex maintainers?",
# fails the anchor and keeps its ``codex`` routing.
#
# Each phrasing maps to the canonical CiteVyn question that best matches the
# section of the About-CiteVyn source it is really asking for. Retrieval AND
# generation both see the canonical form, so the answer reads as an answer to
# the question rather than to a fragment; the ORIGINAL utterance is still what
# gets persisted as the user's message, so the transcript shows what they typed.
# Dictation and mobile keyboards emit a CURLY apostrophe (U+2019), and some
# transcribers a modifier letter apostrophe (U+02BC). "what’s your name" must
# behave exactly like "what's your name" — this project exists because the owner
# dictates questions, so the smart-quote form is the COMMON case, not the exotic one.
_APOS = r"['\u2019\u02bc]"

# An optional, closed set of discourse openers. Without it #300's own symptom
# survives for the commonest natural phrasings — "hey, who are you?" is not a bare
# greeting (``_GREETING_RE`` requires the message to END after the greeting), so it
# fell through to the refusal the issue was filed about. Each opener must be followed
# by whitespace or a comma, and the whole message is still anchored at both ends, so
# "so what can you do with the Gemini API?" keeps its ``gemini_api`` routing.
_SELF_REFERENCE_OPENER = r"(?:(?:hi|hey|hello|ok|okay|so|well|um)\b[\s,]+)?"

_SELF_REFERENCE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            r"who\s+are\s+you",
            r"who(?:\s+|" + _APOS + r")re\s+you",
            r"what\s+are\s+you",
            r"who\s+am\s+i\s+(?:talking|speaking|chatting)\s+(?:to|with)",
            r"what(?:" + _APOS + r"s|\s+is)\s+your\s+name",
            r"tell\s+me\s+about\s+yourself",
            r"introduce\s+yourself",
        ),
        "What is CiteVyn?",
    ),
    (
        (
            r"what\s+can\s+you\s+do",
            r"what\s+do\s+you\s+do",
            r"what\s+are\s+you\s+for",
            r"how\s+can\s+you\s+help(?:\s+me)?",
            r"what\s+can\s+you\s+help\s+(?:me\s+)?with",
            r"help",
        ),
        "What can CiteVyn do?",
    ),
    (
        (
            r"what\s+do\s+you\s+know(?:\s+about)?",
            r"what\s+do\s+you\s+cover",
            r"what\s+topics?\s+do\s+you\s+cover",
            r"what\s+can\s+you\s+answer",
            r"what\s+can\s+i\s+ask(?:\s+you)?",
            r"what\s+sources?\s+do\s+you\s+use",
        ),
        "What does CiteVyn cover?",
    ),
)

_SELF_REFERENCE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (
        re.compile(
            r"^\s*" + _SELF_REFERENCE_OPENER + r"(?:" + "|".join(phrasings) + r")[\s,.!?]*$",
            re.IGNORECASE,
        ),
        canonical,
    )
    for phrasings, canonical in _SELF_REFERENCE_RULES
)


def canonicalize_self_reference(question: str) -> str:
    """Rewrite a self-referential question into the CiteVyn question it means.

    "who are you?" becomes "What is CiteVyn?"; "what do you cover?" becomes
    "What does CiteVyn cover?". Anything not on the closed list above — including
    a listed phrasing that carries a substantive tail ("what can you do with the
    Gemini API?") — is returned VERBATIM, so every question that routes correctly
    today keeps routing exactly as it did.

    Pure and deterministic. Call it on the ROUTING/RETRIEVAL query only; the
    user's original utterance is what gets persisted.
    """
    if not question or not question.strip():
        return question
    for pattern, canonical in _SELF_REFERENCE_PATTERNS:
        if pattern.match(question):
            return canonical
    return question


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
