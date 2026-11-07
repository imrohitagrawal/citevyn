"""Regression tests from the round-1 review of #596: the fallback pass and the grader
really do the work the report claims.

The first file (``test_eval_fallback_pass_and_judge.py``) replaced ``_judge_cases``
with a spy, so it proved which clients were HANDED to it, not which ones answered
and graded. Here the real ``_judge_cases`` runs on the hermetic SQLite catalog with
recording fake clients, and the gate is checked one answer oracle at a time.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.core.config import Settings
from app.llm.types import LLMResult
from tests.eval import runner as runner_mod
from tests.eval.cases import load_cases
from tests.eval.paths import GOLDEN_PATH
from tests.eval.runner import JudgedCase

ANSWER = "google/gemini-3.6-flash"
FALLBACK = "openai/gpt-4o-mini"
JUDGE = "anthropic/claude-haiku-5.5"


class _Recording:
    """A non-stub client that returns fixed text and counts its calls."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.closed = False

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:  # noqa: ARG002
        self.calls += 1
        return LLMResult(
            text=self.text, input_tokens=1, output_tokens=1, model="fake", provider="router"
        )

    async def aclose(self) -> None:
        self.closed = True


class _Forbidden(_Recording):
    """Stands in for the process-wide client; any call to it is the bug."""

    async def complete(self, **_: Any) -> LLMResult:
        raise AssertionError("the process-wide client was called")


def _cases() -> list[Any]:
    """Two single-turn cases and one multi-turn case, all hermetic."""
    golden = [c for c in load_cases(GOLDEN_PATH) if not c.postgres_only]
    literal = [c for c in golden if c.kind == "literal"]
    followup = [c for c in golden if c.kind == "followup" and c.history]
    # Partner: the golden set really has hermetic cases of both shapes.
    assert len(literal) >= 2
    assert followup
    return [*literal[:2], followup[0]]


def test_the_real_judge_cases_answers_with_answer_llm_and_grades_with_judge_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: ``_judge_cases`` drops ``llm=answer_llm`` on the final turn or on a
    history turn (the fallback pass would answer with the main model) or
    ``client=judge_llm`` (the grader would be the answer model), while the report
    still names the intended models."""
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    forbidden = _Forbidden("")
    # Not the stub, so the judged path runs; and any use of it fails the test.
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: forbidden)
    # The orchestrator and the judge fall back to the shared client when no client
    # is passed; make that fallback fail loudly too, so a dropped hand-off on ANY
    # turn (history turns included) or on the grader is caught.
    from app.answer import orchestrator as orchestrator_mod
    from tests.eval import judge as judge_mod

    monkeypatch.setattr(orchestrator_mod, "get_llm_client", lambda settings=None: forbidden)
    monkeypatch.setattr(judge_mod, "get_llm_client", lambda settings=None: forbidden)
    answer = _Recording("Use the --model flag to choose a model.")
    judge = _Recording(json.dumps({"score": 4, "rationale": "fake"}))
    cases = _cases()

    judged = asyncio.run(
        runner_mod._judge_cases(
            cases,
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            answer_llm=answer,  # type: ignore[arg-type]
            judge_llm=judge,  # type: ignore[arg-type]
        )
    )

    assert len(judged) == len(cases)
    assert answer.calls >= len(cases)
    # Default panel: 3 standard framings + 1 adversarial call per case.
    assert judge.calls == 4 * len(cases)
    assert all(j.error is None for j in judged), [j.error for j in judged]
    assert [j.score for j in judged] == [4] * len(cases)


def _judged(score: int) -> list[JudgedCase]:
    return [JudgedCase(case_id="c1", kind="literal", answer="a", no_answer=False, score=score)]


def test_the_fallback_report_holds_the_fallback_pass_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the fallback sections are built from the MAIN pass's answers, so a
    failing fallback model would be reported, gated and printed as the main one."""
    passes = iter([_judged(5), _judged(1)])

    async def _spy(cases, *, settings, postgres=False, answer_llm=None, judge_llm=None):  # type: ignore[no-untyped-def]
        return next(passes)

    monkeypatch.setattr(runner_mod, "_judge_cases", _spy)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: object())
    monkeypatch.setattr(runner_mod, "build_metered_llm_client", lambda s: _Recording(""))
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="router",
        openrouter_api_key="or-test",
        openrouter_model=ANSWER,
        eval_fallback_model=FALLBACK,
        eval_judge_model=JUDGE,
    )
    summary = asyncio.run(runner_mod.run_eval_async(settings=settings))
    assert summary["judge"]["mean_score"] == 5
    assert summary["fallback"]["judge"]["mean_score"] == 1
    assert any(
        f.startswith(f"fallback answer model {FALLBACK}: mean judge score 1.00")
        for f in runner_mod.gate_failures(summary)
    )


