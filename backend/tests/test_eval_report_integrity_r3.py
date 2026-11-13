"""#598 Step 2b, review round 1: a case's record only grows.

Builder's tests for the fixes after the final-commit review of the report
integrity work. Free and hermetic, reusing the fakes of
``test_eval_report_integrity.py``. The Postgres half (a savepoint that cannot be
released) is ``test_eval_report_integrity_r3_pg.py``.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import pathlib
import stat
import time
from typing import Any

import pytest

from app.answer import orchestrator as orchestrator_mod
from app.cost.call_site import CallSite, get_call_site
from app.llm.errors import LLMUnavailable
from app.llm.types import LLMResult
from tests.eval import runner as runner_mod
from tests.eval.runner import JudgedCase
from tests.test_eval_report_fields import (
    FALLBACK,
    _exact_first_retriever,
    _fillers,
    _gate,
    _golden,
    _Judge,
    _Model,
    _run,
)
from tests.test_eval_report_integrity import (
    _IDS,
    ANSWERED,
    _clients,
    _harness,
    _ordered_ids,
    _run_settings,
    _ServedJudge,
    _SiteFails,
    _stages,
)
from tests.test_eval_report_round1 import _judge_with


def _audit_fails_on(monkeypatch: pytest.MonkeyPatch, call: int, exc: BaseException) -> None:
    """The orchestrator's ``call``-th audit write (1-based) raises ``exc``: a harness
    defect after the turn's model calls were made."""
    real = orchestrator_mod.Orchestrator._persist_audit
    state = {"n": 0}

    async def _audit(self: Any, **kwargs: Any) -> Any:
        state["n"] += 1
        if state["n"] == call:
            raise exc
        return await real(self, **kwargs)

    monkeypatch.setattr(orchestrator_mod.Orchestrator, "_persist_audit", _audit)


class _RouterFor(_Model):
    """``_Model``, except the answer calls asking one of ``questions`` come back as
    served by the fallback model (production's fallback answering a Gemini blip)."""

    def __init__(self, reply: Any, questions: list[str]) -> None:
        super().__init__(reply)
        self.questions = questions

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        result = await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )
        if get_call_site() is CallSite.answer and any(
            f"Question: {q}\n" in user for q in self.questions
        ):
            return LLMResult(
                text=result.text, input_tokens=1, output_tokens=1, model=FALLBACK, provider="router"
            )
        return result


# --- 1. the answer error is decided from the calls, even when ask() then raises -------


def test_a_wrong_model_final_answer_then_a_crash_is_still_an_answer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the wrong-model check runs only after ``ask`` returns, so an answer
    the fallback model wrote is lost when the turn then crashes (the audit write
    fails), and the case is a harness error only."""
    case = _golden()["claude_api_lit_ratelimit"]
    _audit_fails_on(monkeypatch, 1, KeyError("audit write failed"))
    (judged,) = _run(monkeypatch, [case], _Model(ANSWERED, provider="router", model=FALLBACK))
    assert [(c["site"], c["model"]) for c in judged.model_calls] == [("answer", FALLBACK)]
    assert judged.answer_errored is True
    assert "answered by" in (judged.error or "")
    assert _stages(judged) == ["answer"]  # the crash is recorded as well


def test_a_wrong_model_history_answer_then_a_final_crash_is_an_answer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a history turn answered by another model is forgotten because the
    final turn crashed before the case was classified."""
    case = _golden()["followup_claude_api_raise"]
    _audit_fails_on(monkeypatch, 2, KeyError("final audit write failed"))
    (judged,) = _run(monkeypatch, [case], _RouterFor(ANSWERED, [case.history[0]]))
    assert [(c["turn"], c["model"]) for c in judged.model_calls if c["site"] == "answer"] == [
        ("history", FALLBACK),
        ("final", "gemini-3.6-flash"),
    ]
    assert judged.answer_errored is True
    assert "(history turn)" in (judged.error or "")
    assert _stages(judged) == ["answer"]


