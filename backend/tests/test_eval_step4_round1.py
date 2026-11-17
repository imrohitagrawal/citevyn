"""The shipped-corpus judged eval after its first review round (#598 Step 4).

Free and hermetic: the SQLite catalog and scripted clients, reusing the helpers of
``test_grader_check_round1.py``, ``test_grader_check_runner.py`` and
``test_eval_step4_report.py``. This module drives ``run_eval_async`` with the grader
check on, which writes the job summary, so it is in the conftest's
``JOB_SUMMARY_SHIELDED_MODULES``.

Each test names the change that turns it red.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.eval import grader_check as gc
from tests.eval import runner as runner_mod
from tests.eval.latency import grader_latency
from tests.eval.paths import GOLDEN_PATH
from tests.test_eval_report_fields import FALLBACK, _golden, _Model
from tests.test_eval_report_integrity import _clients
from tests.test_eval_step4_report import _judge, _summary
from tests.test_grader_check_round1 import _run
from tests.test_grader_check_runner import _run_settings_switch, _Scripted

ANSWERED = "The limit is 50 requests per minute [1]."
DISPATCH_REF = "refs/heads/eval/598-step4"
TAG_REF = "refs/tags/v0.12.0"


# --- 1. the probe latency kind ----------------------------------------------------------------


def test_the_grader_check_reports_probe_latency_from_the_probes_own_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the main pass's ``grader_latency`` loses its ``probe`` kind, or the
    probe figures are computed from anything but the probe's own grader calls."""
    summary = _run(monkeypatch, _Scripted())
    calls = summary["grader_check"]["probe"]["calls"]
    assert len(calls) > 0  # partner: the probe really graded
    latency = summary["judge"]["grader_latency"]
    assert set(latency) == {"blind", "passages", "probe"}
    assert latency["probe"] == grader_latency(calls)
    assert latency["probe"]["calls"] == len(calls)
    # The probe runs once per run: the fallback pass does not repeat it.
    assert "probe" not in summary["fallback"]["judge"]["grader_latency"]


# --- 3. a capped switch-off dispatch is refused near 00:00 UTC --------------------------------


class _Started(Exception):
    """Raised in place of the eval: ``main`` let the run start."""


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    async def _fake(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        raise _Started

    monkeypatch.setattr(runner_mod, "run_eval_async", _fake)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: _run_settings_switch(False))
    return calls


def _at(monkeypatch: pytest.MonkeyPatch, hour: int, minute: int = 0) -> None:
    moment = datetime(2026, 11, 16, hour, minute, tzinfo=UTC)
    monkeypatch.setattr(gc, "utc_now", lambda: moment, raising=True)


def _main(
    monkeypatch: pytest.MonkeyPatch, *, event: str, ref: str, capped: bool = False
) -> int | None:
    """``main()`` for one run. ``capped``: ci.yml marked it a capped baseline dispatch
    (``CITEVYN_EVAL_CAPPED_DISPATCH=true``, round 2: only that mark holds a run to the
    window, never the event or the ref)."""
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", ref)
    if capped:
        monkeypatch.setenv("CITEVYN_EVAL_CAPPED_DISPATCH", "true")
    else:
        monkeypatch.delenv("CITEVYN_EVAL_CAPPED_DISPATCH", raising=False)
    try:
        return runner_mod.main(["--quiet"])
    except _Started:
        return None


def test_a_capped_switch_off_dispatch_is_refused_too_close_to_utc_midnight(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], started: list[Any]
) -> None:
    """Red when: a capped baseline dispatch (capped by ci.yml's ``CAP_USD`` line) starts with
    less of the UTC day left than its job may run, so the daily cap could reset
    mid-run; or the refusal comes after the eval started, is not one line, or does
    not name the reason and the UTC window."""
    _at(monkeypatch, 23, 0)
    code = _main(monkeypatch, event="workflow_dispatch", ref=DISPATCH_REF, capped=True)
    assert started == []
    assert code == 2
    out = capsys.readouterr().out.strip()
    assert len(out.splitlines()) == 1, out
    assert "nothing was spent" in out
    assert "60 minutes" in out and "00:00 UTC" in out, out
    assert f"between 00:00 and {gc.capped_dispatch_window_end()} UTC" in out, out