def test_clients_are_closed_and_the_error_kept_when_a_pass_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a failing pass leaves a built client open, or a close replaces the
    pass's own error."""
    built: list[_Recording] = []

    def _build(settings: Settings) -> _Recording:
        client = _Recording("")
        built.append(client)
        return client

    calls = {"n": 0}

    async def _spy(cases, *, settings, postgres=False, answer_llm=None, judge_llm=None):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("fallback pass failed")
        return []

    monkeypatch.setattr(runner_mod, "_judge_cases", _spy)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: object())
    monkeypatch.setattr(runner_mod, "build_metered_llm_client", _build)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="router",
        openrouter_api_key="or-test",
        eval_fallback_model=FALLBACK,
        eval_judge_model=JUDGE,
    )
    with pytest.raises(RuntimeError, match="fallback pass failed"):
        asyncio.run(runner_mod.run_eval_async(settings=settings))
    assert len(built) == 2
    assert all(c.closed for c in built)


def test_fallback_and_judge_are_router_clients_without_reasoning_from_a_gemini_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the extra clients keep the run's provider (with ``gemini`` that
    builds Gemini-with-fallback and ignores the fallback model), or inherit its
    reasoning effort."""
    built: list[Settings] = []

    def _build(settings: Settings) -> _Recording:
        built.append(settings)
        return _Recording("")

    async def _spy(cases, *, settings, postgres=False, answer_llm=None, judge_llm=None):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(runner_mod, "_judge_cases", _spy)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: object())
    monkeypatch.setattr(runner_mod, "build_metered_llm_client", _build)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="gemini",
        gemini_api_key="g-test",
        openrouter_api_key="or-test",
        openrouter_reasoning_effort="minimal",
        eval_fallback_model=FALLBACK,
        eval_judge_model=JUDGE,
    )
    asyncio.run(runner_mod.run_eval_async(settings=settings))
    assert len(built) == 2
    assert sorted(s.openrouter_model for s in built) == sorted([FALLBACK, JUDGE])
    for s in built:
        assert s.llm_provider == "router"
        assert s.openrouter_reasoning_effort is None


@pytest.mark.parametrize("model_field", ["eval_fallback_model", "eval_judge_model"])
def test_an_empty_eval_model_is_refused_at_config_time(model_field: str) -> None:
    """Red when: an empty id parses and builds a client on model ``""``."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{model_field: ""})  # type: ignore[call-arg,arg-type]


# --- every answer gate applies to the fallback pass ------------------------------


def _healthy() -> dict[str, Any]:
    return {
        "judge": {
            "available": True,
            "judged": 10,
            "scored": 10,
            "mean_score": 4.5,
            "refusal_total": 3,
            "refusal_leaks_judged": 0,
            "subset": None,
        },
        "injection": {"declared": 2, "cases": 2, "leaks": []},
        "multi_turn": {"cases": 1, "echoes": []},
        "groundedness": {"cases_with_facts": 2, "under_grounded": []},
    }


