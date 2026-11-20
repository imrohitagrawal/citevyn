"""The grader check in the judged eval runner (#598 Step 3).

Written before the implementation (test designer). Free and hermetic: the SQLite
catalog and scripted clients (the stub never grades, so every judged path here
uses a scripted grader). The contract the builder writes to:

* The switch is ``Settings.eval_passage_grading`` (``CITEVYN_EVAL_PASSAGE_GRADING``,
  off by default; see ``test_grader_check_settings.py``).
* **Off**: the run is exactly today's. The report has no ``grader_check`` key, no
  case carries a ``passage_*`` key, and grader-call entries keep today's keys.
* **On**, per graded case, in BOTH passes, after the blind panel and never
  concurrently with it, a second panel of the same size and veto grades the same
  answer with its passages (``judge.score_answer_passage_panel_async``). The
  passages are the case's ``evidence``, or on a cache hit its ``cache_origin``'s
  evidence. A turn that truly retrieved nothing gets the fixed no-passages block.
  When the passages are unknown (the evidence read failed, or the cache origin's
  evidence is ``None``) the case is not graded with passages.
  New per-case fields (on only): ``passage_score``, ``passage_standard_scores``,
  ``passage_adversarial_score``, ``passage_rationale``, ``passage_grader_calls``
  (entries shaped like ``grader_calls``) and ``passage_error``.
* Report-only: every passage failure of any kind (unknown passages, a parse
  failure, a timeout, any exception, a call served by an answer model) sets
  ``passage_error`` only. It never sets ``error`` or ``harness_error``, never
  touches the blind fields, and passage scores never enter the mean or a gate.
* With the switch on, every grader-call entry (blind, passage and probe) also
  records ``input_tokens`` and ``output_tokens`` from its ``LLMResult``.
* The probe (``tests/eval/grader_check.py``) runs ONCE per run, in
  ``run_eval_async``, before the first case of the main pass: the WRONG and
  CORRECT canned answers, each graded blind and with ``probe_passages()``, three
  repeats each (2 x 2 x 3 panels). The report is written as soon as it is done.
* ``summary["grader_check"]`` (on only, in every save from the probe on):
  ``probe`` (``{"cells": {"wrong"|"correct": {"blind"|"passages": [3 scores]}}}``),
  ``decision`` and ``reasons`` (``grader_check.decide``), ``paired``
  (``{"main": table, "fallback": table}``, ``grader_check.paired_table``),
  ``spend`` (``{"blind"|"passages"|"probe": {"calls", "input_tokens",
  "output_tokens"}}`` from the grader-call records), ``stopped`` (``None``, "stopped
  by the spend cap", or "stopped: the spend meter could not be read") and
  ``commit`` (``GITHUB_SHA`` when set).
* A ``CostLimitReached`` on any call (answer, grader, probe) is labelled
  ``budget: `` in the case's ``error`` (for a case call) and sets ``stopped`` from
  its cause: none means the cap was reached; a cause means the meter could not be
  read.
* ``main()``, with the switch on, refuses before anything runs (non-zero exit, a
  line saying nothing was run) when ``grader_check.start_refusal(ref=GITHUB_REF,
  now=grader_check.utc_now())`` gives a reason; and prints
  ``grader_check.report_lines(summary)``.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import pathlib
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.cost.call_site import CallSite, get_call_site
from app.llm.errors import CostLimitReached, LLMUnavailable
from app.llm.types import LLMResult
from tests.eval import judge as judge_mod
from tests.eval import runner as runner_mod
from tests.eval.runner import JudgedCase
from tests.test_eval_report_fields import ANSWER_MODEL, EXPECT, FALLBACK, _golden, _Judge, _Model
from tests.test_eval_report_integrity import _IDS, _clients, _run_settings

NO_PASSAGES = "No passages were retrieved for this answer."
PROBE_QUESTION = "What models does Codex support?"
ANSWERED = "The limit is 50 requests per minute [1]."
PASSAGE_KEYS = {
    "passage_score",
    "passage_standard_scores",
    "passage_adversarial_score",
    "passage_rationale",
    "passage_grader_calls",
    "passage_error",
}
# Today's report keys (measured on the commit these tests were written on), so
# the switch-off run can be held to them exactly.
TODAY_TOP = {
    "embedder",
    "fallback",
    "grader",
    "groundedness",
    "harness_errors",
    "in_progress",
    "injection",
    "judge",
    "models",
    "multi_turn",
    "passes_completed",
    "retrieval",
    "total_cases",
    # #598 Step 4: which corpus each half ran on, and the answer index's size.
    "corpus",
    "index_chunks",
    # The runner's deadline: why the run stopped (None when it was not stopped),
    # and the deadline it ran under (None without one).
    # RED WHEN: a written report lacks either key (test_switch_off_report_keys_...).
    "stopped",
    "deadline",
}
TODAY_CASE = {
    "adversarial_score",
    "answer",
    "answer_errored",
    "cache_origin",
    "case_id",
    "contested",
    "decline_reason",
    "echoed_prior",
    "error",
    "evidence",
    "fact_coverage",
    "gate_margin",
    "gate_passed",
    "gate_top_score",
    "grader_calls",
    "harness_error",
    "history_replies",
    "injection_hits",
    "kind",
    "missing_facts",
    "model_calls",
    "no_answer",
    "outcome",
    "question",
    "rationale",
    "raw_reply",
    "resolved_query",
    "retrieval_global",
    "retry_kept",
    "retry_reply",
    "score",
    "spread",
    "standard_median",
    "standard_scores",
    # #598 Step 4: the must_say oracle (report only) and the shipped-retrieval check.
    "must_say_missing",
    "shipped_gold_in_evidence",
    # #576: the writer's primary-failure causes on a turn served from the cache.
    "cached_primary_failures",
}
TODAY_GRADER_CALL = {"duration_seconds", "model", "provider", "rationale", "score"}
BLIND_FIELDS = (
    "answer",
    "score",
    "rationale",
    "standard_scores",
    "standard_median",
    "adversarial_score",
    "spread",
    "contested",
    "error",
    "harness_error",
    "answer_errored",
)


def _gc() -> Any:
    return importlib.import_module("tests.eval.grader_check")


def _mode(user: str) -> str:
    return "passages" if ("<<<PASSAGE" in user or NO_PASSAGES in user) else "blind"


class _Scripted:
    """A grader whose verdict is decided per call from the prompt.

    ``rule(system, user, mode)`` returns a score (int), raw text (str), or an
    exception to raise. The rationale names the mode, so two runs grade alike
    whatever the call order. Every call is kept, with a shared ``log`` if given.
    """

    def __init__(
        self,
        rule: Any = None,
        *,
        model: Any = "fake-judge",
        tokens: tuple[int, int] = (10, 2),
        log: list[str] | None = None,
    ) -> None:
        self.rule = rule or (lambda _s, _u, _m: 4)
        self.model = model
        self.tokens = tokens
        self.log = log
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        mode = _mode(user)
        probe = PROBE_QUESTION in user
        self.calls.append({"system": system, "user": user, "mode": mode, "probe": probe})
        if self.log is not None:
            self.log.append("probe" if probe else f"grade-{mode}")
        out = self.rule(system, user, mode)
        if isinstance(out, BaseException):
            raise out
        text = (
            out
            if isinstance(out, str)
            else json.dumps({"score": out, "rationale": f"{mode} rationale"})
        )
        served = self.model(mode) if callable(self.model) else self.model
        return LLMResult(
            text=text,
            input_tokens=self.tokens[0],
            output_tokens=self.tokens[1],
            model=served,
            provider="router",
        )

    async def aclose(self) -> None:
        return None

    def for_question(self, question: str, mode: str | None = None) -> list[dict[str, Any]]:
        return [
            c for c in self.calls if question in c["user"] and (mode is None or c["mode"] == mode)
        ]


class _LoggedModel(_Model):
    """``_Model`` that logs each answer call to a shared list."""

    def __init__(self, reply: Any, log: list[str], **kwargs: Any) -> None:
        super().__init__(reply, **kwargs)
        self.log = log

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if get_call_site() is CallSite.answer:
            self.log.append("answer")
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


def _switch(on: bool, **overrides: Any) -> Settings:
    settings = Settings(_env_file=None, eval_passage_grading=on, **overrides)  # type: ignore[call-arg]
    assert settings.eval_passage_grading is on  # type: ignore[attr-defined]
    return settings


def _run_cases(
    monkeypatch: pytest.MonkeyPatch,
    cases: list[Any],
    grader: Any,
    *,
    on: bool = True,
    model: Any = None,
    answer_models: tuple[str, ...] = (),
) -> list[JudgedCase]:
    """The REAL ``_judge_cases`` on the hermetic catalog, panel 1 (CI's size)."""
    settings = _switch(on)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    monkeypatch.setattr(judge_mod, "get_llm_client", lambda settings=None: _Judge())
    return asyncio.run(
        runner_mod._judge_cases(
            cases,
            settings=settings,
            answer_llm=runner_mod.AttributingLLM(model or _Model(ANSWERED)),  # type: ignore[arg-type]
            judge_llm=grader,
            expect_answerer=EXPECT,
            answer_models=answer_models,
        )
    )


def _run_settings_switch(on: bool, **overrides: Any) -> Settings:
    settings = _run_settings(eval_passage_grading=on, **overrides)
    assert settings.eval_passage_grading is on  # type: ignore[attr-defined]
    return settings


def _full_run(
    monkeypatch: pytest.MonkeyPatch,
    grader: Any,
    *,
    on: bool = True,
    main: Any = None,
    report: pathlib.Path | None = None,
) -> dict[str, Any]:
    """The REAL ``run_eval_async`` (both passes) on the hermetic catalog."""
    _clients(monkeypatch, grader=grader, main=main)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    return asyncio.run(
        runner_mod.run_eval_async(ids=_IDS, settings=_run_settings_switch(on), report_path=report)
    )


def _probe_rule(wrong: dict[str, int], correct: dict[str, int], case: int = 4) -> Any:
    """Probe calls scored per answer and mode; every case call scores ``case``."""
    gc_correct = None

    def rule(_system: str, user: str, mode: str) -> int:
        nonlocal gc_correct
        if PROBE_QUESTION not in user:
            return case
        if gc_correct is None:
            gc_correct = _gc().CORRECT_ANSWER
        return (correct if gc_correct in user else wrong)[mode]

    return rule


def _cases(summary: dict[str, Any], section: str = "main") -> list[dict[str, Any]]:
    judge = summary["judge"] if section == "main" else summary["fallback"]["judge"]
    return judge["cases"]


# --- the per-case passage grade -----------------------------------------------------------


def test_every_graded_case_gets_a_blind_and_a_passage_grade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the switch does not add the passage panel, or a case's passage
    record is missing, or the passage calls are mixed into the blind ``grader_calls``."""
    golden = _golden()
    cases = [golden["claude_api_lit_ratelimit"], golden["refusal_k8s"]]
    judged = _run_cases(monkeypatch, cases, _Scripted(lambda _s, _u, m: 4 if m == "blind" else 3))
    assert [j.case_id for j in judged] == [c.id for c in cases]
    for case in judged:
        assert case.score == 4
        assert len(case.grader_calls) == 2  # blind panel 1 + veto, as on main
        assert case.passage_score == 3  # type: ignore[attr-defined]
        assert case.passage_adversarial_score == 3  # type: ignore[attr-defined]
        assert tuple(case.passage_standard_scores) == (3,)  # type: ignore[attr-defined]
        assert case.passage_rationale == "passages rationale"  # type: ignore[attr-defined]
        assert len(case.passage_grader_calls) == 2  # type: ignore[attr-defined]
        assert case.passage_error is None  # type: ignore[attr-defined]
        assert set(case.as_dict()) >= PASSAGE_KEYS


def test_the_passage_prompt_holds_the_cases_own_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the passage panel is given other passages than the ones the
    answer was made from (its stored ``evidence``, in rank order)."""
    case = _golden()["claude_api_lit_ratelimit"]
    grader = _Scripted()
    (judged,) = _run_cases(monkeypatch, [case], grader)
    assert judged.evidence  # partner: this case retrieved passages
    first = min(judged.evidence, key=lambda e: e["rank"])
    calls = grader.for_question(case.question, "passages")
    assert len(calls) == 2
    for call in calls:
        assert f"<<<PASSAGE 1 | {first['source']}#{first['heading']}>>>" in call["user"]
        assert first["text"] in call["user"]
        assert NO_PASSAGES not in call["user"]


def test_a_case_that_retrieved_nothing_is_graded_with_the_fixed_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a turn that truly retrieved nothing is given no passage section,
    is skipped, or is reported as a passage error."""
    case = _golden()["refusal_k8s"]
    grader = _Scripted()
    (judged,) = _run_cases(monkeypatch, [case], grader)
    assert judged.evidence == [] and judged.harness_error is None  # partner: read, and empty
    calls = grader.for_question(case.question, "passages")
    assert len(calls) == 2
    assert all(NO_PASSAGES in c["user"] and "<<<PASSAGE" not in c["user"] for c in calls)
    assert judged.passage_error is None  # type: ignore[attr-defined]
    assert judged.passage_score == 4  # type: ignore[attr-defined]


def test_the_blind_panel_runs_first_and_the_two_never_interleave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the passage panel runs before or alongside the blind one (call
    records would no longer be in order)."""
    case = _golden()["claude_api_lit_ratelimit"]
    grader = _Scripted()
    _run_cases(monkeypatch, [case], grader)
    modes = [c["mode"] for c in grader.for_question(case.question)]
    assert modes == ["blind", "blind", "passages", "passages"]


def test_a_cache_hit_is_graded_with_its_origins_passages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: a cache hit (whose own ``evidence`` is empty) is graded with the
    no-passages block instead of the passages of the turn that wrote the answer."""
    golden = _golden()
    pair = [golden["citevyn_lit_free"], golden["citevyn_lit_alias_sitewin"]]
    grader = _Scripted()
    writer, hit = _run_cases(
        monkeypatch, pair, grader, model=_Model("CiteVyn is free right now [1].")
    )
    assert hit.outcome == "cache_hit" and hit.evidence == []  # partner: a real hit
    assert hit.cache_origin and hit.cache_origin["evidence"]
    origin_first = min(hit.cache_origin["evidence"], key=lambda e: e["rank"])
    calls = grader.for_question(pair[1].question, "passages")
    assert len(calls) == 2
    for call in calls:
        assert NO_PASSAGES not in call["user"]
        assert origin_first["text"] in call["user"]
        assert f"| {origin_first['source']}#{origin_first['heading']}>>>" in call["user"]
    assert hit.passage_score == 4  # type: ignore[attr-defined]


def test_unreadable_evidence_is_a_passage_error_not_no_passages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a case whose evidence read failed is graded as if it had
    retrieved nothing (the fixed block would say "no passages" about a turn that
    did retrieve some), instead of being left ungraded with ``passage_error``."""
    case = _golden()["claude_api_lit_ratelimit"]

    async def _broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("evidence read failed")

    monkeypatch.setattr(runner_mod, "_turn_evidence", _broken)
    grader = _Scripted()
    (judged,) = _run_cases(monkeypatch, [case], grader)
    assert judged.score == 4  # partner: the blind grade still ran
    assert grader.for_question(case.question, "passages") == []
    assert judged.passage_score is None  # type: ignore[attr-defined]
    assert judged.passage_error  # type: ignore[attr-defined]
    assert judged.passage_grader_calls == []  # type: ignore[attr-defined]


def test_a_cache_origin_without_evidence_is_a_passage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a cache hit whose origin's evidence is unknown (``None``) is graded
    with the no-passages block instead of being left ungraded with ``passage_error``."""
    golden = _golden()
    pair = [golden["citevyn_lit_free"], golden["citevyn_lit_alias_sitewin"]]
    real = runner_mod._turn_evidence
    seen: list[int] = []

    async def _first_fails(*args: Any, **kwargs: Any) -> Any:
        seen.append(1)
        if len(seen) == 1:
            raise RuntimeError("evidence read failed")
        return await real(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "_turn_evidence", _first_fails)
    grader = _Scripted()
    writer, hit = _run_cases(
        monkeypatch, pair, grader, model=_Model("CiteVyn is free right now [1].")
    )
    assert hit.outcome == "cache_hit"
    assert hit.cache_origin is not None and hit.cache_origin["evidence"] is None  # partner
    assert grader.for_question(pair[1].question, "passages") == []
    assert hit.passage_score is None and hit.passage_error  # type: ignore[attr-defined]
    assert writer.passage_score is None and writer.passage_error  # type: ignore[attr-defined]


# --- passage failures are report-only ------------------------------------------------------


def _blind_intact(case: JudgedCase) -> None:
    assert case.score == 4 and case.rationale == "blind rationale"
    assert case.error is None and case.harness_error is None
    assert case.passage_score is None  # type: ignore[attr-defined]


def test_self_grading_on_a_passage_call_voids_only_the_passage_grade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a passage call served by an answer model is used as the passage
    grade, or voids the blind grade, or becomes a harness error (passage grading
    is report-only)."""
    case = _golden()["claude_api_lit_ratelimit"]
    grader = _Scripted(model=lambda mode: FALLBACK if mode == "passages" else "fake-judge")
    (judged,) = _run_cases(monkeypatch, [case], grader, answer_models=(ANSWER_MODEL, FALLBACK))
    _blind_intact(judged)
    assert "self-grading" in (judged.passage_error or "").lower()  # type: ignore[attr-defined]
    served = [c["model"] for c in judged.passage_grader_calls]  # type: ignore[attr-defined]
    assert served == [FALLBACK, FALLBACK]  # partner: the calls were made and kept


def test_a_parse_failure_on_a_passage_call_leaves_the_blind_grade_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a malformed passage verdict errors the case, voids its blind
    grade, or is lost instead of being recorded as ``passage_error``."""
    case = _golden()["claude_api_lit_ratelimit"]
    grader = _Scripted(lambda _s, _u, m: 4 if m == "blind" else "not a verdict")
    (judged,) = _run_cases(monkeypatch, [case], grader)
    _blind_intact(judged)
    assert "judge_parse" in (judged.passage_error or "")  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("boom"), LLMUnavailable("grader timed out"), KeyError("candidates")],
    ids=["runtime", "unavailable", "keyerror"],
)
def test_any_exception_in_passage_grading_is_a_passage_error_only(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """Red when: an exception in the passage panel aborts the case or the pass,
    becomes a harness error (which fails the harness-integrity check) or a case
    error, instead of a ``passage_error`` naming its type."""
    golden = _golden()
    cases = [golden["claude_api_lit_ratelimit"], golden["refusal_k8s"]]
    grader = _Scripted(lambda _s, _u, m: 4 if m == "blind" else exc)
    judged = _run_cases(monkeypatch, cases, grader)
    assert [j.case_id for j in judged] == [c.id for c in cases]  # the pass went on
    for case in judged:
        _blind_intact(case)
        assert type(exc).__name__ in (case.passage_error or "")  # type: ignore[attr-defined]
    summary = runner_mod._summarize(cases, {}, judged, judge_available=True, judgeable=cases)
    assert summary["judge"]["harness_errors"] == []
    assert summary["judge"]["scored"] == 2  # partner: both blind grades count


def test_switch_off_makes_no_passage_call_and_adds_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: passage grading runs, or its keys appear, with the switch off
    (the off run must be exactly today's)."""
    case = _golden()["claude_api_lit_ratelimit"]
    grader = _Scripted()
    (judged,) = _run_cases(monkeypatch, [case], grader, on=False)
    assert len(grader.calls) == 2 and judged.score == 4  # partner: the blind panel ran
    assert all(c["mode"] == "blind" for c in grader.calls)
    data = judged.as_dict()
    assert set(data) == TODAY_CASE
    assert all(set(c) == TODAY_GRADER_CALL for c in data["grader_calls"])


# --- the whole run: off is today's, on never moves a gate ------------------------------------


def test_switch_off_report_keys_and_exit_are_todays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: with the switch off, the written report gains or loses a key
    (``grader_check``, ``passage_*``, token fields), the probe runs, or the gates
    change."""
    grader = _Scripted()
    report = tmp_path / "r.json"
    summary = _full_run(monkeypatch, grader, on=False, report=report)
    written = json.loads(report.read_text(encoding="utf-8"))
    assert set(written) == TODAY_TOP
    for section in ("main", "fallback"):
        cases = _cases(written, section)
        assert len(cases) == 2  # partner: both cases ran in both passes
        for case in cases:
            assert set(case) == TODAY_CASE
            assert all(set(c) == TODAY_GRADER_CALL for c in case["grader_calls"])
    assert len(grader.calls) == 8 and not any(c["probe"] for c in grader.calls)
    assert runner_mod.gate_failures(summary) == []


def test_switch_on_with_every_passage_score_1_moves_no_blind_field_and_no_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a passage score reaches the mean, a gate or a blind field (the
    switch would then change the release decision it is only meant to study)."""
    off = _full_run(monkeypatch, _Scripted(lambda _s, _u, m: 4 if m == "blind" else 1), on=False)
    on = _full_run(monkeypatch, _Scripted(lambda _s, _u, m: 4 if m == "blind" else 1), on=True)
    for section in ("main", "fallback"):
        before, after = _cases(off, section), _cases(on, section)
        assert [c["passage_score"] for c in after] == [1, 1]  # partner: passages scored 1
        for b, a in zip(before, after, strict=True):
            assert {k: b[k] for k in BLIND_FIELDS} == {k: a[k] for k in BLIND_FIELDS}
            strip = ("duration_seconds", "input_tokens", "output_tokens")
            assert [{k: v for k, v in c.items() if k not in strip} for c in b["grader_calls"]] == [
                {k: v for k, v in c.items() if k not in strip} for c in a["grader_calls"]
            ]
    for key in ("mean_score", "scored", "judged"):
        assert off["judge"][key] == on["judge"][key]
        assert off["fallback"]["judge"][key] == on["fallback"]["judge"][key]
    assert runner_mod.gate_failures(on) == runner_mod.gate_failures(off) == []
    # Partner: the gates are live on this summary; a low BLIND mean does fail it.
    low = json.loads(json.dumps(on, default=str))
    low["judge"]["mean_score"] = 1.0
    assert any("mean judge score" in f for f in runner_mod.gate_failures(low))


# --- the probe ---------------------------------------------------------------------------------


def test_the_probe_runs_once_before_the_first_case_and_is_saved_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: the probe runs after a case (a cut-off run could lose the
    decision), runs once per pass, makes another number of calls than
    2 answers x 2 modes x 3 repeats x (panel 1 + veto) = 24, is not saved after
    EACH of its four cells in order (a run cut off mid-probe would lose the cells
    already paid for), or its full save comes with or after a case."""
    log: list[str] = []
    grader = _Scripted(
        _probe_rule({"blind": 1, "passages": 1}, {"blind": 5, "passages": 5}), log=log
    )
    writes: list[dict[str, Any]] = []
    answers_before: list[int] = []
    real_write = runner_mod.write_report

    def _capture(path: Any, summary: dict[str, Any], settings: Any, **kwargs: Any) -> None:
        writes.append(json.loads(json.dumps(summary, default=str)))
        answers_before.append(log.count("answer"))
        real_write(path, summary, settings, **kwargs)

    monkeypatch.setattr(runner_mod, "write_report", _capture)
    _full_run(
        monkeypatch,
        grader,
        main=_LoggedModel(ANSWERED, log),
        report=tmp_path / "r.json",
    )
    assert "answer" in log  # partner: cases were answered
    assert log.count("probe") == 24
    assert max(i for i, e in enumerate(log) if e == "probe") < log.index("answer")
    # One save after each probe cell, in the order the probe grades them.
    order = [
        ("wrong", "blind"),
        ("wrong", "passages"),
        ("correct", "blind"),
        ("correct", "passages"),
    ]
    assert len(writes) >= 4
    for n, write in enumerate(writes[:4], start=1):
        cells = write["grader_check"]["probe"]["cells"]
        lengths = {(a, m): len((cells.get(a) or {}).get(m) or []) for a, m in order}
        assert lengths == {cell: (3 if i < n else 0) for i, cell in enumerate(order)}, (n, cells)
        assert write["judge"]["cases"] == []
    last_probe_save = writes[3]
    assert answers_before[3] == 0  # no case had run before the full probe save
    assert last_probe_save["grader_check"]["decision"] == "keep"
    assert last_probe_save["grader_check"]["paired"]["main"]["coverage_label"] == "not available"
    # Every later save carries it too.
    assert all(w.get("grader_check", {}).get("decision") == "keep" for w in writes[3:])


@pytest.mark.parametrize(
    ("wrong_passages", "decision"),
    [(3, "drop"), (1, "inconclusive")],
    ids=["drop-established", "nothing-established"],
)
def test_a_probe_cut_off_after_the_wrong_answer_cells_keeps_them_and_their_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, wrong_passages: int, decision: str
) -> None:
    """Red when: a run cut off during the probe saves nothing (the wrong-answer
    cells already paid for are lost), or the saved decision ignores a drop the
    finished cells already establish, or a partial probe without one is saved as
    anything but inconclusive with a "probe not finished" reason."""
    wrong = {"blind": 1, "passages": wrong_passages}
    correct_answer = _gc().CORRECT_ANSWER

    def rule(system: str, user: str, mode: str) -> Any:
        if PROBE_QUESTION in user and correct_answer in user:
            return KeyboardInterrupt()  # the run is cut off at the first correct-answer call
        return _probe_rule(wrong, {"blind": 5, "passages": 5})(system, user, mode)

    report = tmp_path / "r.json"
    with pytest.raises(KeyboardInterrupt):
        _full_run(monkeypatch, _Scripted(rule), report=report)
    assert report.exists(), "a probe cut off after two finished cells saved nothing"
    written = json.loads(report.read_text(encoding="utf-8"))
    check = written["grader_check"]
    cells = check["probe"]["cells"]
    assert cells["wrong"] == {"blind": [1, 1, 1], "passages": [wrong_passages] * 3}
    assert not (cells.get("correct") or {}).get("blind")  # partner: really cut off there
    assert written["judge"]["cases"] == []
    assert check["decision"] == decision
    if decision == "inconclusive":
        assert any("probe not finished" in r for r in check["reasons"]), check["reasons"]


def test_the_probe_grades_with_the_fixed_codex_passages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the probe's passage calls do not carry the fixture's Codex
    passages, or its blind calls do."""
    grader = _Scripted()
    _full_run(monkeypatch, grader)
    probe = [c for c in grader.calls if c["probe"]]
    assert len(probe) == 24  # partner: the probe ran
    passages = [c for c in probe if c["mode"] == "passages"]
    blind = [c for c in probe if c["mode"] == "blind"]
    assert len(passages) == len(blind) == 12
    assert all("<<<PASSAGE 1 | codex#CLI flags>>>" in c["user"] for c in passages)
    assert all("--model flag selects the model" in c["user"] for c in passages)
    assert all("<<<PASSAGE" not in c["user"] for c in blind)
    correct = _gc().CORRECT_ANSWER
    assert sum(correct in c["user"] for c in probe) == 12


@pytest.mark.parametrize(
    ("wrong", "correct", "decision"),
    [
        ({"blind": 1, "passages": 1}, {"blind": 5, "passages": 5}, "keep"),
        ({"blind": 1, "passages": 3}, {"blind": 5, "passages": 5}, "drop"),
        ({"blind": 1, "passages": 1}, {"blind": 5, "passages": 3}, "drop"),
    ],
    ids=["keep", "drop-wrong-raised", "drop-correct-lowered"],
)
def test_the_reports_decision_comes_from_the_probe(
    monkeypatch: pytest.MonkeyPatch,
    wrong: dict[str, int],
    correct: dict[str, int],
    decision: str,
) -> None:
    """Red when: the report's decision is not the rule applied to the probe's
    own scores (for example, read from the golden cases' scores)."""
    summary = _full_run(monkeypatch, _Scripted(_probe_rule(wrong, correct)))
    check = summary["grader_check"]
    assert check["decision"] == decision
    assert check["probe"]["cells"]["wrong"]["passages"] == [wrong["passages"]] * 3
    if decision == "drop":
        assert check["reasons"]


def test_the_probe_is_excluded_from_every_answer_figure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the probe's scores (the wrong answer scores 1) enter the mean, the
    judged or scored counts, or the case list."""
    summary = _full_run(
        monkeypatch,
        _Scripted(_probe_rule({"blind": 1, "passages": 1}, {"blind": 5, "passages": 5})),
    )
    assert summary["grader_check"]["probe"]["cells"]["wrong"]["blind"] == [1, 1, 1]  # partner
    for judge in (summary["judge"], summary["fallback"]["judge"]):
        assert judge["judged"] == judge["scored"] == 2
        assert judge["mean_score"] == 4.0
        assert "codex_unprompted_evidence_phrase" not in {c["case_id"] for c in judge["cases"]}


# --- report sections ------------------------------------------------------------------------------


def test_the_report_has_a_paired_table_per_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the blind-vs-passages table is missing for a pass."""
    summary = _full_run(monkeypatch, _Scripted(lambda _s, _u, m: 4 if m == "blind" else 5))
    paired = summary["grader_check"]["paired"]
    for section in ("main", "fallback"):
        assert paired[section]["counts"] == {"raised": 2, "lowered": 0, "same": 0, "missing": 0}
        assert paired[section]["coverage"] == 1.0


def test_grader_calls_record_tokens_and_the_spend_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: grader calls do not record their tokens, or the report's split
    by role (blind, passages, probe) does not add up from them."""
    summary = _full_run(monkeypatch, _Scripted(tokens=(10, 2)))
    case = _cases(summary)[0]
    assert {c["input_tokens"] for c in case["grader_calls"]} == {10}
    assert {c["output_tokens"] for c in case["passage_grader_calls"]} == {2}
    assert summary["grader_check"]["spend"] == {
        "blind": {"calls": 8, "input_tokens": 80, "output_tokens": 16},
        "passages": {"calls": 8, "input_tokens": 80, "output_tokens": 16},
        "probe": {"calls": 24, "input_tokens": 240, "output_tokens": 48},
    }


def test_the_report_records_the_commit_it_ran_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the report cannot say which commit was measured (the eval files
    are diffed against the merge commit after merge)."""
    monkeypatch.setenv("GITHUB_SHA", "0123456789abcdef0123456789abcdef01234567")
    summary = _full_run(monkeypatch, _Scripted())
    assert summary["grader_check"]["commit"] == "0123456789abcdef0123456789abcdef01234567"


# --- the spend cap ------------------------------------------------------------------------------

_CAP = "Daily cost limit reached ($1.00 of $1.00); paid model calls are stopped."
_UNREADABLE = "Cost budget cannot be verified (meter store unavailable); refusing paid calls."
_BUDGET = re.compile(r"\bbudget: ")


def _case_question(case_id: str) -> str:
    return _golden()[case_id].question


@pytest.mark.parametrize(
    ("exc", "stopped"),
    [
        (CostLimitReached(_CAP), "stopped by the spend cap"),
        (
            CostLimitReached(_UNREADABLE, cause=OSError("db down")),
            "stopped: the spend meter could not be read",
        ),
    ],
    ids=["cap", "meter-unreadable"],
)
def test_a_budget_stop_on_a_grader_call_is_labelled_with_its_cause(
    monkeypatch: pytest.MonkeyPatch, exc: CostLimitReached, stopped: str
) -> None:
    """Red when: a ``CostLimitReached`` on a grader call reads as an ordinary
    outage, or the report does not say why it stopped, or reads a meter failure as
    the cap being reached (or the other way round)."""
    question = _case_question(_IDS[1])
    grader = _Scripted(lambda _s, u, _m: exc if question in u else 4)
    summary = _full_run(monkeypatch, grader)
    stopped_case = [c for c in _cases(summary) if c["case_id"] == _IDS[1]]
    assert stopped_case and _BUDGET.search(stopped_case[0]["error"] or ""), stopped_case
    healthy = [c for c in _cases(summary) if c["case_id"] == _IDS[0]]
    assert healthy and not _BUDGET.search(healthy[0]["error"] or "")  # partner
    assert summary["grader_check"]["stopped"] == stopped


def test_a_budget_stop_on_an_answer_call_is_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: an answer call stopped by the spend cap is reported only as an
    answer failure, without the ``budget`` label or the report's stop line."""
    question = _case_question(_IDS[1])

    def reply(prompt: str) -> str:
        if f"Question: {question}\n" in prompt:
            raise CostLimitReached(_CAP)
        return ANSWERED

    summary = _full_run(monkeypatch, _Scripted(), main=_Model(reply))
    stopped_case = [c for c in _cases(summary) if c["case_id"] == _IDS[1]]
    assert stopped_case and stopped_case[0]["answer_errored"] is True  # partner
    assert _BUDGET.search(stopped_case[0]["error"] or ""), stopped_case
    assert summary["grader_check"]["stopped"] == "stopped by the spend cap"


def test_a_budget_stop_during_the_probe_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the cap stopping the probe leaves no stop line, or the decision is
    made from the missing probe scores instead of being inconclusive."""
    grader = _Scripted(lambda _s, u, _m: CostLimitReached(_CAP) if PROBE_QUESTION in u else 4)
    summary = _full_run(monkeypatch, grader)
    check = summary["grader_check"]
    assert check["stopped"] == "stopped by the spend cap"
    assert check["decision"] == "inconclusive"
    assert check["probe"]["cells"]["wrong"]["blind"] == [None, None, None]


def test_a_healthy_run_is_not_reported_as_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partner of the stop lines. Red when: a run the cap never touched says it
    was stopped."""
    summary = _full_run(monkeypatch, _Scripted())
    assert summary["grader_check"]["stopped"] is None
    assert all(not _BUDGET.search(c["error"] or "") for c in _cases(summary))


# --- main(): refusals before anything runs, and the printed lines ------------------------------


@pytest.fixture
def no_run(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    started: list[Any] = []

    async def _never(**kwargs: Any) -> dict[str, Any]:
        started.append(kwargs)
        raise RuntimeError("the eval ran")

    monkeypatch.setattr(runner_mod, "run_eval_async", _never)
    return started


def _late(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend it is 23:00 UTC (60 minutes before the spend window resets)."""
    monkeypatch.setattr(
        _gc(), "utc_now", lambda: datetime(2026, 11, 16, 23, 0, tzinfo=UTC), raising=True
    )


def _noon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _gc(), "utc_now", lambda: datetime(2026, 11, 16, 12, 0, tzinfo=UTC), raising=True
    )


@pytest.mark.parametrize(
    ("ref", "late"),
    [("refs/heads/main", False), ("refs/tags/v0.12.0", False), ("refs/heads/eval/x", True)],
    ids=["main", "release-tag", "too-late"],
)
def test_main_refuses_a_grader_check_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_run: list[Any],
    ref: str,
    late: bool,
) -> None:
    """Red when: a grader-check run starts on main, on a release tag, or with too
    little of the UTC day left; the refusal must come before the eval starts."""
    monkeypatch.setattr(runner_mod, "get_settings", lambda: _run_settings_switch(True))
    monkeypatch.setenv("GITHUB_REF", ref)
    (_late if late else _noon)(monkeypatch)
    try:
        code: Any = runner_mod.main(["--quiet"])
    except SystemExit as exc:  # a refusal may exit instead of returning a code
        code = exc.code
    assert code not in (0, None)
    assert no_run == []
    said = capsys.readouterr().out + (code if isinstance(code, str) else "")
    assert "nothing was run" in said.lower(), said


@pytest.mark.parametrize("on", [True, False], ids=["branch-run", "switch-off-on-a-tag"])
def test_main_lets_an_allowed_run_start(
    monkeypatch: pytest.MonkeyPatch, no_run: list[Any], on: bool
) -> None:
    """Partner. Red when: the refusal also stops a grader check on a branch at
    noon UTC, or a normal (switch off) release run on a tag late in the day."""
    monkeypatch.setattr(runner_mod, "get_settings", lambda: _run_settings_switch(on))
    if on:
        monkeypatch.setenv("GITHUB_REF", "refs/heads/eval/598-grader-check")
        _noon(monkeypatch)
    else:
        monkeypatch.setenv("GITHUB_REF", "refs/tags/v0.12.0")
        _late(monkeypatch)
    with pytest.raises(RuntimeError, match="the eval ran"):
        runner_mod.main(["--quiet"])
    assert len(no_run) == 1


def test_main_prints_the_grader_check_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: the decision, its reasons and the probe table are not printed (the
    job summary is where they are read)."""
    _clients(monkeypatch, grader=_Scripted())
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    monkeypatch.setattr(runner_mod, "get_settings", lambda: _run_settings_switch(True))
    monkeypatch.delenv("GITHUB_REF", raising=False)
    _noon(monkeypatch)
    runner_mod.main(["--ids", ",".join(_IDS), "--report", str(tmp_path / "r.json")])
    out = capsys.readouterr().out
    assert "GRADER CHECK" in out and "GRADER PROBE" in out
