"""The shipped-corpus judged eval after its first review round (#598 Step 4), on
Postgres (``postgres`` job).

``index_chunks`` is the count of chunk rows the judge session holds after ingest,
not a number stated from the shipped map; and every evidence passage of a judged
case must carry a content checksum of the shipped map, else the case has a harness
error naming it. No provider is called (the fakes of ``test_eval_step4_pg.py``).

Each test names the change that turns it red.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, select, update

from app.core.config import get_settings
from app.models import Chunk, Document
from tests.eval import runner as runner_mod
from tests.eval import shipped as shipped_mod
from tests.test_eval_report_integrity import _clients
from tests.test_eval_shipped_pg import (  # noqa: F401 - pg_schema and settings are fixtures
    pg_schema,
    settings,
)
from tests.test_eval_step4_pg import (
    RATE_KEY,
    SHIPPED_PASSAGES,
    _key,
    _run_settings,
    _wire,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("CITEVYN_PG_TEST_URL") is None and get_settings().pg_test_url is None,
        reason="Set CITEVYN_PG_TEST_URL to a postgresql+psycopg:// URL.",
    ),
]

BAD_CHECKSUM = "0" * 64


def _chunk_ids(source: str, heading: str) -> Any:
    return (
        select(Chunk.chunk_id)
        .join(Document, Chunk.document_id == Document.document_id)
        .where(Document.source_name == source, Chunk.heading == heading)
        .scalar_subquery()
    )


def _altered_session(monkeypatch: pytest.MonkeyPatch, change: Any) -> None:
    """Wrap the judge session: ``change(session)`` runs after the shipped ingest."""
    real = runner_mod.shipped_judge_session

    @contextlib.asynccontextmanager
    async def _wrapped(settings: Any) -> AsyncIterator[Any]:  # noqa: F811
        async with real(settings) as session:
            await change(session)
            yield session

    monkeypatch.setattr(runner_mod, "shipped_judge_session", _wrapped)


def _other_key() -> str:
    return next(k for k in shipped_mod.shipped_heading_keys() if k != RATE_KEY)


def test_index_chunks_is_the_count_of_chunk_rows_the_judge_session_holds(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Red when: ``index_chunks`` (and the printed corpus line) states the shipped
    map's size instead of counting the chunk rows of the session the answers ran on,
    so an ingest that lost a passage still reads as 44. (Since round 2 such a run
    also stops before its first case: ``test_eval_step4_round2_pg.py``.)"""
    source, heading = _other_key().split("#", 1)

    async def _drop_one(session: Any) -> None:
        await session.execute(delete(Chunk).where(Chunk.chunk_id == _chunk_ids(source, heading)))

    _altered_session(monkeypatch, _drop_one)
    _wire(monkeypatch)
    _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    pg = _run_settings(settings)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: pg)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    report = tmp_path / "r.json"
    runner_mod.main(["--postgres", "--ids", "claude_api_lit_ratelimit", "--report", str(report)])
    out = capsys.readouterr().out
    summary = json.loads(report.read_text(encoding="utf-8"))
    # Partner: the judge session was opened and counted (and refused the short index).
    assert [h["stage"] for h in summary["harness_errors"]] == ["index"]
    assert summary["corpus"]["answers"] == "shipped"
    assert summary["index_chunks"] == SHIPPED_PASSAGES - 1
    assert "Judged answers: shipped corpus (43 passages)" in out, out


def test_the_unaltered_run_counts_44_and_prints_the_shipped_corpus_line(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Partner of the count. Red when: the printed corpus line is missing on the real
    shipped path, or the measured count of an untouched ingest is not 44."""
    _wire(monkeypatch)
    _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    pg = _run_settings(settings)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: pg)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    report = tmp_path / "r.json"
    runner_mod.main(["--postgres", "--ids", "claude_api_lit_ratelimit", "--report", str(report)])
    out = capsys.readouterr().out
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert summary["index_chunks"] == SHIPPED_PASSAGES
    assert (
        "Judged answers: shipped corpus (44 passages); the retrieval lines above are on "
        "the fixture corpus. Not comparable with judged runs before #598 Step 4."
    ) in out.splitlines(), out


def _main_run(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> dict[str, Any]:
    _wire(monkeypatch)
    _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    pg = _run_settings(settings)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: pg)
    report = tmp_path / "r.json"
    runner_mod.main(["--postgres", "--ids", "claude_api_lit_ratelimit", "--report", str(report)])
    return json.loads(report.read_text(encoding="utf-8"))


def test_an_evidence_passage_outside_the_shipped_map_is_a_harness_error(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Red when: a judged answer made from a passage whose content checksum is not in
    the shipped checksum map is graded silently, in either pass, instead of being a
    harness error that names the case and the passage."""
    source, heading = RATE_KEY.split("#", 1)

    async def _corrupt(session: Any) -> None:
        await session.execute(
            update(Chunk)
            .where(Chunk.chunk_id == _chunk_ids(source, heading))
            .values(content_checksum=BAD_CHECKSUM)
        )

    _altered_session(monkeypatch, _corrupt)
    summary = _main_run(settings, monkeypatch, tmp_path)
    assert summary["passes_completed"] == ["main", "fallback"]  # partner: both passes ran
    for section in (summary["judge"], summary["fallback"]["judge"]):
        (case,) = section["cases"]
        bad = [e for e in case["evidence"] if e.get("content_checksum") == BAD_CHECKSUM]
        assert [_key(e) for e in bad] == [RATE_KEY]  # partner: the answer used that passage
        provenance = [h for h in section["harness_errors"] if h["type"] == "ShippedProvenance"]
        assert len(provenance) == 1, section["harness_errors"]
        assert provenance[0]["case_id"] == case["case_id"]
        assert case["case_id"] in provenance[0]["message"]
        assert RATE_KEY in provenance[0]["message"]


def test_shipped_evidence_raises_no_provenance_error(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Partner of the provenance error. Red when: the check flags a passage whose
    checksum is in the shipped map (every judged case would error)."""
    summary = _main_run(settings, monkeypatch, tmp_path)
    for section in (summary["judge"], summary["fallback"]["judge"]):
        (case,) = section["cases"]
        assert case["evidence"]  # partner: there was evidence to check
        assert section["harness_errors"] == []
