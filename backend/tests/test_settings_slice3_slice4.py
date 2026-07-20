"""Settings added for Slice 3 and Slice 4.

The defaults documented in ``docs/RELEASE_PLAN.md`` and
``docs/API_SPEC.md`` are pinned here so future changes are intentional.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.config import (
    DEFAULT_NO_ANSWER_FALLBACK,
    DEFAULT_UNSUPPORTED_REFUSAL,
    Settings,
    get_settings,
)


def test_default_llm_provider_is_stub() -> None:
    settings = Settings()
    assert settings.llm_provider == "stub"
    assert settings.llm_model == "claude-opus-4-8"
    assert settings.llm_max_tokens == 1024
    assert 0.0 <= settings.llm_temperature <= 1.0


def test_default_embedding_dim_matches_migration() -> None:
    settings = Settings()
    assert settings.embedding_provider == "stub"
    assert settings.embedding_model == "gemini-embedding-001"
    # Must match the pgvector column dimension in migration 0004 (vector(1536)).
    assert settings.embedding_dim == 1536


def test_default_retrieval_and_cache_settings() -> None:
    settings = Settings()
    assert settings.retrieval_top_k == 6
    assert settings.retrieval_max_candidates == 20
    # v2 since #169 — the bump is the cache-invalidation mechanism for the poisoned
    # follow-up rows, so this pin is load-bearing: silently reverting it to "v1" would
    # re-serve them.
    assert settings.answer_policy_version == "v2"
    assert settings.cache_ttl_seconds == 86_400
    assert settings.cache_enabled is True


def test_default_response_copy_matches_spec() -> None:
    settings = Settings()
    assert settings.unsupported_refusal == DEFAULT_UNSUPPORTED_REFUSAL
    assert settings.no_answer_fallback == DEFAULT_NO_ANSWER_FALLBACK
    assert "Claude" in settings.unsupported_refusal
    assert "Codex" in settings.unsupported_refusal


def test_unsupported_refusal_nudges_toward_citevyn_meta_questions() -> None:
    """#84 item 5. A near-miss meta question ("what is Pro?") never says "CiteVyn",
    so it routes to ``unsupported``; naming the meta-domain in the refusal is the
    only hint the user gets that a phrasing exists which works. Additive — the four
    products still come first, so this reads as scope, not as an upsell."""
    refusal = Settings().unsupported_refusal
    assert "CiteVyn itself" in refusal
    assert refusal.index("Claude") < refusal.index("CiteVyn itself")


_FRONTEND_KNOWLEDGE_BASE = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "data" / "knowledgeBase.ts"
)

# ``export const GENERIC_REFUSAL =`` followed by one double-quoted literal, which
# ruff's TS counterpart (prettier) wraps onto the next line — hence the ``\s*``.
_GENERIC_REFUSAL_DECL = re.compile(
    r"^export const GENERIC_REFUSAL\s*=\s*(\"(?:[^\"\\]|\\.)*\")\s*;",
    re.MULTILINE,
)

# Phrases that make a string literal a refusal. A literal carrying either one is
# a user-visible refusal and must therefore BE the pinned constant, not a variant
# of it. Kept broad on purpose: the two known drifts changed a contraction and
# dropped a clause, and both of these survive that kind of edit.
_REFUSAL_MARKERS = ("credible source material", "I can answer questions about")


def _ts_string_literals(src: str) -> list[str]:
    """Every string literal in a TS source, comments stripped, escapes decoded.

    Hand-rolled rather than regexed because the file's *comments* quote the
    refusal wording while discussing the drift — a naive regex would read those
    as literals and the guard would pass on prose. Single-quoted and template
    literals are tracked only so their contents are not mistaken for code.

    Limitation, deliberate: regex literals are not tokenised, so a future regex
    containing an unpaired quote character would desynchronise this scanner. The
    file has none; if one is added this test fails loudly rather than silently,
    which is the correct direction to fail.
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            i = src.find("\n", i)
            if i == -1:
                break
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif c in "\"'`":
            quote, buf, i = c, [], i + 1
            while i < n and src[i] != quote:
                if src[i] == "\\":
                    buf.append(src[i : i + 2])
                    i += 2
                    continue
                buf.append(src[i])
                i += 1
            i += 1
            if quote == '"':
                # json.loads decodes exactly the escape set this file uses.
                out.append(json.loads('"' + "".join(buf) + '"'))
        else:
            i += 1
    return out


