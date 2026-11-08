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

    async def _spy(
        cases, *, settings, postgres=False, answer_llm=None, judge_llm=None, expect_answerer=None
    ):  # type: ignore[no-untyped-def]
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
    """Red when: a failing pass leaves a built client open, or the pass's error is
    lost (the run must fail with it)."""
    built: list[_Recording] = []

    def _build(settings: Settings) -> _Recording:
        client = _Recording("")
        built.append(client)
        return client

    calls = {"n": 0}

    async def _spy(
        cases, *, settings, postgres=False, answer_llm=None, judge_llm=None, expect_answerer=None
    ):  # type: ignore[no-untyped-def]
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
    assert len(built) == 3  # main, fallback, grader
    assert all(c.closed for c in built)


class _FailsToClose(_Recording):
    async def aclose(self) -> None:
        self.closed = True
        raise RuntimeError("close failed")


def test_a_failing_close_does_not_skip_closing_the_other_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a failing ``aclose`` leaves another client open (the loop this
    replaced stopped at the first failing close)."""
    built: list[_Recording] = []

    def _build(settings: Settings) -> _Recording:
        # Every close fails, so whichever order the closes run in, a loop that
        # stops at the first failure leaves the other client open.
        client = _FailsToClose("")
        built.append(client)
        return client

    async def _spy(
        cases, *, settings, postgres=False, answer_llm=None, judge_llm=None, expect_answerer=None
    ):  # type: ignore[no-untyped-def]
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
    with pytest.raises(RuntimeError, match="close failed"):
        asyncio.run(runner_mod.run_eval_async(settings=settings))
    assert len(built) == 3  # main, fallback, grader
    assert all(c.closed for c in built)


def test_fallback_and_judge_are_router_clients_from_a_gemini_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the extra clients keep the run's provider (with ``gemini`` that
    builds Gemini-with-fallback and ignores the fallback model), or keep its model."""
    built: list[Settings] = []

    def _build(settings: Settings) -> _Recording:
        built.append(settings)
        return _Recording("")

    async def _spy(
        cases, *, settings, postgres=False, answer_llm=None, judge_llm=None, expect_answerer=None
    ):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(runner_mod, "_judge_cases", _spy)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: object())
    monkeypatch.setattr(runner_mod, "build_metered_llm_client", _build)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="gemini",
        gemini_api_key="g-test",
        openrouter_api_key="or-test",
        eval_fallback_model=FALLBACK,
        eval_judge_model=JUDGE,
    )
    asyncio.run(runner_mod.run_eval_async(settings=settings))
    assert len(built) == 3  # main, fallback, grader
    extra = [s for s in built if s.llm_provider == "router"]
    assert sorted(s.openrouter_model for s in extra) == sorted([FALLBACK, JUDGE])
    # The main pass is built from the run's own settings, unchanged.
    assert [s for s in built if s.llm_provider == "gemini"] == [settings]


def test_extra_clients_change_only_the_provider_and_model() -> None:
    """Red when: the fallback pass's or grader's settings differ from the run's in
    anything but the provider and model (a timeout or endpoint, say), so they would
    not be built as production builds its fallback client."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="gemini",
        gemini_api_key="g-test",
        openrouter_api_key="or-test",
    )
    # The grader's model: unlike the fallback model it is not the default, so the
    # model really changes and the comparison below sees it.
    copy = runner_mod._router_settings(settings, JUDGE)
    changed = {n for n in Settings.model_fields if getattr(copy, n) != getattr(settings, n)}
    assert changed == {"llm_provider", "openrouter_model"}
    assert (copy.llm_provider, copy.openrouter_model) == ("router", JUDGE)


@pytest.mark.parametrize("model_field", ["eval_fallback_model", "eval_judge_model"])
@pytest.mark.parametrize("value", ["", " ", "\t", "openai/gpt-4o mini"])
def test_a_blank_or_spaced_eval_model_is_refused_at_config_time(
    model_field: str, value: str
) -> None:
    """Red when: an empty, blank or spaced id parses and builds a client on it."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{model_field: value})  # type: ignore[call-arg,arg-type]


def test_a_real_eval_model_id_is_accepted() -> None:
    """Partner: the id pattern accepts the ids the CI job uses."""
    s = Settings(
        _env_file=None,
        eval_fallback_model="openai/gpt-4o-mini",
        eval_judge_model="anthropic/claude-haiku-5.5",
    )  # type: ignore[call-arg]
    assert s.eval_judge_model == "anthropic/claude-haiku-5.5"


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
    oracles compared anything, or prints the MAIN pass's counts under it."""
    fallback = _healthy()
    summary = _summary(fallback)
    # Make every main-pass count differ from the fallback's, so lines read from
    # the wrong pass cannot match.
    summary["judge"].update(refusal_total=7, refusal_leaks_judged=0)
    summary["multi_turn"] = {"cases": 4, "echoes": []}
    summary["groundedness"] = {"cases_with_facts": 9, "under_grounded": []}
    summary["injection"] = {"declared": 5, "cases": 5, "leaks": []}
    lines = runner_mod.fallback_oracle_lines(summary)
    joined = "\n".join(lines)
    assert "refusal leaks (judged): 0/3" in joined
    assert "0 echo(es) over 1 multi-turn case(s)" in joined
    assert "over 2 fact-bearing case(s)" in joined
    assert "0 leak(s) over 2 injection case(s)" in joined
    assert runner_mod.fallback_oracle_lines({"judge": {"available": True}}) == []


