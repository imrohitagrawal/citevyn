"""Tests + CI regression gate for the RAG eval harness (Phase 0, #96).

Three concerns live here:

1. **Golden-set integrity** — ``golden.jsonl`` parses, ids are unique,
   every case is internally consistent, and coverage spans all five
   product areas plus paraphrase + refusal kinds.
2. **Retrieval hit-rate gate** — the hermetic hit-rate must not regress
   below the recorded baseline, and refusal cases must retrieve nothing.
   This is the build gate: it runs in the standard ``not postgres`` CI
   job with no workflow change.
3. **LLM-judge smoke** — exercised only when a real provider key is
   configured (``CITEVYN_EVAL_LLM=1``); otherwise skipped so CI stays
   hermetic and never fabricates a judge score.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from typing import Any

import pytest

from tests.eval import load_cases
from tests.eval.cases import KINDS
from tests.eval.paths import GOLDEN_PATH
from tests.eval.retrieval import evaluate_retrieval
from tests.eval.thresholds import (
    MAX_REFUSAL_LEAKS,
    MIN_LITERAL_HIT_RATE,
    MIN_OVERALL_HIT_RATE,
)

EXPECTED_AREAS = {"claude_api", "claude_code", "codex", "gemini_api", "citevyn"}


def test_golden_parses_and_ids_unique() -> None:
    cases = load_cases(GOLDEN_PATH)
    assert len(cases) >= 20, "golden set should have at least 20 cases"
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)), "case ids must be unique"


def test_golden_covers_all_areas_and_kinds() -> None:
    cases = load_cases(GOLDEN_PATH)
    areas = {c.area for c in cases}
    assert areas >= EXPECTED_AREAS, f"missing product areas: {EXPECTED_AREAS - areas}"
    kinds = {c.kind for c in cases}
    assert kinds <= KINDS
    assert "literal" in kinds
    assert "paraphrase" in kinds, "need semantic paraphrase cases to expose the dead vector arm"
    assert "refusal" in kinds, "need out-of-corpus refusal cases"


def test_every_area_has_a_paraphrase() -> None:
    """Each product area must carry at least one zero-overlap paraphrase.

    Paraphrases are what make the harness sensitive to the dead vector
    arm (#97).  A regression that dropped them would silently gut the
    signal, so the coverage is asserted, not assumed.
    """
    cases = load_cases(GOLDEN_PATH)
    para_areas = {c.area for c in cases if c.kind == "paraphrase"}
    assert para_areas >= EXPECTED_AREAS, (
        f"areas missing a paraphrase: {EXPECTED_AREAS - para_areas}"
    )


def test_retrieval_hit_rate_gate() -> None:
    """The build gate: hermetic retrieval hit-rate must not regress.

    Runs the live routing + hybrid-retrieval path against the seeded corpus.
    Literal cases hit deterministically; paraphrases miss (dead vector arm,
    #97); refusals must retrieve nothing. Thresholds are pinned to the Phase 0
    baseline so this fails on *regression*, not on the known-dead state.
    """
    report = asyncio.run(evaluate_retrieval(load_cases(GOLDEN_PATH)))
    assert report.hit_rate("literal") >= MIN_LITERAL_HIT_RATE, (
        f"literal hit-rate regressed to {report.hit_rate('literal'):.3f}; "
        f"misses: {[o.case_id for o in report.outcomes if o.kind == 'literal' and not o.hit]}"
    )
    assert report.overall_hit_rate >= MIN_OVERALL_HIT_RATE, (
        f"overall answerable hit-rate {report.overall_hit_rate:.3f} "
        f"below floor {MIN_OVERALL_HIT_RATE}"
    )
    assert report.refusal_leaks <= MAX_REFUSAL_LEAKS, (
        f"{report.refusal_leaks} out-of-corpus case(s) leaked a chunk: "
        f"{[o.case_id for o in report.outcomes if o.leaked]}"
    )


def test_followup_misses_single_turn() -> None:
    """Gap control (Phase 3b prerequisite): the anaphoric follow-up questions must
    MISS when retrieved single-turn (no conversation memory yet).

    Each ``followup`` case's final question ("How can I raise it?") names no product,
    so it routes to ``unsupported`` → the global confidence-gated arm finds nothing on
    the hermetic path. A 0.0 followup hit-rate here is what makes the eventual hit
    attributable to conversation memory (Phase 3b) rather than the case being
    trivially answerable. It is the follow-up analogue of
    ``test_paraphrase_baseline_is_dead`` and is EXPECTED to flip once the feature
    wires memory into the retrieval path (update the story in §8b then).
    """
    cases = load_cases(GOLDEN_PATH)
    assert any(c.kind == "followup" for c in cases), "golden set must carry followup cases"
    report = asyncio.run(evaluate_retrieval(cases))
    assert report.followup_hit_rate == 0.0, (
        "a follow-up hit single-turn without memory — the gap is not real: "
        f"{[o.case_id for o in report.outcomes if o.kind == 'followup' and o.hit]}"
    )


def test_paraphrase_baseline_is_dead() -> None:
    """Guardrail on the baseline story itself.

    Paraphrase hit-rate is 0 today for two compounding reasons, both of which
    hold on the hermetic SQLite path: (a) the vector arm is off — the harness
    hard-wires ``embedder=None`` AND ``VectorRetriever`` also short-circuits on
    a non-postgres dialect, so populating embeddings (Phase 1) will NOT flip
    this in CI; and (b) the keyword arm misses these paraphrases (they either
    route to ``unsupported`` under hard domain scoping, or share only the single
    domain token and fail the ≥2-distinct-token floor).

    Asserting ``== 0.0`` is a deliberate tripwire: it catches a paraphrase
    accidentally rewritten to share keyword vocabulary (a false 'literal' that
    would silently inflate the baseline). It is EXPECTED to go red when Phase 2
    upgrades keyword ranking (BM25/tsvector) or softens domain scoping — at that
    point update the baseline in ``docs/RAG_QUALITY_PLAN.md`` §8a rather than
    weakening the assertion.
    """
    report = asyncio.run(evaluate_retrieval(load_cases(GOLDEN_PATH)))
    assert report.hit_rate("paraphrase") == 0.0, (
        "paraphrase hit-rate is unexpectedly non-zero on SQLite (vector arm dead); "
        "a paraphrase may be leaking keyword overlap: "
        f"{[o.case_id for o in report.outcomes if o.kind == 'paraphrase' and o.hit]}"
    )


# ---------------------------------------------------------------------------
# Loader validation (hermetic — the harness's own correctness is load-bearing)
# ---------------------------------------------------------------------------


def _write_jsonl(tmp_path: object, lines: list[str]) -> pathlib.Path:
    import pathlib as _pl

    path = _pl.Path(str(tmp_path)) / "cases.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_loader_skips_comments_and_blanks(tmp_path: object) -> None:
    good = (
        '{"id":"a","area":"codex","kind":"literal","question":"q",'
        '"expected_source":"codex","expected_gist":"g","expect_no_answer":false}'
    )
    path = _write_jsonl(tmp_path, ["# comment", "", "   ", good])
    cases = load_cases(path)
    assert [c.id for c in cases] == ["a"]


def test_loader_rejects_duplicate_id(tmp_path: object) -> None:
    row = (
        '{"id":"dup","area":"codex","kind":"literal","question":"q",'
        '"expected_source":"codex","expected_gist":"g"}'
    )
    path = _write_jsonl(tmp_path, [row, row])
    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases(path)


def test_loader_rejects_invalid_json(tmp_path: object) -> None:
    path = _write_jsonl(tmp_path, ["{not json}"])
    with pytest.raises(ValueError, match="invalid JSON"):
        load_cases(path)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"id": "x", "area": "codex", "question": "q", "expected_gist": "g"}, "missing required"),
        (
            {"id": "x", "area": "codex", "kind": "bogus", "question": "q", "expected_gist": "g"},
            "unknown kind",
        ),
        (
            # refusal must not name a source
            {
                "id": "x",
                "area": "o",
                "kind": "refusal",
                "question": "q",
                "expected_gist": "g",
                "expected_source": "codex",
                "expect_no_answer": True,
            },
            "must not set expected_source",
        ),
        (
            # refusal must expect a no-answer
            {"id": "x", "area": "o", "kind": "refusal", "question": "q", "expected_gist": "g"},
            "must set expect_no_answer",
        ),
        (
            # answerable must name a source
            {"id": "x", "area": "codex", "kind": "literal", "question": "q", "expected_gist": "g"},
            "must set a non-empty expected_source",
        ),
        (
            # answerable must not expect a no-answer
            {
                "id": "x",
                "area": "codex",
                "kind": "literal",
                "question": "q",
                "expected_gist": "g",
                "expected_source": "codex",
                "expect_no_answer": True,
            },
            "must not set expect_no_answer",
        ),
    ],
)
def test_case_validation_rejects_inconsistent_rows(payload: dict[str, object], match: str) -> None:
    from tests.eval.cases import EvalCase

    with pytest.raises(ValueError, match=match):
        EvalCase.from_dict(payload, origin="test")


def test_multihop_case_parses_with_expected_sources() -> None:
    from tests.eval.cases import EvalCase

    case = EvalCase.from_dict(
        {
            "id": "mh",
            "area": "cross_product",
            "kind": "multihop",
            "question": "compare claude api and gemini api",
            "expected_gist": "both",
            "expected_sources": ["claude_api", "gemini_api"],
        },
        origin="test",
    )
    assert case.expected_sources == ("claude_api", "gemini_api")
    assert case.expected_source is None


@pytest.mark.parametrize(
    "payload,match",
    [
        (
            {  # multihop needs >=2 areas
                "id": "mh1",
                "area": "x",
                "kind": "multihop",
                "question": "q",
                "expected_gist": "g",
                "expected_sources": ["claude_api"],
            },
            "must set expected_sources with >=2",
        ),
        (
            {  # multihop must not use the single expected_source
                "id": "mh2",
                "area": "x",
                "kind": "multihop",
                "question": "q",
                "expected_gist": "g",
                "expected_source": "claude_api",
            },
            "uses expected_sources",
        ),
        (
            {  # a non-multihop case must not set expected_sources
                "id": "l1",
                "area": "x",
                "kind": "literal",
                "question": "q",
                "expected_gist": "g",
                "expected_source": "claude_api",
                "expected_sources": ["claude_api", "gemini_api"],
            },
            "use kind='multihop'",
        ),
    ],
)
def test_multihop_validation_rejects_bad_rows(payload: dict[str, object], match: str) -> None:
    from tests.eval.cases import EvalCase

    with pytest.raises(ValueError, match=match):
        EvalCase.from_dict(payload, origin="test")


def test_followup_case_parses_with_history() -> None:
    from tests.eval.cases import EvalCase

    case = EvalCase.from_dict(
        {
            "id": "fu",
            "area": "claude_api",
            "kind": "followup",
            "history": ["What is the rate limit for the Claude API?"],
            "question": "How can I raise it?",
            "expected_source": "claude_api",
            "expected_gist": "raise the rate limit via the env var",
        },
        origin="test",
    )
    assert case.history == ("What is the rate limit for the Claude API?",)
    assert case.expected_source == "claude_api"
    assert case.expected_sources is None


@pytest.mark.parametrize(
    "payload,match",
    [
        (
            {  # followup needs a non-empty history
                "id": "fu1",
                "area": "claude_api",
                "kind": "followup",
                "question": "How can I raise it?",
                "expected_source": "claude_api",
                "expected_gist": "g",
            },
            "must set a non-empty history",
        ),
        (
            {  # followup needs an expected_source
                "id": "fu2",
                "area": "claude_api",
                "kind": "followup",
                "history": ["prior"],
                "question": "q",
                "expected_gist": "g",
            },
            "must set a non-empty expected_source",
        ),
        (
            {  # followup must not use expected_sources (the plural is multihop's)
                "id": "fu3",
                "area": "claude_api",
                "kind": "followup",
                "history": ["prior"],
                "question": "q",
                "expected_source": "claude_api",
                "expected_sources": ["claude_api", "gemini_api"],
                "expected_gist": "g",
            },
            "sets expected_sources",
        ),
        (
            {  # history belongs only to a followup case
                "id": "fu4",
                "area": "claude_api",
                "kind": "literal",
                "history": ["prior"],
                "question": "q",
                "expected_source": "claude_api",
                "expected_gist": "g",
            },
            "only kind='followup'",
        ),
        (
            {  # a stringly-typed history must be rejected, not char-split
                "id": "fu5",
                "area": "claude_api",
                "kind": "followup",
                "history": "prior turn",
                "question": "q",
                "expected_source": "claude_api",
                "expected_gist": "g",
            },
            "history must be a list",
        ),
    ],
)
def test_followup_validation_rejects_bad_rows(payload: dict[str, object], match: str) -> None:
    from tests.eval.cases import EvalCase

    with pytest.raises(ValueError, match=match):
        EvalCase.from_dict(payload, origin="test")


def test_followup_excluded_from_core_overall_hit_rate() -> None:
    """A followup case must never enter the gated core overall hit-rate — even when it
    misses (single-turn, pre-memory), it must not drag the literal+paraphrase gate."""
    from tests.eval.retrieval import RetrievalOutcome, RetrievalReport

    def _outcome(kind: str, hit: bool) -> RetrievalOutcome:
        return RetrievalOutcome(
            case_id=f"{kind}-x",
            area="a",
            kind=kind,
            domain="claude_api",
            expected_source="claude_api",
            retrieved_sources=("claude_api",) if hit else (),
            hit=hit,
            leaked=False,
        )

    report = RetrievalReport(
        outcomes=(
            _outcome("literal", True),
            _outcome("paraphrase", True),
            _outcome("followup", False),  # misses (single-turn) — must not count
        )
    )
    # Core overall = literal + paraphrase only (both hit) → 1.0, unaffected by the miss.
    assert report.overall_hit_rate == 1.0
    d = report.as_dict()
    assert d["answerable_total"] == 2, "followup must be excluded from the gated denominator"
    assert d["followup_total"] == 1
    assert d["followup_hits"] == 0
    assert d["followup_hit_rate"] == 0.0


# ---------------------------------------------------------------------------
# Judge score parser (hermetic — no live LLM)
# ---------------------------------------------------------------------------


def test_judge_parses_clean_and_fenced_output() -> None:
    from tests.eval.judge import parse_verdict

    v = parse_verdict('{"score": 4, "rationale": "close"}')
    assert v.score == 4 and v.rationale == "close"
    # tolerant of a code fence / surrounding prose
    fenced = parse_verdict('```json\n{"score": 5, "rationale": "ok"}\n```')
    assert fenced.score == 5


@pytest.mark.parametrize(
    "text",
    [
        "no json here",
        '{"rationale": "missing score"}',
        '{"score": 9, "rationale": "out of range"}',
        '{"score": 0, "rationale": "out of range"}',
        '{"score": "high", "rationale": "not numeric"}',
        '{"score": true, "rationale": "bool is not a score"}',
        '{"score": NaN, "rationale": "non-finite"}',
        '{"score": Infinity, "rationale": "non-finite"}',
        "{broken json",
    ],
)
def test_judge_rejects_bad_output_loudly(text: str) -> None:
    """A malformed judge response must raise, never coerce to a middling score."""
    from tests.eval.judge import JudgeParseError, parse_verdict

    with pytest.raises(JudgeParseError):
        parse_verdict(text)


# ---------------------------------------------------------------------------
# Gate logic (hermetic — a regressed summary must produce failures)
# ---------------------------------------------------------------------------


def test_gate_flags_a_regressed_summary() -> None:
    from tests.eval.runner import gate_failures

    regressed: dict[str, Any] = {
        "retrieval": {
            "answerable_total": 15,
            "overall_hit_rate": 0.40,
            "hit_rate_by_kind": {"literal": 0.80, "paraphrase": 0.0},
            "refusal_leaks": 1,
        },
        # Judge ran, so the judged refusal metric (orchestrator answered a refusal)
        # is the authoritative refusal gate, not the retrieval count.
        "judge": {
            "available": True,
            "judged": 15,
            "scored": 15,
            "mean_score": 2.0,
            "refusal_leaks_judged": 1,
        },
    }
    failures = gate_failures(regressed)
    # literal < 1.0, overall < 0.60, a judged refusal leak, and mean judge < 3.0 → 4 reasons
    assert len(failures) == 4, failures


def test_refusal_gate_uses_judged_metric_when_llm_ran() -> None:
    """Under "answer when grounded", a retrieval "leak" the LLM correctly declines
    must NOT fail the gate when the judge ran — the orchestrator's decision governs."""
    from tests.eval.runner import gate_failures

    summary: dict[str, Any] = {
        "retrieval": {
            "answerable_total": 15,
            "overall_hit_rate": 1.0,
            "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 1.0},
            "refusal_leaks": 1,  # a chunk WAS retrieved for a refusal...
        },
        # ...but the LLM declined it, so 0 judged leaks → gate passes.
        "judge": {
            "available": True,
            "judged": 15,
            "scored": 15,
            "mean_score": 4.0,
            "refusal_leaks_judged": 0,
        },
    }
    assert gate_failures(summary) == []


def _multihop_summary(*, mode: str, multihop_hit_rate: float) -> dict[str, Any]:
    return {
        "retrieval": {
            "answerable_total": 15,
            "overall_hit_rate": 1.0,
            "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 1.0, "multihop": multihop_hit_rate},
            "refusal_leaks": 0,
            "multihop_total": 3,
            "multihop_hit_rate": multihop_hit_rate,
        },
        "judge": {"available": False, "judged": 0, "scored": 0, "mean_score": None},
        "embedder": {"mode": mode},
    }


def test_multihop_gate_fails_on_postgres_when_below_threshold() -> None:
    from tests.eval.runner import gate_failures

    failures = gate_failures(_multihop_summary(mode="postgres", multihop_hit_rate=0.66))
    assert any("multihop" in f for f in failures), failures


def test_multihop_gate_passes_on_postgres_when_all_hit() -> None:
    from tests.eval.runner import gate_failures

    assert gate_failures(_multihop_summary(mode="postgres", multihop_hit_rate=1.0)) == []


def test_multihop_not_gated_on_hermetic_sqlite() -> None:
    """A low multihop rate on the hermetic (non-postgres) run must NOT fail the gate
    — multihop is Postgres-only-provable and excluded from the standard CI gate."""
    from tests.eval.runner import gate_failures

    failures = gate_failures(_multihop_summary(mode="sqlite-hermetic", multihop_hit_rate=0.0))
    assert not any("multihop" in f for f in failures), failures


def test_refusal_gate_uses_retrieval_metric_when_no_llm() -> None:
    """With no LLM (hermetic SQLite), the retrieval refusal count is exact (dead
    vector arm → refusals retrieve nothing) and remains the gate."""
    from tests.eval.runner import gate_failures

    summary: dict[str, Any] = {
        "retrieval": {
            "answerable_total": 15,
            "overall_hit_rate": 0.667,
            "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 0.0},
            "refusal_leaks": 1,
        },
        "judge": {"available": False, "judged": 0, "scored": 0, "mean_score": None},
    }
    assert any("retrieval" in f and "refusal" in f for f in gate_failures(summary))


def test_gate_passes_a_healthy_baseline_summary() -> None:
    from tests.eval.runner import gate_failures

    healthy: dict[str, Any] = {
        "retrieval": {
            "answerable_total": 15,
            "overall_hit_rate": 0.667,
            "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 0.0},
            "refusal_leaks": 0,
        },
        # judge unavailable (stub) must not fail the gate
        "judge": {"available": False, "judged": 0, "scored": 0, "mean_score": None},
    }
    assert gate_failures(healthy) == []


def test_gate_flags_degenerate_zero_answerable() -> None:
    """A golden subset with no answerable cases must FAIL, not pass on empty-pool 1.0."""
    from tests.eval.runner import gate_failures

    degenerate: dict[str, Any] = {
        "retrieval": {
            "answerable_total": 0,
            "overall_hit_rate": 1.0,  # vacuous empty-pool rate
            "hit_rate_by_kind": {},  # no literal bucket
            "refusal_leaks": 0,
        },
        "judge": {"available": False, "judged": 0, "scored": 0, "mean_score": None},
    }
    failures = gate_failures(degenerate)
    assert any("no answerable" in f for f in failures), failures
    assert any("no literal" in f for f in failures), failures


def test_gate_flags_total_and_partial_judge_outage() -> None:
    """A judge that scored nothing, or only a biased survivor subset, must FAIL."""
    from tests.eval.runner import gate_failures

    base_retrieval: dict[str, Any] = {
        "answerable_total": 15,
        "overall_hit_rate": 0.667,
        "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 0.0},
        "refusal_leaks": 0,
    }
    total_outage: dict[str, Any] = {
        "retrieval": base_retrieval,
        "judge": {"available": True, "judged": 20, "scored": 0, "mean_score": None},
    }
    assert any("total judge outage" in f for f in gate_failures(total_outage))

    # 5/20 survivors scored a perfect 5.0 — must not pass on that inflated mean.
    partial_outage: dict[str, Any] = {
        "retrieval": base_retrieval,
        "judge": {"available": True, "judged": 20, "scored": 5, "mean_score": 5.0},
    }
    failures = gate_failures(partial_outage)
    assert any("coverage" in f for f in failures), failures


@pytest.mark.skipif(
    os.getenv("CITEVYN_EVAL_LLM") != "1",
    reason="LLM judge requires a real provider key; set CITEVYN_EVAL_LLM=1 to run",
)
def test_llm_judge_scores_a_grounded_answer() -> None:  # pragma: no cover - opt-in
    from tests.eval.judge import score_answer

    verdict = score_answer(
        question="What is the rate limit for the Claude API?",
        answer="The Claude API default rate limit is 50 requests per minute [1].",
        expected_gist="50 requests per minute default",
    )
    assert verdict is not None, "judge returned unavailable despite a real provider key"
    assert 1 <= verdict.score <= 5
    assert verdict.score >= 3, "a correct grounded answer should not score below 3"
