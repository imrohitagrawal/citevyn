"""The spend export of a judged run (#598 Step 3).

Written before the implementation (test designer). The contract, a new
read-only module ``tests/eval/spend_export.py`` (no model call, no write to the
database):

* ``python -m tests.eval.spend_export --out PATH [--report PATH]``, run from
  ``backend/``, reads every ``provider_calls`` row of the database in
  ``CITEVYN_DATABASE_URL`` (the judged job's database is fresh, so its rows are
  this run's) and writes JSON to ``PATH``, creating its folder. Exit 0.
* ``summarize(rows, report=None) -> dict``: the same export from rows already
  read (``ProviderCall`` objects) and the eval report (a dict) or ``None``:

  - ``label``: ``"recorded book cost"``;
  - ``groups``: one entry per (``provider``, ``model``, ``kind``) with ``rows``,
    ``input_tokens``, ``output_tokens``, ``book_cost_usd`` (the rows' recorded
    ``cost_usd`` summed), ``priced_rows``, ``unpriced_rows`` and
    ``tokens_estimated_rows``;
  - ``totals``: ``rows``, ``input_tokens``, ``output_tokens``, ``book_cost_usd``,
    ``unpriced_rows`` and ``tokens_estimated_rows`` over all rows;
  - ``observed_failures``: ``{model: count}`` of the failed calls the eval itself
    recorded in the report (answer ``model_calls`` with ``failed``, and grader
    calls that failed, under the report's grader model), both passes; ``{}``
    without a report;
  - ``gaps``: plain sentences naming what the recorded cost cannot see: failed
    attempts that were never recorded, meter writes that were lost, rows with
    estimated tokens, and unpriced rows (counted as zero).

  Money is a decimal string or number; it is compared here as ``Decimal``.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import pathlib
import subprocess
import sys
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cost.meter import build_call
from app.models import Base
from app.models.provider_calls import ProviderCall

BACKEND = pathlib.Path(__file__).resolve().parents[1]
GEMINI = "gemini-3.6-flash"
GRADER = "anthropic/claude-haiku-5.5"


def _export_module() -> Any:
    return importlib.import_module("tests.eval.spend_export")


def _rows() -> list[ProviderCall]:
    """Two priced Gemini answer rows, one with estimated tokens, a grader row, an
    embedding row and an unpriced row."""
    return [
        build_call(
            kind="llm", provider="gemini", model=GEMINI, input_tokens=1000, output_tokens=200
        ),
        build_call(
            kind="llm",
            provider="gemini",
            model=GEMINI,
            input_tokens=500,
            output_tokens=1024,
            tokens_estimated=True,
        ),
        build_call(kind="llm", provider="router", model=GRADER, input_tokens=900, output_tokens=60),
        build_call(
            kind="embedding",
            provider="gemini",
            model="gemini-embedding-001",
            input_tokens=40,
            output_tokens=0,
        ),
        build_call(
            kind="llm",
            provider="router",
            model="vendor/unknown-model",
            input_tokens=7,
            output_tokens=3,
        ),
    ]


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def _group(export: dict[str, Any], provider: str, model: str, kind: str) -> dict[str, Any]:
    (group,) = [
        g
        for g in export["groups"]
        if (g["provider"], g["model"], g["kind"]) == (provider, model, kind)
    ]
    return group


def _report() -> dict[str, Any]:
    """An eval report with two failed answer calls and one failed grader call."""

    def case(*, answer_failed: bool, grader_failed: bool) -> dict[str, Any]:
        return {
            "model_calls": [
                {"site": "answer", "provider": "gemini", "model": GEMINI, "failed": answer_failed},
                {"site": "condense", "provider": "gemini", "model": GEMINI, "failed": False},
            ],
            "grader_calls": [
                {
                    "score": None if grader_failed else 4,
                    "rationale": "call failed: LLMUnavailable: timed out"
                    if grader_failed
                    else "ok",
                    "provider": None if grader_failed else "router",
                    "model": None if grader_failed else GRADER,
                }
            ],
        }

    return {
        "models": {"answer": GEMINI, "fallback": "openai/gpt-4o-mini", "judge": GRADER},
        "grader": {"model": GRADER, "timeout_seconds": 60.0},
        "judge": {"cases": [case(answer_failed=True, grader_failed=False)]},
        "fallback": {
            "judge": {
                "cases": [
                    case(answer_failed=True, grader_failed=True),
                    case(answer_failed=False, grader_failed=False),
                ]
            }
        },
    }


def test_rows_are_grouped_by_provider_model_and_kind_with_tokens_and_book_cost() -> None:
    """Red when: rows are not grouped by provider, model AND kind (an embedding row
    would hide inside an LLM model's line), or tokens or recorded cost are lost."""
    rows = _rows()
    export = _export_module().summarize(rows)
    assert export["label"] == "recorded book cost"
    gemini = _group(export, "gemini", GEMINI, "llm")
    assert (gemini["rows"], gemini["input_tokens"], gemini["output_tokens"]) == (2, 1500, 1224)
    assert _money(gemini["book_cost_usd"]) == _money(rows[0].cost_usd + rows[1].cost_usd)
    assert _money(gemini["book_cost_usd"]) > 0  # partner: the price book priced them
    embedding = _group(export, "gemini", "gemini-embedding-001", "embedding")
    assert embedding["rows"] == 1 and embedding["input_tokens"] == 40
    assert len(export["groups"]) == 4


def test_priced_unpriced_and_estimated_rows_are_counted_never_hidden() -> None:
    """Red when: an unpriced row is dropped or silently counted as a priced zero,
    or rows with estimated tokens are not counted (both make the recorded cost
    read lower than the spend)."""
    export = _export_module().summarize(_rows())
    unknown = _group(export, "router", "vendor/unknown-model", "llm")
    assert (unknown["priced_rows"], unknown["unpriced_rows"]) == (0, 1)
    assert (unknown["input_tokens"], unknown["output_tokens"]) == (7, 3)
    gemini = _group(export, "gemini", GEMINI, "llm")
    assert (gemini["priced_rows"], gemini["tokens_estimated_rows"]) == (2, 1)
    totals = export["totals"]
    assert totals["rows"] == 5
    assert (totals["unpriced_rows"], totals["tokens_estimated_rows"]) == (1, 1)
    assert totals["input_tokens"] == 1000 + 500 + 900 + 40 + 7
    assert _money(totals["book_cost_usd"]) == _money(sum(r.cost_usd for r in _rows()))


def test_observed_failures_come_from_the_eval_report_in_both_passes() -> None:
    """Red when: the failed calls the eval recorded (which leave no row) are not
    counted next to the spend, or the fallback pass's are missed."""
    export = _export_module().summarize(_rows(), _report())
    assert export["observed_failures"] == {GEMINI: 2, GRADER: 1}


def test_without_a_report_there_are_no_observed_failures_but_the_gaps_are_listed() -> None:
    """Red when: the export invents failures without a report, or stops naming the
    four things the recorded cost cannot see."""
    export = _export_module().summarize(_rows())
    assert export["observed_failures"] == {}
    gaps = " ".join(export["gaps"]).lower()
    for gap in ("failed attempts", "meter writes", "estimated", "unpriced"):
        assert gap in gaps, export["gaps"]


async def _seed(url: str, rows: list[ProviderCall]) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all(rows)
            await session.commit()
    finally:
        await engine.dispose()


def _command(out: pathlib.Path, report: pathlib.Path | None = None) -> list[str]:
    command = [sys.executable, "-m", "tests.eval.spend_export", "--out", str(out)]
    if report is not None:
        command += ["--report", str(report)]
    return command


def _env(url: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("CITEVYN_")}
    env["CITEVYN_DATABASE_URL"] = url
    return env


@pytest.mark.parametrize("with_report", [False, True], ids=["db-only", "with-report"])
def test_the_command_the_workflow_runs_exports_a_seeded_table(
    tmp_path: pathlib.Path, with_report: bool
) -> None:
    """Red when: ``python -m tests.eval.spend_export --out ...`` (the workflow's
    command, run from ``backend/``) fails, writes nothing, reads another database
    than ``CITEVYN_DATABASE_URL``, or makes the folder of ``--out`` a precondition."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    asyncio.run(_seed(url, _rows()))
    out = tmp_path / "artifacts" / "spend.json"
    report = None
    if with_report:
        report = tmp_path / "eval_report.json"
        report.write_text(json.dumps(_report()), encoding="utf-8")
    done = subprocess.run(
        _command(out, report),
        cwd=BACKEND,
        env=_env(url),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["totals"]["rows"] == 5
    assert written["observed_failures"] == ({GEMINI: 2, GRADER: 1} if with_report else {})


def test_the_command_writes_nothing_to_the_database(tmp_path: pathlib.Path) -> None:
    """Red when: the exporter adds, changes or removes a row (it must only read)."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    asyncio.run(_seed(url, _rows()))
    db = tmp_path / "eval.db"
    before = db.read_bytes()
    done = subprocess.run(
        _command(tmp_path / "spend.json"),
        cwd=BACKEND,
        env=_env(url),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "spend.json").exists()  # partner: it really ran
    assert db.read_bytes() == before


# --- #576 part 1: the primary failures the fallback answered ----------------------------


def _note(cause: dict[str, Any], seconds: float) -> dict[str, Any]:
    return {
        "provider": "gemini",
        "model": GEMINI,
        "error_type": "LLMUnavailable",
        "kind": "http_status",
        "status_code": None,
        "provider_status": None,
        "reason": None,
        "duration_seconds": seconds,
        "thoughts_tokens": None,
        "answer_tokens": None,
        "max_output_tokens": None,
        **cause,
    }


def _report_with_notes() -> dict[str, Any]:
    report = _report()
    report["judge"]["cases"][0]["model_calls"][1]["primary_failures"] = [
        _note({"status_code": 429, "provider_status": "RESOURCE_EXHAUSTED"}, 1.5)
    ]
    report["fallback"]["judge"]["cases"][1]["model_calls"][0]["primary_failures"] = [
        _note({"kind": "empty_answer", "reason": "MAX_TOKENS"}, 0.25)
    ]
    return report


def test_primary_failures_are_counted_per_model_with_their_causes_in_both_passes() -> None:
    """Red when: one pass is skipped, or the ``primary_failures`` field is ignored."""
    export = _export_module().summarize([], _report_with_notes())
    assert export["primary_failures"] == {
        GEMINI: {
            "calls": 2,
            "by_cause": {
                "gemini empty_answer MAX_TOKENS": 1,
                "gemini http_status 429 RESOURCE_EXHAUSTED": 1,
            },
            "seconds": 1.75,
        }
    }


@pytest.mark.parametrize("report", [None, {}, {"judge": {"cases": [{"model_calls": [{}]}]}}])
def test_an_old_report_has_no_primary_failures_and_does_not_crash(report: Any) -> None:
    """Red when: a report written before this field (or none) crashes the export,
    or the key goes missing."""
    export = _export_module().summarize([], report)
    assert export["primary_failures"] == {}


def test_primary_failures_are_never_folded_into_the_observed_failures() -> None:
    """Red when: the new records are double-counted as failed calls."""
    module = _export_module()
    with_notes = module.summarize([], _report_with_notes())
    without = module.summarize([], _report())
    assert with_notes["primary_failures"]  # partner: the notes were seen
    for key in ("observed_failures", "observed_failure_seconds", "refused_by_cap"):
        assert with_notes[key] == without[key], key


def test_the_first_gap_says_hidden_primary_calls_are_now_counted_but_not_costed() -> None:
    """Red when: the gap text still says a primary call the fallback hid is unseen,
    or stops naming the missing tokens (#496)."""
    first = _export_module().GAPS[0]
    assert "primary_failures" in first and "#496" in first
    assert "a primary call the fallback hid, and" not in first
    assert first.lower().startswith("failed attempts")
