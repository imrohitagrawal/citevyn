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
# the CiteVyn docs, and rewrites the user's text on the way. A MISS just makes
# the user rephrase. The rejection tests below therefore matter more than the
# happy-path ones, and every phrase in them came from an adversarial review that
# broke an earlier, looser version of this matcher.


@pytest.mark.parametrize(
    "question",
    [
        # Single tokens — no language a user of this tool is likely to type
        # spells a word this way, so these need no contextual guard.
        "what is sitewin?",
        "what is citevin?",
        "what is sitevyn?",
        "what is sitevin?",
        "what is citewin?",
        "what is sightvyn?",
        "what is sightwin?",
        "Is SiteWin free to use?",
        "Is sitewin free to use right now?",
        "does sitewin cover gemini?",
        # Separated only where the tail ("vyn") is not a word anywhere.
        "what is cite vyn?",
        "what is site vyn?",
        "what is sight vyn?",
        "what is cite-vyn?",
        "what is site-vyn?",
        # A sentence-final alias is still the product, not a filename.
        "I was reading about sitewin.",
    ],
)
def test_classify_domain_recognizes_citevyn_aliases(question: str) -> None:
    """A mangled CiteVyn name still routes to the ``citevyn`` domain, so the
    indexed About-CiteVyn source can answer instead of the generic refusal."""
    assert classify_domain(question) is Domain.citevyn


@pytest.mark.parametrize(
    "question",
    [
        # --- "win" as a VERB. "may the best site win!" is a set phrase; an
        #     earlier matcher rewrote it to "may the best CiteVyn!". ---
        "may the best site win!",
        "did Bob's site win?",
        "does our new site win?",
        "did the site win the award?",
        "which site win does better?",
        # --- "site win" as ordinary sales/analytics vocabulary ---
        "what is our site win rate?",
        "the site win percentage",
        "the recent site win cost us the deal",
        "congrats on the huge site win!",
        "we had a big site win, then we celebrated",
        "site win data for Q3",
        "site win trend",
        "site win % is up",
        "what was the site win-rate last quarter?",
        "cite win-loss reasons",
        "how many site wins did we have?",
        "Q3 site win costs",
        # --- the product-shaped frames are misses TOO. Recognizing these would
        #     mean recognizing the ones above; see the module docstring. ---
        "what is site win?",
        "site win pro",
        "is site win free?",
    ],
)
def test_two_ordinary_words_never_route_to_citevyn(question: str) -> None:
    """ "site win" / "cite win" are two ordinary English words and are DELIBERATELY
    not recognized.

    Two adversarial review rounds each broke a matcher that tried to admit them —
    first a blocklist of metric nouns, then a fail-closed allowlist with a
    determiner guard, which one adjective walked straight through ("may the best
    site win!"). Surrounding-token rules cannot separate this phrase from ordinary
    English, and a false hit costs far more than a miss. The last three cases pin
    that the miss is intentional, not an oversight: a user who says "site win" gets
    the refusal and can type "sitewin", which works.
    """
    assert classify_domain(question) is not Domain.citevyn


@pytest.mark.parametrize(
    "question",
    [
        "please cite VIN and mileage",
        "cite VIN numbers in the claim",
        "what does the site VIN decoder cost?",
        "upload the site vin list",
        "the site-vin lookup",
        "le site vin est en panne",
    ],
)
def test_separated_vin_spellings_are_not_the_product(question: str) -> None:
    """ "VIN" is an ordinary English noun (Vehicle Identification Number) and "vin"
    is French for wine, so "cite vin" / "site vin" are NOT safe separated aliases —
    an earlier version rewrote "please cite VIN and mileage" to "please CiteVyn and
    mileage". Only the single-token spellings ("citevin", "sitevin") are kept."""
    assert classify_domain(question) is not Domain.citevyn


@pytest.mark.parametrize(
    "text",
    [
        "Visit https://sitewin.example.com/docs",
        "why does sitewin.example.com return 502?",
        "the file sitevin.py failed",
        "see /srv/sitewin/config.yml",
        "email me at bob@sitewin.io",
    ],
)
def test_alias_inside_an_identifier_is_left_alone(text: str) -> None:
    """An alias inside a hostname, URL or filename is the IDENTIFIER the user is
    asking about, not the product name. Rewriting it corrupts the very string the
    question is about ("sitewin.example.com" -> "CiteVyn.example.com")."""
    assert canonicalize_product_name(text) == text
    assert classify_domain(text) is not Domain.citevyn


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
        "mysitewinapp settings",
    ],
)
def test_word_boundaries_protect_against_incidental_substrings(question: str) -> None:
    """The aliases occur as substrings inside ordinary English ("webSITE WINner",
    "offSITE WINter"). Word boundaries, not context rules, are what stop those."""
    assert classify_domain(question) is not Domain.citevyn


@pytest.mark.parametrize(
    "question,expected",
    [
        ("what is sitewin?", "what is CiteVyn?"),
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
        "may the best site win!",
        "website winner announcement",
        "how many site wins did we have?",
        "please cite VIN and mileage",
        "How do I configure Claude Code permissions?",
    ],
)
def test_canonicalize_leaves_non_citevyn_text_untouched(question: str) -> None:
    """The rewriter shares its pattern with the classifier, so it can never rewrite
    text the classifier would not also have routed to ``citevyn``. Silently turning
    "may the best site win!" into "may the best CiteVyn!" would corrupt the query."""
    assert canonicalize_product_name(question) == question


def test_canonicalize_only_rewrites_what_routes_to_citevyn() -> None:
    """The invariant that keeps the two from drifting: nothing is rewritten unless it
    routes to ``citevyn``. (The converse does not hold — a question already spelled
    canonically routes there but needs no rewrite.)"""
    samples = [
        "what is sitewin?",
        "site win pricing",
        "our site win rate",
        "may the best site win!",
        "What is CiteVyn?",
        "Claude Code permissions",
        "please cite VIN and mileage",
        "https://sitewin.example.com",
    ]
    for q in samples:
        rewritten = canonicalize_product_name(q) != q
        routed = classify_domain(q) is Domain.citevyn
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
