"""The judged eval runner's own deadline and grading budget.

Written before the implementation (test designer). Free and hermetic: the SQLite
catalog, scripted answer and grader clients, and a fake clock. The contract for
``backend/tests/eval/runner.py``:

* **The clock seam.** A module-level ``_clock`` (``time.monotonic`` by default),
  read at call time, never bound as a default argument. The run's start is read
  from it at the top of ``run_eval_async``, so the retrieval half counts. Tests
  patch ``runner._clock``.
* **Deadline D** (``Settings.eval_deadline_seconds``; ``None`` = no deadline,
  today's run). Checked before each new paid unit: before the probe, before each
  probe cell, at the top of each case, and before the fallback pass is built.
  It fires when ``now - start >= D``. When it fires: the unit does not start, the
  run starts nothing more, the summary's top-level ``stopped`` is set (in every
  save from then on, through the normal per-case save) with ``reason ==
  "deadline"``, ``deadline_seconds`` and ``not_run`` (``{"main": [...],
  "fallback": [...]}``, case ids, via ``_not_reached``); a run-level
  ``harness_errors`` entry with ``stage == "deadline"`` is added; the pass in
  which it fired is not added to ``passes_completed``; ``gate_failures(summary)[0]``
  starts with ``runner deadline:``; and ``main()`` prints ``EVAL STOPPED AT THE
  RUNNER DEADLINE`` (even under ``--quiet``) and exits 1.
* **The report always carries** top-level ``stopped`` (``None`` on a run that was
  not stopped) and ``deadline`` (the deadline in seconds, or ``None``).
* **Grading budget G** (``Settings.eval_grading_budget_seconds``). Before EACH
  grading panel of a case (blind, then passage), ``remaining = start + D + G - now``
  must be at least that one panel's call count (``panel_size() + 1``) x
  ``eval_judge_timeout_seconds``. If not: no grader call is made for that panel;
  the blind panel gets ``error`` ``grade: not graded, over budget`` (no score, no
  harness error); the passage panel gets ``passage_error`` ``not graded with
  passages: not graded, over budget``; ``stopped`` is set with the case id in
  ``stopped["over_budget"]``, and the run ends after that case.
* **Start-up refusal.** A judged run whose budget is below the LARGEST single
  panel's calls x the grader timeout raises ``SystemExit`` naming
  ``CITEVYN_EVAL_GRADING_BUDGET_SECONDS`` and saying nothing was run, before the
  retrieval half and before any answer client is built. The largest single panel
  is ``panel_size() + 1`` calls whether passage grading is on or off (the passage
  panel is the same size), never the sum of both panels.

Each test carries one ``RED WHEN`` line naming the change that turns it red.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

import pytest

from app.core.config import Settings
from app.cost.call_site import CallSite, get_call_site
from app.llm.types import LLMResult
from tests.eval import runner as runner_mod
from tests.eval.cases import load_cases
from tests.eval.paths import GOLDEN_PATH
from tests.test_eval_report_fields import _Model, _two_pass_settings
from tests.test_eval_report_integrity import ANSWERED, _clients
from tests.test_grader_check_runner import PROBE_QUESTION, _Scripted

START = 1000.0
#: An unusual deadline, so a hard-coded value in the runner cannot pass by chance.
D = 137
TWO = ["claude_api_lit_ratelimit", "codex_lit_modelflag"]
THREE = ["claude_api_lit_ratelimit", "claude_api_lit_envvar", "codex_lit_modelflag"]
OVER_BUDGET = "grade: not graded, over budget"
PASSAGE_OVER_BUDGET = "not graded with passages: not graded, over budget"
#: Probe calls at panel 1: 2 answers x 2 modes x 3 repeats x (1 framing + veto).
PROBE_CALLS = 24
#: One probe cell (3 repeats of one answer in one mode) at panel 1.
CELL_CALLS = 6


class _Clock:
    """A fake monotonic clock: ``now`` only moves when a test moves it."""

    def __init__(self, start: float = START) -> None:
        self.start = self.now = start

    def __call__(self) -> float:
        return self.now

    def at(self, offset: float) -> None:
        self.now = self.start + offset


def _patch_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    # raising=True: the seam must exist as a module attribute of the runner.
    monkeypatch.setattr(runner_mod, "_clock", clock)
    return clock


class _Paced(_Model):
    """Answers like ``_Model``; the answer call for a question listed in ``at``
    moves the clock to ``start + offset`` (idempotent, so a retry moves nothing)."""

    def __init__(self, clock: _Clock, at: dict[str, float], **kwargs: Any) -> None:
        super().__init__(ANSWERED, **kwargs)
        self.clock, self.at = clock, at

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if get_call_site() is CallSite.answer:
            for question, offset in self.at.items():
                if f"Question: {question}\n" in user:
                    self.clock.at(offset)
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


def _q(case_id: str) -> str:
    return {c.id: c for c in load_cases(GOLDEN_PATH)}[case_id].question


def _settings(
    *,
    deadline: int | None = D,
    budget: int | None = 100,
    judge_timeout: float = 1.0,
    fallback: str | None = None,
    passages: bool = False,
    **overrides: Any,
) -> Settings:
    settings = _two_pass_settings(
        eval_fallback_model=fallback,
        eval_judge_timeout_seconds=judge_timeout,
        eval_passage_grading=passages,
        eval_deadline_seconds=deadline,
        eval_grading_budget_seconds=budget,
        **overrides,
    )
    # extra="ignore" would drop an unknown field silently; fail on the missing one.
    assert settings.eval_deadline_seconds == deadline  # type: ignore[attr-defined]
    assert settings.eval_grading_budget_seconds == budget  # type: ignore[attr-defined]
    return settings


def _run(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    ids: list[str],
    *,
    main: Any = None,
    grader: Any = None,
    report: pathlib.Path | None = None,
    panel: str = "1",
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    built = _clients(monkeypatch, main=main, grader=grader)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", panel)
    summary = asyncio.run(runner_mod.run_eval_async(ids=ids, settings=settings, report_path=report))
    return summary, built


def _ids(section: dict[str, Any]) -> list[str]:
    return [c["case_id"] for c in section["cases"]]


def _deadline_errors(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in summary.get("harness_errors") or [] if e.get("stage") == "deadline"]


def _assert_stopped_by_the_deadline(summary: dict[str, Any]) -> None:
    stopped = summary["stopped"]
    assert stopped is not None and stopped["reason"] == "deadline", stopped
    assert len(_deadline_errors(summary)) == 1, summary.get("harness_errors")
    failures = runner_mod.gate_failures(summary)
    assert failures and failures[0].startswith("runner deadline:"), failures


# --- the deadline at the top of each case -------------------------------------------------


@pytest.mark.parametrize(("offset", "stops"), [(D - 1, False), (D, True)], ids=["D-1", "D"])
def test_the_deadline_stops_the_next_case_at_d_and_not_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, offset: int, stops: bool
) -> None:
    # RED WHEN: the case-top check is removed, uses > not >=, or ignores the setting.
    clock = _patch_clock(monkeypatch)
    first, second = TWO
    report = tmp_path / "eval_report.json"
    summary, _built = _run(
        monkeypatch,
        _settings(),
        TWO,
        main=_Paced(clock, {_q(first): offset}),
        report=report,
    )
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["deadline"] == D
    if stops:
        assert _ids(summary["judge"]) == [first]
        _assert_stopped_by_the_deadline(summary)
        assert summary["stopped"]["deadline_seconds"] == D
        assert summary["stopped"]["not_run"]["main"] == [second]
        assert summary["passes_completed"] == []
        assert written["stopped"] == json.loads(json.dumps(summary["stopped"], default=str))
    else:
        assert _ids(summary["judge"]) == [first, second]  # partner: D-1 runs on
        assert summary["stopped"] is None and written["stopped"] is None
        assert _deadline_errors(summary) == []
        assert summary["passes_completed"] == ["main"]
        assert not any(f.startswith("runner deadline:") for f in runner_mod.gate_failures(summary))


@pytest.mark.parametrize(("deadline", "budget", "ran"), [(90, 80, 1), (150, 80, 2)])
def test_two_deadlines_give_two_case_counts(
    monkeypatch: pytest.MonkeyPatch, deadline: int, budget: int, ran: int
) -> None:
    # RED WHEN: the runner uses a fixed deadline instead of eval_deadline_seconds.
    clock = _patch_clock(monkeypatch)
    paced = _Paced(clock, {_q(case_id): 100.0 * (n + 1) for n, case_id in enumerate(THREE)})
    summary, _built = _run(
        monkeypatch, _settings(deadline=deadline, budget=budget), THREE, main=paced
    )
    assert _ids(summary["judge"]) == THREE[:ran]
    _assert_stopped_by_the_deadline(summary)
    assert summary["stopped"]["not_run"]["main"] == THREE[ran:]
    assert all(c["score"] == 4 for c in summary["judge"]["cases"])  # the run cases graded


def test_without_a_deadline_the_clock_never_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # RED WHEN: a run without a deadline stops, or drops the stopped/deadline keys.
    clock = _patch_clock(monkeypatch)
    first, second = TWO
    summary, _built = _run(
        monkeypatch,
        _settings(deadline=None, budget=None),
        TWO,
        main=_Paced(clock, {_q(first): 1_000_000.0}),
    )
    assert _ids(summary["judge"]) == [first, second]
    assert summary["stopped"] is None
    assert summary["deadline"] is None
    assert _deadline_errors(summary) == []


# --- the deadline before the fallback pass -----------------------------------------------


@pytest.mark.parametrize(("offset", "stops"), [(D - 1, False), (D, True)], ids=["D-1", "D"])
def test_the_deadline_is_checked_before_the_fallback_pass_is_built(
    monkeypatch: pytest.MonkeyPatch, offset: int, stops: bool
) -> None:
    # RED WHEN: the check before building the fallback pass is removed.
    clock = _patch_clock(monkeypatch)
    first, second = TWO
    summary, built = _run(
        monkeypatch,
        _settings(fallback="openai/gpt-4o-mini"),
        TWO,
        # The clock passes D only during the main pass's LAST case, so every
        # case-top check in the main pass is still before the deadline.
        main=_Paced(clock, {_q(second): offset}),
    )
    assert _ids(summary["judge"]) == [first, second]  # the main pass finished
    if stops:
        assert built["fallback"] == []
        assert not (summary.get("fallback") or {}).get("judge", {}).get("cases")
        _assert_stopped_by_the_deadline(summary)
        assert summary["stopped"]["not_run"] == {"main": [], "fallback": [first, second]}
        assert summary["passes_completed"] == ["main"]
    else:
        # Partner: before the deadline the fallback pass is built and runs.
        assert len(built["fallback"]) == 1
        assert _ids(summary["fallback"]["judge"]) == [first, second]
        assert summary["stopped"] is None
        assert summary["passes_completed"] == ["main", "fallback"]


# --- the deadline before the probe and before each probe cell ------------------------------


def _probe_calls(grader: _Scripted) -> int:
    return sum(1 for c in grader.calls if c["probe"])


@pytest.mark.parametrize(("offset", "stops"), [(599, False), (600, True)], ids=["D-1", "D"])
def test_the_deadline_is_checked_before_the_probe(
    monkeypatch: pytest.MonkeyPatch, offset: int, stops: bool
) -> None:
    # RED WHEN: the probe starts without a deadline check after the retrieval half.
    clock = _patch_clock(monkeypatch)
    real = runner_mod.evaluate_retrieval

    async def _slow_retrieval(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        clock.at(offset)
        return result

    monkeypatch.setattr(runner_mod, "evaluate_retrieval", _slow_retrieval)
    grader = _Scripted()
    main = _Model(ANSWERED)
    summary, _built = _run(
        monkeypatch,
        _settings(deadline=600, budget=300, judge_timeout=60.0, passages=True),
        TWO,
        main=main,
        grader=grader,
    )
    if stops:
        assert _probe_calls(grader) == 0
        assert main.answer_prompts == []
        _assert_stopped_by_the_deadline(summary)
        assert summary["stopped"]["not_run"]["main"] == TWO
        assert summary["passes_completed"] == []
    else:
        # Partner: before the deadline the probe makes its calls and the cases run.
        assert _probe_calls(grader) == PROBE_CALLS
        assert _ids(summary["judge"]) == TWO
        assert summary["stopped"] is None


def test_the_deadline_is_checked_before_each_probe_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    # RED WHEN: the deadline check before each probe cell is removed.
    clock = _patch_clock(monkeypatch)
    seen = {"probe": 0}

    def _rule(_system: str, user: str, mode: str) -> int:
        if PROBE_QUESTION in user:
            seen["probe"] += 1
            if seen["probe"] == CELL_CALLS:
                clock.at(600)  # the first cell's last call ends at the deadline
        return 4

    grader = _Scripted(_rule)
    main = _Model(ANSWERED)
    summary, _built = _run(
        monkeypatch,
        _settings(deadline=600, budget=300, judge_timeout=60.0, passages=True),
        TWO,
        main=main,
        grader=grader,
    )
    assert _probe_calls(grader) == CELL_CALLS  # the first cell finished; no other started
    assert main.answer_prompts == []
    _assert_stopped_by_the_deadline(summary)
    assert summary["passes_completed"] == []


# --- main(): the printed line and the exit code -----------------------------------------


@pytest.mark.parametrize(("offset", "stops"), [(D - 1, False), (D, True)], ids=["D-1", "D"])
def test_main_prints_the_stop_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    offset: int,
    stops: bool,
) -> None:
    # RED WHEN: main() does not print the stop line under --quiet or exits 0 when stopped.
    clock = _patch_clock(monkeypatch)
    settings = _settings()
    first, _second = TWO
    _clients(monkeypatch, main=_Paced(clock, {_q(first): offset}))
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)
    report = tmp_path / "eval_report.json"
    code = runner_mod.main(["--ids", ",".join(TWO), "--report", str(report), "--quiet"])
    out = capsys.readouterr().out
    written = json.loads(report.read_text(encoding="utf-8"))
    if stops:
        assert code == 1
        assert "EVAL STOPPED AT THE RUNNER DEADLINE" in out, out
        assert any("runner deadline:" in ln for ln in out.splitlines()), out
        assert written["stopped"]["reason"] == "deadline"
        assert written["in_progress"] is None
    else:
        # Partner: the same run before the deadline prints neither line.
        assert "EVAL STOPPED AT THE RUNNER DEADLINE" not in out
        assert "runner deadline:" not in out
        assert written["stopped"] is None


# --- the grading budget, per panel -----------------------------------------------------


class _PacedGrader(_Scripted):
    """``_Scripted`` (score 4); each blind CASE call (never a probe call) moves the
    clock on by ``step`` seconds."""

    def __init__(self, clock: _Clock, step: float) -> None:
        def _rule(_system: str, user: str, mode: str) -> int:
            if PROBE_QUESTION not in user and mode == "blind":
                clock.now += step
            return 4

        super().__init__(_rule)

    def case_calls(self, mode: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if not c["probe"] and c["mode"] == mode]


#: D 600, G 300, grader timeout 60, panel 1: each panel is 2 calls = 120 s.
BUDGET_SETTINGS: dict[str, Any] = {"deadline": 600, "budget": 300, "judge_timeout": 60.0}
ONE = ["claude_api_lit_ratelimit"]


@pytest.mark.parametrize(
    ("blind_seconds_per_call", "passage_graded"),
    [(55.0, True), (70.0, False)],
    ids=["140-left-graded", "110-left-over-budget"],
)
def test_the_passage_panel_is_checked_against_what_is_left_after_the_blind_panel(
    monkeypatch: pytest.MonkeyPatch, blind_seconds_per_call: float, passage_graded: bool
) -> None:
    # RED WHEN: the budget is not re-checked before the passage panel.
    clock = _patch_clock(monkeypatch)
    (case_id,) = ONE
    # The answer ends 650 s in: 250 s left of D + G = 900 s.
    main = _Paced(clock, {_q(case_id): 650.0})
    grader = _PacedGrader(clock, blind_seconds_per_call)
    summary, _built = _run(
        monkeypatch,
        _settings(passages=True, **BUDGET_SETTINGS),
        ONE,
        main=main,
        grader=grader,
    )
    (case,) = summary["judge"]["cases"]
    assert case["score"] == 4 and len(grader.case_calls("blind")) == 2  # blind panel graded
    if passage_graded:
        # 250 - 2 x 55 = 140 s left >= 120 s: graded.
        assert case["passage_error"] is None
        assert case["passage_score"] == 4
        assert len(grader.case_calls("passages")) == 2
        assert summary["stopped"] is None
    else:
        # 250 - 2 x 70 = 110 s left < 120 s: no passage call at all.
        assert case["passage_error"] == PASSAGE_OVER_BUDGET
        assert grader.case_calls("passages") == []
        assert case["passage_grader_calls"] == []
        assert case["harness_error"] is None
        assert summary["stopped"] is not None
        assert summary["stopped"]["over_budget"] == [case_id]
        assert runner_mod.gate_failures(summary)[0].startswith("runner deadline:")


@pytest.mark.parametrize(
    ("answer_ends_at", "graded"),
    [(780.0, True), (790.0, False)],
    ids=["120-left-graded", "110-left-over-budget"],
)
def test_the_blind_panel_is_not_started_over_budget(
    monkeypatch: pytest.MonkeyPatch, answer_ends_at: float, graded: bool
) -> None:
    # RED WHEN: the blind-panel budget check is removed, moved after the call, or uses >.
    clock = _patch_clock(monkeypatch)
    (case_id,) = ONE
    grader = _PacedGrader(clock, 0.0)
    summary, _built = _run(
        monkeypatch,
        _settings(**BUDGET_SETTINGS),
        ONE,
        main=_Paced(clock, {_q(case_id): answer_ends_at}),
        grader=grader,
    )
    (case,) = summary["judge"]["cases"]
    assert case["answer"]  # the case was answered either way
    if graded:
        # Partner: 120 s left is exactly one panel; it is graded.
        assert case["score"] == 4 and len(grader.case_calls("blind")) == 2
        assert summary["stopped"] is None
    else:
        assert OVER_BUDGET in (case["error"] or ""), case["error"]
        assert grader.calls == []
        assert case["score"] is None
        assert case["harness_error"] is None
        assert summary["stopped"]["over_budget"] == [case_id]
        assert runner_mod.gate_failures(summary)[0].startswith("runner deadline:")


# --- the start-up refusal: the budget must hold the largest single panel -----------------


class _Reached(Exception):
    """Raised by the retrieval half: the run got past every start-up refusal."""


@pytest.mark.parametrize(
    ("panel", "passages", "budget", "refused"),
    [
        ("1", False, 119, True),
        ("1", False, 120, False),
        ("1", True, 119, True),
        ("1", True, 120, False),  # not the sum of both panels (240)
        ("3", False, 239, True),
        ("3", False, 240, False),
        ("3", True, 239, True),
        ("3", True, 240, False),  # not the sum of both panels (480)
    ],
)
def test_a_budget_below_the_largest_panel_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, panel: str, passages: bool, budget: int, refused: bool
) -> None:
    # RED WHEN: the start-up refusal is missing, late, or sums both panels.
    _patch_clock(monkeypatch)
    built = _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", panel)
    retrievals: list[Any] = []

    async def _retrieval(*args: Any, **kwargs: Any) -> Any:
        retrievals.append(args)
        raise _Reached

    monkeypatch.setattr(runner_mod, "evaluate_retrieval", _retrieval)
    settings = _settings(deadline=600, budget=budget, judge_timeout=60.0, passages=passages)
    if refused:
        with pytest.raises(SystemExit, match="CITEVYN_EVAL_GRADING_BUDGET_SECONDS") as info:
            asyncio.run(runner_mod.run_eval_async(ids=TWO, settings=settings))
        assert "Nothing was run" in str(info.value)
        assert retrievals == []
        assert built["main"] == [] and built["fallback"] == []
    else:
        # Partner: a budget that holds the largest panel starts the run.
        with pytest.raises(_Reached):
            asyncio.run(runner_mod.run_eval_async(ids=TWO, settings=settings))
        assert len(retrievals) == 1
