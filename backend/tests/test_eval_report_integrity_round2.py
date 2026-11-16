"""#598 Step 2b, review round 2: the builder's tests for the round's fixes.

Free and hermetic, reusing the fakes of ``test_eval_report_integrity.py``. The
Postgres half (a COMMIT or a killed connection inside a case) is in
``test_eval_report_integrity_r3_pg.py``.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import stat
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.answer import orchestrator as orchestrator_mod
from app.core.config import Settings
from app.llm.errors import LLMUnavailable
from tests.eval import runner as runner_mod
from tests.test_eval_report_fields import EXPECT, _fillers, _gate, _golden, _Judge, _Model, _run
from tests.test_eval_report_integrity import (
    _IDS,
    ANSWERED,
    _clients,
    _harness,
    _run_settings,
    _stages,
)

# --- 1. only the failed call's own error (or one raised FROM it) is explained ----------


def _raised_while_handling(cause: BaseException, *, chain: bool) -> BaseException:
    """An error raised inside the handler of ``cause``: ``from cause`` or not."""
    try:
        try:
            raise cause
        except type(cause):
            if chain:
                raise KeyError("raised from the failed call") from cause
            raise KeyError("an unrelated defect in the handler")  # noqa: B904
    except KeyError as exc:
        return exc
    raise AssertionError("unreachable")


@pytest.mark.parametrize(("chain", "explained"), [(True, True), (False, False)])
def test_only_the_cause_chain_explains_an_escaped_error(chain: bool, explained: bool) -> None:
    """Red when: the ``__context__`` chain is followed, so an unrelated error raised
    while a failed answer call's error was being handled passes as that call's own
    failure (R01), or ``raise X from failed`` is no longer explained."""
    cause = LLMUnavailable("answer call failed")
    escaped = _raised_while_handling(cause, chain=chain)
    assert escaped.__context__ is cause  # partner: both were raised inside the handler
    assert runner_mod._explained_by(escaped, [cause]) is explained
    assert runner_mod._explained_by(cause, [cause]) is True


def test_a_defect_in_the_failed_calls_handler_is_a_harness_error_and_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a KeyError raised inside the orchestrator's handler of a failed
    answer call is hidden behind that call's answer error: no harness error, and the
    run passes (the reproduced case: 19 of 20 scored, no gate failure)."""

    class _BrokenWrap(orchestrator_mod.OrchestratorError):
        def __init__(self, *_args: Any) -> None:
            raise KeyError("defect while wrapping the provider error")

    monkeypatch.setattr(orchestrator_mod, "OrchestratorError", _BrokenWrap)

    def down(_prompt: str) -> str:
        raise LLMUnavailable("provider down")

    target = _golden()["claude_api_lit_ratelimit"]
    (judged,) = _run(monkeypatch, [target], _Model(down))
    assert judged.answer_errored is True  # partner: the failed call errors it
    assert [(h["stage"], h["type"]) for h in _harness(judged)] == [("answer", "KeyError")]
    failures = _gate([*_fillers(19), judged], [target])
    assert any("harness" in f and target.id in f for f in failures), failures


def test_the_failed_calls_own_wrapped_error_is_no_harness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner. Red when: the orchestrator's ``OrchestratorError(...) from exc`` for a
    failed answer call is counted as a harness error as well."""

    def down(_prompt: str) -> str:
        raise LLMUnavailable("provider down")

    (judged,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(down))
    assert judged.answer_errored is True and not _harness(judged)


# --- 2. a savepoint that cannot be released stops the pass ----------------------------


def _release_fails_once(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """The first case savepoint's RELEASE raises after ``SELECT 1`` succeeded (a
    fake session: the real release happens, then the error is raised)."""
    inner = runner_mod._judge_session
    state = {"failed": 0}

    @contextlib.asynccontextmanager
    async def _session(settings: Any, *, postgres: bool) -> AsyncIterator[Any]:
        async with inner(settings, postgres=postgres) as session:
            real = session.begin_nested
            depth = {"n": 0}

            @contextlib.asynccontextmanager
            async def _nested() -> AsyncIterator[None]:
                outer = depth["n"] == 0
                depth["n"] += 1
                try:
                    async with real():
                        yield
                finally:
                    depth["n"] -= 1
                if outer and state["failed"] == 0:
                    state["failed"] += 1
                    raise RuntimeError("RELEASE SAVEPOINT failed")

            session.begin_nested = _nested
            yield session

    monkeypatch.setattr(runner_mod, "_judge_session", _session)
    return state


def test_a_failed_release_keeps_the_case_and_stops_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a failed RELEASE (the session may be unusable) lets the pass keep
    paying for answers, or drops the finished case, or leaves no single run-level
    error saying how many cases did not run."""
    state = _release_fails_once(monkeypatch)
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    golden = _golden()
    cases = [golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]]
    model = _Model(ANSWERED)
    run_errors: list[dict[str, Any]] = []
    judged = asyncio.run(
        runner_mod._judge_cases(
            cases,
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            answer_llm=runner_mod.AttributingLLM(model),  # type: ignore[arg-type]
            judge_llm=_Judge(),  # type: ignore[arg-type]
            expect_answerer=EXPECT,
            run_errors=run_errors,
        )
    )
    assert state["failed"] == 1  # partner: the release really failed
    (kept,) = judged
    assert kept.answer == ANSWERED and kept.no_answer is False and kept.score == 4
    assert _stages(kept) == ["savepoint"]
    assert len(model.answer_prompts) == 1  # no further paid call
    assert run_errors == [
        {
            "stage": "session",
            "type": "SessionUnusable",
            "message": f"session unusable after case {cases[0].id}; 1 case(s) not run",
        }
    ]