def test_a_healthy_history_then_a_final_crash_is_a_harness_error_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner. Red when: the crash itself is classed as an answer error."""
    case = _golden()["followup_claude_api_raise"]
    _audit_fails_on(monkeypatch, 2, KeyError("final audit write failed"))
    (judged,) = _run(monkeypatch, [case], _Model(ANSWERED))
    assert judged.answer_errored is False
    assert _stages(judged) == ["answer"]


# --- 2. a failed call's own error vs an unrelated failure ------------------------------


def test_a_failed_answer_call_alone_is_an_answer_error_and_no_harness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the failed call's own error, escaping ``ask`` (as itself or as the
    ``OrchestratorError`` raised from it), is also counted as a harness error, or
    the case is not an answer error."""

    def down(_prompt: str) -> str:
        raise LLMUnavailable("provider down")

    (judged,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(down))
    assert judged.answer_errored is True and "answer call failed" in (judged.error or "")
    assert not _harness(judged)
    assert judged.no_answer is None  # no response was seen


def test_a_swallowed_failed_retry_plus_an_unrelated_crash_is_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: an unrelated crash after a failed (swallowed) #208 retry is hidden
    behind the retry's answer error and leaves no harness error, so the run passes
    with a harness defect (M11)."""
    _exact_first_retriever(monkeypatch)
    replies = iter(["No markers."])

    def reply(_prompt: str) -> str:
        try:
            return next(replies)
        except StopIteration:
            raise LLMUnavailable("retry down") from None

    _audit_fails_on(monkeypatch, 1, KeyError("harness defect after the retry"))
    target = dataclasses.replace(_golden()["codex_lit_modelflag"], expected_facts=())
    (judged,) = _run(monkeypatch, [target], _Model(reply))
    assert judged.answer_errored is True  # partner: the failed retry errors it
    assert [(h["stage"], h["type"]) for h in _harness(judged)] == [("answer", "KeyError")]
    failures = _gate([*_fillers(19), judged], [target])
    assert any("harness" in f and target.id in f for f in failures), failures


# --- 3. report fields are kept one by one ---------------------------------------------


def test_a_failed_evidence_read_keeps_the_outcome_and_the_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: one failed read of the report drops every field read before it
    (outcome, decline reason, replies, resolved query)."""
    real = runner_mod._turn_evidence
    calls = {"n": 0}

    async def _fails(session: Any, session_id: Any) -> Any:
        calls["n"] += 1
        raise RuntimeError("evidence read failed")

    monkeypatch.setattr(runner_mod, "_turn_evidence", _fails)
    case = _golden()["claude_api_lit_ratelimit"]
    (judged,) = _run(monkeypatch, [case], _Model(ANSWERED))
    assert calls["n"] == 1 and real is not _fails
    assert judged.outcome == "answer" and judged.decline_reason is None
    assert judged.raw_reply == ANSWERED and judged.resolved_query == case.question
    assert judged.evidence == []
    assert [(h["stage"], h["type"]) for h in _harness(judged)] == [("report", "RuntimeError")]
    assert judged.score == 4  # still graded


def test_a_missing_audit_row_still_keeps_the_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: a failed audit read stops the evidence read after it."""

    async def _no_audit(self: Any, **_: Any) -> None:
        return None

    monkeypatch.setattr(orchestrator_mod.Orchestrator, "_persist_audit", _no_audit)
    (judged,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(ANSWERED))
    assert judged.outcome is None
    assert judged.evidence, "the evidence read was skipped after the audit read failed"
    assert [(h["stage"], h["type"]) for h in _harness(judged)] == [("report", "LookupError")]


def test_a_healthy_case_reads_both_report_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partner of the two above: no harness error, both reads filled."""
    (judged,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(ANSWERED))
    assert judged.outcome == "answer" and judged.evidence and not _harness(judged)


# --- 5. a case with no response is not observed, not declined -------------------------


