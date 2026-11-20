"""The judged runner's deadline work, review round 1: regression tests for the fixes.

Free and hermetic: the SQLite catalog and scripted clients, reused from the test
designer's files and the report tests. No key, no network.

Each test carries one ``RED WHEN`` line naming the change that turns it red.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import Settings
from app.cost.call_site import CallSite, call_site, get_call_site
from app.llm.attempts import PrimaryFailure
from app.llm.errors import LLMUnavailable
from app.llm.types import LLMResult
from tests.eval import runner as runner_mod
from tests.eval.thresholds import MIN_JUDGE_COVERAGE
from tests.test_eval_call_total_timeout import ONE, _StallingAnswer
from tests.test_eval_deadline import (
    OVER_BUDGET,
    PASSAGE_OVER_BUDGET,
    TWO,
    _Paced,
    _patch_clock,
    _q,
)
from tests.test_eval_deadline import _run as _deadline_run
from tests.test_eval_deadline import _settings as _deadline_settings
from tests.test_eval_limits_cover_grader_timeout import _constant, _loaded
from tests.test_eval_report_fields import (
    EXPECT,
    _exact_first_retriever,
    _golden,
    _Judge,
    _Model,
    _two_pass_settings,
)
from tests.test_eval_report_integrity import ANSWERED, _clients
from tests.test_grader_check_ci import _RENDERINGS
from tests.test_grader_check_runner import PROBE_QUESTION, _mode, _Scripted

TOTAL = 0.3
#: Ten hermetic literal cases: one stalled grade leaves coverage 0.9, two leave 0.8.
TEN = [
    "claude_api_lit_ratelimit",
    "claude_api_lit_envvar",
    "claude_code_lit_permissions",
    "claude_code_lit_allowdeny",
    "codex_lit_modelflag",
    "codex_lit_help",
    "claude_code_lit_install",
    "claude_code_lit_update",
    "codex_lit_install",
    "gemini_api_lit_authheader",
]


async def _forever() -> None:
    await asyncio.Event().wait()


# --- finding 1: a call cut by its total counts as the provider timeout it replaces --------


class _StallOn(_Judge):
    """A grader scoring 4, whose calls for the listed questions never return."""

    def __init__(self, questions: set[str]) -> None:
        super().__init__()
        self.questions = questions

    async def complete(self, **kwargs: Any) -> LLMResult:
        if any(q in kwargs["user"] for q in self.questions):
            await _forever()
        return await super().complete(**kwargs)


def _graded_run(monkeypatch: pytest.MonkeyPatch, stalled: list[str]) -> dict[str, Any]:
    settings = _two_pass_settings(eval_fallback_model=None, eval_judge_timeout_seconds=TOTAL)
    golden = _golden()
    _clients(monkeypatch, grader=_StallOn({golden[i].question for i in stalled}))
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    return asyncio.run(
        asyncio.wait_for(runner_mod.run_eval_async(ids=TEN, settings=settings), timeout=60.0)
    )


def _judge_lines(failures: list[str]) -> list[str]:
    return [f for f in failures if "harness integrity" in f or "judge" in f]


def test_a_grader_stalled_past_its_total_is_judge_unavailable_and_the_gate_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RED WHEN: CallTotalTimeout is not an LLMUnavailable, so the blind grade's
    # `except Exception` makes the stalled grade a 'grade' harness error.
    stalled = TEN[0]
    summary = _graded_run(monkeypatch, [stalled])
    judge = summary["judge"]
    cases = {c["case_id"]: c for c in judge["cases"]}
    assert list(cases) == TEN
    case = cases[stalled]
    assert (case["error"] or "").startswith("judge_unavailable:"), case["error"]
    assert "CallTotalTimeout" in case["error"]
    assert case["harness_error"] is None and judge["harness_errors"] == []
    assert judge["scored"] == 9 and judge["judged"] == 10
    assert MIN_JUDGE_COVERAGE <= 9 / 10  # the one stall stays inside the floor
    assert _judge_lines(runner_mod.gate_failures(summary)) == []


def test_stalls_that_drop_coverage_below_the_floor_fail_on_coverage_not_integrity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RED WHEN: stalled grades fail the run as harness errors instead of through
    # the judge-coverage floor (MIN_JUDGE_COVERAGE).
    summary = _graded_run(monkeypatch, TEN[:2])
    assert summary["judge"]["scored"] == 8
    lines = _judge_lines(runner_mod.gate_failures(summary))
    assert len(lines) == 1, lines
    assert lines[0].startswith("judge scored only 8/10 cases"), lines


def _judge_cases(monkeypatch: pytest.MonkeyPatch, model: Any) -> runner_mod.JudgedCase:
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    (judged,) = asyncio.run(
        asyncio.wait_for(
            runner_mod._judge_cases(
                [_golden()["codex_lit_modelflag"]],
                settings=Settings(_env_file=None),  # type: ignore[call-arg]
                answer_llm=runner_mod.AttributingLLM(model, total_timeout_seconds=TOTAL),
                judge_llm=_Judge(),  # type: ignore[arg-type]
                expect_answerer=EXPECT,
            ),
            timeout=30.0,
        )
    )
    return judged


def _answer_calls(case: runner_mod.JudgedCase) -> list[tuple[bool, str | None]]:
    return [(c["failed"], c["error_type"]) for c in case.model_calls if c["site"] == "answer"]


class _RetryStalls(_Model):
    """Its first answer call declines uncited; its second (the #208 retry) stalls."""

    def __init__(self) -> None:
        super().__init__("No markers.")
        self.answer_calls = 0

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if get_call_site() is CallSite.answer:
            self.answer_calls += 1
            if self.answer_calls == 2:
                await _forever()
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


def test_a_208_retry_cut_by_its_total_keeps_the_first_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RED WHEN: CallTotalTimeout is not an LLMUnavailable, so the orchestrator's
    # best-effort retry does not catch it and the turn's refusal is lost.
    _exact_first_retriever(monkeypatch)
    model = _RetryStalls()
    judged = _judge_cases(monkeypatch, model)
    assert model.answer_calls == 2  # partner: the retry really ran
    assert _answer_calls(judged) == [(False, None), (True, "CallTotalTimeout")]
    assert judged.no_answer is True and judged.answer  # the first refusal, as served
    assert judged.raw_reply == "No markers." and judged.retry_reply is None
    assert judged.answer_errored is True and judged.score is None
    assert judged.harness_error is None


def test_a_main_answer_call_cut_by_its_total_is_an_answer_error_of_kind_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RED WHEN: CallTotalTimeout is not an LLMUnavailable with kind "timeout", or
    # the cut main answer call is not recorded as the case's answer error.
    async def scenario() -> BaseException | None:
        llm = runner_mod.AttributingLLM(_StallingAnswer(ANSWERED), total_timeout_seconds=TOTAL)
        try:
            with call_site(CallSite.answer):
                await llm.complete(system="s", user="u", max_tokens=8, temperature=0.0)
        except BaseException as exc:  # noqa: BLE001 - the type is the assertion
            return exc
        return None

    caught = asyncio.run(scenario())
    assert isinstance(caught, runner_mod.CallTotalTimeout), repr(caught)
    assert isinstance(caught, LLMUnavailable) and caught.kind == "timeout"
    record = PrimaryFailure.from_error(caught, provider="p", model="m", duration_seconds=0.3)
    assert (record.kind, record.reason) == ("timeout", "CallTotalTimeout")

    judged = _judge_cases(monkeypatch, _StallingAnswer(ANSWERED))
    assert _answer_calls(judged) == [(True, "CallTotalTimeout")]
    assert judged.answer_errored is True and judged.score is None
    assert "answer call failed (final, answer: CallTotalTimeout" in (judged.error or "")
    assert judged.harness_error is None


# --- finding 2: each wrapper gets its total, the CI deadline stays tight -----------------


def _bounded(settings: Settings, ids: list[str], seconds: float = 30.0) -> dict[str, Any]:
    # A wrapper without its total would hang forever; the outer bound turns that red.
    return asyncio.run(
        asyncio.wait_for(runner_mod.run_eval_async(ids=ids, settings=settings), timeout=seconds)
    )


def test_the_fallback_answer_client_gets_the_call_total(monkeypatch: pytest.MonkeyPatch) -> None:
    # RED WHEN: run_eval_async builds the fallback pass's AttributingLLM without
    # eval_call_total_timeout_seconds.
    settings = _two_pass_settings(
        eval_fallback_model="openai/gpt-4o-mini", eval_call_total_timeout_seconds=TOTAL
    )
    stalling = _StallingAnswer(ANSWERED, provider="router", model="openai/gpt-4o-mini")
    _clients(monkeypatch, fallback=stalling)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    summary = _bounded(settings, ONE)
    (main,) = summary["judge"]["cases"]
    assert main["score"] == 4  # partner: the main pass answered and graded
    (case,) = summary["fallback"]["judge"]["cases"]
    assert "CallTotalTimeout" in (case["error"] or ""), case["error"]
    assert summary["fallback"]["judge"]["spend_may_be_unrecorded"] >= 1


class _StallWhen(_Scripted):
    """``_Scripted`` (score 4), except calls that ``stall(user, mode)`` picks never return."""

    def __init__(self, stall: Any) -> None:
        super().__init__()
        self.stall = stall

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if self.stall(user, _mode(user)):
            self.calls.append({"user": user, "mode": _mode(user), "probe": PROBE_QUESTION in user})
            await _forever()
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


def _checking_settings() -> Settings:
    return _two_pass_settings(
        eval_fallback_model=None, eval_judge_timeout_seconds=TOTAL, eval_passage_grading=True
    )


def test_the_passage_panel_recorder_gets_the_judge_timeout_as_its_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RED WHEN: the passage panel's _RecordingGrader is built without
    # eval_judge_timeout_seconds as its total.
    grader = _StallWhen(lambda user, mode: mode == "passages" and PROBE_QUESTION not in user)
    _clients(monkeypatch, grader=grader)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    summary = _bounded(_checking_settings(), ONE)
    (case,) = summary["judge"]["cases"]
    assert case["score"] == 4  # partner: the blind panel graded
    assert "CallTotalTimeout" in (case["passage_error"] or ""), case["passage_error"]
    assert any(c.get("spend_may_be_unrecorded") for c in case["passage_grader_calls"])


def test_the_probe_recorder_gets_the_judge_timeout_as_its_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RED WHEN: the probe's _RecordingGrader is built without
    # eval_judge_timeout_seconds as its total.
    grader = _StallWhen(lambda user, _mode: PROBE_QUESTION in user)
    _clients(monkeypatch, grader=grader)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    summary = _bounded(_checking_settings(), ONE)
    probe = summary["grader_check"]["probe"]
    assert probe["errors"], probe  # partner: the probe's calls were cut
    assert all("CallTotalTimeout" in e["message"] for e in probe["errors"]), probe["errors"]
    assert all(c.get("spend_may_be_unrecorded") for c in probe["calls"])
    (case,) = summary["judge"]["cases"]
    assert case["score"] == 4  # the cases still ran after the cut probe


#: How far below its bound a judged rendering's deadline may sit before the run is
#: being stopped for no reason.
DEADLINE_SLACK_SECONDS = 60


@pytest.mark.parametrize("rendering", list(_RENDERINGS))
def test_no_judged_rendering_wastes_more_than_a_minute_of_its_step(
    rendering: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RED WHEN: a judged CI rendering's deadline falls more than 60 s below
    # step x 60 - S - C - T (the grader-check path's bound is 13677 s).
    settings, step_minutes = _loaded(rendering, tmp_path, monkeypatch)
    deadline = settings.eval_deadline_seconds
    budget = settings.eval_grading_budget_seconds
    call_total = settings.eval_call_total_timeout_seconds
    assert deadline is not None and budget is not None and call_total is not None
    bound = (
        step_minutes * 60
        - _constant("DEADLINE_STARTUP_SLACK_SECONDS")
        - (_constant("DEADLINE_MAX_ANSWER_CALLS_PER_CASE") * call_total + budget)
        - _constant("DEADLINE_TEARDOWN_RESERVE_SECONDS")
    )
    assert deadline >= bound - DEADLINE_SLACK_SECONDS, (rendering, deadline, bound)
    if rendering == "dispatch-on":
        assert bound == 13677  # partner: the grader-check path keeps its long deadline


def test_the_runner_constants_hold_their_measured_values() -> None:
    # RED WHEN: DEADLINE_STARTUP_SLACK_SECONDS < 63 or DEADLINE_MAX_ANSWER_CALLS_PER_CASE < 3.
    # Measured on GitHub Actions run 44600158315 (2026-11-19): start-up took at most
    # 2.4 s (3 s + 60 s margin = 63 s), and case followup_gemini_credfile_contentnoun
    # made 3 answer-client calls (2 answer, 1 condense) in both passes.
    assert runner_mod.DEADLINE_STARTUP_SLACK_SECONDS >= 63
    assert runner_mod.DEADLINE_MAX_ANSWER_CALLS_PER_CASE >= 3


def test_a_stop_after_the_probe_names_the_main_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # RED WHEN: the deadline's pass_name is not reset to "main" after the probe, so
    # a stop in the main pass is recorded against the probe.
    clock = _patch_clock(monkeypatch)
    first, second = TWO
    grader = _Scripted()
    summary, _built = _deadline_run(
        monkeypatch,
        _deadline_settings(deadline=600, budget=300, judge_timeout=60.0, passages=True),
        TWO,
        main=_Paced(clock, {_q(first): 600.0}),
        grader=grader,
    )
    assert any(c["probe"] for c in grader.calls)  # partner: the probe ran first
    assert [c["case_id"] for c in summary["judge"]["cases"]] == [first]
    stopped = summary["stopped"]
    assert stopped["at"] == {"pass": "main", "case": second}, stopped
    assert "(main)" in stopped["line"], stopped["line"]


def test_a_blind_panel_over_budget_with_passage_grading_on_sets_passage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RED WHEN: a blind panel skipped over budget, with passage grading on, does
    # not record the passage panel's own over-budget reason.
    clock = _patch_clock(monkeypatch)
    (case_id,) = ONE
    grader = _Scripted()
    summary, _built = _deadline_run(
        monkeypatch,
        _deadline_settings(deadline=600, budget=300, judge_timeout=60.0, passages=True),
        ONE,
        main=_Paced(clock, {_q(case_id): 790.0}),
        grader=grader,
    )
    (case,) = summary["judge"]["cases"]
    assert case["answer"]  # partner: the case was answered
    assert OVER_BUDGET in (case["error"] or ""), case["error"]
    assert case["passage_error"] == PASSAGE_OVER_BUDGET
    assert [c for c in grader.calls if not c["probe"]] == []
    assert case["harness_error"] is None
