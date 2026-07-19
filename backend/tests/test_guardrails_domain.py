"""Domain guardrail classification tests."""

from __future__ import annotations

import pytest

from app.guardrails.domain import (
    ALLOWED_DOMAINS,
    Domain,
    canonicalize_product_name,
    classify_domain,
    classify_domains,
    is_unsupported,
)

# ---------------------------------------------------------------------------
# classify_domains — multi-hop detection (Phase 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question,expected",
    [
        # Single product → single-element list (no multi-hop).
        ("How do I configure Claude Code permissions?", [Domain.claude_code]),
        ("What is the Claude API rate limit?", [Domain.claude_api]),
        ("codex --model flag", [Domain.codex]),
        # The generic bare-"claude" catch-all is NOT a multi-hop signal.
        ("how do I use Claude?", []),
        # Cross-product → both, in most-specific-first order (the Phase-3 gap).
        (
            "How do the rate limits compare between the Claude API and Gemini?",
            [Domain.claude_api, Domain.gemini_api],
        ),
        (
            "How does authentication differ between the Gemini API and the Claude API?",
            [Domain.claude_api, Domain.gemini_api],
        ),
        # 'claude code' must NOT also pull claude_api from the \bclaude\b catch-all.
        ("Claude Code permissions and Codex flags", [Domain.claude_code, Domain.codex]),
        # Empty → [].
        ("", []),
        ("   ", []),
    ],
)
def test_classify_domains_multi(question: str, expected: list[Domain]) -> None:
    assert classify_domains(question) == expected


def test_classify_domains_citevyn_short_circuits_over_products() -> None:
    """#49 invariant preserved: a question naming CiteVyn is about CiteVyn itself,
    even when it also names a product — it must NOT decompose into product multi-hop."""
    assert classify_domains("Does CiteVyn cover the Gemini API?") == [Domain.citevyn]
    assert classify_domains("Which is better in CiteVyn, Codex or Claude Code?") == [Domain.citevyn]


def test_classify_domains_agrees_with_classify_domain_on_single_product() -> None:
    """For a single-product question, the first multi-domain entry equals the
    single classifier's result (they share the same patterns)."""
    for q in ("Claude API rate limit", "codex help", "Gemini API auth", "Claude Code permissions"):
        doms = classify_domains(q)
        assert doms and doms[0] is classify_domain(q)


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What model should I use for the Claude API?", Domain.claude_api),
        ("claude api rate limits", Domain.claude_api),
        ("How do I configure Claude Code permissions?", Domain.claude_code),
        ("claude-code settings", Domain.claude_code),
        ("What is the --model flag for codex?", Domain.codex),
        ("codex --help output", Domain.codex),
        ("Gemini API rate limits", Domain.gemini_api),
        ("gemini-api streaming", Domain.gemini_api),
        ("gemini usage", Domain.gemini_api),
        # CiteVyn-meta questions (#49): about the product itself.
        ("What do I get with CiteVyn Pro?", Domain.citevyn),
        ("Which tools does CiteVyn cover?", Domain.citevyn),
        ("Is CiteVyn accurate or does it hallucinate?", Domain.citevyn),
        ("what is citevyn", Domain.citevyn),
    ],
)
def test_classify_domain_positive(question: str, expected: Domain) -> None:
    assert classify_domain(question) is expected


@pytest.mark.parametrize(
    "question",
    [
        "Does CiteVyn support the Gemini API?",
        "Can CiteVyn answer Claude Code questions?",
        "Which is better in CiteVyn, Codex or Claude?",
    ],
)
def test_classify_domain_citevyn_wins_over_product_mention(question: str) -> None:
    """A question that names CiteVyn is about the product itself even when it
    also mentions a product keyword — ``citevyn`` is checked first."""
    assert classify_domain(question) is Domain.citevyn


@pytest.mark.parametrize(
    "question,expected",
    [
        # ``\bcitevyn\b`` is a whole-word match: it must NOT fire on the
        # letters embedded in another token, and a product keyword in the
        # same text should then win normally.
        ("recitevynize the paragraph", Domain.unsupported),
        ("mycitevynapp gemini api settings", Domain.gemini_api),
    ],
)
def test_classify_domain_citevyn_requires_word_boundary(question: str, expected: Domain) -> None:
    assert classify_domain(question) is expected


