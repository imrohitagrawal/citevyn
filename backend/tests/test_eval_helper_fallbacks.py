"""#598: which calls by another model make a judged case errored.

Owner decision (2026-11-12): only an ANSWER call by another model does, on the
final turn or on a history turn (a history answer shapes the follow-up's
context). A helper call (condense, alias check) by another model is reported in
``model_calls`` and the per-pass ``helper_fallbacks``, printed, and never gated.

Free and hermetic. Each test names the change that turns it red.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.cost.call_site import CallSite, get_call_site
from app.llm.types import LLMResult
from tests.eval import runner as runner_mod
from tests.test_eval_report_fields import (
    ANSWER_MODEL,
    FALLBACK,
    _fillers,
    _gate,
    _golden,
    _Model,
    _run,
)
from tests.test_eval_report_round1 import _CondenseBy, _judge_with

ANSWERED = "The limit is 50 requests per minute [1]."


def test_a_condense_only_fallback_is_graded_normally_and_listed_as_a_helper_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a follow-up whose answer the expected model wrote, but whose
    condense call fell back, is errored (not graded), or the fallback is not
    listed and printed as a helper fallback, or it fails a gate."""
    case = _golden()["followup_claude_api_raise"]
    condensed = "How can I raise the Claude API rate limit?"
    model = _CondenseBy(
        # A different reply per turn, so the echo oracle sees a real follow-up.
        lambda p: "Set CLAUDE_API_RATE_LIMIT to raise it [1]." if condensed in p else ANSWERED,
        condensed,
        provider="router",
        model=FALLBACK,
    )
    (judged,) = _judge_with(monkeypatch, [case], model)
    # Partner: the condense call really came from the fallback model.
    assert [(c["site"], c["expected"]) for c in judged.model_calls if c["turn"] == "final"] == [
        ("condense", False),
        ("answer", True),
    ]
    assert judged.answer_errored is False and judged.error is None
    assert judged.score == 4  # the fake grader's real score
    assert judged.fact_coverage == 1.0
    summary = runner_mod._summarize([case], {}, [judged], judge_available=True, judgeable=[case])
    assert summary["judge"]["helper_fallbacks"] == [
        {"case_id": case.id, "calls": f"condense by router {FALLBACK} (final turn)"}
    ]
    lines = runner_mod.pass_report_lines(summary["judge"])
    assert any(
        line.startswith(f"helper calls by another model: {case.id} (condense by router")
        for line in lines
    ), lines
    assert _gate([*_fillers(), judged], [case]) == []


def test_an_answer_call_by_another_model_on_the_final_turn_still_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the answer call itself, written by another model, is treated as a
    helper call and graded as the expected model's answer."""
    case = _golden()["claude_api_lit_ratelimit"]
    (judged,) = _run(monkeypatch, [case], _Model(ANSWERED, provider="router", model=FALLBACK))
    assert judged.answer_errored is True and judged.score is None
    assert "answer by router" in (judged.error or "")
    summary = runner_mod._summarize([case], {}, [judged], judge_available=True, judgeable=[case])
    assert summary["judge"]["helper_fallbacks"] == []
    assert any(f"errored: {case.id}" in f for f in _gate([*_fillers(), judged], [case]))


class _HistoryAnsweredBy(_Model):
    """The expected model, except the HISTORY turn's answer call (its prompt asks
    exactly the history question), which the fallback model writes."""

    def __init__(self, reply: str, history_question: str) -> None:
        super().__init__(reply)
        self.history_question = history_question

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        result = await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )
        if get_call_site() is CallSite.answer and f"Question: {self.history_question}\n" in user:
            return result.model_copy(update={"provider": "router", "model": FALLBACK})
        return result


def test_a_history_turns_answer_by_another_model_errors_the_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a follow-up whose HISTORY answer another model wrote is graded as
    the expected model's conversation (that answer shapes the follow-up)."""
    case = _golden()["followup_claude_api_raise"]
    (judged,) = _run(monkeypatch, [case], _HistoryAnsweredBy(ANSWERED, case.history[0]))
    calls: list[dict[str, Any]] = judged.model_calls
    # Partner: the final turn's answer was the expected model's.
    assert [(c["turn"], c["model"]) for c in calls if c["site"] == "answer"] == [
        ("history", FALLBACK),
        ("final", ANSWER_MODEL),
    ]
    assert judged.answer_errored is True and judged.score is None
    assert "answer by router" in (judged.error or "") and "(history turn)" in (judged.error or "")
    assert any(f"errored: {case.id}" in f for f in _gate([*_fillers(), judged], [case]))
