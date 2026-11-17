"""Report fields and spend behaviour of the judged eval (#598 Step 4), free and hermetic.

Written before the implementation (test designer). The contract the builder writes to:

* ``grader_latency(calls)``, a pure function in ``tests/eval/runner.py`` or
  ``tests/eval/latency.py``, over grader-call records (``score``, ``rationale``,
  ``duration_seconds``). A call that raised (``rationale`` starting "call failed:")
  is failed; every other call completed (a malformed verdict still returned).
  Returns ``calls``, ``completed``, ``failed``, ``failed_seconds``, ``p50_seconds``,
  ``p90_seconds`` and ``max_seconds``; the percentiles are
  ``statistics.quantiles(method="inclusive")`` over completed calls only, and with no
  completed call they are ``None`` (printed "not available"), never 0.
* Each pass's ``judge`` section carries ``grader_latency`` by kind: ``blind`` always,
  ``passages`` with the grader check on. ``pass_report_lines`` prints it on one line.
* ``JudgedCase.shipped_gold_in_evidence``: ``True`` when one of the case's evidence
  ``source#heading`` keys is in its ``shipped_gold``, ``False`` when none is, ``None``
  when the case has no shipped gold (absent or ``[]``).
* The report's top level gains ``corpus`` (``{"retrieval", "answers"}``: "fixture" or
  "shipped") and ``index_chunks`` (the chunks in the index the answers ran on). On the
  hermetic run both halves are the fixture.
* On the ``--postgres`` judged path, a retrieval whose ``vector_degrade`` is not
  ``none`` is a harness error on that case, naming the degrade.
* With the grader check off, a spend-cap stop on an answer call does not stop the
  pass; the cases error, the gates go red, and the spend export counts an unwrapped
  ``CostLimitReached`` as refused and a wrapped one as an observed failure.
* Debt (both pass today; each is red against the mutant it names): with the check
  on, a wrapped cap refusal on an answer call is counted as refused; and a
  ``test_grader_check_round1`` test never writes to the real job summary.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import importlib
import json
import os
import pathlib
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from app.answer import orchestrator as orchestrator_mod
from app.core.config import Settings
from app.cost.call_site import CallSite, get_call_site
from app.llm.errors import CostLimitReached, LLMUnavailable
from app.llm.types import LLMResult
from app.retrieval.types import RetrievalResult, VectorDegrade
from tests.conftest import corpus_fixture_specs
from tests.eval import judge as judge_mod
from tests.eval import runner as runner_mod
from tests.eval import spend_export
from tests.eval.retrieval import seeded_session
from tests.test_eval_report_fields import ANSWER_MODEL, EXPECT, GRADER, _golden, _Judge, _Model
from tests.test_eval_report_integrity import _clients, _run_settings

BACKEND = pathlib.Path(__file__).resolve().parents[1]
ANSWERED = "The limit is 50 requests per minute [1]."
_CAP = "Daily cost limit reached ($0.14 of $0.14); paid model calls are stopped."
LATENCY_KEYS = {
    "calls",
    "completed",
    "failed",
    "failed_seconds",
    "p50_seconds",
    "p90_seconds",
    "max_seconds",
}


def _latency() -> Callable[[list[dict[str, Any]]], dict[str, Any]]:
    """The function under test, from the runner or a small ``latency`` module."""
    found = getattr(runner_mod, "grader_latency", None)
    if found is None:
        try:
            found = importlib.import_module("tests.eval.latency").grader_latency
        except ModuleNotFoundError:
            found = None
    assert found is not None, "no grader_latency in tests.eval.runner or tests.eval.latency"
    return found


def _call(seconds: float, *, failed: bool = False, parse: bool = False) -> dict[str, Any]:
    if failed:
        return {
            "score": None,
            "rationale": "call failed: LLMUnavailable: grader timed out",
            "duration_seconds": seconds,
            "provider": None,
            "model": None,
        }
    return {
        "score": None if parse else 4,
        "rationale": "judge_parse: no JSON" if parse else "fine",
        "duration_seconds": seconds,
        "provider": "router",
        "model": GRADER,
    }


def _judge(
    monkeypatch: pytest.MonkeyPatch,
    cases: list[Any],
    model: Any,
    *,
    settings: Settings | None = None,
    postgres: bool = False,
    judge_llm: Any = None,
) -> list[runner_mod.JudgedCase]:
    """The REAL ``_judge_cases``, panel 1."""
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    monkeypatch.setattr(judge_mod, "get_llm_client", lambda settings=None: _Judge())
    return asyncio.run(
        runner_mod._judge_cases(
            cases,
            settings=settings or Settings(_env_file=None),  # type: ignore[call-arg]
            postgres=postgres,
            answer_llm=runner_mod.AttributingLLM(model),
            judge_llm=judge_llm or _Judge(),  # type: ignore[arg-type]
            expect_answerer=EXPECT,
        )
    )


def _summary(cases: list[Any], judged: list[Any]) -> dict[str, Any]:
    return runner_mod._summarize(cases, {}, judged, judge_available=True, judgeable=cases)


# --- grader latency ------------------------------------------------------------------------


def test_latency_percentiles_are_inclusive_over_completed_calls() -> None:
    """Red when: the percentile method changes (the exclusive default gives p90 9.9 on
    1..10, not 9.1), so the figures stop matching Step 3's 1.04 s / 2.09 s method."""
    result = _latency()([_call(float(s)) for s in range(1, 11)])
    assert set(result) == LATENCY_KEYS
    assert result["calls"] == 10 and result["completed"] == 10 and result["failed"] == 0
    assert result["p50_seconds"] == pytest.approx(5.5)
    assert result["p90_seconds"] == pytest.approx(9.1)
    assert result["max_seconds"] == pytest.approx(10.0)
    assert result["failed_seconds"] == pytest.approx(0.0)