# ---------------------------------------------------------------------------
# CiteVyn name recognition — speech-to-text aliases (#84 item 1)
# ---------------------------------------------------------------------------
#
# The owner dictates questions and speech-to-text reliably mangles "CiteVyn".
# Those questions used to refuse even though the About-CiteVyn source is indexed
# and can answer them — a RECOGNITION gap, not a corpus gap.
#
# The asymmetry that shapes these tests: this guardrail ROUTES, so a false
# positive produces a confidently-WRONG, confidently-CITED answer sourced from
# the CiteVyn docs. A MISS just makes the user rephrase. Every alias below is
# therefore paired with a false-positive guard, and the guard tests matter more
# than the happy-path ones.


@pytest.mark.parametrize(
    "question",
    [
        # --- unambiguous: single tokens that are not English words ---
        "what is sitewin?",
        "what is citevin?",
        "what is sitevyn?",
        "what is sitevin?",
        "what is citewin?",
        "what is sightvyn?",
        "Is SiteWin free to use?",
        # --- unambiguous: two tokens, second is not an English word ---
        "what is cite vyn?",
        "what is site vyn?",
        "what is cite vin?",
        "what is site vin?",
        "what is sight vyn?",
        # --- hyphenated ---
        "what is cite-vyn?",
        "what is site-vyn?",
        # --- ambiguous pair, but with no metric noun following ---
        "what is site win?",
        "what does site win cover?",
        "is cite win accurate?",
    ],
)
def test_classify_domain_recognizes_citevyn_aliases(question: str) -> None:
    """A mangled CiteVyn name still routes to the ``citevyn`` domain, so the
    indexed About-CiteVyn source can answer instead of the generic refusal."""
    assert classify_domain(question) is Domain.citevyn


@pytest.mark.parametrize(
    "question",
    [
        # The owner's named false positives: ordinary analytics phrasing.
        "what is our site win rate?",
        "the site win percentage",
        # Same shape, other metric nouns.
        "improve the site win ratio",
        "site win conversion for this quarter",
        "what is the site win probability?",
        "cite win rate in the report",
        # Possessives are a strong "this is my business metric" signal.
        "our site win numbers are up",
        "my site win streak",
        # Plural is a different word and must not match at all.
        "how many site wins did we have?",
        # Embedded in a larger token — the word-boundary rule still holds.
        "mysitewinapp settings",
    ],
)
def test_classify_domain_ambiguous_aliases_do_not_false_positive(question: str) -> None:
    """A false hit here yields a confidently-cited answer about the WRONG subject,
    so an ordinary phrase that merely contains "site win" must never route to
    ``citevyn``. A miss is far cheaper than a false hit."""
    assert classify_domain(question) is not Domain.citevyn


@pytest.mark.parametrize(
    "question", ["our site win", "my site win", "your site win", "their site win", "its site win"]
)
def test_possessive_guard_blocks_ambiguous_alias_on_its_own(question: str) -> None:
    """The possessive guard must hold WITHOUT help from the metric-noun guard, so a
    later edit cannot drop one and have the other mask the regression."""
    assert classify_domain(question) is not Domain.citevyn


@pytest.mark.parametrize(
    "question", ["site win rate", "site win percentage", "site win odds", "site win margin"]
)
def test_metric_noun_guard_blocks_ambiguous_alias_on_its_own(question: str) -> None:
    """Mirror of the above: the metric-noun guard must hold without a possessive."""
    assert classify_domain(question) is not Domain.citevyn


@pytest.mark.parametrize(
    "question",
    [
        # Leading boundary: the alias sits inside a longer word.
        "website winner announcement",
        "offsite winter retreat",
        "campsite winds tonight",
        "parasite winter cycle",
        "exquisite wine pairing",
        "requisite winning margin",
        "composite winding diagram",
        # Trailing boundary: the alias is a prefix of a longer word.
        "a site window manager",
        "opposite window frame",
    ],
)
def test_word_boundaries_protect_against_incidental_substrings(question: str) -> None:
    """ "site win" occurs as a substring inside plenty of ordinary English
    ("webSITE WINner", "offSITE WINter"). The word boundaries, not the guards, are
    what stop those — keep them proven separately."""
    assert classify_domain(question) is not Domain.citevyn


@pytest.mark.parametrize(
    "question", ["site win", "what is site win", "site win pricing", "SITE WIN", "Site-Win trust"]
)
def test_ambiguous_alias_still_matches_when_no_guard_applies(question: str) -> None:
    """The guards must not be so broad that the alias never fires — that would make
    the fix a no-op for exactly the phrasing the owner dictates."""
    assert classify_domain(question) is Domain.citevyn