def test_a_stopped_main_pass_is_not_completed_and_runs_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a main pass stopped by an unusable session is reported as complete,
    or the fallback pass still runs (more paid calls), or the run passes."""
    _release_fails_once(monkeypatch)
    built = _clients(monkeypatch)
    summary = asyncio.run(runner_mod.run_eval_async(ids=_IDS, settings=_run_settings()))
    assert built["fallback"] == []
    assert summary["passes_completed"] == []
    assert [e["stage"] for e in summary["harness_errors"]] == ["session"]
    failures = runner_mod.gate_failures(summary)
    assert any("harness" in f and "session unusable" in f for f in failures), failures


def test_a_report_read_error_does_not_stop_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partner. Red when: any harness error, not only one at the savepoint, stops
    the pass."""
    real = runner_mod._turn_evidence
    calls = {"n": 0}

    async def _first_fails(session: Any, session_id: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("evidence read failed")
        return await real(session, session_id)

    monkeypatch.setattr(runner_mod, "_turn_evidence", _first_fails)
    golden = _golden()
    broken, healthy = _run(
        monkeypatch,
        [golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]],
        _Model(ANSWERED),
    )
    assert _stages(broken) == ["report"] and healthy.score == 4 and not _harness(healthy)


# --- 3. the paid reply survives a crash after the answer call --------------------------


def test_n1_paid_reply_survives_a_final_turn_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the final turn's reply text is copied only after ``ask`` returns, so
    a crash after the paid answer call (the audit write) loses it (R22)."""

    async def _boom(self: Any, **_: Any) -> Any:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(orchestrator_mod.Orchestrator, "_persist_audit", _boom)
    model = _Model(ANSWERED)
    (case,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], model)
    assert len(model.answer_prompts) == 1
    assert case.raw_reply == ANSWERED
    assert case.no_answer is None and _stages(case) == ["answer"]


# --- 4. a compact date is stripped too; -latest and -preview are not -----------------


@pytest.mark.parametrize(
    ("served", "model", "same"),
    [
        ("anthropic/claude-3-5-sonnet-20241022", "anthropic/claude-3-5-sonnet", True),
        ("claude-3-5-sonnet", "anthropic/claude-3-5-sonnet-20241022", True),
        ("openai/gpt-4o-mini-20240718", "openai/gpt-4o-mini-2024-07-18", True),
        ("claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20250101", True),
        ("google/gemini-3.6-flash-latest", "gemini-3.6-flash", False),
        ("google/gemini-3.6-flash-preview-05-20", "gemini-3.6-flash", False),
        ("gemini-3.6-flash-1234567", "gemini-3.6-flash", False),
    ],
)
def test_a_compact_date_is_stripped_both_ways(served: str, model: str, same: bool) -> None:
    """Red when: a compact ``-YYYYMMDD`` date is kept on either side (a self-graded
    Anthropic id passes), or ``-latest``/``-preview`` (which can name a different
    model) or a digit run that is not a date is stripped."""
    assert runner_mod._same_model(served, model) is same
    assert runner_mod._same_model(model, served) is same


# --- advisories: the report file ------------------------------------------------------


def test_the_report_mode_follows_the_umask(tmp_path: pathlib.Path) -> None:
    """Red when: the report is forced to 0644 although the process umask asks for a
    narrower mode (the writer before the atomic write honoured it)."""
    old = os.umask(0o077)
    try:
        report = tmp_path / "r.json"
        runner_mod.write_report(report, {"a": 1}, _run_settings())
        mode = stat.S_IMODE(os.stat(report).st_mode)
    finally:
        os.umask(old)
    assert mode == 0o600


def test_the_stale_temp_clean_up_treats_the_report_name_literally(
    tmp_path: pathlib.Path,
) -> None:
    """Red when: glob characters in the report's name widen the clean-up to another
    report's temp files."""
    other = tmp_path / ".r1.json.abc.tmp"
    other.write_text("x", encoding="utf-8")
    os.utime(other, (1, 1))
    own = tmp_path / ".r[12].json.abc.tmp"
    own.write_text("x", encoding="utf-8")
    os.utime(own, (1, 1))
    runner_mod.write_report(tmp_path / "r[12].json", {"a": 1}, _run_settings(), stale_before=10)
    assert other.exists()
    assert not own.exists()  # partner: this report's own stale temp file is removed
