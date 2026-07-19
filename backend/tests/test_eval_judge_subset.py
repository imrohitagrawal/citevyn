"""Judged-eval subset selection (#153 Layer 6 — CI spend bounding).

The subset is a COST control that deliberately reduces judged coverage. That makes
its contract safety-critical in one specific way: it must never silently drop a
judge-independent oracle (prompt-injection resistance, the multi-turn echo oracle,
or a ``judge_only`` case that has no other validation path at all). These tests pin
that, plus determinism and the "full run is untouched" invariant — a subset feature
that changed the default behaviour would be a regression in the gate itself.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tests.eval.cases import EvalCase, load_cases
from tests.eval.paths import GOLDEN_PATH
from tests.eval.subset import is_priority, select_judge_subset


def _case(
    case_id: str,
    kind: str = "literal",
    *,
    must_not_contain: tuple[str, ...] = (),
    judge_only: bool = False,
    history: tuple[str, ...] = (),
    expected_facts: tuple[str, ...] = (),
) -> EvalCase:
    """Build an EvalCase directly (bypassing ``from_dict``'s cross-field validation).

    The selector only reads ``id``/``kind``/``must_not_contain``/``judge_only``, so a
    minimal instance is the honest fixture here; going through the JSONL schema would
    test the schema, not the selector.
    """
    return EvalCase(
        id=case_id,
        area="claude",
        kind=kind,
        question="q",
        expected_source=None if kind == "refusal" else "claude",
        expected_gist="g",
        expect_no_answer=kind == "refusal",
        raw={},
        history=history,
        must_not_contain=must_not_contain,
        expected_facts=expected_facts,
        judge_only=judge_only,
        postgres_only=judge_only,
    )


# ---------------------------------------------------------------------------
# The full-run invariant
# ---------------------------------------------------------------------------


def test_limit_none_selects_everything_and_drops_nothing() -> None:
    cases = [_case(f"c{i}") for i in range(10)]
    selected, dropped = select_judge_subset(cases, limit=None)
    assert selected == cases
    assert dropped == []


def test_limit_at_or_above_case_count_is_a_full_run() -> None:
    cases = [_case(f"c{i}") for i in range(5)]
    for limit in (5, 6, 500):
        selected, dropped = select_judge_subset(cases, limit=limit)
        assert selected == cases, f"limit={limit} must be a full run"
        assert dropped == []


def test_limit_below_one_is_rejected() -> None:
    cases = [_case(f"c{i}") for i in range(5)]
    with pytest.raises(ValueError):
        select_judge_subset(cases, limit=0)


def test_a_nonsense_limit_is_rejected_even_when_it_would_be_a_full_run() -> None:
    """Validation must precede the `limit >= len(cases)` short-circuit.

    Otherwise a negative limit is silently accepted whenever the case list is small
    enough — a caller bug that then hides until the golden set grows.
    """
    with pytest.raises(ValueError):
        select_judge_subset([], limit=-5)
    with pytest.raises(ValueError):
        select_judge_subset([_case("c0")], limit=-1)


# ---------------------------------------------------------------------------
# Priority: the oracles must survive ANY limit
# ---------------------------------------------------------------------------


def test_every_zero_tolerance_oracle_case_is_never_dropped() -> None:
    """A tiny limit must not switch off a zero-tolerance gate.

    Each of these classes fails the run on a SINGLE occurrence, so dropping one
    does not average a metric down — it disables the check. Sampling is only sound
    for the judge's 1-5 mean.
    """
    cases = [
        _case("inj", must_not_contain=("PWNED",)),
        _case("fu", kind="followup", history=("prior",)),
        _case("facts", expected_facts=("ANTHROPIC_API_KEY",)),
        _case("ref", kind="refusal"),
        _case("jo", judge_only=True),
        *[_case(f"plain{i}") for i in range(20)],
    ]
    oracles = {"inj", "fu", "facts", "ref", "jo"}
    selected, dropped = select_judge_subset(cases, limit=1)
    assert oracles <= {c.id for c in selected}
    assert not (oracles & {c.id for c in dropped})


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("injection", {"must_not_contain": ("PWNED",)}),
        ("groundedness", {"expected_facts": ("ANTHROPIC_API_KEY",)}),
        ("multi-turn echo", {"kind": "followup", "history": ("prior",)}),
        ("refusal leak", {"kind": "refusal"}),
        ("judge-only", {"judge_only": True}),
    ],
)
def test_each_oracle_class_is_pinned_independently(label: str, kwargs: dict[str, object]) -> None:
    """Pinned one-per-test so narrowing is_priority by ONE class still fails.

    A single combined assertion would keep passing as long as any one class was
    still retained, which is exactly how a partial regression slips through.
    """
    target = _case("oracle", **kwargs)  # type: ignore[arg-type]
    cases = [target, *[_case(f"plain{i}") for i in range(20)]]
    selected, _ = select_judge_subset(cases, limit=1)
    assert "oracle" in {c.id for c in selected}, f"the {label} oracle case was dropped"


def test_priority_pool_larger_than_the_limit_is_still_kept_whole() -> None:
    cases = [_case(f"inj{i}", must_not_contain=("X",)) for i in range(5)] + [
        _case(f"plain{i}") for i in range(5)
    ]
    selected, _dropped = select_judge_subset(cases, limit=2)
    # The limit is a target, not a licence to drop an oracle.
    assert len(selected) == 5
    assert all(c.must_not_contain for c in selected)


def test_is_priority_matches_the_documented_classes() -> None:
    assert is_priority(_case("a", must_not_contain=("X",)))
    assert is_priority(_case("b", kind="followup", history=("p",)))
    assert is_priority(_case("c", judge_only=True))
    assert is_priority(_case("d", expected_facts=("X",)))
    assert is_priority(_case("e", kind="refusal"))
    # A plain literal/paraphrase case contributes only to the judge's MEAN, which
    # is the one judged metric that degrades gracefully under sampling.
    assert not is_priority(_case("f"))
    assert not is_priority(_case("g", kind="paraphrase"))


# ---------------------------------------------------------------------------
# Stratification + determinism
# ---------------------------------------------------------------------------


def test_fill_is_stratified_across_kinds() -> None:
    """A bounded run must not collapse onto one kind.

    Ten paraphrases listed first would, under a naive head-N slice, consume the
    whole budget and judge zero literal cases.
    """
    cases = [_case(f"p{i}", kind="paraphrase") for i in range(10)] + [
        _case(f"l{i}") for i in range(10)
    ]
    selected, _ = select_judge_subset(cases, limit=6)
    assert {c.kind for c in selected} == {"literal", "paraphrase"}
    assert sum(1 for c in selected if c.kind == "literal") == 3


def test_a_tight_budget_follows_the_fixed_kind_order_not_file_order() -> None:
    """Stratification uses a FIXED kind order, not the golden file's ordering.

    Round-robin alone is not enough: with lanes ordered by first appearance, which
    kind wins a 1-case budget would depend on how the golden file happens to be
    sorted. ``literal`` leads ``_KIND_ORDER`` because a literal miss is the
    strongest, least flaky answer-quality signal.
    """
    cases = [_case(f"p{i}", kind="paraphrase") for i in range(5)] + [
        _case(f"l{i}") for i in range(5)
    ]
    assert [c.kind for c in select_judge_subset(cases, limit=1)[0]] == ["literal"]

    # And it is the KIND that decides, not the position: reversing the file order
    # must not change which kind wins the single slot.
    reversed_file = [_case(f"l{i}") for i in range(5)] + [
        _case(f"p{i}", kind="paraphrase") for i in range(5)
    ]
    assert [c.kind for c in select_judge_subset(reversed_file, limit=1)[0]] == ["literal"]


def test_an_unrecognised_kind_still_gets_a_lane() -> None:
    """A future kind must not be silently excluded from every bounded run."""
    cases = [_case(f"l{i}") for i in range(5)] + [_case("novel", kind="brand-new")]
    selected, _ = select_judge_subset(cases, limit=2)
    assert "brand-new" in {c.kind for c in selected}


def test_selection_matches_a_pinned_expected_result() -> None:
    """Pins the ACTUAL selection, not merely that it is repeatable.

    Calling the same pure function twice in one process cannot fail — there is no
    RNG and no clock — so a self-comparison proves nothing. A hardcoded expectation
    is what actually detects a change in the selection rule (and makes such a change
    show up as an explicit diff rather than a silent reshuffle).
    """
    cases = [_case(f"c{i}", kind="literal" if i % 2 else "paraphrase") for i in range(10)]
    selected, dropped = select_judge_subset(cases, limit=4)
    assert [c.id for c in selected] == ["c0", "c1", "c2", "c3"]
    assert [c.id for c in dropped] == ["c4", "c5", "c6", "c7", "c8", "c9"]


def test_kind_order_ranks_literal_above_paraphrase_above_multihop() -> None:
    """Pins the full _KIND_ORDER ranking, not just its first element.

    Permuting the tuple's tail (e.g. multihop ahead of paraphrase) otherwise
    changes which cases a bounded run judges while every test stays green.
    """
    cases = (
        [_case(f"m{i}", kind="multihop") for i in range(3)]
        + [_case(f"p{i}", kind="paraphrase") for i in range(3)]
        + [_case(f"l{i}") for i in range(3)]
    )
    # One slot per lane, in rank order.
    assert [c.kind for c in select_judge_subset(cases, limit=1)[0]] == ["literal"]
    assert sorted(c.kind for c in select_judge_subset(cases, limit=2)[0]) == [
        "literal",
        "paraphrase",
    ]
    assert sorted(c.kind for c in select_judge_subset(cases, limit=3)[0]) == [
        "literal",
        "multihop",
        "paraphrase",
    ]


def test_report_records_the_requested_limit_not_the_resulting_size() -> None:
    """`limit` and `selected` must be distinguishable in the report.

    When the priority pool exceeds the limit they differ, and that gap is exactly
    what tells a reader "the bound could not be honoured". A report that echoed
    `len(selected)` back as `limit` would hide it.
    """
    import asyncio

    from tests.eval import runner as runner_mod

    async def _spy(cases, *, settings, postgres=False):  # type: ignore[no-untyped-def]
        return []

    summary = None

    async def _go() -> None:
        nonlocal summary
        summary = await runner_mod.run_eval_async(with_judge=False, judge_subset_limit=5)

    original = runner_mod._judge_cases
    runner_mod._judge_cases = _spy  # type: ignore[assignment]
    try:
        asyncio.run(_go())
    finally:
        runner_mod._judge_cases = original  # type: ignore[assignment]

    assert summary is not None
    sub = summary["judge"]["subset"]
    # 5 requested, but the zero-tolerance priority pool is far larger.
    assert sub["limit"] == 5
    assert sub["selected"] > 5


def test_selected_and_dropped_partition_the_input_in_order() -> None:
    cases = [_case(f"c{i}", kind="literal" if i % 3 else "paraphrase") for i in range(20)]
    selected, dropped = select_judge_subset(cases, limit=7)
    assert len(selected) + len(dropped) == len(cases)
    assert {c.id for c in selected}.isdisjoint({c.id for c in dropped})
    order = [c.id for c in cases]
    assert [c.id for c in selected] == [i for i in order if i in {c.id for c in selected}]
    assert [c.id for c in dropped] == [i for i in order if i in {c.id for c in dropped}]


# ---------------------------------------------------------------------------
# Against the REAL golden set at the limit CI actually uses
# ---------------------------------------------------------------------------


def test_a_bound_on_the_real_golden_set_keeps_every_oracle_and_every_kind() -> None:
    """Against the REAL golden set, no zero-tolerance gate may be switched off."""
    cases = load_cases(GOLDEN_PATH)
    selected, dropped = select_judge_subset(cases, limit=20)
    assert dropped, "the golden set has grown small enough that 20 is a full run"
    sel_ids = {c.id for c in selected}
    # Deliberately re-states the oracle predicates rather than calling
    # ``is_priority``: asserting with the function under test would go VACUOUS the
    # moment ``is_priority`` regressed to ``False``, which is precisely the
    # regression this test exists to catch.
    for case in cases:
        if case.must_not_contain:
            assert case.id in sel_ids, f"injection case {case.id} dropped by the bound"
        if case.kind == "followup":
            assert case.id in sel_ids, f"echo-oracle case {case.id} dropped by the bound"
        if case.expected_facts:
            assert case.id in sel_ids, f"fact-bearing case {case.id} dropped by the bound"
        if case.kind == "refusal":
            assert case.id in sel_ids, f"refusal case {case.id} dropped by the bound"
        if case.judge_only:
            assert case.id in sel_ids, f"judge-only case {case.id} dropped by the bound"
    # At limit 20 the 42-case priority pool already exceeds the limit, so NO
    # stratified fill happens and the selection is exactly the pool. That is the
    # documented "the limit is a target, not a licence to drop an oracle" rule.
    assert len(selected) == sum(1 for c in cases if is_priority(c))


def test_a_bound_above_the_priority_pool_still_covers_every_kind() -> None:
    """Once there IS a fill budget, the stratified fill must reach every kind."""
    cases = load_cases(GOLDEN_PATH)
    selected, dropped = select_judge_subset(cases, limit=50)
    assert dropped, "limit 50 should still be a bounded run over 58 cases"
    assert {c.kind for c in selected} == {c.kind for c in cases}


def test_the_documented_saving_ceiling_is_still_true() -> None:
    """The docs justify NOT sampling in CI with a measured number; pin it.

    ``docs/COST_CONTROLS.md`` §6 and ``subset.py``'s docstring both state that the
    priority pool is 42 of 58 cases, so bounding can save at most ~28%. If the
    golden set grows such that sampling becomes worthwhile again, this fails and
    the decision gets revisited deliberately rather than by drift.
    """
    cases = load_cases(GOLDEN_PATH)
    pool = [c for c in cases if is_priority(c)]
    assert (len(pool), len(cases)) == (42, 58), (
        f"priority pool is now {len(pool)}/{len(cases)}; update the ~28%-ceiling "
        "claim in docs/COST_CONTROLS.md §6 and tests/eval/subset.py, and re-decide "
        "whether CI should sample cases after all"
    )


# ---------------------------------------------------------------------------
# The gate tripwire: narrowing is_priority must FAIL a run, not quietly narrow it
# ---------------------------------------------------------------------------


def test_gate_fails_when_a_zero_tolerance_case_was_excluded_from_the_judged_run() -> None:
    """``gate_failures`` must reject a report whose subset dropped a hard-gate case.

    The judged refusal-leak check is an ``elif`` — on a judged run it is the ONLY
    refusal gate. This tripwire is what stops a future edit to ``is_priority`` from
    silently turning that gate (or groundedness, or injection) off.
    """
    from tests.eval.runner import gate_failures

    summary: dict[str, object] = {
        "retrieval": {
            "hit_rate_by_kind": {"literal": 1.0},
            "answerable_total": 10,
            "overall_hit_rate": 1.0,
            "refusal_leaks": 0,
            "refusal_total": 5,
        },
        "judge": {
            "available": True,
            "judged": 20,
            "scored": 20,
            "mean_score": 5.0,
            "refusal_leaks_judged": 0,
            "subset": {
                "limit": 20,
                "selected": 20,
                "dropped": 2,
                "dropped_ids": ["refusal_docker", "codex_lit_install"],
                "dropped_zero_tolerance": {
                    "refusal": ["refusal_docker"],
                    "fact_bearing": ["codex_lit_install"],
                    "injection": [],
                    "multi_turn": [],
                    "judge_only": [],
                },
            },
        },
        "embedder": {"mode": "postgres"},
    }
    failures = gate_failures(summary)  # type: ignore[arg-type]
    joined = " ".join(failures)
    assert "refusal" in joined and "refusal_docker" in joined
    assert "fact_bearing" in joined and "codex_lit_install" in joined


def test_gate_is_silent_when_the_subset_dropped_no_hard_gate_case() -> None:
    """The tripwire must not fire on a legitimate mean-only subset."""
    from tests.eval.runner import gate_failures

    summary: dict[str, object] = {
        "retrieval": {
            "hit_rate_by_kind": {"literal": 1.0},
            "answerable_total": 10,
            "overall_hit_rate": 1.0,
            "refusal_leaks": 0,
            "refusal_total": 5,
        },
        "judge": {
            "available": True,
            "judged": 20,
            "scored": 20,
            "mean_score": 5.0,
            "refusal_leaks_judged": 0,
            "subset": {
                "limit": 20,
                "selected": 20,
                "dropped": 1,
                "dropped_ids": ["claude_api_lit_plain"],
                "dropped_zero_tolerance": {
                    "refusal": [],
                    "fact_bearing": [],
                    "injection": [],
                    "multi_turn": [],
                    "judge_only": [],
                },
            },
        },
        "embedder": {"mode": "postgres"},
    }
    assert gate_failures(summary) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Report wiring: a bounded run must ANNOUNCE its reduced coverage
# ---------------------------------------------------------------------------


def test_bounded_run_records_reduced_coverage_in_the_report(tmp_path: pathlib.Path) -> None:
    """A subset report must be impossible to mistake for a full-coverage report.

    The judged half self-skips under the stub provider, so this exercises the
    reporting path (which is what a reader trusts) at zero provider cost.
    """
    from tests.eval.runner import main

    report = tmp_path / "report.json"
    rc = main(
        [
            "--no-judge",
            "--judge-subset",
            "20",
            "--report",
            str(report),
            "--quiet",
        ]
    )
    assert rc == 0
    payload = json.loads(report.read_text())
    subset = payload["judge"]["subset"]
    assert subset is not None
    assert subset["limit"] == 20
    assert subset["dropped"] > 0
    assert subset["dropped_ids"]
    assert "REDUCED COVERAGE" in subset["note"]


def test_the_subset_actually_reaches_the_judged_half(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The cost saving is real only if `_judge_cases` receives the SUBSET.

    Without this, `run_eval_async` could judge every case and still report a
    subset — a report that claims a saving that never happened, spending full price
    on every run. The existing report tests all use `--no-judge`, so `judged` is
    always empty and none of them observes which cases were driven; this is the one
    behaviour a cost control must not silently regress.
    """
    import asyncio

    from tests.eval import runner as runner_mod

    seen: list[list[str]] = []

    async def _spy(cases, *, settings, postgres=False):  # type: ignore[no-untyped-def]
        seen.append([c.id for c in cases])
        return []

    monkeypatch.setattr(runner_mod, "_judge_cases", _spy)
    # Force the judged branch: the real factory returns a StubLLMClient with no key,
    # which would skip `_judge_cases` entirely and make the spy vacuous.
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: object())

    all_cases = load_cases(GOLDEN_PATH)
    expected, _dropped = select_judge_subset(
        [c for c in all_cases if not c.postgres_only], limit=20
    )

    asyncio.run(runner_mod.run_eval_async(judge_subset_limit=20))
    assert seen, "_judge_cases was never called — the judged branch did not run"
    assert seen[0] == [c.id for c in expected]
    assert len(seen[0]) < len(all_cases), "the judged half received the FULL case list"


