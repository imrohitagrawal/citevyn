"""#598 Step 2, review round 1: the builder's tests for the round's fixes.

Free and hermetic, like ``test_eval_report_fields.py``, whose fakes they reuse.
Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest

from app.answer import orchestrator as orchestrator_mod
from app.core.config import Settings
from app.cost.call_site import CallSite, get_call_site
from app.llm.errors import LLMUnavailable
from app.llm.types import LLMResult
from tests.eval import runner as runner_mod
from tests.test_eval_report_fields import (
    ANSWER_MODEL,
    EXPECT,
    FALLBACK,
    LOW_SCORE_MARK,
    _gate,
    _golden,
    _Judge,
    _judged_case,
    _Model,
    _prompt_evidence,
    _run,
    _spy_passes,
    _stored_evidence,
    _two_pass_settings,
)

ANSWERED = "The limit is 50 requests per minute [1]."


def _gemini_settings() -> Settings:
    """A non-stub provider, so the orchestrator's condense call really fires."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="gemini",
        gemini_api_key="placeholder-x",
        gemini_model=ANSWER_MODEL,
    )


def _judge_with(
    monkeypatch: pytest.MonkeyPatch, cases: list[Any], model: Any, judge: Any = None
) -> list[runner_mod.JudgedCase]:
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    return asyncio.run(
        runner_mod._judge_cases(
            cases,
            settings=_gemini_settings(),
            answer_llm=runner_mod.AttributingLLM(model),
            judge_llm=judge or _Judge(),  # type: ignore[arg-type]
            expect_answerer=EXPECT,
        )
    )


class _CondenseBy(_Model):
    """Answers like ``_Model``; the condense call returns ``condensed`` from the
    given provider and model (production's fallback answering a Gemini blip)."""

    def __init__(self, reply: Any, condensed: str, *, provider: str, model: str) -> None:
        super().__init__(reply)
        self.condensed, self.condense_provider, self.condense_model = condensed, provider, model

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if get_call_site() is CallSite.condense:
            return LLMResult(
                text=self.condensed,
                input_tokens=1,
                output_tokens=1,
                model=self.condense_model,
                provider=self.condense_provider,  # type: ignore[arg-type]
            )
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


# --- 1. a wrong model: true coverage, failed through the errored line ---------------


def test_a_fact_case_answered_by_the_wrong_model_fails_on_its_errored_line_with_true_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a wrong-model answer's coverage is invented (0.0) instead of
    computed from its text, its report fields are dropped, or it no longer fails
    groundedness through the errored line naming the call site and model."""
    case = _golden()["claude_api_lit_ratelimit"]
    (judged,) = _run(monkeypatch, [case], _Model(ANSWERED, provider="router", model=FALLBACK))
    assert judged.answer_errored is True
    assert judged.fact_coverage == 1.0  # the answer does state the fact
    assert judged.missing_facts == ()
    assert judged.outcome == "answer" and judged.raw_reply == ANSWERED  # details attached
    assert [(c["site"], c["model"], c["expected"]) for c in judged.model_calls] == [
        ("answer", FALLBACK, False)
    ]
    failures = _gate([judged], [case])
    line = [f for f in failures if f"errored: {case.id}" in f]
    assert line and "grounded" in line[0] and "answer by router" in line[0], failures
    assert not any("NOT fully grounded" in f for f in failures), failures


def test_a_condense_only_fallback_shows_true_coverage_and_names_condense(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a follow-up whose answer was the expected model's, but whose
    condense call fell back, is reported as ungrounded (coverage 0.0), or its
    error and model_calls do not say it was the condense call."""
    case = _golden()["followup_claude_api_raise"]
    model = _CondenseBy(
        "Set CLAUDE_API_RATE_LIMIT to raise it [1].",
        "How can I raise the Claude API rate limit?",
        provider="router",
        model=FALLBACK,
    )
    (judged,) = _judge_with(monkeypatch, [case], model)
    assert judged.answer_errored is True
    assert judged.fact_coverage == 1.0
    assert "condense by router" in (judged.error or "")
    wrong = [c for c in judged.model_calls if not c["expected"]]
    assert [(c["site"], c["turn"]) for c in wrong] == [("condense", "final")]
    failures = _gate([judged], [case])
    assert any(f"errored: {case.id}" in f and "condense" in f for f in failures), failures


