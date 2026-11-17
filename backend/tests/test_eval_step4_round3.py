"""The judged eval after the third review round of #598 Step 4: the report and the
spend export say whether the run was capped and which ceiling was in force.

The report's top-level keys are pinned exactly (``TODAY_TOP`` in
``test_grader_check_runner.py``), so the record sits in the existing ``grader``
section, beside the grader's timeout (the limits the run ran under):
``grader.spend_cap == {"capped_dispatch": bool, "ceiling_usd": float | None}``. The
spend export copies it from the report as ``spend_cap`` (``None`` without a report);
its own step does not see the eval step's settings. Free and hermetic.

Each test names the change that turns it red.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.eval import grader_check as gc
from tests.eval import runner as runner_mod
from tests.eval import spend_export
from tests.test_eval_report_integrity import _clients
from tests.test_grader_check_runner import _run_settings_switch


def _report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, *, capped: bool, ceiling: float
) -> dict[str, Any]:
    _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    settings = _run_settings_switch(False, cost_hard_daily_usd=ceiling)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(gc, "utc_now", lambda: datetime(2026, 11, 17, 12, 0, tzinfo=UTC))
    if capped:
        monkeypatch.setenv("CITEVYN_EVAL_CAPPED_DISPATCH", "true")
    else:
        monkeypatch.delenv("CITEVYN_EVAL_CAPPED_DISPATCH", raising=False)
    report = tmp_path / "r.json"
    code = runner_mod.main(
        ["--ids", "claude_api_lit_ratelimit", "--report", str(report), "--quiet"]
    )
    assert code is not None
    return json.loads(report.read_text(encoding="utf-8"))


def test_a_capped_run_records_its_cap_and_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: a capped baseline run's report does not say it was capped, or does
    not carry the ceiling the run's settings were held to."""
    summary = _report(monkeypatch, tmp_path, capped=True, ceiling=0.14)
    assert summary["judge"]["judged"] == 1  # partner: the run really judged
    assert summary["grader"]["spend_cap"] == {"capped_dispatch": True, "ceiling_usd": 0.14}


def test_an_uncapped_run_records_no_cap_and_its_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: a run without the capped mark is recorded as capped, or its ceiling
    (the daily guard in force) is dropped."""
    summary = _report(monkeypatch, tmp_path, capped=False, ceiling=10.0)
    assert summary["judge"]["judged"] == 1
    assert summary["grader"]["spend_cap"] == {"capped_dispatch": False, "ceiling_usd": 10.0}


def test_a_disabled_budget_records_no_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: with the budget guard off, a ceiling no call is held to is recorded."""
    _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    settings = _run_settings_switch(False, cost_budget_enabled=False)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)
    monkeypatch.delenv("CITEVYN_EVAL_CAPPED_DISPATCH", raising=False)
    report = tmp_path / "r.json"
    runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--report", str(report), "--quiet"])
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert summary["grader"]["spend_cap"] == {"capped_dispatch": False, "ceiling_usd": None}


@pytest.mark.parametrize(
    "cap",
    [
        {"capped_dispatch": True, "ceiling_usd": 0.14},
        {"capped_dispatch": False, "ceiling_usd": 10.0},
    ],
    ids=["capped", "uncapped"],
)
def test_the_spend_export_carries_the_reports_cap(cap: dict[str, Any]) -> None:
    """Red when: the spend export drops the run's cap record or states another one."""
    report = {"grader": {"model": "m", "spend_cap": cap}, "judge": {"cases": []}}
    export = spend_export.summarize([], report)
    assert export["spend_cap"] == cap


def test_the_spend_export_without_a_report_states_no_cap() -> None:
    """Red when: the export invents a cap record it could not read."""
    assert spend_export.summarize([])["spend_cap"] is None
    assert spend_export.summarize([], {"judge": {"cases": []}})["spend_cap"] is None
