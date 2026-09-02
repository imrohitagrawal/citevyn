"""Domain guardrail classification tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.guardrails.domain import (
    _CITEVYN_ALIASES,
    _CITEVYN_RE,
    ALLOWED_DOMAINS,
    Domain,
    canonicalize_product_name,
    canonicalize_self_reference,
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
    "question",
    [
        # All of these routed to citevyn BEFORE aliases existed and must keep doing so.
        # An earlier revision applied the identifier guards to the canonical token too
        # and silently broke every one of them.
        "is citevyn.com free?",
        "what does citevyn.io cost",
        "email support@citevyn please",
        "docs.citevyn pricing",
        "anti-citevyn rant",
        "pre-citevyn workflow",
        "the claude-code-vs-citevyn writeup",
    ],
)
def test_canonical_name_is_never_narrowed_by_the_identifier_guards(question: str) -> None:
    """The guards exist for the lower-confidence ALIASES. The canonical spelling keeps its
    original un-guarded match, so a branch whose purpose is to make MORE product questions
    answer cannot end up making fewer of them answer."""
    assert classify_domain(question) is Domain.citevyn


@pytest.mark.parametrize(
    "text",
    [
        # Alias as the TRAILING segment of an identifier...
        "Visit https://sitewin.example.com/docs",
        "why does sitewin.example.com return 502?",
        "the file sitevin.py failed",
        "see /srv/sitewin/config.yml",
        "email me at bob@sitewin.io",
        # ...and as the LEADING segment. Guarding only one side left these open.
        "sitewin@example.com is the contact",
        "SITEWIN-1234 is blocked",
        "sitewin:8080 is down",
        "the sitewin/main branch",
        "package sitewin==1.2.3",
        # ...and as the FINAL segment, where nothing follows the alias. These are the
        # ONLY shape the BEFORE guard covers — every case above is also caught by the
        # AFTER guard, so without these the BEFORE guard could be deleted silently.
        "docs.sitewin",
        "v2.sitewin is deployed",
        "the github.com/acme/sitewin repo",
        "api.sitewin returned 500",
        "ping svc:sitewin",
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


# ---------------------------------------------------------------------------
# Offline-copy convergence (#84 item 4)
# ---------------------------------------------------------------------------


def _frontend_alias_list() -> tuple[str, ...]:
    """Parse ``CITEVYN_ALIASES`` out of the frontend's offline mirror.

    Parsing the TypeScript source rather than importing it keeps this test in the
    pytest suite (no node toolchain), which is what makes the drift guard actually
    run — a guard that only fires in a job someone can skip is not a guard.
    """
    ts = _FRONTEND_ALIAS_MODULE.read_text(encoding="utf-8")
    body = re.search(r"CITEVYN_ALIASES:\s*readonly string\[\]\s*=\s*\[(.*?)\];", ts, re.S)
    assert body is not None, f"CITEVYN_ALIASES not found in {_FRONTEND_ALIAS_MODULE}"
    # Comment lines inside the array quote alias spellings in prose, so scan only
    # the code lines. TS string literals escape the backslash ("\\t") where the
    # Python list uses a raw string (r"\t") — unescape so the two compare as the
    # same regex source.
    entries: list[str] = []
    for line in body.group(1).splitlines():
        code = line.strip()
        if code.startswith("//"):
            continue
        entries.extend(m.replace("\\\\", "\\") for m in re.findall(r'"([^"]*)"', code))
    return tuple(entries)


_FRONTEND_ALIAS_MODULE = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "citevynAliases.ts"
)


def test_frontend_offline_alias_list_mirrors_the_guardrail() -> None:
    """The demo/offline path has its own copy of this alias list (it never reaches
    this module), so the same question was recognized live and refused offline —
    #84 item 4. The copy is hand-kept; this pins it so a one-sided edit fails a
    test here instead of silently re-opening the divergence."""
    assert _frontend_alias_list() == _CITEVYN_ALIASES


def test_frontend_offline_mirror_keeps_the_canonical_and_guard_branches() -> None:
    """Mirroring only the alias LIST is not enough: dropping the un-guarded canonical
    branch would narrow "is citevyn.com free?", and dropping the identifier guards
    would rewrite "sitewin.example.com". Pin both structural halves of the pattern.

    The TS spells these differently from the Python on purpose — `\\w`/`\\b` are
    ASCII-only in JS, and a lookbehind is below the frontend's browser baseline —
    so this pins the JS *constructs* rather than the Python source text. What pins
    the two matchers to the same ANSWERS is the shared corpus test below.
    """
    ts = _FRONTEND_ALIAS_MODULE.read_text(encoding="utf-8")
    # Canonical branch, still un-guarded by the identifier guards.
    assert "citevyn(?![${WORD_CHAR}])" in ts
    # Both identifier guards, still symmetric.
    assert "(?:^|[^${IDENTIFIER_CHAR}])" in ts
    assert "(?![${IDENTIFIER_CHAR}]*[${WORD_CHAR}])" in ts
    # The guards must be built from the Unicode word class, not JS's ASCII `\w`,
    # or the mirror silently accepts what this module rejects (see the corpus).
    assert "\\\\p{L}\\\\p{N}_" in ts
    # A lookbehind here throws a SyntaxError at MODULE LOAD on a baseline browser
    # (Safari < 16.4), taking the whole bundle down rather than degrading.
    assert "(?<" not in ts


_FRONTEND_PARITY_CORPUS = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "citevynAliases.cases.json"
)


def _parity_cases() -> list[tuple[str, bool, str]]:
    payload = json.loads(_FRONTEND_PARITY_CORPUS.read_text(encoding="utf-8"))
    return [(c["q"], c["match"], c["why"]) for c in payload["cases"]]


@pytest.mark.parametrize("question,expected,why", _parity_cases())
def test_frontend_offline_mirror_agrees_on_the_shared_corpus(
    question: str, expected: bool, why: str
) -> None:
    """Run the SAME corpus the frontend suite runs, against THIS module's regex.

    Pinning the alias list proves the two lists agree; it proves nothing about the
    matchers wrapped around them, and the two are written in different regex
    dialects (JS `\\w`/`\\b` are ASCII-only, Python's are Unicode-aware, and the JS
    side cannot use a lookbehind at all). The list pin passed while the mirror
    matched "sitewinа"/"cafésitewin" that this module refuses. One corpus, two
    runners, so a rewrite on either side that changes an answer fails on the other.
    """
    assert bool(_CITEVYN_RE.search(question)) is expected, why


# ---------------------------------------------------------------------------
# Self-referential questions (#300)
# ---------------------------------------------------------------------------
#
# RED before the fix: every question below classified ``unsupported`` and was
# refused with "NO SOURCE — REFUSED" even though the indexed About-CiteVyn source
# answers it. Reverting ``canonicalize_self_reference`` to ``return question``
# turns every ``test_self_reference_*_is_rewritten`` case red.


@pytest.mark.parametrize(
    "question,expected",
    [
        ("who are you?", "What is CiteVyn?"),
        ("Who are you", "What is CiteVyn?"),
        ("WHO ARE YOU?!", "What is CiteVyn?"),
        ("who're you?", "What is CiteVyn?"),
        # Every remaining alternative of the regex, so none is reachable-but-unasserted:
        # deleting any one of them must turn a case below red (review finding).
        ("who re you", "What is CiteVyn?"),  # the \\s+ branch of who(?:\\s+|')re
        ("who am i chatting with", "What is CiteVyn?"),  # the "chatting" branch
        ("who am i talking with?", "What is CiteVyn?"),
        ("who am i speaking to?", "What is CiteVyn?"),
        ("what can you help with?", "What can CiteVyn do?"),  # optional "me"
        ("what topic do you cover?", "What does CiteVyn cover?"),  # singular topics?
        ("what source do you use?", "What does CiteVyn cover?"),  # singular sources?
        # Dictation and phone keyboards emit a curly apostrophe; the owner dictates,
        # so this is the COMMON spelling, not an exotic one.
        ("what\u2019s your name?", "What is CiteVyn?"),
        ("who\u2019re you", "What is CiteVyn?"),
        ("what\u02bcs your name", "What is CiteVyn?"),
        # A closed set of discourse openers. Without these #300's own symptom survives
        # for the commonest natural phrasing: "hey, who are you?" is NOT a bare greeting
        # (_GREETING_RE requires the message to end after the greeting), so before this
        # it fell straight through to the refusal the issue was filed about.
        ("hey, who are you?", "What is CiteVyn?"),
        ("hi, what can you do?", "What can CiteVyn do?"),
        ("so what do you cover?", "What does CiteVyn cover?"),
        ("ok what are you?", "What is CiteVyn?"),
        ("well, help", "What can CiteVyn do?"),
        # Trailing punctuation the single-character-class tail accepts.
        ("who are you,", "What is CiteVyn?"),
        ("who are you...", "What is CiteVyn?"),
        ("what can you do?!", "What can CiteVyn do?"),
        ("what are you?", "What is CiteVyn?"),
        ("who am I talking to?", "What is CiteVyn?"),
        ("who am i speaking with", "What is CiteVyn?"),
        ("what's your name?", "What is CiteVyn?"),
        ("what is your name", "What is CiteVyn?"),
        ("tell me about yourself", "What is CiteVyn?"),
        ("introduce yourself.", "What is CiteVyn?"),
        ("what can you do?", "What can CiteVyn do?"),
        ("what do you do?", "What can CiteVyn do?"),
        ("what are you for?", "What can CiteVyn do?"),
        ("how can you help me?", "What can CiteVyn do?"),
        ("how can you help?", "What can CiteVyn do?"),
        ("what can you help me with?", "What can CiteVyn do?"),
        ("help", "What can CiteVyn do?"),
        ("help?", "What can CiteVyn do?"),
        ("  help  ", "What can CiteVyn do?"),
        ("what do you know?", "What does CiteVyn cover?"),
        ("what do you know about?", "What does CiteVyn cover?"),
        ("what do you cover?", "What does CiteVyn cover?"),
        ("what topics do you cover?", "What does CiteVyn cover?"),
        ("what can you answer?", "What does CiteVyn cover?"),
        ("what can I ask you?", "What does CiteVyn cover?"),
        ("what can i ask?", "What does CiteVyn cover?"),
        ("what sources do you use?", "What does CiteVyn cover?"),
    ],
)
def test_self_reference_is_rewritten_to_the_citevyn_question_it_means(
    question: str, expected: str
) -> None:
    """A listed self-referential phrasing becomes the CiteVyn question it means."""
    assert canonicalize_self_reference(question) == expected


@pytest.mark.parametrize(
    "question,why",
    [
        (
            "who are the Codex maintainers?",
            "the issue's named negative: a real Codex question opening with 'who are'",
        ),
        (
            "what can you do with the Gemini API?",
            "a listed phrasing with a substantive tail is a real product question",
        ),
        (
            "what do you know about the Claude API rate limits?",
            "same, and it already routes to claude_api correctly",
        ),
        ("help me install Claude Code", "'help' as a verb with an object, not a bare cry"),
        ("help with codex login", "same"),
        ("what are you doing about the codex outage", "tail past the phrase"),
        ("who are you going to bill for this?", "tail past the phrase"),
        ("what is CiteVyn?", "already routes to citevyn — must not be rewritten"),
        ("what do you cover in the enterprise plan", "tail past the phrase"),
        ("", "empty input"),
        ("   ", "whitespace-only input"),
        ("what is Claude Code?", "an ordinary product question"),
        ("so what is Claude Code?", "an opener must not smuggle a product question through"),
        ("hey, what can you do with the Gemini API?", "opener + listed phrase + product tail"),
        ("hello, who are the Codex maintainers?", "opener does not weaken the end anchor"),
        ("hey there, help me install codex", "opener + help as a verb with an object"),
        ("selfhelp", "'help' only as a substring, not the whole message"),
        ("help help", "not a listed phrasing"),
    ],
)
def test_self_reference_leaves_everything_else_verbatim(question: str, why: str) -> None:
    """Anything off the closed list — including a listed phrasing carrying a
    substantive tail — is returned byte-for-byte, so today's routing is unchanged."""
    assert canonicalize_self_reference(question) == question, why


