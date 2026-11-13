"""#598 Step 2b, review round 1, on real Postgres (``postgres`` job).

A case whose savepoint cannot be released keeps its finished record, and the case
after it runs on a healthy transaction. Also the cache rows a failed case wrote,
and a database error swallowed inside ``ask``. The REAL ``_judge_cases`` runs over
the shipped corpus with recorded vectors, as in ``test_eval_report_integrity_pg.py``.

Each test names the change that turns it red.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from app.answer import orchestrator as orchestrator_mod
from app.cache import answer_cache as answer_cache_mod
from app.core.config import get_settings
from tests.eval import runner as runner_mod
from tests.test_eval_report_fields import EXPECT, _Judge, _Model
from tests.test_eval_report_integrity_pg import (  # noqa: F401 - pg_schema and settings are fixtures
    _BAD_SQL,
    ANSWERED,
    _cases,
    _harness,
    _judge,
    _shipped_corpus,
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


def _capture_session(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Keep the pass's session, so a fake grader can poison its transaction."""
    inner = runner_mod._judge_session
    captured: dict[str, Any] = {}

    @contextlib.asynccontextmanager
    async def _session(s: Any, *, postgres: bool) -> AsyncIterator[Any]:
        async with inner(s, postgres=postgres) as session:
            captured["session"] = session
            yield session

    monkeypatch.setattr(runner_mod, "_judge_session", _session)
    return captured


class _PoisonsOnce(_Judge):
    """A grader whose first call runs a failing statement on the pass's session and
    swallows the error: the transaction is left aborted after the case's last
    database read, so only the case savepoint's RELEASE meets it."""

    def __init__(self, captured: dict[str, Any]) -> None:
        super().__init__()
        self.captured, self.poisoned = captured, False

    async def complete(self, **kwargs: Any) -> Any:
        if not self.poisoned:
            self.poisoned = True
            with contextlib.suppress(Exception):
                await self.captured["session"].execute(text(_BAD_SQL))
        return await super().complete(**kwargs)


async def test_a_savepoint_that_cannot_be_released_keeps_the_finished_case(
    settings: Any,  # noqa: F811 - the fixture imported above
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a failure releasing the case savepoint rebuilds the case from
    scratch (its answer, ``no_answer`` and score are lost), or is not a harness
    error, or the next case inherits the aborted transaction."""
    _shipped_corpus(monkeypatch, settings, tmp_path)
    captured = _capture_session(monkeypatch)
    judge = _PoisonsOnce(captured)
    broken, healthy = await runner_mod._judge_cases(
        _cases(),
        settings=settings,
        postgres=True,
        answer_llm=runner_mod.AttributingLLM(_Model(ANSWERED)),  # type: ignore[arg-type]
        judge_llm=judge,  # type: ignore[arg-type]
        expect_answerer=EXPECT,
    )
    assert judge.poisoned  # partner: the transaction really was poisoned
    assert broken.answer == ANSWERED and broken.no_answer is False
    assert broken.outcome == "answer" and broken.evidence
    assert broken.score == 4 and broken.answer_errored is False
    assert [h["stage"] for h in _harness(broken)] == ["savepoint"]
    assert not _harness(healthy) and healthy.outcome == "answer" and healthy.evidence
    assert healthy.score == 4


async def test_a_cache_row_written_by_a_failed_case_is_rolled_back(
    settings: Any,  # noqa: F811 - the fixture imported above
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the cache row of a case that failed after writing it survives the
    case rollback, so its twin is served a hit no known turn wrote."""
    _shipped_corpus(monkeypatch, settings, tmp_path)
    first, _second = _cases()
    twin = dataclasses.replace(first, id="pg_first_twin")
    real_put = answer_cache_mod.PostgresAnswerCacheStore.put
    state = {"n": 0}

    async def _put_then_fail(self: Any, *, cache_key: str, value: Any) -> None:
        await real_put(self, cache_key=cache_key, value=value)
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("after the put")

    monkeypatch.setattr(answer_cache_mod.PostgresAnswerCacheStore, "put", _put_then_fail)
    broken, again = await _judge(settings, [first, twin])
    assert [h["stage"] for h in _harness(broken)] == ["answer"]
    assert again.outcome == "answer" and again.score == 4 and not _harness(again)


async def test_a_failed_case_keeps_an_earlier_cases_cache_row(
    settings: Any,  # noqa: F811 - the fixture imported above
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: one case's rollback removes the cache row an earlier healthy case
    wrote, or forgets who wrote it, so a later hit on it is not graded."""
    _shipped_corpus(monkeypatch, settings, tmp_path)
    first, second = _cases()
    twin = dataclasses.replace(first, id="pg_first_twin")
    real = orchestrator_mod.Orchestrator._persist_audit
    state = {"n": 0}

    async def _audit(self: Any, **kwargs: Any) -> Any:
        state["n"] += 1
        if state["n"] == 2:
            await self._session.execute(text(_BAD_SQL))
        return await real(self, **kwargs)

    monkeypatch.setattr(orchestrator_mod.Orchestrator, "_persist_audit", _audit)
    a, b, c = await _judge(settings, [first, second, twin])
    assert a.score == 4 and not _harness(a)
    assert [h["stage"] for h in _harness(b)] == ["answer"]
    assert c.outcome == "cache_hit" and c.cache_origin and c.cache_origin["case_id"] == first.id
    assert c.score == 4 and not _harness(c)