# --- 2. a report read that fails marks the case; the pass goes on --------------------


def test_a_failed_report_read_marks_its_case_and_the_pass_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: an error reading one case's report fields aborts the pass (every
    answer after it is lost), or the case is not marked and gated as errored."""
    real = runner_mod._final_turn
    calls = {"n": 0}

    async def _flaky(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away")
        return await real(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "_final_turn", _flaky)
    golden = _golden()
    first, second = _run(
        monkeypatch,
        [golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]],
        _Model("An answer [1]."),
    )
    assert first.error == "report: RuntimeError: database went away"
    assert first.answer_errored is True and first.score is None
    assert second.error is None and second.outcome == "answer"  # the pass went on


def test_a_missing_audit_row_is_a_report_error_not_a_silent_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a turn with no ``ask_question`` audit row is reported with
    ``outcome`` None as if nothing were wrong."""

    async def _no_audit(self: Any, **_: Any) -> None:
        return None

    monkeypatch.setattr(orchestrator_mod.Orchestrator, "_persist_audit", _no_audit)
    (case,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(ANSWERED))
    assert case.answer_errored is True
    assert case.error and case.error.startswith("report: LookupError: no ask_question audit row")


# --- 3. test gaps -----------------------------------------------------------------


def test_a_followup_stores_the_final_turns_evidence_when_the_history_turn_retrieved_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the evidence is read from the history turn's user message (the
    oldest instead of the newest), which only shows when the two turns retrieve
    different passages."""
    golden = _golden()
    case = dataclasses.replace(
        golden["followup_claude_api_raise"],
        id="followup_two_topics",
        history=(golden["codex_lit_modelflag"].question,),
        question=golden["claude_api_lit_ratelimit"].question,
        expected_facts=(),
    )
    model = _Model("An answer [1].")
    (judged,) = _run(monkeypatch, [case], model)
    history, final = (_prompt_evidence(p) for p in model.answer_prompts)
    assert history != final  # partner: the turns really saw different passages
    assert _stored_evidence(judged) == final


def test_the_raw_reply_skips_the_condense_call_on_a_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: replies are not filtered to the answer call site, so the
    condense call's text is reported as the raw reply or as a retry."""
    case = _golden()["followup_claude_api_raise"]
    model = _CondenseBy(
        lambda p: "FINAL [1]." if "CONDENSED" in p or case.question in p else "HISTORY [1].",
        "CONDENSED: how can I raise the Claude API rate limit?",
        provider="gemini",
        model=ANSWER_MODEL,
    )
    (judged,) = _judge_with(monkeypatch, [case], model)
    final_sites = [c["site"] for c in judged.model_calls if c["turn"] == "final"]
    assert final_sites == ["condense", "answer"]  # partner: condense really fired
    assert judged.error is None
    assert judged.raw_reply == "FINAL [1]."
    assert judged.retry_reply is None and judged.retry_kept is False


class _TimedOutJudge(_Judge):
    async def complete(self, **_: Any) -> LLMResult:
        self.calls += 1
        raise LLMUnavailable("openrouter timed out after 60.0s")


def test_a_grader_timeout_is_recorded_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: a grader call that times out inside ``_judge_cases`` leaves the
    case without its ``judge_unavailable`` error or without the call's entry."""
    (case,) = _run(
        monkeypatch,
        [_golden()["claude_api_lit_ratelimit"]],
        _Model(ANSWERED),
        judge=_TimedOutJudge(),
    )
    assert case.error and case.error.startswith("judge_unavailable")
    (call,) = case.grader_calls
    assert call["score"] is None and "timed out" in call["rationale"]
    assert isinstance(call["duration_seconds"], float) and call["duration_seconds"] >= 0


