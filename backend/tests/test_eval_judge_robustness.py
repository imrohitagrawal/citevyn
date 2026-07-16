"""Judge-robustness tests (Item 1): prompt-ensemble panel, adversarial veto, and the
deterministic groundedness metric.

Three concerns:

1. **Groundedness** (hermetic, no LLM) — word-boundary fact matching, any-of
   alternatives, and the corpus-anchored integrity of ``expected_facts``.
2. **Panel aggregation** (hermetic) — the median smooths standard-member noise while
   the adversarial member VETOES (never gets averaged away), and ``contested`` is a
   same-rubric signal. Uses a scripted fake LLM client so no network is touched.
3. **Robustness proof** (opt-in, real key via ``CITEVYN_EVAL_LLM=1``) — a
   deliberately-wrong answer scores low AND is caught by groundedness, and the same
   answer scores stably across repeated panel runs (bounded variance).
"""

from __future__ import annotations

import json
import os

import pytest

from app.llm.types import LLMResult
from tests.conftest import seed_catalog
from tests.eval.cases import EvalCase, load_cases
from tests.eval.groundedness import fact_coverage, fact_covered, missing_facts, normalize
from tests.eval.judge import (
    JudgeVerdict,
    aggregate_panel,
    panel_size,
    score_answer_panel_async,
)
from tests.eval.paths import GOLDEN_PATH
from tests.eval.thresholds import CONTESTED_SPREAD

# ---------------------------------------------------------------------------
# Groundedness — word-boundary matching (the BLOCKER the plan-review caught)
# ---------------------------------------------------------------------------


def test_numeric_superstring_does_not_credit_a_wrong_number() -> None:
    """ "50 requests per minute" must NOT be found in "150 requests per minute" —
    the exact digit-prefix error the deterministic metric exists to catch."""
    fact = "50 requests per minute"
    assert fact_covered("a default of 50 requests per minute", fact)
    assert not fact_covered("the limit is 150 requests per minute", fact)
    assert not fact_covered("the limit is 500 requests per minute", fact)


def test_identifier_facts_match_at_symbol_boundaries() -> None:
    assert fact_covered("pass it in the x-goog-api-key header", "x-goog-api-key")
    # A longer token must not satisfy the shorter fact.
    assert not fact_covered("use x-goog-api-key-v2 instead", "x-goog-api-key")
    assert fact_covered("run codex --help for flags", "codex --help")
    assert fact_covered("the --model flag picks the model", "--model")


def test_any_of_alternatives() -> None:
    fact = "50 requests per minute|50 req/min"
    assert fact_covered("throttled at 50 req/min", fact)
    assert fact_covered("throttled at 50 requests per minute", fact)
    # "150 req/min" contains "50 req/min" as a substring — the digit-prefix guard must
    # reject it for the `req/min` alternative too (not merely because it is absent).
    assert not fact_covered("throttled at 150 req/min", fact)


def test_decimal_or_comma_numeric_prefix_does_not_credit_a_wrong_number() -> None:
    """A decimal/thousands-separator prefix is a wrong value (off by 100x) and must not
    satisfy the fact — the same class as the digit-prefix guard."""
    fact = "50 requests per minute"
    assert not fact_covered("the ratio is 0.50 requests per minute", fact)
    assert not fact_covered("about 1,50 requests per minute", fact)
    # trailing punctuation still matches (lookahead unchanged)
    assert fact_covered("run codex --help.", "codex --help")


def test_coverage_and_missing() -> None:
    facts = ["CLAUDE_API_RATE_LIMIT", "50 requests per minute"]
    answer = "Set CLAUDE_API_RATE_LIMIT to change the limit."
    assert fact_coverage(answer, facts) == 0.5
    assert missing_facts(answer, facts) == ["50 requests per minute"]
    assert fact_coverage("nothing relevant", []) == 1.0  # vacuous


def test_normalize_collapses_whitespace_and_case() -> None:
    assert normalize("  X-Goog-API-Key\n  header ") == "x-goog-api-key header"


# ---------------------------------------------------------------------------
# Golden integrity — every expected_fact is groundable in the seed corpus
# ---------------------------------------------------------------------------


