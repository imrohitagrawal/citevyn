"""#598 Step 2b on real Postgres (``postgres`` job): one case's DB error never poisons the pass.

On Postgres a failed statement aborts the whole transaction: every later statement
fails with ``InFailedSqlTransaction`` until a rollback. The judged run drives every
case on ONE session over the seeded catalog, so without a per-case savepoint
(``session.begin_nested()``) one case's DB error loses every case after it; and a
plain ``session.rollback()`` would wipe the seeded catalog instead. SQLite does not
show this, which is why these run here. Written before the implementation (test
designer); the contract is in ``test_eval_report_integrity.py``.

The REAL ``_judge_cases`` runs over the shipped corpus with the recorded vectors of
``test_eval_shipped_pg.py``; the answer and grader clients are fakes.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import pathlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

from app.answer import orchestrator as orchestrator_mod
from app.core.config import get_settings
from app.cost.call_site import CallSite, get_call_site
from app.llm.types import LLMResult
from tests.eval import judge as judge_mod
from tests.eval import runner as runner_mod
from tests.test_eval_report_fields import EXPECT, _golden, _Judge, _Model
from tests.test_eval_shipped_pg import (  # noqa: F401 - pg_schema and settings are fixtures
    GLOBAL_QUESTION,
    SCOPED_QUESTION,
    _embedder,
    _shipped,
    pg_schema,
    settings,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("CITEVYN_PG_TEST_URL") is None and get_settings().pg_test_url is None,
        reason="Set CITEVYN_PG_TEST_URL to a postgresql+psycopg:// URL.",
    ),
]

ANSWERED = "An answer [1]."
JUDGE = "anthropic/claude-haiku-5.5"


def _harness(case: Any) -> list[dict[str, Any]]:
    value = (
        case.get("harness_error")
        if isinstance(case, dict)
        else getattr(case, "harness_error", None)
    )
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _shipped_corpus(monkeypatch: pytest.MonkeyPatch, pg_settings: Any, tmp_path: Any) -> None:
    """Point the judged run at the shipped corpus on this test's own schema."""
    embedder = _embedder(tmp_path)
    shipped = _shipped()
    monkeypatch.setattr(orchestrator_mod, "get_embedder", lambda _s: embedder)
    monkeypatch.setattr(
        orchestrator_mod, "configured_embedder_identity", lambda _s: embedder.query_identity
    )

    @contextlib.asynccontextmanager
    async def _session(_settings: Any, *, postgres: bool) -> AsyncIterator[Any]:
        assert postgres
        async with shipped.shipped_corpus_session(
            pg_settings, embedder, embedder.query_identity
        ) as (
            session,
            _,
        ):
            yield session

    monkeypatch.setattr(runner_mod, "_judge_session", _session)
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda *_a, **_k: _Judge())
    monkeypatch.setattr(judge_mod, "get_llm_client", lambda *_a, **_k: _Judge())


def _cases() -> list[Any]:
    """Two answerable cases on the shipped corpus: the first is the one that fails."""
    base = _golden()["claude_api_lit_ratelimit"]
    plain = {"history": (), "expected_facts": (), "must_not_contain": (), "postgres_only": True}
    return [
        dataclasses.replace(base, id="pg_first", question=SCOPED_QUESTION, **plain),
        dataclasses.replace(base, id="pg_second", question=GLOBAL_QUESTION, **plain),
    ]


async def _judge(pg_settings: Any, cases: list[Any]) -> list[Any]:
    return await runner_mod._judge_cases(
        cases,
        settings=pg_settings,
        postgres=True,
        answer_llm=runner_mod.AttributingLLM(_Model(ANSWERED)),  # type: ignore[arg-type]
        judge_llm=_Judge(),  # type: ignore[arg-type]
        expect_answerer=EXPECT,
    )


_BAD_SQL = "SELECT * FROM table_that_does_not_exist_598"