def test_grader_calls_are_reset_per_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the grader's record is not cleared per case, so the second case
    carries the first case's calls too."""
    golden = _golden()
    judge = _Judge()
    first, second = _run(
        monkeypatch,
        [golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]],
        _Model("An answer [1]."),
        judge=judge,
    )
    assert judge.calls == 8  # partner: both cases were graded by the panel
    assert [c["rationale"] for c in first.grader_calls] == ["r1", "r2", "r3", "r4"]
    assert [c["rationale"] for c in second.grader_calls] == ["r5", "r6", "r7", "r8"]


# --- 4. redaction ------------------------------------------------------------------


def test_redaction_keeps_the_json_valid_and_never_touches_field_names(tmp_path: Any) -> None:
    """Red when: redaction runs over the serialized text, so a key equal to a JSON
    literal or a field name breaks the file or renames a field."""
    settings = Settings(_env_file=None, admin_api_key="true")  # type: ignore[call-arg]
    report = tmp_path / "r.json"
    runner_mod.write_report(report, {"ok": True, "true": "it is true"}, settings)
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "ok": True,
        "true": "it is [REDACTED]",
    }


def test_redaction_covers_salts_and_url_userinfo(tmp_path: Any) -> None:
    """Red when: a salt, or the user:password of a URL-valued setting, reaches the
    report. Partner: a URL password alone is kept, since it can be an ordinary
    word (CI's database password is ``citevyn``)."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        rate_limit_key_salt="pepper-salt-value",
        database_url="postgresql+psycopg://citevyn:citevyn@localhost:5432/db",
    )
    report = tmp_path / "r.json"
    text = "salt pepper-salt-value url postgresql://citevyn:citevyn@host/db case citevyn_lit_free"
    runner_mod.write_report(report, {"e": text}, settings)
    written = json.loads(report.read_text(encoding="utf-8"))["e"]
    assert "pepper-salt-value" not in written and "citevyn:citevyn@" not in written
    assert "citevyn_lit_free" in written


def test_printed_pass_lines_are_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: a key a grader echoed into a low-score rationale is printed to
    the CI log in the clear."""
    key = "or-SECRET-router-9876543210"
    settings = _two_pass_settings(openrouter_api_key=key)
    _spy_passes(
        monkeypatch,
        [[_judged_case("low_main", 1, f"saw {key}")], [_judged_case("fine", 5)]],
        settings,
    )
    runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--report", str(tmp_path / "r.json")])
    marked = [ln for ln in capsys.readouterr().out.splitlines() if LOW_SCORE_MARK in ln]
    assert marked and "low_main" in marked[0]  # partner: the line was printed
    assert key not in marked[0] and "[REDACTED]" in marked[0]


# --- 5. passes_completed ------------------------------------------------------------


def test_the_report_says_which_passes_it_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Red when: a report written after the main pass, by a run cut short, cannot
    be told from a complete one."""
    report = tmp_path / "r.json"
    _spy_passes(
        monkeypatch,
        [[_judged_case("a", 5)], RuntimeError("fallback pass died")],
        _two_pass_settings(),
    )
    with pytest.raises(RuntimeError):
        runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--report", str(report), "--quiet"])
    assert json.loads(report.read_text(encoding="utf-8"))["passes_completed"] == ["main"]
    _spy_passes(monkeypatch, [[_judged_case("a", 5)], [_judged_case("a", 5)]], _two_pass_settings())
    runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--report", str(report), "--quiet"])
    assert json.loads(report.read_text(encoding="utf-8"))["passes_completed"] == [
        "main",
        "fallback",
    ]
