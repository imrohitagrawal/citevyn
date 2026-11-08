"""The judged eval's second pass (production's fallback model) and its own grader (#596).

``CITEVYN_EVAL_FALLBACK_MODEL`` makes the judged run answer every case a second
time with production's fallback model, under the same answer gates.
``CITEVYN_EVAL_JUDGE_MODEL`` gives the grader its own model, so no answer model
grades itself. Everything here is offline: clients are recorded, never called.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import Settings
from tests.eval import runner as runner_mod

ANSWER = "gemini-3.6-flash"
FALLBACK = "openai/gpt-4o-mini"
JUDGE = "anthropic/claude-haiku-5.5"


def _settings(**overrides: Any) -> Settings:
    # The CI judged job's shape: production's Gemini client, both keys.
    base: dict[str, Any] = {
        "llm_provider": "gemini",
        "gemini_api_key": "g-test",
        "openrouter_api_key": "or-test",
        "gemini_model": ANSWER,
        "eval_fallback_model": FALLBACK,
        "eval_judge_model": JUDGE,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class _Recorded:
    """A client stand-in that remembers the settings it was built from."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"built": [], "calls": []}

    def _build(settings: Settings) -> _Recorded:
        client = _Recorded(settings)
        state["built"].append(client)
        return client

    async def _spy(cases, *, settings, postgres=False, answer_llm=None, judge_llm=None):  # type: ignore[no-untyped-def]
        state["calls"].append({"answer_llm": answer_llm, "judge_llm": judge_llm, "n": len(cases)})
        return []

    monkeypatch.setattr(runner_mod, "build_metered_llm_client", _build)
    monkeypatch.setattr(runner_mod, "_judge_cases", _spy)
    # Force the judged branch: with the stub, the judged half skips itself.
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: object())
    return state


def test_two_passes_run_and_share_one_grader(recorded: dict[str, Any]) -> None:
    """Red when: the fallback pass does not run, runs on the main client, or the
    passes are graded by different clients or by an answer client."""
    asyncio.run(runner_mod.run_eval_async(settings=_settings()))
    calls = recorded["calls"]
    assert len(calls) == 2
    main_pass, fallback_pass = calls
    assert main_pass["answer_llm"] is None  # the process-wide client: CI's main model
    assert fallback_pass["answer_llm"] is not None
    assert fallback_pass["answer_llm"].settings.openrouter_model == FALLBACK
    assert main_pass["judge_llm"] is fallback_pass["judge_llm"]
    assert main_pass["judge_llm"].settings.openrouter_model == JUDGE
    assert main_pass["n"] == fallback_pass["n"] > 0


def test_fallback_and_judge_are_openrouter_clients(recorded: dict[str, Any]) -> None:
    """Red when: the fallback pass or the grader keeps the run's ``gemini`` provider,
    which would build Gemini-with-fallback and ignore the model it was given."""
    asyncio.run(runner_mod.run_eval_async(settings=_settings()))
    assert len(recorded["built"]) == 2
    for client in recorded["built"]:
        assert client.settings.llm_provider == "router"


def test_built_clients_are_closed(recorded: dict[str, Any]) -> None:
    """Red when: the run leaks the extra clients' connection pools."""
    asyncio.run(runner_mod.run_eval_async(settings=_settings()))
    assert len(recorded["built"]) == 2
    assert all(c.closed for c in recorded["built"])


def test_no_fallback_no_judge_model_keeps_one_pass(recorded: dict[str, Any]) -> None:
    """Red when: a run without the two settings changes behaviour (local runs)."""
    asyncio.run(
        runner_mod.run_eval_async(
            settings=_settings(eval_fallback_model=None, eval_judge_model=None)
        )
    )
    assert recorded["calls"] == [
        {"answer_llm": None, "judge_llm": None, "n": recorded["calls"][0]["n"]}
    ]
    assert recorded["built"] == []


def test_report_names_every_model(recorded: dict[str, Any]) -> None:
    """Red when: the report cannot show which models answered and graded."""
    summary = asyncio.run(runner_mod.run_eval_async(settings=_settings()))
    assert summary["models"] == {"answer": ANSWER, "fallback": FALLBACK, "judge": JUDGE}
    assert summary["fallback"]["model"] == FALLBACK
    for section in ("judge", "injection", "multi_turn", "groundedness"):
        assert section in summary["fallback"]


