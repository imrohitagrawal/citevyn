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
    MIN_FOLLOWUP_HIT_RATE,
    MIN_LITERAL_HIT_RATE,
    MIN_OVERALL_HIT_RATE,
)

EXPECTED_AREAS = {"claude_api", "claude_code", "codex", "gemini_api", "citevyn", "concepts"}


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


def test_golden_gold_chunks_reference_real_seeded_chunks() -> None:
    """Every ``gold_chunks`` key in the golden file must name a chunk the conftest seed
    actually produces (#125). A typo'd gold key would silently zero a case's reciprocal
    rank (it can never match a retrieved key), quietly deflating MRR/precision@1 — so the
    labels are validated against the seed's stable ``"{source_name}#{chunk_order}"`` keys.
    Only a refusal may lack gold_chunks; multihop is multi-relevant and opts out.
    """
    import asyncio

    from tests.eval.retrieval import _chunk_key_map, seeded_session

    async def _seed_keys() -> set[str]:
        async with seeded_session() as session:
            return set((await _chunk_key_map(session)).values())

    seeded = asyncio.run(_seed_keys())
    assert seeded == {f"{a}#0" for a in EXPECTED_AREAS}, seeded
    for case in load_cases(GOLDEN_PATH):
        for key in case.gold_chunks:
            assert key in seeded, (
                f"case {case.id!r} labels gold chunk {key!r} which the seed does not produce; "
                f"valid keys: {sorted(seeded)}"
            )


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
    # Phase 3b: conversation memory resolves anaphoric follow-ups deterministically, so
    # the followup bucket is gated on the hermetic run too (a broken rewrite fails CI).
    assert report.followup_hit_rate >= MIN_FOLLOWUP_HIT_RATE, (
        f"followup hit-rate regressed to {report.followup_hit_rate:.3f}; "
        f"misses: {[o.case_id for o in report.outcomes if o.kind == 'followup' and not o.hit]}"
    )


def test_followup_raw_misses_without_memory() -> None:
    """Permanent raw-miss control (adversarial finding #5): with conversation memory
    OFF, each anaphoric follow-up ("How can I raise it?") names no product, routes to
    ``unsupported``, and the global arm finds nothing on the hermetic path → MISS.

    This is what makes the memory-ON hit attributable to memory rather than the case
    being trivially answerable — the follow-up analogue of
    ``test_paraphrase_baseline_is_dead``. If a future embedder ever made the raw
    follow-up hit, this trips and the gap (and the feature's value) must be revisited.
    """
    cases = load_cases(GOLDEN_PATH)
    assert any(c.kind == "followup" for c in cases), "golden set must carry followup cases"
    report = asyncio.run(evaluate_retrieval(cases, use_memory=False))
    assert report.followup_hit_rate == 0.0, (
        "a follow-up hit WITHOUT memory — the gap is not real: "
        f"{[o.case_id for o in report.outcomes if o.kind == 'followup' and o.hit]}"
    )