@pytest.mark.parametrize(
    "question,expected",
    [
        ("what is sitewin?", "what is CiteVyn?"),
        ("what is site win?", "what is CiteVyn?"),
        ("Is SiteWin free to use?", "Is CiteVyn free to use?"),
        ("what is cite-vyn?", "what is CiteVyn?"),
        ("does sitewin cover gemini?", "does CiteVyn cover gemini?"),
        # Already canonical → unchanged (idempotent).
        ("What is CiteVyn?", "What is CiteVyn?"),
        # No alias → byte-for-byte identical.
        ("What is the Claude API rate limit?", "What is the Claude API rate limit?"),
        ("", ""),
    ],
)
def test_canonicalize_product_name(question: str, expected: str) -> None:
    """Routing the alias is not enough — "what is sitewin?" has no content word that
    appears in the corpus, so retrieval returns nothing and the user still gets a
    refusal. Canonicalizing is what makes the indexed About-CiteVyn chunks match."""
    assert canonicalize_product_name(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "what is our site win rate?",
        "the site win percentage",
        "website winner announcement",
        "how many site wins did we have?",
        "our site win",
    ],
)
def test_canonicalize_leaves_non_citevyn_text_untouched(question: str) -> None:
    """The rewriter shares the guarded pattern with the classifier, so it can never
    rewrite text the classifier would not also have routed to ``citevyn``. Silently
    turning "our site win rate" into "our CiteVyn rate" would corrupt the query."""
    assert canonicalize_product_name(question) == question


def test_canonicalize_agrees_with_classify_domain() -> None:
    """The invariant that keeps the two from drifting: a question is rewritten if and
    only if it routes to ``citevyn``."""
    samples = [
        "what is sitewin?",
        "site win pricing",
        "our site win rate",
        "website winner",
        "What is CiteVyn?",
        "Claude Code permissions",
        "how many site wins did we have?",
    ]
    for q in samples:
        rewritten = canonicalize_product_name(q) != q
        routed = classify_domain(q) is Domain.citevyn
        # A question already spelled canonically routes to citevyn but needs no rewrite.
        assert not rewritten or routed, f"{q!r} was rewritten but does not route to citevyn"


def test_classify_domains_short_circuits_on_an_alias_too() -> None:
    """The multi-hop decomposer shares the citevyn pattern, so an aliased question
    that also names a product is still a question ABOUT CiteVyn (#49) — it must not
    fan out to the named product."""
    assert classify_domains("does sitewin cover the gemini api?") == [Domain.citevyn]
    assert classify_domains("what is our site win rate for gemini api calls?") == [
        Domain.gemini_api
    ]


@pytest.mark.parametrize(
    "question",
    [
        "Who won the World Cup?",
        "Explain quantum entanglement",
        "Python list comprehension",
        "What is the weather today?",
        "Recipes for chocolate cake",
    ],
)
def test_classify_domain_unsupported(question: str) -> None:
    assert classify_domain(question) is Domain.unsupported


def test_classify_domain_empty_returns_unsupported() -> None:
    assert classify_domain("") is Domain.unsupported
    assert classify_domain("   ") is Domain.unsupported
    assert classify_domain("\n\t") is Domain.unsupported


def test_classify_domain_prefers_claude_code_over_claude_api() -> None:
    """A question that mentions both ``Claude`` and ``Claude Code`` should
    resolve to ``claude_code`` (more specific match wins)."""
    assert (
        classify_domain("How do I configure Claude Code for the Claude API?") is Domain.claude_code
    )


def test_allowed_domains_contains_all_supported() -> None:
    assert Domain.claude_api in ALLOWED_DOMAINS
    assert Domain.claude_code in ALLOWED_DOMAINS
    assert Domain.codex in ALLOWED_DOMAINS
    assert Domain.gemini_api in ALLOWED_DOMAINS
    assert Domain.citevyn in ALLOWED_DOMAINS
    assert Domain.unsupported not in ALLOWED_DOMAINS
    assert not is_unsupported(Domain.citevyn)


def test_is_unsupported_helper() -> None:
    assert is_unsupported(Domain.unsupported) is True
    assert is_unsupported(Domain.claude_api) is False


def test_general_is_response_only_neutral_domain() -> None:
    """``Domain.general`` (#89) is stamped on greeting replies by the
    orchestrator; the guardrail never produces it and it is not a refusal.
    It must stay out of ``ALLOWED_DOMAINS`` (not a retrievable product area)
    and out of ``classify_domain``'s outputs so the classify/refuse logic is
    untouched."""
    assert is_unsupported(Domain.general) is False
    assert Domain.general not in ALLOWED_DOMAINS
    # The classifier maps a bare greeting to ``unsupported`` (not ``general``);
    # the neutral relabel happens later, in the orchestrator's greeting path.
    for question in ("hello", "hi there", "good morning", "", "Who won the World Cup?"):
        assert classify_domain(question) is not Domain.general