@pytest.mark.parametrize("judge", [ANSWER, FALLBACK])
def test_a_judge_that_is_an_answer_model_is_refused(recorded: dict[str, Any], judge: str) -> None:
    """Red when: the run lets a model grade its own answers."""
    with pytest.raises(SystemExit, match="grade"):
        asyncio.run(runner_mod.run_eval_async(settings=_settings(eval_judge_model=judge)))
    assert recorded["calls"] == []


# --- the gate: the fallback pass faces the same answer gates ------------------


def _answer_sections(*, mean: float, leaks: list[str] | None = None) -> dict[str, Any]:
    return {
        "judge": {
            "available": True,
            "judged": 10,
            "scored": 10,
            "mean_score": mean,
            "refusal_leaks_judged": 0,
            "subset": None,
        },
        "injection": {"declared": 0, "cases": 0, "leaks": []},
        "multi_turn": {"cases": 1, "echoes": leaks or []},
        "groundedness": {"cases_with_facts": 0, "under_grounded": []},
    }


def _summary(
    *, main_mean: float, fallback: dict[str, Any] | None, hit_rate: float = 1.0
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "retrieval": {
            "answerable_total": 5,
            "hit_rate_by_kind": {"literal": 1.0},
            "overall_hit_rate": hit_rate,
            "refusal_leaks": 0,
        },
        "embedder": {"mode": "postgres"},
        **_answer_sections(mean=main_mean),
    }
    if fallback is not None:
        summary["fallback"] = {"model": FALLBACK, **fallback}
    return summary


def test_a_failing_fallback_pass_fails_the_run() -> None:
    """Red when: the fallback pass's answer gates are not applied."""
    failures = runner_mod.gate_failures(
        _summary(main_mean=4.5, fallback=_answer_sections(mean=2.0))
    )
    assert len(failures) == 1
    assert failures[0].startswith(f"fallback answer model {FALLBACK}: ")
    assert "mean judge score 2.00" in failures[0]


def test_zero_tolerance_gates_apply_to_the_fallback_pass() -> None:
    """Red when: a judge-independent oracle (here the multi-turn echo) is skipped for
    the fallback pass."""
    failures = runner_mod.gate_failures(
        _summary(main_mean=4.5, fallback=_answer_sections(mean=4.5, leaks=["fu_1"]))
    )
    assert any(
        f.startswith(f"fallback answer model {FALLBACK}: ") and "fu_1" in f for f in failures
    )


def test_identical_answer_failures_are_reported_for_each_pass() -> None:
    """Red when: a fallback failure is hidden because its text equals the main one."""
    failures = runner_mod.gate_failures(
        _summary(main_mean=2.0, fallback=_answer_sections(mean=2.0))
    )
    assert len(failures) == 2


def test_retrieval_failures_are_reported_once() -> None:
    """Red when: the retrieval gate, which both passes share, is reported twice."""
    failures = runner_mod.gate_failures(
        _summary(main_mean=4.5, fallback=_answer_sections(mean=4.5), hit_rate=0.1)
    )
    assert len([f for f in failures if "overall hit-rate" in f]) == 1
    assert not any(f.startswith("fallback") for f in failures)


def test_a_passing_run_with_a_fallback_pass_has_no_failures() -> None:
    """Partner of the above: both passes healthy means a clean gate."""
    assert (
        runner_mod.gate_failures(_summary(main_mean=4.5, fallback=_answer_sections(mean=4.5))) == []
    )


def test_the_log_names_the_models_and_the_fallback_pass(
    recorded: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """Red when: the CI log hides which models ran, or omits the fallback pass."""
    monkeypatch.setattr(runner_mod, "get_settings", _settings)
    runner_mod.main(["--report", str(tmp_path / "r.json")])
    out = capsys.readouterr().out
    assert f"Models: answer {ANSWER}, fallback {FALLBACK}, judge {JUDGE}" in out
    assert f"Fallback pass ({FALLBACK})" in out
    # The fallback pass's oracle counts reach the log too, beside the main pass's.
    assert out.count("Multi-turn echo:") == 2