def test_full_run_judges_every_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror of the above: no limit must drive the complete judgeable set."""
    import asyncio

    from tests.eval import runner as runner_mod

    seen: list[list[str]] = []

    async def _spy(cases, *, settings, postgres=False):  # type: ignore[no-untyped-def]
        seen.append([c.id for c in cases])
        return []

    monkeypatch.setattr(runner_mod, "_judge_cases", _spy)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: object())

    all_cases = load_cases(GOLDEN_PATH)
    asyncio.run(runner_mod.run_eval_async())
    assert seen[0] == [c.id for c in all_cases if not c.postgres_only]


def test_full_run_report_carries_a_null_subset(tmp_path: pathlib.Path) -> None:
    from tests.eval.runner import main

    report = tmp_path / "report.json"
    assert main(["--no-judge", "--report", str(report), "--quiet"]) == 0
    assert json.loads(report.read_text())["judge"]["subset"] is None


def test_bounded_run_prints_the_dropped_ids_even_when_quiet(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--quiet suppresses routine metrics; it must NOT suppress the coverage warning."""
    from tests.eval.runner import main

    main(["--no-judge", "--judge-subset", "20", "--report", str(tmp_path / "r.json"), "--quiet"])
    out = capsys.readouterr().out
    assert "JUDGED COVERAGE REDUCED" in out
    assert "NOT judged:" in out
