"""#598 Step 2, review round 2: the builder's tests for the round's fixes.

Free and hermetic. Each test names the change that turns it red.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from tests.eval import runner as runner_mod
from tests.eval.runner import JudgedCase
from tests.test_eval_report_fields import (
    FALLBACK,
    LOW_SCORE_MARK,
    _errored,
    _fillers,
    _gate,
    _golden,
    _judged_case,
    _Model,
    _run,
    _spy_passes,
    _two_pass_settings,
)

KEY = "or-SECRET-router-1357924680"


def test_printed_gate_failure_lines_are_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: ``main`` prints a gate failure line (which carries an errored
    case's exception text) without redacting the configured keys."""
    followup = _golden()["followup_claude_api_raise"]
    errored = JudgedCase(
        case_id=followup.id,
        kind="followup",
        answer="",
        no_answer=True,
        error=f"answer: LLMUnavailable: 401 for key {KEY}",
        answer_errored=True,
    )
    _spy_passes(
        monkeypatch,
        [[errored], [dataclasses.replace(errored)]],
        _two_pass_settings(openrouter_api_key=KEY),
    )
    code = runner_mod.main(["--ids", followup.id, "--report", str(tmp_path / "r.json"), "--quiet"])
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("  - ") and "errored:" in ln]
    assert code == 1 and lines, out  # partner: the errored line really was printed
    assert KEY not in out
    assert all("[REDACTED]" in ln for ln in lines)


def test_the_fallback_passs_printed_lines_are_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: the fallback pass's per-pass lines are printed unredacted (only the
    main pass's go through the redaction)."""
    _spy_passes(
        monkeypatch,
        [[_judged_case("fine", 5)], [_judged_case("low_fallback", 1, f"saw {KEY}")]],
        _two_pass_settings(openrouter_api_key=KEY),
    )
    runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--report", str(tmp_path / "r.json")])
    marked = [ln for ln in capsys.readouterr().out.splitlines() if LOW_SCORE_MARK in ln]
    assert len(marked) == 1 and "low_fallback" in marked[0]  # partner: it was printed
    assert KEY not in marked[0] and "[REDACTED]" in marked[0]


def test_an_errored_followup_without_facts_fails_on_the_multi_turn_line() -> None:
    """Red when: the multi-turn gate's errored line is dropped. With no facts on the
    follow-up, the groundedness line cannot name it, so only this line can."""
    followup = dataclasses.replace(_golden()["followup_claude_api_raise"], expected_facts=())
    failures = _gate([*_fillers(), _errored(followup.id, "followup")], [followup])
    named = [f for f in failures if followup.id in f]
    assert len(named) == 1 and "multi-turn" in named[0], failures


def test_an_errored_case_is_recorded_as_not_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: an errored case that did get an answer (written by another model)
    is recorded with ``no_answer`` False, so an errored refusal would count as a
    refusal leak and the answer would look like the product's own."""
    (case,) = _run(
        monkeypatch,
        [_golden()["claude_api_lit_ratelimit"]],
        _Model("The limit is 50 requests per minute [1].", provider="router", model=FALLBACK),
    )
    assert case.outcome == "answer"  # partner: the product did answer
    assert case.answer_errored is True
    assert case.no_answer is True
