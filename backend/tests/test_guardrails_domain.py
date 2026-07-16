"""Domain guardrail classification tests."""

from __future__ import annotations

import pytest

from app.guardrails.domain import (
    ALLOWED_DOMAINS,
    Domain,
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