def test_failed_calls_are_counted_with_their_seconds_but_kept_out_of_the_percentiles() -> None:
    """Red when: a call that raised (a timeout at 60 s) enters the percentiles or the
    max, or is not counted apart with its total seconds."""
    calls = [_call(float(s)) for s in range(1, 11)] + [_call(60.0, failed=True)] * 2
    result = _latency()(calls)
    assert result["calls"] == 12 and result["completed"] == 10 and result["failed"] == 2
    assert result["failed_seconds"] == pytest.approx(120.0)
    assert result["p50_seconds"] == pytest.approx(5.5)
    assert result["p90_seconds"] == pytest.approx(9.1)
    assert result["max_seconds"] == pytest.approx(10.0)


def test_a_malformed_verdict_is_a_completed_call() -> None:
    """Red when: a call that returned an unparseable verdict is counted as failed (it
    took its time and came back; only a call that raised failed)."""
    result = _latency()([_call(1.0), _call(3.0, parse=True)])
    assert result["completed"] == 2 and result["failed"] == 0
    assert result["max_seconds"] == pytest.approx(3.0)


@pytest.mark.parametrize("calls", [[], [_call(60.0, failed=True)]], ids=["none", "all-failed"])
def test_no_completed_call_gives_null_never_zero(calls: list[dict[str, Any]]) -> None:
    """Red when: with no completed call a percentile or the max reads 0 (a 0 s grader
    looks like a fast one) instead of ``None``."""
    result = _latency()(calls)
    assert result["completed"] == 0 and result["calls"] == len(calls)
    assert result["p50_seconds"] is None
    assert result["p90_seconds"] is None
    assert result["max_seconds"] is None


def test_a_single_completed_call_is_every_percentile() -> None:
    """Red when: one completed call raises or gives no figure (``quantiles`` needs care
    with one point)."""
    result = _latency()([_call(2.5)])
    assert result["p50_seconds"] == result["p90_seconds"] == result["max_seconds"] == 2.5


