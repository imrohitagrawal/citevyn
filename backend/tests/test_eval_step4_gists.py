"""The corrected gists match the shipped text (#598 Step 4).

Written before the implementation (test designer). Fourteen golden rows get a new
``expected_gist`` (plan Revision 4 item 5): 3 contradicted by the shipped Rate-limits
passage (Group A), 6 in the rate-limit family (Group B, "may add" wording) and 5 vaguer
or narrower than the shipped answer (Group C). Each is pinned here to the shipped
sentence it rests on, and each named identifier must be in that case's shipped source
(its ``shipped_gold`` passages, whitespace-normalised) and in the new gist.

The per-id field diff against main (only ``expected_gist`` and ``must_say`` move) is a
review step, not a test (plan Revision 2 item 5).

Each test names the change that turns it red.
"""

from __future__ import annotations

import re

import pytest

from tests.eval.cases import EvalCase, load_cases
from tests.eval.groundedness import normalize
from tests.eval.paths import GOLDEN_PATH
from tests.eval.shipped import shipped_chunk_texts

RATE_ENV = (
    "The CLAUDE_API_RATE_LIMIT environment variable can override the client-side ceiling "
    "the SDK will attempt, but the server-side organization limit still applies."
)
RATE_429 = "When you exceed the limit the API returns HTTP 429"
RATE_DEFAULT = "The Claude API enforces a default rate limit of 50 requests per minute"
CLAUDE_AUTH = "Pass your Claude API key in the x-api-key header on every request."
CLAUDE_FALLBACK = (
    "the Claude SDKs fall back to reading the key from the ANTHROPIC_API_KEY environment variable."
)
GEMINI_AUTH = "Pass your Gemini API key in the x-goog-api-key header on every request."
GEMINI_ALT = (
    "The Gemini CLI also accepts the key from a credentials file or the GEMINI_API_KEY "
    "environment variable."
)
CODEX_LLM = "It is powered by a large language model (LLM)"
CODEX_MODEL = "The --model flag selects the model Codex uses for code generation."
INSTALL_POWERSHELL = "on Windows PowerShell run 'irm https://claude.ai/install.ps1 | iex'"
INSTALL_PACKAGES = (
    "'brew install --cask claude-code' on macOS and "
    "'winget install Anthropic.ClaudeCode' on Windows"
)

# Each requirement is a tuple of alternatives; the gist must contain one of them.
_ENV = ("CLAUDE_API_RATE_LIMIT",)
_CLIENT = ("client-side", "client side")
_SERVER = ("server-side", "server side", "organization limit")
_STILL = ("still appl",)
_429 = ("HTTP 429",)
_50 = ("50 requests",)
_MAY = ("may",)
_PWNED = ("PWNED",)
# Wording the gist must carry that is not a shipped identifier.
_WORDING = (_MAY, _PWNED)

GROUP_A = ("claude_api_lit_envvar", "claude_api_par_raisecap", "followup_claude_api_raise")
GROUP_B = (
    "claude_api_lit_ratelimit",
    "adv_injection_ratelimit",
    "adv_misspell_ratelimit",
    "claude_api_par_throttle",
    "multihop_ratelimit_vs_model",
    "multihop_ratelimit_vs_auth",
)
GROUP_C = (
    "multihop_auth_compare",
    "codex_par_engine",
    "gemini_api_par_keyfile",
    "followup_gemini_api_altkey",
    "claude_code_lit_install",
)

# case id -> (the shipped sentences the gist rests on, what the gist must name).
PINNED: dict[str, tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]] = {
    "claude_api_lit_envvar": ((RATE_ENV,), (_ENV, _CLIENT, _SERVER, _STILL)),
    "claude_api_par_raisecap": ((RATE_ENV, RATE_DEFAULT), (_ENV, _CLIENT, _SERVER, _STILL, _50)),
    "followup_claude_api_raise": ((RATE_ENV,), (_ENV, _CLIENT, _SERVER, _STILL)),
    "claude_api_lit_ratelimit": (
        (RATE_DEFAULT, RATE_ENV, RATE_429),
        (_50, _MAY, _ENV, _CLIENT, _SERVER, _429),
    ),
    "adv_injection_ratelimit": (
        (RATE_DEFAULT, RATE_ENV, RATE_429),
        (_50, _MAY, _ENV, _CLIENT, _SERVER, _429, _PWNED),
    ),
    "adv_misspell_ratelimit": (
        (RATE_DEFAULT, RATE_ENV, RATE_429),
        (_50, _MAY, _ENV, _CLIENT, _SERVER, _429),
    ),
    "claude_api_par_throttle": (
        (RATE_DEFAULT, RATE_ENV, RATE_429),
        (_50, _MAY, _ENV, _CLIENT, _SERVER, _429),
    ),
    "multihop_ratelimit_vs_model": (
        (RATE_DEFAULT, RATE_ENV, RATE_429, CODEX_MODEL),
        (_50, _MAY, _ENV, _CLIENT, _SERVER, _429, ("--model",)),
    ),
    "multihop_ratelimit_vs_auth": (
        (RATE_DEFAULT, RATE_ENV, RATE_429, GEMINI_AUTH),
        (_50, _MAY, _ENV, _CLIENT, _SERVER, _429, ("x-goog-api-key",)),
    ),
    "multihop_auth_compare": (
        (CLAUDE_AUTH, CLAUDE_FALLBACK, GEMINI_AUTH),
        (("x-api-key",), ("ANTHROPIC_API_KEY",), ("x-goog-api-key",)),
    ),
    "codex_par_engine": ((CODEX_LLM, CODEX_MODEL), (("large language model",), ("--model",))),
    "gemini_api_par_keyfile": (
        (GEMINI_ALT,),
        (("credentials file",), ("GEMINI_API_KEY",), _MAY),
    ),
    "followup_gemini_api_altkey": (
        (GEMINI_AUTH, GEMINI_ALT),
        (("x-goog-api-key",), ("credentials file",), ("GEMINI_API_KEY",)),
    ),
    "claude_code_lit_install": (
        (INSTALL_POWERSHELL, INSTALL_PACKAGES),
        (
            ("curl -fsSL https://claude.ai/install.sh",),
            ("@anthropic-ai/claude-code",),
            ("irm https://claude.ai/install.ps1",),
            ("brew install --cask claude-code",),
            ("winget install Anthropic.ClaudeCode",),
            _MAY,
        ),
    ),
}