def test_a_capped_dispatch_runs_with_enough_of_the_day_left(
    monkeypatch: pytest.MonkeyPatch, started: list[Any]
) -> None:
    """Partner of the refusal. Red when: a capped baseline dispatch is refused at noon."""
    _at(monkeypatch, 12, 0)
    assert _main(monkeypatch, event="workflow_dispatch", ref=DISPATCH_REF, capped=True) is None
    assert len(started) == 1


@pytest.mark.parametrize(
    ("event", "ref"),
    [
        ("workflow_dispatch", DISPATCH_REF),
        ("workflow_dispatch", TAG_REF),
        ("push", "refs/heads/main"),
        ("push", TAG_REF),
    ],
    ids=["dispatch-on-a-branch", "dispatch-on-a-tag", "push-main", "tag-push"],
)
def test_uncapped_runs_are_not_held_to_the_window(
    monkeypatch: pytest.MonkeyPatch, started: list[Any], event: str, ref: str
) -> None:
    """Red when: the window refusal reaches a run ci.yml does not mark capped (a push,
    a tag run, or a dispatch without the ``capped_baseline`` input on a branch or a
    tag, such as a release re-run the gate accepts): release gating would change
    beyond the corpus."""
    _at(monkeypatch, 23, 30)
    assert _main(monkeypatch, event=event, ref=ref) is None
    assert len(started) == 1


def test_the_window_threshold_is_read_against_a_fixed_clock() -> None:
    """Red when: the threshold is off by the minute at its edge (just enough left
    runs; one minute less is refused)."""
    need = gc.MIN_MINUTES_BEFORE_UTC_MIDNIGHT_CAPPED_DISPATCH
    edge = datetime(2026, 11, 17, tzinfo=UTC).timestamp() - need * 60
    at_edge = datetime.fromtimestamp(edge, tz=UTC)
    late = datetime.fromtimestamp(edge + 60, tz=UTC)
    assert gc.capped_dispatch_refusal(capped=True, now=at_edge) is None
    refusal = gc.capped_dispatch_refusal(capped=True, now=late)
    assert refusal is not None and "00:00 UTC" in refusal


# --- 4. the log says which corpus the judged answers ran on -----------------------------------


def test_the_corpus_line_names_the_shipped_corpus_and_its_measured_size() -> None:
    """Red when: the judged block does not say it ran on the shipped corpus, states
    a size other than the report's ``index_chunks``, or drops the comparability
    warning."""
    line = runner_mod.judged_corpus_line(
        {"corpus": {"retrieval": "fixture", "answers": "shipped"}, "index_chunks": 43}
    )
    assert line == (
        "Judged answers: shipped corpus (43 passages); the retrieval lines above are on "
        "the fixture corpus. Not comparable with judged runs before #598 Step 4."
    )


def test_the_corpus_line_on_the_hermetic_run_names_the_fixture() -> None:
    """Red when: a fixture-corpus judged run is printed as a shipped one."""
    line = runner_mod.judged_corpus_line(
        {"corpus": {"retrieval": "fixture", "answers": "fixture"}, "index_chunks": 6}
    )
    assert line.startswith("Judged answers: fixture corpus (6 passages)")
    assert "shipped" not in line


def test_the_judged_block_starts_with_the_corpus_line_and_holds_the_judged_leaks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    """Red when: the corpus line is missing or printed after the judge's mean, or the
    judged refusal-leak line still sits under the "(fixture corpus)" retrieval
    heading instead of the judged block."""
    _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    monkeypatch.setattr(runner_mod, "get_settings", lambda: _run_settings_switch(False))
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    report = tmp_path / "r.json"
    runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--report", str(report)])
    lines = capsys.readouterr().out.splitlines()
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert summary["judge"]["judged"] == 1  # partner: the judged half ran
    corpus = [i for i, ln in enumerate(lines) if ln.startswith("Judged answers:")]
    mean = [i for i, ln in enumerate(lines) if ln.startswith("Judge (panel")]
    leaks = [i for i, ln in enumerate(lines) if "refusal leaks (judged, orchestrator" in ln]
    assert len(corpus) == 1 and len(mean) == 1 and len(leaks) == 1, lines
    assert lines[corpus[0]] == runner_mod.judged_corpus_line(summary)
    assert corpus[0] < mean[0] < leaks[0], lines
    # Every line between the retrieval heading and the corpus line is a retrieval line.
    retrieval = next(i for i, ln in enumerate(lines) if ln.startswith("Retrieval hit-rate"))
    assert all("judged" not in ln for ln in lines[retrieval : corpus[0]]), lines