def _break(sections: dict[str, Any], gate: str) -> dict[str, Any]:
    j, inj = sections["judge"], sections["injection"]
    if gate == "mean":
        j["mean_score"] = 2.0
    elif gate == "coverage":
        j["scored"] = 5
    elif gate == "total_outage":
        j["scored"] = 0
    elif gate == "refusal_leaks":
        j["refusal_leaks_judged"] = 1
    elif gate == "zero_tolerance_subset":
        j["subset"] = {"dropped_zero_tolerance": {"refusal": ["r1"]}}
    elif gate == "injection_leak":
        inj["leaks"] = [{"case_id": "i1", "hits": ["SENTINEL"]}]
    elif gate == "injection_shortfall":
        inj["cases"] = 1
    elif gate == "echo":
        sections["multi_turn"]["echoes"] = ["f1"]
    elif gate == "groundedness":
        sections["groundedness"]["under_grounded"] = [{"case_id": "g1", "missing": ["x"]}]
    else:  # pragma: no cover - a typo in the parameter list
        raise AssertionError(gate)
    return sections


ANSWER_GATES = [
    "mean",
    "coverage",
    "total_outage",
    "refusal_leaks",
    "zero_tolerance_subset",
    "injection_leak",
    "injection_shortfall",
    "echo",
    "groundedness",
]


def _summary(fallback: dict[str, Any]) -> dict[str, Any]:
    return {
        "retrieval": {
            "answerable_total": 5,
            "hit_rate_by_kind": {"literal": 1.0},
            "overall_hit_rate": 1.0,
            "refusal_leaks": 0,
            "followup_total": 2,
            "followup_hit_rate": 1.0,
        },
        "embedder": {"mode": "postgres"},
        **_healthy(),
        "fallback": {"model": FALLBACK, **fallback},
    }


def test_a_healthy_fallback_pass_passes() -> None:
    """Partner of the parametrised test: the healthy baseline it breaks is clean."""
    assert runner_mod.gate_failures(_summary(_healthy())) == []


@pytest.mark.parametrize("gate", ANSWER_GATES)
def test_every_answer_gate_applies_to_the_fallback_pass(gate: str) -> None:
    """Red when: any answer gate stops applying to the fallback pass (for example
    moved under the retrieval-only branch), while the main pass still enforces it."""
    failures = runner_mod.gate_failures(_summary(_break(_healthy(), gate)))
    assert len(failures) == 1, failures
    assert failures[0].startswith(f"fallback answer model {FALLBACK}: ")


def test_a_retrieval_gate_is_reported_once_not_per_pass() -> None:
    """Red when: the follow-up hit-rate gate (retrieval-only) runs for each pass."""
    summary = _summary(_healthy())
    summary["retrieval"]["followup_hit_rate"] = 0.0
    failures = runner_mod.gate_failures(summary)
    assert len(failures) == 1
    assert "followup hit-rate" in failures[0]
    assert not failures[0].startswith("fallback")


def test_the_log_prints_the_fallback_oracle_counts() -> None:
    """Red when: the CI log shows the fallback pass's mean but hides whether its
    oracles compared anything."""
    lines = runner_mod.fallback_oracle_lines(_summary(_healthy()))
    joined = "\n".join(lines)
    assert "refusal leaks (judged): 0/3" in joined
    assert "0 echo(es) over 1 multi-turn case(s)" in joined
    assert "over 2 fact-bearing case(s)" in joined
    assert "0 leak(s) over 2 injection case(s)" in joined
    assert runner_mod.fallback_oracle_lines({"judge": {"available": True}}) == []


@pytest.mark.parametrize("judged", [0, 9])
def test_a_fallback_pass_that_judged_fewer_cases_fails(judged: int) -> None:
    """Red when: an empty or partial fallback pass (0 or 9 of the main pass's 10
    cases) passes the gate on the population it happened to judge."""
    fallback = _healthy()
    fallback["judge"]["judged"] = judged
    fallback["judge"]["scored"] = judged
    if judged == 0:
        fallback["judge"]["mean_score"] = None
    failures = runner_mod.gate_failures(_summary(fallback))
    assert any("both passes must drive the same cases" in f for f in failures), failures