def test_each_pass_reports_blind_latency_and_prints_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the pass's judge section lacks ``grader_latency`` or computes it from
    other calls than the pass's blind grader calls, the switch-off run reports a
    passage or probe kind, or the log prints it other than once per pass."""
    golden = _golden()
    cases = [golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]]
    judged = _judge(monkeypatch, cases, _Model(ANSWERED))
    judge = _summary(cases, judged)["judge"]
    latency = judge.get("grader_latency")
    assert latency is not None, "the pass has no grader_latency"
    assert set(latency) == {"blind"}
    calls = [c for case in judged for c in case.grader_calls]
    assert len(calls) == 4  # partner: panel 1 + veto, two cases
    assert latency["blind"] == _latency()(calls)
    lines = [ln for ln in runner_mod.pass_report_lines(judge) if "latency" in ln.lower()]
    assert len(lines) == 1, lines
    assert "p50" in lines[0] and "p90" in lines[0]


def test_a_pass_with_no_completed_grader_call_prints_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a pass whose grader calls all failed prints a 0 s latency instead of
    "not available"."""

    class _Down(_Judge):
        async def complete(self, **_: Any) -> LLMResult:
            raise LLMUnavailable("grader timed out")

    cases = [_golden()["claude_api_lit_ratelimit"]]
    judged = _judge(monkeypatch, cases, _Model(ANSWERED), judge_llm=_Down())
    assert judged[0].grader_calls  # partner: the grader was called and failed
    judge = _summary(cases, judged)["judge"]
    assert judge["grader_latency"]["blind"]["completed"] == 0
    (line,) = [ln for ln in runner_mod.pass_report_lines(judge) if "latency" in ln.lower()]
    assert "not available" in line


def test_the_grader_check_adds_the_passage_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: with the grader check on, the passage calls are not reported as their
    own latency kind (or are mixed into the blind one)."""
    settings = Settings(_env_file=None, eval_passage_grading=True)  # type: ignore[call-arg]
    cases = [_golden()["claude_api_lit_ratelimit"]]
    judged = _judge(monkeypatch, cases, _Model(ANSWERED), settings=settings)
    assert judged[0].passage_grader_calls  # partner: passage calls were made
    latency = _summary(cases, judged)["judge"]["grader_latency"]
    assert {"blind", "passages"} <= set(latency)
    assert latency["blind"] == _latency()(judged[0].grader_calls)
    assert latency["passages"] == _latency()(judged[0].passage_grader_calls or [])


# --- shipped_gold_in_evidence ----------------------------------------------------------------


def test_shipped_gold_in_evidence_is_true_false_or_null_per_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the field is missing, or constant (always true, always false or always
    null), so a retrieval miss on the shipped corpus cannot be told from a bad answer."""
    golden = _golden()
    hit = golden["claude_api_lit_ratelimit"]  # gold claude_api#Rate limits, retrieved
    miss = dataclasses.replace(golden["claude_api_lit_envvar"], shipped_gold=("claude_api#Errors",))
    empty = dataclasses.replace(golden["codex_lit_modelflag"], shipped_gold=())
    refusal = golden["refusal_k8s"]  # no shipped gold at all
    judged = _judge(monkeypatch, [hit, miss, empty, refusal], _Model(ANSWERED))
    assert [bool(j.evidence) for j in judged][:3] == [True, True, True]  # partner: retrieved
    keys = {f"{e['source']}#{e['heading']}" for e in judged[1].evidence}
    assert "claude_api#Errors" not in keys  # partner: the miss really is a miss
    values = {j.case_id: getattr(j, "shipped_gold_in_evidence", "absent") for j in judged}
    assert values == {
        hit.id: True,
        miss.id: False,
        empty.id: None,
        refusal.id: None,
    }
    assert all("shipped_gold_in_evidence" in j.as_dict() for j in judged)