def test_frontend_generic_refusal_is_byte_identical_to_the_backend_default() -> None:
    """The demo/offline path has its own hand-copied refusal string
    (``knowledgeBase.ts::GENERIC_REFUSAL``) because it never reaches this module.
    It had NO pin and had already drifted — it said "I don't have" where this
    constant says "I do not have", so the demo was quietly a different product
    from the live one. That is the same silent-drift failure #84 item 4 fixed for
    the alias list, and it needs the same treatment.

    The pin extracts the DECLARATION and compares it. The previous version of
    this test asserted ``DEFAULT_UNSUPPORTED_REFUSAL in ts`` — a whole-file
    substring check, which is satisfied by *any* literal in the file carrying the
    backend string, so a real drift of ``GENERIC_REFUSAL`` survived it. That was
    not hypothetical: the "laptop" KB entry held a second, separately-drifted
    hand-copy at the time.

    This runs in pytest on purpose — a guard that only fires in a job someone can
    skip is not a guard.
    """
    ts = _FRONTEND_KNOWLEDGE_BASE.read_text(encoding="utf-8")
    decls = _GENERIC_REFUSAL_DECL.findall(ts)
    assert len(decls) == 1, (
        f'expected exactly one `export const GENERIC_REFUSAL = "…";` declaration in '
        f"knowledgeBase.ts, found {len(decls)}"
    )
    assert json.loads(decls[0]) == DEFAULT_UNSUPPORTED_REFUSAL, (
        "GENERIC_REFUSAL in knowledgeBase.ts has drifted from "
        "DEFAULT_UNSUPPORTED_REFUSAL; re-copy the backend string verbatim."
    )


def test_frontend_has_exactly_one_refusal_literal() -> None:
    """No second hand-copy of the refusal may exist in knowledgeBase.ts.

    The "laptop" KB entry used to re-type the refusal instead of referencing
    ``GENERIC_REFUSAL``, so demo mode emitted TWO different refusal texts — and
    the copy users hit most visibly (it is in both MARQUEE and DEMO_ORDER) was
    the one missing the #84-item-5 "or about CiteVyn itself" nudge. Pinning only
    the declaration would let that be reintroduced, so pin the whole file: every
    refusal-shaped literal must BE the backend string. A new refusing entry
    therefore has to reference the constant.
    """
    literals = _ts_string_literals(_FRONTEND_KNOWLEDGE_BASE.read_text(encoding="utf-8"))
    refusals = [s for s in literals if any(m in s for m in _REFUSAL_MARKERS)]
    assert refusals == [DEFAULT_UNSUPPORTED_REFUSAL], (
        "knowledgeBase.ts must contain exactly one refusal literal, the "
        "GENERIC_REFUSAL declaration, byte-identical to DEFAULT_UNSUPPORTED_REFUSAL. "
        f"Found {len(refusals)}: {refusals!r}. Reference GENERIC_REFUSAL instead of "
        "re-typing the wording."
    )


def test_settings_env_override(monkeypatch) -> None:
    monkeypatch.setenv("CITEVYN_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CITEVYN_RETRIEVAL_TOP_K", "10")
    monkeypatch.setenv("CITEVYN_CACHE_ENABLED", "false")
    get_settings.cache_clear()

    try:
        settings = get_settings()
        assert settings.llm_provider == "anthropic"
        assert settings.retrieval_top_k == 10
        assert settings.cache_enabled is False
    finally:
        get_settings.cache_clear()