async def _seed_corpus_text() -> str:
    """Concatenated normalized chunk text of the conftest seed corpus."""
    import tempfile

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    with tempfile.NamedTemporaryFile(suffix=".db") as fh:
        engine = create_async_engine(f"sqlite+aiosqlite:///{fh.name}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
            async with factory() as session:
                catalog = await seed_catalog(session)
            return normalize(" ".join(str(c.chunk_text) for c in catalog["chunks"]))  # type: ignore[attr-defined]
        finally:
            await engine.dispose()


async def test_every_expected_fact_is_groundable_in_the_corpus() -> None:
    """At least ONE alternative of every declared fact must appear in the seed corpus.

    This is the any-of groundability invariant: a fact can never assert something the
    corpus cannot support, while extra alternatives are free-form answer-phrasing
    synonyms (not required to be in the corpus)."""
    corpus = await _seed_corpus_text()
    offenders: list[tuple[str, str]] = []
    for case in load_cases(GOLDEN_PATH):
        for fact in case.expected_facts:
            alts = [a for a in fact.split("|") if a.strip()]
            if not any(fact_covered(corpus, alt) for alt in alts):
                offenders.append((case.id, fact))
    assert not offenders, f"expected_facts not groundable in the seed corpus: {offenders}"


def test_refusal_case_rejects_expected_facts() -> None:
    with pytest.raises(ValueError, match="must not set expected_facts"):
        EvalCase.from_dict(
            {
                "id": "r",
                "area": "o",
                "kind": "refusal",
                "question": "q",
                "expected_gist": "g",
                "expect_no_answer": True,
                "expected_facts": ["x"],
            },
            origin="test",
        )


def test_expected_facts_must_be_a_list() -> None:
    with pytest.raises(ValueError, match="expected_facts must be a list"):
        EvalCase.from_dict(
            {
                "id": "x",
                "area": "codex",
                "kind": "literal",
                "question": "q",
                "expected_source": "codex",
                "expected_gist": "g",
                "expected_facts": "codex --help",
            },
            origin="test",
        )


def test_expected_facts_elements_must_be_strings() -> None:
    """A non-string element (e.g. a bare number) must be rejected at parse time, not
    crash later with an AttributeError inside the matcher."""
    with pytest.raises(ValueError, match="expected_facts must be a list of strings"):
        EvalCase.from_dict(
            {
                "id": "x",
                "area": "claude_api",
                "kind": "literal",
                "question": "q",
                "expected_source": "claude_api",
                "expected_gist": "g",
                "expected_facts": [50],
            },
            origin="test",
        )


# ---------------------------------------------------------------------------
# Panel aggregation — median smooths noise, adversarial VETOES (not averaged)
# ---------------------------------------------------------------------------


def _v(score: int, rationale: str = "") -> JudgeVerdict:
    return JudgeVerdict(score=score, rationale=rationale or f"s{score}")


def test_median_smooths_a_noisy_standard_outlier() -> None:
    # One judge misfires low; the median of the standard members ignores it.
    p = aggregate_panel([_v(5), _v(5), _v(2)], adversarial=None)
    assert p.standard_median == 5
    assert p.score == 5
    assert p.spread == 3
    assert p.contested is (CONTESTED_SPREAD <= 3)


def test_adversarial_vetoes_a_plausible_but_wrong_answer() -> None:
    # THE case the design exists for: standard judges are fooled (5,5); the skeptic
    # scores 2. Folding into a median would discard the 2 → 5. The veto floors it to 2.
    p = aggregate_panel([_v(5), _v(5)], adversarial=_v(2, "wrong rate limit"))
    assert p.standard_median == 5
    assert p.adversarial_score == 2
    assert p.score == 2, "adversarial must veto, not be averaged away"
    assert p.rationale == "wrong rate limit"


def test_adversarial_cannot_raise_a_low_median() -> None:
    # The skeptic is a floor, never a lift.
    p = aggregate_panel([_v(2), _v(2), _v(2)], adversarial=_v(5))
    assert p.score == 2


def test_contested_ignores_adversarial_pessimism() -> None:
    # Standard members fully agree (5,5,5); the adversarial sits low by design. That
    # constant rubric gap must NOT read as "contested".
    p = aggregate_panel([_v(5), _v(5), _v(5)], adversarial=_v(3))
    assert p.spread == 0
    assert p.contested is False


def test_odd_panel_median_is_a_single_member() -> None:
    p = aggregate_panel([_v(3), _v(4), _v(5)], adversarial=None)
    assert p.standard_median == 4  # no fractional averaging


def test_rationale_matches_the_median_score_not_list_position() -> None:
    # Unsorted members: median-by-value is 3, and the rationale must describe the 3,
    # not whatever verdict sits at the middle INDEX (the 5).
    p = aggregate_panel([_v(2, "two"), _v(5, "five"), _v(3, "three")], adversarial=None)
    assert p.standard_median == 3
    assert p.rationale == "three"


def test_panel_size_is_odd_and_clamped() -> None:
    # Default is odd; the env override is clamped to the available framings and forced
    # odd so the median never averages.
    assert panel_size() % 2 == 1
    os.environ["CITEVYN_EVAL_JUDGE_PANEL"] = "2"
    try:
        assert panel_size() % 2 == 1
    finally:
        del os.environ["CITEVYN_EVAL_JUDGE_PANEL"]


def test_summarize_aggregates_groundedness_over_fact_bearing_cases_only() -> None:
    """_summarize must average coverage over ONLY cases that declare facts, build
    under_grounded from the sub-1.0 ones, and keep groundedness judge-INDEPENDENT (a
    case whose judge errored but whose fact_coverage was computed still counts)."""
    from tests.eval.runner import JudgedCase, _summarize

    judged = [
        JudgedCase(
            case_id="a", kind="literal", answer="x", no_answer=False, score=5, fact_coverage=1.0
        ),
        # judge errored (score None) but the deterministic coverage was still computed
        JudgedCase(
            case_id="b",
            kind="literal",
            answer="",
            no_answer=True,
            error="judge_unavailable: boom",
            fact_coverage=0.0,
            missing_facts=("50 requests per minute",),
        ),
        # no declared facts → excluded from the groundedness aggregate entirely
        JudgedCase(case_id="c", kind="paraphrase", answer="y", no_answer=False, score=4),
    ]
    summary = _summarize([], {"stub": True}, judged, judge_available=True)
    g = summary["groundedness"]
    assert g["cases_with_facts"] == 2  # a + b, not c
    assert g["grounded_fact_rate"] == 0.5  # (1.0 + 0.0) / 2
    assert [u["case_id"] for u in g["under_grounded"]] == ["b"]
    assert g["under_grounded"][0]["missing"] == ["50 requests per minute"]


class _ScriptedLLM:
    """Fake LLM client that returns a queued JSON verdict per ``complete`` call."""

    def __init__(self, scores: list[int]) -> None:
        self._texts = [json.dumps({"score": s, "rationale": f"scripted {s}"}) for s in scores]
        self._i = 0

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:  # noqa: ARG002
        text = self._texts[self._i]
        self._i += 1
        return LLMResult(
            text=text, input_tokens=1, output_tokens=1, model="scripted", provider="stub"
        )

    async def aclose(self) -> None:
        return None


async def test_score_answer_panel_end_to_end_veto() -> None:
    """score_answer_panel_async wires framings→median and the adversarial→veto.

    Default panel size is 3 standard framings + 1 adversarial = 4 scripted calls; the
    standard median (5) is vetoed by the adversarial (1)."""
    client = _ScriptedLLM([5, 5, 5, 1])
    verdict = await score_answer_panel_async(
        question="q", answer="a", expected_gist="g", client=client
    )
    assert verdict is not None
    assert verdict.standard_median == 5
    assert verdict.adversarial_score == 1
    assert verdict.score == 1


# ---------------------------------------------------------------------------
# Robustness proof — opt-in, needs a real provider key (CITEVYN_EVAL_LLM=1)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.getenv("CITEVYN_EVAL_LLM") != "1",
    reason="robustness proof needs a real provider key; set CITEVYN_EVAL_LLM=1",
)
async def test_wrong_answer_scores_low_and_fails_groundedness() -> (
    None
):  # pragma: no cover - opt-in
    """A deliberately-wrong answer must score low on the panel AND fail groundedness."""
    wrong = "The Claude API allows 5000 requests per second with no configuration."
    verdict = await score_answer_panel_async(
        question="What is the rate limit for the Claude API?",
        answer=wrong,
        expected_gist="The Claude API default rate limit is 50 requests per minute.",
    )
    assert verdict is not None
    assert verdict.score <= 2, f"wrong answer scored {verdict.score}"
    assert fact_coverage(wrong, ["50 requests per minute|50 req/min"]) == 0.0


@pytest.mark.skipif(
    os.getenv("CITEVYN_EVAL_LLM") != "1",
    reason="robustness proof needs a real provider key; set CITEVYN_EVAL_LLM=1",
)
async def test_panel_score_is_stable_across_runs() -> None:  # pragma: no cover - opt-in
    """The same answer judged repeatedly must have bounded variance (temp-0 panel)."""
    scores: list[int] = []
    for _ in range(3):
        verdict = await score_answer_panel_async(
            question="What is the rate limit for the Claude API?",
            answer="The Claude API default rate limit is 50 requests per minute [1].",
            expected_gist="The Claude API default rate limit is 50 requests per minute.",
        )
        assert verdict is not None
        scores.append(verdict.score)
    assert max(scores) - min(scores) <= 1, f"panel score unstable across runs: {scores}"