# --- corpus and index_chunks on the hermetic run ---------------------------------------------


def test_the_hermetic_report_says_both_halves_ran_on_the_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the report has no ``corpus`` or ``index_chunks``, or the hermetic run
    claims the shipped corpus, or the chunk count is not the fixture's."""
    _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    summary = asyncio.run(
        runner_mod.run_eval_async(ids=["claude_api_lit_ratelimit"], settings=_run_settings())
    )
    assert summary["judge"]["judged"] == 1  # partner: the judged half ran
    assert summary.get("corpus") == {"retrieval": "fixture", "answers": "fixture"}
    assert summary.get("index_chunks") == len(corpus_fixture_specs())


# --- a vector degrade on the --postgres judged path -----------------------------------------


def test_a_vector_degrade_on_the_postgres_judged_path_is_a_harness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a judged case answered with the vector arm degraded (keyword-only
    passages) is graded as if the vector arm were live, instead of being a harness
    error that names the case and the degrade."""
    golden = _golden()
    degraded, healthy = golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]
    real = orchestrator_mod._default_retriever

    class _Degrade:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def retrieve(self, question: str, **kwargs: Any) -> RetrievalResult:
            result = await self._inner.retrieve(question, **kwargs)
            if question == degraded.question:
                return RetrievalResult(hits=result.hits, vector_degrade=VectorDegrade.mismatch)
            return result

        async def retrieve_multi(self, *args: Any, **kwargs: Any) -> RetrievalResult:
            return await self._inner.retrieve_multi(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator_mod, "_default_retriever", lambda *a, **k: _Degrade(real(*a, **k))
    )

    @contextlib.asynccontextmanager
    async def _hermetic(_settings: Any, *, postgres: bool) -> AsyncIterator[Any]:
        assert postgres
        async with seeded_session() as session:
            yield session

    monkeypatch.setattr(runner_mod, "_judge_session", _hermetic)
    judged = _judge(monkeypatch, [degraded, healthy], _Model(ANSWERED), postgres=True)
    bad, good = judged
    assert bad.evidence  # partner: the degraded retrieval still served passages
    assert good.harness_error is None and good.score == 4  # partner: no false alarm
    entries = bad.harness_error or []
    assert any("mismatch" in f"{h.get('type')} {h.get('message')}" for h in entries), entries
    named = _summary([degraded, healthy], judged)["judge"]["harness_errors"]
    assert [h["case_id"] for h in named] == [degraded.id]


# --- the spend cap on a switch-off dispatch ------------------------------------------------


class _Capped(_Model):
    """Every answer call raises what ``make(question)`` returns for its prompt."""

    def __init__(self, make: Callable[[str], BaseException]) -> None:
        super().__init__(ANSWERED)
        self.make = make
        self.raised = 0

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if get_call_site() is CallSite.answer:
            self.raised += 1
            raise self.make(user)
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


def _wrapped(how: str) -> LLMUnavailable:
    if how == "cause":
        return LLMUnavailable("answer failed", cause=CostLimitReached(_CAP))
    exc = LLMUnavailable("answer failed")
    exc.__cause__ = CostLimitReached(_CAP)
    return exc


def _export(summary: dict[str, Any]) -> dict[str, Any]:
    report = json.loads(json.dumps({**summary, "grader": {"model": GRADER}}, default=str))
    return spend_export.summarize([], report)


def test_a_cap_stop_with_the_switch_off_errors_the_cases_and_the_pass_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the switch-off dispatch's backstop (plan Revision 4 item 1). Red when: a
    cap stop on an answer call stops the pass, leaves its case graded, keeps the gates
    green, or the export counts an unwrapped refusal as a failure or a wrapped one
    (whose name is ``LLMUnavailable`` and which has no ``refused_by_cap`` with the
    switch off) as refused."""
    golden = _golden()
    first, second = golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]

    def make(prompt: str) -> BaseException:
        return _wrapped("cause") if first.question in prompt else CostLimitReached(_CAP)

    model = _Capped(make)
    judged = _judge(monkeypatch, [first, second], model)
    assert [j.case_id for j in judged] == [first.id, second.id]  # the pass went on
    assert model.raised >= 2  # partner: each case's answer call was refused
    assert all(j.answer_errored and j.score is None for j in judged)
    summary = _summary([first, second], judged)
    retrieval = {
        "answerable_total": 2,
        "hit_rate_by_kind": {"literal": 1.0},
        "overall_hit_rate": 1.0,
        "refusal_leaks": 0,
    }
    assert runner_mod.gate_failures({**summary, "retrieval": retrieval})  # the gates are red
    export = _export(summary)
    assert export["refused_by_cap"] == {ANSWER_MODEL: 1}
    assert export["observed_failures"] == {ANSWER_MODEL: 1}


