"""The shipped-corpus judged eval after its second review round (#598 Step 4), on
Postgres (``postgres`` job).

A judged run on the shipped corpus whose judge session does not hold exactly the
shipped passages (the size of the shipped checksum map) is a run-level harness error
that names both counts, and no answer is paid for. No provider is called (the fakes of
``test_eval_step4_pg.py``).

Each test names the change that turns it red.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import pytest
from sqlalchemy import delete

from app.core.config import get_settings
from app.models import Chunk
from tests.eval import runner as runner_mod
from tests.eval import shipped as shipped_mod
from tests.test_eval_report_integrity import _clients
from tests.test_eval_shipped_pg import (  # noqa: F401 - pg_schema and settings are fixtures
    pg_schema,
    settings,
)
from tests.test_eval_step4_pg import _run_settings, _wire
from tests.test_eval_step4_round1_pg import _altered_session, _chunk_ids, _other_key

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("CITEVYN_PG_TEST_URL") is None and get_settings().pg_test_url is None,
        reason="Set CITEVYN_PG_TEST_URL to a postgresql+psycopg:// URL.",
    ),
]


def _run(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> tuple[int, dict[str, Any], list[Any]]:
    _wire(monkeypatch)
    _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    pg = _run_settings(settings)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: pg)
    report = tmp_path / "r.json"
    code = runner_mod.main(
        ["--postgres", "--ids", "claude_api_lit_ratelimit", "--report", str(report), "--quiet"]
    )
    summary = json.loads(report.read_text(encoding="utf-8"))
    return code, summary, runner_mod.gate_failures(summary)


def test_a_judge_session_missing_a_shipped_passage_is_a_harness_error_naming_the_count(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Red when: a shipped judged run whose index holds fewer chunk rows than the
    shipped checksum map has passages is graded as if nothing were wrong, the error
    does not name both counts, or answers are still paid for on the wrong index."""
    shipped = len(shipped_mod.shipped_heading_keys())
    assert shipped == 44  # partner: the shipped map really has the shipped passages
    source, heading = _other_key().split("#", 1)

    async def _drop_one(session: Any) -> None:
        await session.execute(delete(Chunk).where(Chunk.chunk_id == _chunk_ids(source, heading)))

    _altered_session(monkeypatch, _drop_one)
    code, summary, failures = _run(settings, monkeypatch, tmp_path)
    assert summary["index_chunks"] == shipped - 1  # partner: the session lost one
    index = [h for h in summary["harness_errors"] if h["stage"] == "index"]
    assert len(index) == 1, summary["harness_errors"]
    assert index[0]["type"] == "IndexSizeMismatch"
    assert f"{shipped - 1} chunk rows" in index[0]["message"], index[0]
    assert f"{shipped} passages" in index[0]["message"], index[0]
    # No answer was paid for on the wrong index, in either pass.
    assert summary["judge"]["cases"] == []
    assert summary["passes_completed"] == []
    assert any("harness integrity" in f and "IndexSizeMismatch" in f for f in failures), failures
    assert code != 0


def test_the_untouched_shipped_index_raises_no_index_error(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Partner. Red when: the size check fires on the real shipped ingest (every
    judged run would fail)."""
    _code, summary, _failures = _run(settings, monkeypatch, tmp_path)
    assert summary["index_chunks"] == len(shipped_mod.shipped_heading_keys())
    assert summary["passes_completed"] == ["main", "fallback"]  # partner: both passes ran
    assert [h for h in summary["harness_errors"] if h["stage"] == "index"] == []