@pytest.mark.parametrize(
    "question",
    [
        "who are you?",
        "what can you do?",
        "what do you cover?",
        "help",
        "tell me about yourself",
    ],
)
def test_self_referential_questions_reach_the_citevyn_domain_after_rewrite(
    question: str,
) -> None:
    """The whole point of the rewrite: these route to ``citevyn``, not ``unsupported``.

    Without ``canonicalize_self_reference`` every one of them is ``unsupported``
    (that is the #300 bug), so this test is red on the unfixed tree.
    """
    assert classify_domain(question) is Domain.unsupported  # the raw utterance still is
    assert classify_domain(canonicalize_self_reference(question)) is Domain.citevyn


def test_self_reference_rewrite_is_idempotent() -> None:
    """Re-running the rewrite on its own output is a no-op (the canonical forms
    name CiteVyn, so they are off the list by construction)."""
    for question in ("who are you?", "what can you do?", "what do you cover?", "help"):
        once = canonicalize_self_reference(question)
        assert canonicalize_self_reference(once) == once


def test_self_reference_negative_matches_the_codex_maintainers_case_end_to_end() -> None:
    """The issue's explicit negative keeps its ``codex`` routing through the rewrite."""
    question = "who are the Codex maintainers?"
    assert classify_domain(canonicalize_self_reference(question)) is Domain.codex