def test_a_crashed_refusal_is_not_observed_and_not_a_safe_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a refusal case that crashed before any response is recorded with
    ``no_answer`` True, so it counts as a correct decline in the refusal figures."""
    refusal = _golden()["refusal_k8s"]
    _audit_fails_on(monkeypatch, 1, RuntimeError("audit write failed"))
    (case,) = _run(monkeypatch, [refusal], _Model(ANSWERED))
    assert case.no_answer is None
    summary = runner_mod._summarize(
        [refusal], {}, [case], judge_available=True, judgeable=[refusal]
    )
    judge = summary["judge"]
    assert judge["refusal_total"] == 1  # still in the population
    assert judge["refusal_leaks_judged"] == 0
    assert judge["refusal_not_observed"] == [refusal.id]
    assert [h["case_id"] for h in judge["harness_errors"]] == [refusal.id]


def test_not_observed_cases_are_listed_per_figure_and_stay_in_the_errored_lists() -> None:
    """Red when: an unobserved case counts as a leak (``not no_answer``), is missing
    from the "not observed" list of a figure it belongs to, or drops out of an
    errored list; and when an unknown observation alone invents a harness failure."""
    golden = _golden()
    refusal, followup = golden["refusal_k8s"], golden["followup_claude_api_raise"]
    fact = golden["claude_api_lit_ratelimit"]
    unseen = [
        JudgedCase(case_id=refusal.id, kind="refusal", answer="", no_answer=None),
        JudgedCase(case_id=followup.id, kind="followup", answer="", no_answer=None),
        JudgedCase(case_id=fact.id, kind="literal", answer="", no_answer=None, answer_errored=True),
    ]
    cases = [refusal, followup, fact]
    summary = runner_mod._summarize(cases, {}, unseen, judge_available=True, judgeable=cases)
    assert summary["judge"]["refusal_leaks_judged"] == 0
    assert summary["judge"]["refusal_not_observed"] == [refusal.id]
    assert summary["multi_turn"]["not_observed"] == [followup.id]
    fact_bearing = {c.id for c in cases if c.expected_facts}
    assert fact.id in fact_bearing  # partner: the fact case really declares facts
    assert summary["groundedness"]["not_observed"] == [
        j.case_id for j in unseen if j.case_id in fact_bearing
    ]
    assert [e["case_id"] for e in summary["groundedness"]["errored"]] == [fact.id]
    failures = _gate([*_fillers(), *unseen], cases)
    assert not any("harness" in f for f in failures), failures


# --- 6. self-grading: one symmetric match; a self-graded score is never used -----------


@pytest.mark.parametrize(
    ("served", "model", "same"),
    [
        ("google/gemini-3.6-flash", "gemini-3.6-flash", True),
        ("gemini-3.6-flash", "google/gemini-3.6-flash", True),
        ("openai/gpt-4o-mini-2024-07-18", "openai/gpt-4o-mini", True),
        ("openai/gpt-4o-mini", "openai/gpt-4o-mini-2024-07-18", True),
        ("google/gemini-3.6-flash:free", "gemini-3.6-flash", True),
        ("gemini-3.6-flash", "google/gemini-3.6-flash:free", True),
        ("google/gemini-3.6-flash-001", "gemini-3.6-flash", True),
        ("gemini-3.6-flash", "google/gemini-3.6-flash-001", True),
        ("gemini-3.6-flash-001", "gemini-3.6-flash-002", False),
        ("google/gemini-3.6-flash-lite", "gemini-3.6-flash", False),
        ("gemini-3.6-flash", "google/gemini-3.6-flash-lite", False),
        ("openai/gpt-4o", "openai/gpt-4o-mini", False),
        ("openai/gpt-4o-mini", "openai/gpt-4o", False),
        ("anthropic/gemini-3.6-flash-judge", "gemini-3.6-flash", False),
        ("x/my-gemini-3.6-flash", "gemini-3.6-flash", False),
        ("anthropic/claude-haiku-5.5", "gemini-3.6-flash", False),
    ],
)
def test_one_model_matching_rule_both_ways(served: str, model: str, same: bool) -> None:
    """Red when: the match is one-sided (the decorations stripped from one id only),
    ignores an OpenRouter tag or a pinned version, or confuses two versions or a
    longer model name with the answer model."""
    assert runner_mod._same_model(served, model) is same
    assert runner_mod._same_model(model, served) is same


@pytest.mark.parametrize(
    ("judge", "fallback"),
    [
        ("openai/gpt-4o-mini", "openai/gpt-4o-mini-2024-07-18"),
        ("openai/gpt-4o-mini-2024-07-18", "openai/gpt-4o-mini"),
    ],
)
def test_the_startup_self_grading_refusal_is_symmetric(
    monkeypatch: pytest.MonkeyPatch, judge: str, fallback: str
) -> None:
    """Red when: the start-up check lets a grader that is an answer model through
    because the date suffix sits on the other side."""
    _clients(monkeypatch)
    _no_retrieval(monkeypatch)
    settings = _run_settings(eval_fallback_model=fallback).model_copy(
        update={"eval_judge_model": judge}
    )
    with pytest.raises(SystemExit, match="must not grade its own answers"):
        asyncio.run(runner_mod.run_eval_async(ids=_IDS, settings=settings))


@pytest.mark.parametrize("text", [None, "not a verdict"], ids=["parsed", "malformed"])
def test_a_self_graded_score_never_counts(
    monkeypatch: pytest.MonkeyPatch, text: str | None
) -> None:
    """Red when: a grade served by the answer model keeps its score, so it enters the
    mean (M20), or its rationale, panel scores or ``contested`` still feed the
    report, or a malformed self-graded verdict hides the self-grading."""
    case = _golden()["claude_api_lit_ratelimit"]
    (judged,) = _run(
        monkeypatch,
        [case],
        _Model(ANSWERED),
        judge=_ServedJudge(model="google/gemini-3.6-flash:free", text=text),
    )
    assert "grade" in _stages(judged)
    assert judged.score is None
    summary = runner_mod._summarize([case], {}, [judged], judge_available=True, judgeable=[case])
    assert summary["judge"]["mean_score"] is None and summary["judge"]["scored"] == 0
    if text is None:
        # The discarded verdict feeds nothing printed or gated; the grader calls
        # still hold what was observed.
        assert judged.standard_scores == () and judged.rationale == ""
        assert judged.standard_median is None and judged.adversarial_score is None
        assert judged.contested is False and judged.spread is None
        assert [c["score"] for c in judged.grader_calls] == [4, 4, 4, 4]


# --- 7. every refusal a judged run can hit comes before the retrieval half ------------


def _no_retrieval(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    ran: list[Any] = []

    async def _retrieval(*_a: Any, **_k: Any) -> Any:
        ran.append(1)
        raise AssertionError("retrieval ran before the refusal")

    monkeypatch.setattr(runner_mod, "evaluate_retrieval", _retrieval)
    return ran


def test_a_grader_without_its_timeout_is_refused_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the grader's timeout is checked only when the grader is built after
    the retrieval half (paid embeddings on Postgres)."""
    _clients(monkeypatch)
    ran = _no_retrieval(monkeypatch)
    settings = _run_settings().model_copy(update={"eval_judge_timeout_seconds": None})
    with pytest.raises(SystemExit, match="CITEVYN_EVAL_JUDGE_TIMEOUT_SECONDS"):
        asyncio.run(runner_mod.run_eval_async(ids=_IDS, settings=settings))
    assert ran == []