@pytest.mark.parametrize("how", ["cause", "raised-from"])
def test_debt_a_wrapped_cap_refusal_on_an_answer_call_is_counted_as_refused(
    monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """Debt (passes today). With the grader check on, an answer call refused by the cap
    and wrapped in ``LLMUnavailable`` (``cause=`` or ``raise ... from``) is a cap
    refusal. Red when: runner.py's answer-call check becomes
    ``isinstance(record.cause, CostLimitReached)`` (the wrapped stop reads as an
    outage), or spend_export.py stops counting ``refused_by_cap``."""
    settings = Settings(_env_file=None, eval_passage_grading=True)  # type: ignore[call-arg]
    case = _golden()["claude_api_lit_ratelimit"]
    judged = _judge(monkeypatch, [case], _Capped(lambda _p: _wrapped(how)), settings=settings)
    (entry,) = judged
    failed = [c for c in entry.model_calls if c["failed"] and c["site"] == "answer"]
    assert failed and all(c["error_type"] == "LLMUnavailable" for c in failed)  # wrapped
    assert all(c["refused_by_cap"] is True for c in failed)
    assert "budget: " in (entry.error or "")
    export = _export(_summary([case], judged))
    assert export["refused_by_cap"] == {ANSWER_MODEL: len(failed)}
    assert export["observed_failures"] == {}


def test_the_spend_export_reads_a_switch_off_report() -> None:
    """Red when: the export needs a ``grader_check`` section (every switch-off dispatch
    now exports, and its report has none)."""
    report = {
        "grader": {"model": GRADER},
        "judge": {
            "cases": [
                {
                    "model_calls": [
                        {"model": ANSWER_MODEL, "failed": True, "error_type": "LLMUnavailable"}
                    ],
                    "grader_calls": [_call(60.0, failed=True), _call(1.0)],
                }
            ]
        },
    }
    assert "grader_check" not in report  # partner: the shape a switch-off run writes
    export = spend_export.summarize([], report)
    assert export["observed_failures"] == {ANSWER_MODEL: 1, GRADER: 1}
    assert export["refused_by_cap"] == {}


# --- debt: the round1 module's job-summary shield ---------------------------------------------


def test_debt_a_round1_runner_test_never_writes_to_the_real_job_summary(
    tmp_path: pathlib.Path,
) -> None:
    """Debt (passes today). Red when: ``tests.test_grader_check_round1`` leaves
    ``JOB_SUMMARY_SHIELDED_MODULES``, so its runner tests (switch on, which write the
    grader-check lines after the probe) append to the CI test job's own summary."""
    target = tmp_path / "summary.md"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("CITEVYN_") and k != "GITHUB_STEP_SUMMARY"
    }
    env["GITHUB_STEP_SUMMARY"] = str(target)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_grader_check_round1.py::test_a_blind_only_budget_stop_is_labelled",
        ],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 passed" in done.stdout  # partner: the round1 runner test really ran
    assert not target.exists() or target.read_text(encoding="utf-8") == ""
