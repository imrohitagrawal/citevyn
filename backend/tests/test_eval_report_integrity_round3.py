"""#598 Step 2b, last fix round: what a turn harvests in its ``finally``.

A case's record only grows: the probe's facts, the history turns' replies and a
failed call's message are kept even when the turn, or a later one, crashes. Free
and hermetic; the gate fields (Postgres only) are in
``test_eval_report_integrity_r3_pg.py``.

Each test names the change that turns it red.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.cost.call_site import CallSite
from app.llm.errors import LLMUnavailable
from tests.test_eval_report_fields import _golden, _Model, _run
from tests.test_eval_report_integrity import ANSWERED, _SiteFails, _stages
from tests.test_eval_report_integrity_r3 import _audit_fails_on
from tests.test_eval_report_round1 import _judge_with


def test_a_final_turn_crash_after_generation_keeps_the_probe_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the final turn's retrieval facts (resolved query, global route) are
    copied only after ``ask`` returns, so a crash after the paid answer call (the
    audit write) loses them."""
    case = _golden()["claude_api_lit_ratelimit"]
    _audit_fails_on(monkeypatch, 1, KeyError("audit write failed"))
    (judged,) = _run(monkeypatch, [case], _Model(ANSWERED))
    assert _stages(judged) == ["answer"]  # partner: the turn really crashed
    assert judged.raw_reply == ANSWERED
    assert judged.resolved_query == case.question
    assert judged.retrieval_global is False


def test_a_history_turn_crash_keeps_the_paid_history_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a history turn's reply text is kept only for the final turn, so a
    crash on the history turn (after its paid answer call) keeps the call's
    metadata but loses its text."""
    case = _golden()["followup_claude_api_raise"]
    _audit_fails_on(monkeypatch, 1, KeyError("history audit write failed"))
    (judged,) = _run(monkeypatch, [case], _Model(ANSWERED))
    assert [(c["turn"], c["site"]) for c in judged.model_calls] == [("history", "answer")]
    assert judged.history_replies == [{"question": case.history[0], "text": ANSWERED}]
    assert judged.raw_reply is None  # the final turn never ran


def test_a_healthy_followup_keeps_both_turns_replies_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner. Red when: a history reply is reported as the final turn's, or the
    final turn's reply lands among the history replies."""
    case = _golden()["followup_claude_api_raise"]

    def reply(prompt: str) -> str:
        return "FINAL reply [1]." if case.question in prompt else "HISTORY reply [1]."

    (judged,) = _run(monkeypatch, [case], _Model(reply))
    assert judged.history_replies == [{"question": case.history[0], "text": "HISTORY reply [1]."}]
    assert judged.raw_reply == "FINAL reply [1]."


def test_a_failed_helper_call_keeps_its_redacted_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a failed helper call keeps only its error type, so why the
    condense step failed is lost, or its message carries a configured key."""
    case = _golden()["followup_claude_api_raise"]
    # ``placeholder-x`` is the Gemini key the settings of ``_judge_with`` configure.
    model = _SiteFails(
        ANSWERED,
        site=CallSite.condense,
        exc=LLMUnavailable("condense down for key placeholder-x"),
    )
    (judged,) = _judge_with(monkeypatch, [case], model)
    by_site: dict[str, Any] = {(c["turn"], c["site"]): c for c in judged.model_calls}
    failed = by_site[("final", "condense")]
    assert failed["failed"] is True and failed["error_type"] == "LLMUnavailable"
    assert failed["error_message"] == "condense down for key [REDACTED]"
    assert by_site[("final", "answer")]["error_message"] is None  # partner: a call that returned


def test_a_failed_answer_call_keeps_its_message_in_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a failed ANSWER call's record drops its message (it is in the
    case error, but ``model_calls`` is what Step 3 reads per call)."""

    def down(_prompt: str) -> str:
        raise LLMUnavailable("provider down: 503")

    (judged,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(down))
    (call,) = judged.model_calls
    assert call["failed"] is True and call["error_message"] == "provider down: 503"


def test_a_failed_history_answer_call_is_not_a_history_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a history answer call that raised is kept as a reply (with empty
    text), as if the model had answered that turn."""
    case = _golden()["followup_claude_api_raise"]
    model = _SiteFails(
        ANSWERED,
        site=CallSite.answer,
        exc=LLMUnavailable("provider down on the history turn"),
        when=lambda user: f"Question: {case.history[0]}\n" in user,
    )
    (judged,) = _run(monkeypatch, [case], model)
    assert [(c["turn"], c["failed"]) for c in judged.model_calls] == [("history", True)]
    assert judged.history_replies == []