def test_self_reference_tail_does_not_backtrack_quadratically() -> None:
    """The anchor tail must stay a single character class, not nested quantifiers.

    The first version spelled it ``\\s*[?.!]*\\s*$`` — two ``\\s*`` around a
    quantifier that can match empty. A listed phrasing followed by a long
    whitespace run that cannot satisfy ``$`` makes the engine re-split the run
    O(n) ways and rescan O(n) each time: 63 ms of BLOCKED EVENT LOOP for one
    4000-char message (the API's own cap, ``AnswerRequest.message``) against
    ~0.5 us for a real question. ``canonicalize_self_reference`` runs
    synchronously inside ``Orchestrator.ask`` before any await, so that stalls
    every other in-flight request on the machine, not just the sender's.

    Asserted as a RATIO between two input sizes rather than a wall-clock budget,
    so a slow or loaded machine cannot fail it on speed alone. The sizes are 4x
    apart, so linear scaling predicts ~4x and quadratic ~16x; the threshold sits
    at 8, an even multiplicative margin from both. A review of the first version
    of this test measured 3.09 and 6.33 on a contended machine against a 3.0
    threshold set for a 2x gap — too tight to be trustworthy, hence the wider gap.

    Restoring ``\\s*[?.!]*\\s*$`` turns this red, and so does the plausible
    half-fix ``[\\s,.!?]*\\s*$``.
    """
    import time

    stem = "what can you help me with"
    # PARTNER ASSERTION (the project's rule: a check that counts nothing needs a
    # partner proving the thing counted exists). Without this the test degrades
    # SILENTLY to a ratio of ~1.00 the moment ``stem`` stops matching the
    # phrasing list — the regex would bail immediately at both sizes and the
    # ratio assertion would pass while measuring nothing at all.
    assert canonicalize_self_reference(stem) == "What can CiteVyn do?", (
        "the timing probe's stem must still be a LISTED phrasing, or this test "
        "measures the fast reject path and passes vacuously"
    )
    assert canonicalize_self_reference(stem + " " * 8 + "x") == stem + " " * 8 + "x", (
        "the probe must FAIL to match once the tail is unsatisfiable — that failure "
        "is the path that backtracks"
    )

    def elapsed(n: int) -> float:
        # A listed phrasing, then a whitespace run, then a character that makes
        # the ``$`` anchor fail — the worst case for a backtracking tail.
        probe = stem + " " * n + "x"
        best = float("inf")
        for _ in range(5):  # best-of-5 damps scheduler noise
            start = time.perf_counter()
            canonicalize_self_reference(probe)
            best = min(best, time.perf_counter() - start)
        return best

    small = elapsed(1000)
    large = elapsed(4000)  # 4x the input
    # Guard against a divide-by-zero on a machine fast enough to floor `small`.
    ratio = large / max(small, 1e-9)
    assert ratio < 8.0, (
        f"self-reference tail scales super-linearly: 1000 chars {small:.6f}s -> "
        f"4000 chars {large:.6f}s (ratio {ratio:.1f}x, linear would be ~4x). "
        "The tail must be ONE character class, e.g. [\\s,.!?]*$"
    )


def test_self_reference_opener_cannot_smuggle_a_product_question() -> None:
    """The discourse opener widens the START of the match, never the END.

    Adding an opener is only safe because the message stays anchored at both
    ends. If the end anchor were ever dropped, "hey, what can you do with the
    Gemini API?" would be rewritten to a CiteVyn question and answered from the
    wrong source — a confidently-wrong, confidently-cited answer, the exact
    failure mode the #84 alias work spent three rounds avoiding.
    """
    for opener in ("hi", "hey", "hello", "ok", "okay", "so", "well", "um"):
        hijacked = f"{opener}, what can you do with the Gemini API?"
        assert canonicalize_self_reference(hijacked) == hijacked
        assert classify_domain(canonicalize_self_reference(hijacked)) is Domain.gemini_api