# --- 6. an errored answer is not a must_say miss ----------------------------------------------


def _raises(_prompt: str) -> str:
    raise KeyError("candidates")


@pytest.mark.parametrize(
    "model",
    [
        # Another model wrote the answer: the text exists, so a matcher would run on it.
        _Model(ANSWERED, provider="router", model=FALLBACK),
        # The answer call failed outright.
        _Model(_raises),
    ],
    ids=["another-model-answered", "answer-call-failed"],
)
def test_an_errored_answer_is_not_observed_not_a_must_say_miss(
    monkeypatch: pytest.MonkeyPatch, model: _Model
) -> None:
    """Red when: a case whose answer errored is reported as missing its must_say
    entries (an outage or a wrong answerer read as a wrong answer) instead of not
    observed."""
    case = dataclasses.replace(
        _golden()["claude_api_lit_ratelimit"], must_say=("server-side|organization limit",)
    )
    (judged,) = _judge(monkeypatch, [case], model)
    assert judged.answer_errored  # partner: the answer really errored
    assert judged.must_say_missing is None
    must_say = _summary([case], [judged])["judge"]["must_say"]
    assert must_say == {"declared": 1, "missing": [], "not_observed": [case.id]}


# --- 7. the low-score line says whether the shipped gold was retrieved ------------------------


def test_the_low_score_line_prints_shipped_gold_in_evidence() -> None:
    """Red when: a low-score line leaves out ``shipped_gold_in_evidence``, so a
    retrieval miss on the shipped corpus reads like a bad answer in the log."""
    cases = [
        {"case_id": "hit", "score": 2, "rationale": "r", "shipped_gold_in_evidence": True},
        {"case_id": "miss", "score": 1, "rationale": "r", "shipped_gold_in_evidence": False},
        {"case_id": "none", "score": 2, "rationale": "r", "shipped_gold_in_evidence": None},
        {"case_id": "fine", "score": 4, "rationale": "r", "shipped_gold_in_evidence": False},
    ]
    lines = [ln for ln in runner_mod.pass_report_lines({"cases": cases}) if "low score" in ln]
    assert len(lines) == 3, lines  # partner: the three low cases are printed
    by_id = {ln.split(": ")[1]: ln for ln in lines}
    assert "shipped_gold_in_evidence=true" in by_id["hit"]
    assert "shipped_gold_in_evidence=false" in by_id["miss"]
    assert "shipped_gold_in_evidence=null" in by_id["none"]
    assert all(ln.endswith("(not gating until #602)") for ln in lines)


def test_the_golden_header_and_two_gists_say_what_they_are_held_to() -> None:
    """Red when: the golden file's header still anchors every case to a 5-chunk seed
    (the judged answers on --postgres come from the shipped passages), or the two
    gists go back to wording a grader cannot check: "that limit" for the limit the
    setting does not lift, and "shipped ways" for the documented install commands."""
    text = GOLDEN_PATH.read_text(encoding="utf-8")
    header = "\n".join(ln for ln in text.splitlines() if ln.startswith("#"))
    assert "5-chunk" not in header
    assert "6-chunk conftest fixture" in header
    assert "44 shipped" in header and "backend/app/worker/sources/" in " ".join(header.split())
    golden = _golden()
    raisecap = golden["claude_api_par_raisecap"].expected_gist
    assert "does not lift the server-side organization limit" in raisecap
    assert "that limit" not in raisecap
    install = golden["claude_code_lit_install"].expected_gist
    assert "the other documented ways" in install and "shipped ways" not in install