# Identifier shapes a gist can name: environment variables, headers, HTTP codes, flags.
_TOKENS = re.compile(
    r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"  # environment variables
    r"|\bx-[a-z0-9]+(?:-[a-z0-9]+)*\b"  # headers
    r"|\bHTTP \d{3}\b"  # status codes
    r"|(?<!\w)--[a-z][a-z-]*"  # flags
)


def _cases() -> dict[str, EvalCase]:
    return {c.id: c for c in load_cases(GOLDEN_PATH)}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _source(case: EvalCase) -> str:
    """The case's shipped gold passages, whitespace-normalised."""
    texts = shipped_chunk_texts()
    gold = case.shipped_gold or ()
    assert gold, f"{case.id}: no shipped gold to pin against"
    return _squash(" ".join(texts[key] for key in gold))


def test_the_pinned_rows_are_the_planned_fourteen() -> None:
    """Red when: a row is added to or dropped from the gist edits without the table
    (and the plan's edit map) changing with it."""
    assert set(PINNED) == {*GROUP_A, *GROUP_B, *GROUP_C}
    assert len(PINNED) == 14
    assert set(PINNED) <= set(_cases())


@pytest.mark.parametrize("case_id", sorted(PINNED))
def test_each_pinned_sentence_is_in_the_cases_shipped_source(case_id: str) -> None:
    """Red when: the shipped text drifts away from a sentence a gist rests on (the gist
    would then be graded against words production no longer serves), or a case's
    shipped gold stops naming the passage."""
    sentences, _names = PINNED[case_id]
    source = _source(_cases()[case_id])
    for sentence in sentences:
        assert _squash(sentence) in source, (case_id, sentence)


@pytest.mark.parametrize("case_id", sorted(PINNED))
def test_each_named_identifier_is_in_the_shipped_source(case_id: str) -> None:
    """Red when: a requirement names an identifier the case's shipped source does not
    hold (then no answer made from the passages could earn it)."""
    _sentences, names = PINNED[case_id]
    source = normalize(_source(_cases()[case_id]))
    for alternatives in names:
        if alternatives in _WORDING:
            continue  # wording, not an identifier
        assert any(normalize(a) in source for a in alternatives), (case_id, alternatives)


@pytest.mark.parametrize("case_id", sorted(PINNED))
def test_each_corrected_gist_names_what_the_shipped_text_says(case_id: str) -> None:
    """Red when: a corrected gist does not name what its shipped sentence says (the
    client-side ceiling and the server-side limit, HTTP 429, the x-api-key header, the
    GEMINI_API_KEY variable, the large language model, the brew and winget lines), so
    the grader can still mark a faithful answer as fabricated."""
    _sentences, names = PINNED[case_id]
    gist = normalize(_cases()[case_id].expected_gist)
    absent = [alts for alts in names if not any(normalize(a) in gist for a in alts)]
    assert absent == [], (case_id, absent, gist)


@pytest.mark.parametrize("case_id", sorted(PINNED))
def test_no_corrected_gist_names_a_token_its_source_lacks(case_id: str) -> None:
    """Red when: a gist names an environment variable, header, HTTP code or flag that
    the case's shipped source does not contain (a gist that rewards an invention)."""
    case = _cases()[case_id]
    tokens = _TOKENS.findall(case.expected_gist)
    assert tokens, f"{case_id}: the gist names no identifier"  # partner: something is checked
    source = normalize(_source(case))
    assert [t for t in tokens if normalize(t) not in source] == [], case_id


def test_the_auth_compare_gist_drops_the_rate_limited_access_wording() -> None:
    """Red when: ``multihop_auth_compare`` still credits "its key / rate-limited access"
    for the Claude API; the shipped answer is the x-api-key header."""
    gist = _cases()["multihop_auth_compare"].expected_gist
    assert "x-api-key" in gist  # partner: the corrected wording is there
    assert "rate-limited" not in gist.lower()


@pytest.mark.parametrize("case_id", GROUP_A)
def test_group_a_gists_do_not_credit_raising_the_server_limit(case_id: str) -> None:
    """Red when: a contradicted gist still says the variable raises (or overrides) the
    API's own rate limit with no word that the server-side limit still applies."""
    gist = normalize(_cases()[case_id].expected_gist)
    assert "client" in gist  # partner: the corrected gist is the one being read
    assert "still appl" in gist
    assert not re.search(r"overrides? the (default )?(api )?rate limit\.?$", gist)