async def test_a_db_error_inside_ask_does_not_poison_the_later_cases(
    settings: Any,  # noqa: F811 - the fixture imported above
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a DB error raised inside one case's ``ask()`` aborts the pass (it
    is neither ``OrchestratorError`` nor ``LLMUnavailable``), or, caught without a
    savepoint, leaves the transaction failed so every later case errors with
    ``InFailedSqlTransaction`` (A). The failed case records an ``answer`` harness
    error, not an answer error (no answer call failed)."""
    _shipped_corpus(monkeypatch, settings, tmp_path)
    real = orchestrator_mod.Orchestrator._persist_audit
    state = {"n": 0}

    async def _audit_then_fail(self: Any, **kwargs: Any) -> Any:
        state["n"] += 1
        if state["n"] == 1:
            await self._session.execute(text(_BAD_SQL))
        return await real(self, **kwargs)

    monkeypatch.setattr(orchestrator_mod.Orchestrator, "_persist_audit", _audit_then_fail)
    judged = await _judge(settings, _cases())
    assert [j.case_id for j in judged] == ["pg_first", "pg_second"]
    broken, healthy = judged
    assert broken.answer_errored is False
    assert [h["stage"] for h in _harness(broken)] == ["answer"]
    # The case after it ran on a healthy transaction over the intact catalog.
    assert not _harness(healthy) and healthy.error is None
    assert healthy.outcome == "answer" and healthy.evidence
    assert healthy.score == 4


async def test_a_db_error_in_the_report_read_does_not_poison_the_later_cases(
    settings: Any,  # noqa: F811 - the fixture imported above
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a DB error while reading one case's report fields leaves the
    transaction failed, so the next case's ``ask()`` raises
    ``InFailedSqlTransaction`` and the pass is lost (the original #598 Step 2b
    finding 1), or the failed read is not a ``report`` harness error with its
    answer still graded (A, B)."""
    _shipped_corpus(monkeypatch, settings, tmp_path)
    real = runner_mod._final_turn
    state = {"n": 0}

    async def _flaky(session: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        state["n"] += 1
        if state["n"] == 1:
            await session.execute(text(_BAD_SQL))
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(runner_mod, "_final_turn", _flaky)
    broken, healthy = await _judge(settings, _cases())
    assert broken.answer_errored is False
    assert [h["stage"] for h in _harness(broken)] == ["report"]
    assert broken.score == 4  # graded: the answer exists and its writer is known
    assert not _harness(healthy) and healthy.outcome == "answer" and healthy.evidence


async def test_a_healthy_pass_on_postgres_is_graded_as_before(
    settings: Any,  # noqa: F811 - the fixture imported above
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner. Red when: the per-case savepoint changes a healthy case (lost
    rows, a missing audit row, evidence gone) or adds a harness error."""
    _shipped_corpus(monkeypatch, settings, tmp_path)
    judged = await _judge(settings, _cases())
    assert [j.outcome for j in judged] == ["answer", "answer"]
    assert all(j.evidence and j.score == 4 and j.error is None for j in judged)
    assert all(getattr(j, "harness_error", None) in (None, []) for j in judged)


class _InterruptAt(_Model):
    def __init__(self, question: str) -> None:
        super().__init__(ANSWERED)
        self.question = question

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if get_call_site() is CallSite.answer and self.question in user:
            raise KeyboardInterrupt
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


def test_a_postgres_run_cut_off_mid_pass_keeps_its_finished_case(
    settings: Any,  # noqa: F811 - the fixture imported above
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: on the Postgres run (the one Step 3 pays for) the report is written
    only after a whole pass, so a run cut off after its first case keeps nothing
    (E: saved after every case, before any session teardown)."""
    _shipped_corpus(monkeypatch, settings, tmp_path)
    cases = _cases()
    monkeypatch.setattr(runner_mod, "load_cases", lambda _path: cases)

    async def _no_retrieval(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(as_dict=lambda: {})

    monkeypatch.setattr(runner_mod, "evaluate_retrieval", _no_retrieval)
    monkeypatch.setattr(
        runner_mod,
        "build_metered_llm_client",
        lambda s: _Judge() if s.openrouter_model == JUDGE else _InterruptAt(GLOBAL_QUESTION),
    )
    run_settings = settings.model_copy(
        update={
            "llm_provider": "gemini",
            "gemini_api_key": "g-test",
            "gemini_model": EXPECT[1],
            "eval_fallback_model": None,
            "eval_judge_model": JUDGE,
            "eval_judge_timeout_seconds": 60.0,
        }
    )
    report = tmp_path / "eval_report.json"
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            runner_mod.run_eval_async(settings=run_settings, postgres=True, report_path=report)
        )
    assert report.exists(), "no report was written before the run was cut off"
    written = json.loads(report.read_text(encoding="utf-8"))
    assert [c["case_id"] for c in written["judge"]["cases"]] == ["pg_first"]
    assert written["in_progress"] == "main"