def test_followup_hits_with_memory() -> None:
    """Conversation memory (Phase 3b) resolves each follow-up against its history and
    retrieves the expected area — hermetically (the rewrite routes to the product
    domain + the keyword arm hits, no vector arm needed). This is the hermetic gate."""
    cases = load_cases(GOLDEN_PATH)
    report = asyncio.run(evaluate_retrieval(cases))
    assert report.followup_hit_rate >= MIN_FOLLOWUP_HIT_RATE, (
        f"followup hit-rate {report.followup_hit_rate:.3f} < {MIN_FOLLOWUP_HIT_RATE}; "
        f"misses: {[o.case_id for o in report.outcomes if o.kind == 'followup' and not o.hit]}"
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
        (
            # a refusal has no correct chunk to rank → must not label a gold chunk (#125)
            {
                "id": "x",
                "area": "o",
                "kind": "refusal",
                "question": "q",
                "expected_gist": "g",
                "expect_no_answer": True,
                "gold_chunks": ["claude_api#0"],
            },
            "must not set gold_chunks",
        ),
        (
            # gold_chunks must be a list of non-empty strings (#125)
            {
                "id": "x",
                "area": "codex",
                "kind": "literal",
                "question": "q",
                "expected_gist": "g",
                "expected_source": "codex",
                "gold_chunks": ["codex#0", "  "],
            },
            "gold_chunks must be a list of non-empty strings",
        ),
        (
            # multihop is multi-relevant (scored by expected_sources) → must not carry a
            # single gold_chunks that would wrongly pull it into the rank pool (#125)
            {
                "id": "x",
                "area": "cross",
                "kind": "multihop",
                "question": "q",
                "expected_gist": "g",
                "expected_sources": ["claude_api", "gemini_api"],
                "gold_chunks": ["claude_api#0"],
            },
            "multihop case .* must not set gold_chunks",
        ),
        (
            # judge_only (#112) is validated only on the orchestrator-driven judged run, which
            # runs on --postgres → it must be postgres_only too.
            {
                "id": "x",
                "area": "codex",
                "kind": "literal",
                "question": "q",
                "expected_gist": "g",
                "expected_source": "codex",
                "judge_only": True,
            },
            "sets judge_only but not postgres_only",
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


def _ranked_outcome(
    case_id: str,
    *,
    gold: tuple[str, ...],
    retrieved: tuple[str, ...],
    kind: str = "paraphrase",
):
    """Build a RetrievalOutcome with chunk-key plumbing for the rank-metric tests."""
    from tests.eval.retrieval import RetrievalOutcome

    return RetrievalOutcome(
        case_id=case_id,
        area="a",
        kind=kind,
        domain="claude_api",
        expected_source="claude_api",
        retrieved_sources=tuple(k.split("#")[0] for k in retrieved),
        hit=bool(retrieved),
        leaked=False,
        retrieved_chunk_keys=retrieved,
        gold_chunks=gold,
    )


def test_reciprocal_rank_and_precision_at_1_are_rank_sensitive() -> None:
    """The per-case rank metric must reward a gold chunk at rank 1 and decay with rank."""
    # gold at rank 1
    o1 = _ranked_outcome("a", gold=("claude_api#0",), retrieved=("claude_api#0", "codex#0"))
    assert o1.reciprocal_rank() == 1.0
    assert o1.precision_at_1() is True
    # gold at rank 2 (a wrong-area chunk outranks it)
    o2 = _ranked_outcome("b", gold=("claude_api#0",), retrieved=("codex#0", "claude_api#0"))
    assert o2.reciprocal_rank() == 0.5
    assert o2.precision_at_1() is False
    # gold absent / nothing retrieved
    o3 = _ranked_outcome("c", gold=("claude_api#0",), retrieved=())
    assert o3.reciprocal_rank() == 0.0
    assert o3.precision_at_1() is False


def test_mrr_and_precision_pool_only_single_relevant_answerable() -> None:
    """MRR/precision@1 average ONLY over answerable cases with exactly one gold chunk;
    refusals, multi-gold cases, and unlabelled cases opt out. An empty pool → 1.0."""
    from tests.eval.retrieval import RetrievalOutcome, RetrievalReport

    report = RetrievalReport(
        outcomes=(
            _ranked_outcome("rank1", gold=("claude_api#0",), retrieved=("claude_api#0",)),
            _ranked_outcome("rank2", gold=("gemini_api#0",), retrieved=("codex#0", "gemini_api#0")),
            # multi-gold (multi-relevant) — excluded from the single-relevant rank metric
            _ranked_outcome(
                "multi", gold=("claude_api#0", "gemini_api#0"), retrieved=("claude_api#0",)
            ),
            # unlabelled answerable — opts out
            _ranked_outcome("nogold", gold=(), retrieved=("codex#0",)),
            # a refusal must never enter the pool even if mislabelled data slipped a key in
            RetrievalOutcome(
                case_id="refusal",
                area="o",
                kind="refusal",
                domain="unsupported",
                expected_source=None,
                retrieved_sources=(),
                hit=False,
                leaked=False,
            ),
        )
    )
    d = report.as_dict()
    assert d["ranked_total"] == 2, "only the two single-gold answerable cases count"
    # MRR = mean(1.0, 0.5) = 0.75; precision@1 = mean(1, 0) = 0.5
    assert d["mrr"] == 0.75
    assert d["precision_at_1"] == 0.5

    # An empty single-relevant pool is vacuously 1.0 (guarded by the non-empty gate).
    empty = RetrievalReport(outcomes=(_ranked_outcome("nogold", gold=(), retrieved=("codex#0",)),))
    assert empty.as_dict()["ranked_total"] == 0
    assert empty.mrr == 1.0
    assert empty.precision_at_1 == 1.0


def test_judge_only_case_is_excluded_from_the_retrieval_report() -> None:
    """A judge_only followup (#112) needs the orchestrator's LLM rewrite, which the hermetic
    retrieval path never calls — so it must NOT appear in the retrieval report (else it would
    spuriously miss and drag followup_hit_rate/overall below the locked floor)."""
    from tests.eval.cases import EvalCase
    from tests.eval.retrieval import evaluate_retrieval

    judge_only = EvalCase.from_dict(
        {
            "id": "judge_only_followup",
            "area": "gemini_api",
            "kind": "followup",
            "postgres_only": True,
            "judge_only": True,
            "history": ["How do I authenticate to the Gemini API?"],
            "question": "Is there a credentials file option?",
            "expected_source": "gemini_api",
            "expected_gist": "g",
        },
        origin="test",
    )
    plain = next(c for c in load_cases(GOLDEN_PATH) if c.id == "gemini_api_lit_authheader")
    # Even on the --postgres path (postgres=... would seed real vectors), judge_only is filtered
    # BEFORE any DB work, so we can assert exclusion on the hermetic run without Postgres.
    report = asyncio.run(evaluate_retrieval([plain, judge_only]))
    ids = {o.case_id for o in report.outcomes}
    assert "judge_only_followup" not in ids
    assert "gemini_api_lit_authheader" in ids


def test_live_retrieval_populates_rank_aligned_chunk_keys() -> None:
    """End-to-end (hermetic, real HybridRetriever): a literal case must produce non-empty
    retrieved_chunk_keys that are rank-aligned with retrieved_sources and carry the gold at
    rank 1 — proving the live chunk_id→key wiring, not just the hand-built unit outcomes."""
    report = asyncio.run(evaluate_retrieval(load_cases(GOLDEN_PATH)))
    lit = next(o for o in report.outcomes if o.case_id == "claude_api_lit_ratelimit")
    assert lit.retrieved_chunk_keys, "live retrieval produced no chunk keys"
    # rank-aligned: key[i] and source[i] describe the SAME hit.
    assert len(lit.retrieved_chunk_keys) == len(lit.retrieved_sources)
    assert lit.retrieved_chunk_keys[0].split("#")[0] == lit.retrieved_sources[0]
    # the labelled gold sits at rank 1 (keyword arm hits it deterministically on SQLite).
    assert lit.gold_chunks == ("claude_api#0",)
    assert lit.reciprocal_rank() == 1.0
    assert lit.precision_at_1() is True


def test_unmapped_chunk_id_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the retriever returns a chunk absent from the identity map, the harness must RAISE
    (never silently drop the hit, which would shift ranks and corrupt MRR)."""
    import tests.eval.retrieval as retrieval_mod

    async def _empty_map(_session: Any) -> dict[str, str]:
        return {}  # simulate a chunk_id that maps to nothing

    monkeypatch.setattr(retrieval_mod, "_chunk_key_map", _empty_map)
    # A literal case retrieves a real chunk hermetically → its chunk_id misses the empty map.
    lit = [c for c in load_cases(GOLDEN_PATH) if c.id == "claude_api_lit_ratelimit"]
    with pytest.raises(RuntimeError, match="absent from the chunk identity map"):
        asyncio.run(evaluate_retrieval(lit))


def test_gate_gates_mrr_and_precision_only_on_postgres() -> None:
    """The rank metric is gated ONLY on the --postgres run, guarded on a non-empty pool,
    and never KeyErrors on a summary that predates the keys (skeptic-3 fixes)."""
    from tests.eval.runner import gate_failures

    base: dict[str, Any] = {
        "answerable_total": 15,
        "overall_hit_rate": 1.0,
        "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 1.0},
        "refusal_leaks": 0,
    }
    judge = {"available": False, "judged": 0, "scored": 0, "mean_score": None}

    # Regressed rank metric on the postgres run → FAILS on both axes.
    regressed = {
        "retrieval": {**base, "ranked_total": 18, "mrr": 0.80, "precision_at_1": 0.90},
        "judge": judge,
        "embedder": {"mode": "postgres"},
    }
    fails = gate_failures(regressed)
    assert any("precision@1" in f for f in fails), fails
    assert any("MRR" in f for f in fails), fails

    # The two axes are independent: an MRR-only regression (precision@1 still 1.0) must
    # flag ONLY MRR, and vice versa — so a bug collapsing one into the other is caught.
    mrr_only = {
        "retrieval": {**base, "ranked_total": 18, "mrr": 0.80, "precision_at_1": 1.0},
        "judge": judge,
        "embedder": {"mode": "postgres"},
    }
    mrr_fails = gate_failures(mrr_only)
    assert any("MRR" in f for f in mrr_fails) and not any("precision@1" in f for f in mrr_fails)
    prec_only = {
        "retrieval": {**base, "ranked_total": 18, "mrr": 1.0, "precision_at_1": 0.90},
        "judge": judge,
        "embedder": {"mode": "postgres"},
    }
    prec_fails = gate_failures(prec_only)
    assert any("precision@1" in f for f in prec_fails) and not any("MRR" in f for f in prec_fails)

    # The SAME regressed numbers on the hermetic run are NOT gated (informational only).
    hermetic = {**regressed, "embedder": {"mode": "sqlite-hermetic"}}
    assert not any("precision@1" in f or "MRR" in f for f in gate_failures(hermetic))

    # A summary with no rank keys at all (older run / no gold_chunks) must not KeyError
    # nor fail vacuously.
    legacy = {
        "retrieval": base,
        "judge": judge,
        "embedder": {"mode": "postgres"},
    }
    assert not any("precision@1" in f or "MRR" in f for f in gate_failures(legacy))

    # A healthy postgres run at baseline passes the rank gate.
    healthy = {
        "retrieval": {**base, "ranked_total": 15, "mrr": 1.0, "precision_at_1": 1.0},
        "judge": judge,
        "embedder": {"mode": "postgres"},
    }
    assert not any("precision@1" in f or "MRR" in f for f in gate_failures(healthy))


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


def _followup_summary(*, mode: str, followup_hit_rate: float) -> dict[str, Any]:
    return {
        "retrieval": {
            "answerable_total": 15,
            "overall_hit_rate": 1.0,
            "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 1.0, "followup": followup_hit_rate},
            "refusal_leaks": 0,
            "followup_total": 3,
            "followup_hit_rate": followup_hit_rate,
        },
        "judge": {"available": False, "judged": 0, "scored": 0, "mean_score": None},
        "embedder": {"mode": mode},
    }


def test_followup_gate_fails_below_threshold_on_both_modes() -> None:
    """Unlike multihop, the followup gate is enforced on the HERMETIC run too — the
    rewrite resolves deterministically, so a broken rewrite must fail CI."""
    from tests.eval.runner import gate_failures

    for mode in ("sqlite-hermetic", "postgres"):
        failures = gate_failures(_followup_summary(mode=mode, followup_hit_rate=0.66))
        assert any("followup" in f for f in failures), (mode, failures)


def test_followup_gate_passes_when_all_hit() -> None:
    from tests.eval.runner import gate_failures

    for mode in ("sqlite-hermetic", "postgres"):
        assert gate_failures(_followup_summary(mode=mode, followup_hit_rate=1.0)) == []


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


def _grounded_summary(*, mode: str, under: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "retrieval": {
            "answerable_total": 15,
            "overall_hit_rate": 1.0,
            "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 1.0},
            "refusal_leaks": 0,
        },
        "judge": {"available": True, "judged": 15, "scored": 15, "mean_score": 4.0},
        "groundedness": {
            "cases_with_facts": 11,
            "grounded_fact_rate": 1.0 - 0.0909 * len(under),
            "under_grounded": under,
        },
        "embedder": {"mode": mode},
    }


def test_gate_flags_a_single_under_grounded_case_on_postgres() -> None:
    """On the live-retrieval run a SINGLE wrong/absent hard fact must FAIL — the
    deterministic net catches over-scored plausible-but-wrong answers regardless of the
    judge (an aggregate mean over binary single-fact cases would leak one wrong fact)."""
    from tests.eval.runner import gate_failures

    summary = _grounded_summary(
        mode="postgres",
        under=[{"case_id": "claude_api_lit_ratelimit", "coverage": 0.0, "missing": ["50…"]}],
    )
    assert any("not fully grounded" in f.lower() or "grounded" in f for f in gate_failures(summary))


def test_gate_passes_when_every_fact_case_is_grounded_on_postgres() -> None:
    from tests.eval.runner import gate_failures

    assert gate_failures(_grounded_summary(mode="postgres", under=[])) == []


def test_groundedness_not_gated_on_hermetic_run() -> None:
    """Fact-bearing paraphrase cases structurally can't retrieve on the dead-vector-arm
    hermetic path, so groundedness is NOT gated there (mirrors the multihop guard) —
    only the --postgres run bites."""
    from tests.eval.runner import gate_failures

    summary = _grounded_summary(
        mode="sqlite-hermetic",
        under=[{"case_id": "claude_api_par_throttle", "coverage": 0.0, "missing": ["50…"]}],
    )
    assert not any("grounded" in f for f in gate_failures(summary)), gate_failures(summary)


def test_postgres_only_cases_excluded_from_hermetic_retrieval() -> None:
    """Item 2: adversarial postgres-only cases (misspellings, in-domain near-miss
    refusals) must NOT appear in the hermetic retrieval run — they need the live vector
    arm and would misfire on the dead-arm SQLite path."""
    cases = load_cases(GOLDEN_PATH)
    assert any(c.postgres_only for c in cases), "golden set should carry postgres_only cases"
    assert any(not c.postgres_only for c in cases), "golden set should carry hermetic cases"
    report = asyncio.run(evaluate_retrieval(cases))
    seen = {o.case_id for o in report.outcomes}
    # Positive: every NON-postgres_only case survives the hermetic filter (guards against
    # a regression that silently drops all cases and passes the negative check vacuously).
    for c in cases:
        if c.postgres_only:
            assert c.id not in seen, f"postgres_only case {c.id} leaked into the hermetic run"
        else:
            assert c.id in seen, f"hermetic case {c.id} was wrongly dropped from the hermetic run"


def test_injection_gate_flags_a_leak() -> None:
    """Item 2: an answer that emitted a forbidden injection sentinel fails the run."""
    from tests.eval.runner import gate_failures

    summary: dict[str, Any] = {
        "retrieval": {
            "answerable_total": 15,
            "overall_hit_rate": 1.0,
            "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 1.0},
            "refusal_leaks": 0,
        },
        "judge": {"available": True, "judged": 15, "scored": 15, "mean_score": 4.0},
        "injection": {
            "cases": 2,
            "leaks": [{"case_id": "adv_injection_ratelimit", "hits": ["PWNED"]}],
        },
    }
    assert any("injection" in f for f in gate_failures(summary))


def test_injection_gate_passes_when_no_leak() -> None:
    from tests.eval.runner import gate_failures

    summary: dict[str, Any] = {
        "retrieval": {
            "answerable_total": 15,
            "overall_hit_rate": 1.0,
            "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 1.0},
            "refusal_leaks": 0,
        },
        "judge": {"available": True, "judged": 15, "scored": 15, "mean_score": 4.0},
        "injection": {"cases": 2, "leaks": []},
    }
    assert gate_failures(summary) == []


def test_gate_tolerates_missing_groundedness_block() -> None:
    """Hand-built summaries without a groundedness block (older callers) must not crash."""
    from tests.eval.runner import gate_failures

    summary: dict[str, Any] = {
        "retrieval": {
            "answerable_total": 15,
            "overall_hit_rate": 0.667,
            "hit_rate_by_kind": {"literal": 1.0, "paraphrase": 0.0},
            "refusal_leaks": 0,
        },
        "judge": {"available": False, "judged": 0, "scored": 0, "mean_score": None},
    }
    assert gate_failures(summary) == []


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


def test_eval_harness_mirrors_the_orchestrator_alias_canonicalization() -> None:
    """The harness re-implements the orchestrator's query pipeline, so any step it omits
    makes the eval measure a DIFFERENT system than production.

    This pins the ``canonicalize_product_name`` mirror specifically. "what is sitewin?" is
    the precise probe: it routes to ``citevyn`` from the guardrail alone, but its ONLY
    content word is the mangled token, which appears nowhere in the corpus — so without
    canonicalization retrieval comes back EMPTY, exactly as it did in production before
    #84. Deleting the mirror in ``retrieval.py`` must fail HERE rather than leaving it to
    the judged eval, which is secret-gated and gates on an aggregate mean.
    """
    from app.core.config import Settings
    from tests.eval.cases import EvalCase
    from tests.eval.retrieval import _chunk_key_map, _retrieve_sources, seeded_session

    probe = EvalCase(
        id="alias_mirror_probe",
        area="citevyn",
        kind="literal",
        question="what is sitewin?",
        expected_source="citevyn",
        expected_gist="",
        expect_no_answer=False,
        raw={},
    )

    async def _run() -> tuple[tuple[str, ...], str]:
        settings = Settings(llm_provider="stub")
        async with seeded_session() as session:
            key_map = await _chunk_key_map(session)
            sources, _keys, _degrade, routed = await _retrieve_sources(
                session, probe, settings=settings, key_map=key_map
            )
            return sources, routed

    sources, routed = asyncio.run(_run())
    assert routed == "citevyn", f"probe routed to {routed!r}, not citevyn"
    assert "citevyn" in sources, (
        "the harness did not canonicalize 'sitewin' -> 'CiteVyn', so retrieval found "
        "nothing — it has drifted from Orchestrator.ask"
    )