def test_a_grader_without_an_openrouter_key_is_refused_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a grader client that cannot be built (no OpenRouter key) is found
    only after the retrieval half ran."""
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    ran = _no_retrieval(monkeypatch)
    settings = _run_settings(eval_fallback_model=None, openrouter_api_key=None)
    with pytest.raises(SystemExit, match="grader client"):
        asyncio.run(runner_mod.run_eval_async(ids=_IDS, settings=settings))
    assert ran == []


# --- 8. the report file ---------------------------------------------------------------


def test_the_report_is_readable_by_others_as_before(tmp_path: pathlib.Path) -> None:
    """Red when: the atomic write leaves the report 0600 (``mkstemp``'s mode), so a
    CI step or reader running as another user cannot read it."""
    report = tmp_path / "eval_report.json"
    runner_mod.write_report(report, {"ok": 1}, _run_settings())
    assert stat.S_IMODE(os.stat(report).st_mode) == 0o644


def test_a_stale_temp_file_of_this_report_is_removed_and_nothing_else(
    tmp_path: pathlib.Path,
) -> None:
    """Red when: a temp file a killed run left beside the report stays forever, or
    the clean-up removes a fresh temp file, another report's, or another file."""
    report = tmp_path / "eval_report.json"
    old = time.time() - 3600
    stale = tmp_path / ".eval_report.json.abc123.tmp"
    fresh = tmp_path / ".eval_report.json.def456.tmp"
    other = tmp_path / ".other_report.json.abc123.tmp"
    unrelated = tmp_path / "notes.tmp"
    for path in (stale, fresh, other, unrelated):
        path.write_text("x", encoding="utf-8")
    for path in (stale, other, unrelated):
        os.utime(path, (old, old))
    runner_mod.write_report(report, {"ok": 1}, _run_settings(), stale_before=time.time() - 60)
    assert not stale.exists()
    assert fresh.exists() and other.exists() and unrelated.exists()
    assert json.loads(report.read_text(encoding="utf-8")) == {"ok": 1}


# --- 9. checkpoints: a failed save is retried, then stops the paid pass ----------------


def _failing_writes(monkeypatch: pytest.MonkeyPatch, fail: Any) -> list[dict[str, Any]]:
    """``write_report`` raises when ``fail(summary, attempt)`` is true; records each
    attempt's summary."""
    real = runner_mod.write_report
    attempts: list[dict[str, Any]] = []

    def _write(path: Any, summary: dict[str, Any], settings: Any, **kwargs: Any) -> None:
        attempts.append(summary)
        if fail(summary, len(attempts)):
            raise OSError("disk full")
        real(path, summary, settings, **kwargs)

    monkeypatch.setattr(runner_mod, "write_report", _write)
    return attempts


def test_a_save_that_fails_twice_stops_the_pass_and_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: a failed per-case save is raised (the pass is lost), or is only
    recorded while the run keeps paying for answers it cannot save (M42)."""
    main = _Model(ANSWERED)
    built = _clients(monkeypatch, main=main)
    attempts = _failing_writes(monkeypatch, lambda _s, _n: True)
    summary = asyncio.run(
        runner_mod.run_eval_async(
            ids=_IDS, settings=_run_settings(), report_path=tmp_path / "r.json"
        )
    )
    assert len(main.answer_prompts) == 1  # stopped after the first case
    assert built["fallback"] == []  # no further paid pass
    assert [c["case_id"] for c in summary["judge"]["cases"]] == _ordered_ids()[:1]
    stages = [h["stage"] for h in summary["harness_errors"]]
    assert stages and set(stages) == {"report_write"}
    assert len(attempts) >= 2  # retried
    failures = runner_mod.gate_failures(summary)
    assert any("harness" in f and "report_write" in f for f in failures), failures


def test_a_save_that_fails_once_is_retried_and_the_run_goes_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: a single failed save is not retried, so one transient write error
    marks a healthy run broken."""
    _clients(monkeypatch)
    _failing_writes(monkeypatch, lambda _s, n: n == 1)
    report = tmp_path / "r.json"
    summary = asyncio.run(
        runner_mod.run_eval_async(ids=_IDS, settings=_run_settings(), report_path=report)
    )
    assert summary["harness_errors"] == []
    assert summary["passes_completed"] == ["main", "fallback"]
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["in_progress"] is None


def test_a_failed_final_write_is_returned_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: the final write raises, so the summary is never printed or gated,
    or its failure is not in the returned summary."""
    _clients(monkeypatch)
    _failing_writes(
        monkeypatch,
        lambda s, _n: (
            s.get("in_progress") is None and s.get("passes_completed") == ["main", "fallback"]
        ),
    )
    summary = asyncio.run(
        runner_mod.run_eval_async(
            ids=_IDS, settings=_run_settings(), report_path=tmp_path / "r.json"
        )
    )
    assert [h["stage"] for h in summary["harness_errors"]] == ["report_write"]
    assert "at the end" in summary["harness_errors"][0]["message"]
    assert len(summary["fallback"]["judge"]["cases"]) == 2  # nothing lost


# --- 9. history-turn evidence feeds cache provenance (M43) ----------------------------


@pytest.mark.parametrize("history_turns", [1, 2])
def test_a_hit_on_a_history_turns_answer_carries_that_turns_passages(
    monkeypatch: pytest.MonkeyPatch, history_turns: int
) -> None:
    """Red when: the passages of a HISTORY turn that wrote the cache are not read, so
    a later hit on its key has no passages for Step 3's stored-passage grading."""
    golden = _golden()
    followup = golden["followup_claude_api_raise"]
    first_question = followup.history[0]
    history = (first_question, "And is it the same for every model?")[:history_turns]
    writer = dataclasses.replace(followup, id="writes_in_history", history=history)
    hit = dataclasses.replace(
        golden["claude_api_lit_ratelimit"], id="hits_the_history_key", question=first_question
    )
    (solo,) = _run(monkeypatch, [dataclasses.replace(hit, id="solo")], _Model(ANSWERED))
    model = _Model(ANSWERED)
    _writer, judged = _run(monkeypatch, [writer, hit], model)
    assert judged.outcome == "cache_hit"  # partner: served from the history turn's entry
    origin = judged.cache_origin
    assert origin is not None and (origin["case_id"], origin["turn"]) == (writer.id, "history")
    assert origin["evidence"] and origin["evidence"] == solo.evidence
    assert judged.score == 4 and not _harness(judged)


# --- 9. the printed text of a failed helper call (M35) --------------------------------


def test_a_failed_helper_call_is_printed_with_its_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a failed helper call is printed as if a model had made it ("by None
    None") or without its error type."""
    case = _golden()["followup_claude_api_raise"]
    model = _SiteFails(ANSWERED, site=CallSite.condense, exc=LLMUnavailable("condense down"))
    (judged,) = _judge_with(monkeypatch, [case], model)
    summary = runner_mod._summarize([case], {}, [judged], judge_available=True, judgeable=[case])
    assert summary["judge"]["helper_failures"] == [
        {"case_id": case.id, "calls": "condense failed with LLMUnavailable (final turn)"}
    ]
    lines = runner_mod.pass_report_lines(summary["judge"])
    assert (
        f"helper calls that failed: {case.id} (condense failed with LLMUnavailable "
        "(final turn)) (reported, not gating)"
    ) in lines


# --- grader parse failures stay under the coverage floor ------------------------------


def test_a_parse_failure_is_a_judge_error_not_a_harness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: an unparseable grader reply becomes a harness error (an unapproved
    scoring change: it is counted by the judge-coverage floor instead)."""
    (judged,) = _run(
        monkeypatch,
        [_golden()["claude_api_lit_ratelimit"]],
        _Model(ANSWERED),
        judge=_ServedJudge(text="not a verdict"),
    )
    assert (judged.error or "").startswith("judge_parse")
    assert not _harness(judged) and judged.score is None


@pytest.mark.parametrize(("unparsed", "fails"), [(1, False), (2, True)])
def test_parse_failures_either_side_of_the_coverage_floor(unparsed: int, fails: bool) -> None:
    """Red when: the coverage floor (0.9) moves, or parse failures fail the run
    through the harness check: 9 of 10 scored passes, 8 of 10 fails."""
    case = _golden()["claude_api_lit_ratelimit"]
    parsed = _fillers(10 - unparsed)
    broken = [
        JudgedCase(
            case_id=f"unparsed_{i}",
            kind="literal",
            answer="a",
            no_answer=False,
            error="judge_parse: x",
        )
        for i in range(unparsed)
    ]
    failures = _gate([*parsed, *broken], [case])
    assert any("judge scored only" in f for f in failures) is fails, failures
    assert not any("harness" in f for f in failures), failures