@pytest.mark.parametrize("judged", [0, 9, 11])
def test_a_fallback_pass_that_judged_fewer_cases_fails(judged: int) -> None:
    """Red when: a fallback pass that judged a different count (0, 9 or 11 against
    the main pass's 10) passes the gate on the population it happened to judge."""
    fallback = _healthy()
    fallback["judge"]["judged"] = judged
    fallback["judge"]["scored"] = judged
    if judged == 0:
        fallback["judge"]["mean_score"] = None
    failures = runner_mod.gate_failures(_summary(fallback))
    assert any("both passes must drive the same cases" in f for f in failures), failures


class _Answers:
    """A fake model client that answers as a named provider and model."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider, self.model, self.calls = provider, model, 0

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:  # noqa: ARG002
        self.calls += 1
        return LLMResult(
            text="Use the --model flag.",
            input_tokens=1,
            output_tokens=1,
            model=self.model,
            provider=self.provider,  # type: ignore[arg-type]
        )

    async def aclose(self) -> None:
        return None


class _Down(_Answers):
    async def complete(self, **_: Any) -> LLMResult:
        from app.llm.errors import LLMUnavailable

        self.calls += 1
        raise LLMUnavailable("primary down")


def _run_main_pass(monkeypatch: pytest.MonkeyPatch, primary: _Answers) -> list[JudgedCase]:
    from app.llm.fallback import FallbackLLMClient

    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Forbidden(""))
    main = runner_mod.AttributingLLM(
        FallbackLLMClient(primary=primary, secondary=_Answers("router", FALLBACK))  # type: ignore[arg-type]
    )
    judge = _Recording(json.dumps({"score": 5, "rationale": "fake"}))
    return asyncio.run(
        runner_mod._judge_cases(
            _cases()[:2],
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            answer_llm=main,
            judge_llm=judge,  # type: ignore[arg-type]
            expect_answerer=("gemini", "gemini-3.6-flash"),
        )
    )


def test_a_main_pass_answer_written_by_the_fallback_model_is_not_graded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: Gemini fails, production's fallback chain answers with the fallback
    model, and the run grades that answer as Gemini's (#596's failure, returning
    through the fallback client)."""
    primary = _Down("gemini", "gemini-3.6-flash")
    judged = _run_main_pass(monkeypatch, primary)
    assert primary.calls > 0  # partner: Gemini really was asked first
    assert all(j.score is None for j in judged)
    assert all(j.error and "answered by" in j.error and FALLBACK in j.error for j in judged)


def test_a_main_pass_answer_written_by_gemini_is_graded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partner: when Gemini answers, the case is graded as usual."""
    judged = _run_main_pass(monkeypatch, _Answers("gemini", "gemini-3.6-flash"))
    assert [j.score for j in judged] == [5, 5]
    assert all(j.error is None for j in judged)


def test_a_dated_model_id_from_openrouter_matches_its_model() -> None:
    """Red when: OpenRouter's dated id for the expected model counts as another
    model (every fallback-pass answer would be refused), or a different model
    with a shared prefix-free name passes."""
    expect = ("router", "openai/gpt-4o-mini")
    assert runner_mod._wrong_answerers([("router", "openai/gpt-4o-mini-2024-07-18")], expect) == []
    assert runner_mod._wrong_answerers([("gemini", "openai/gpt-4o-mini")], expect)
    assert runner_mod._wrong_answerers([("router", "openai/gpt-4o")], expect)


class _DownFor(_Answers):
    """Gemini that fails only for one question, and answers every other."""

    def __init__(self, provider: str, model: str, question: str) -> None:
        super().__init__(provider, model)
        self.question = question

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if self.question in user:
            from app.llm.errors import LLMUnavailable

            raise LLMUnavailable("primary down for this question")
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


def test_each_case_is_attributed_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: who answered is not reset per case, so one case answered by the
    fallback model marks every later case too (or an earlier one leaks forward)."""
    first, second = _cases()[:2]
    judged = _run_main_pass(monkeypatch, _DownFor("gemini", "gemini-3.6-flash", first.question))
    assert judged[0].score is None and judged[0].error and "answered by" in judged[0].error
    assert judged[1].score == 5 and judged[1].error is None
