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
from tests.test_eval_call_total_timeout import _StallingAnswer
from tests.test_eval_report_fields import (
    EXPECT,
    _exact_first_retriever,
    _golden,
    _Judge,
    _Model,
    _two_pass_settings,
)
from tests.test_eval_report_integrity import ANSWERED, _clients

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
