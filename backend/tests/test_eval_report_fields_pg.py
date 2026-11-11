"""#598 Step 2: the gate fields and the evidence fill on real pgvector (``postgres`` job).

The global confidence gate only runs on Postgres (on SQLite the vector arm returns
before it), so this is the one place the report's ``gate_top_score``,
``gate_margin`` and ``gate_passed`` can be shown to fill. It runs the REAL
``_judge_cases`` over the shipped corpus with the synthetic vectors of
``test_eval_shipped_pg.py``: no provider is called, and the answer and grader
clients are fakes. Written before the implementation (test designer).

Each test names the change that turns it red.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.answer import orchestrator as orchestrator_mod
from app.core.config import get_settings
from tests.eval import runner as runner_mod
from tests.test_eval_report_fields import (
    EXPECT,
    _golden,
    _Judge,
    _Model,
    _prompt_evidence,
    _stored_evidence,
)
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


async def test_the_gate_fields_and_the_evidence_fill_on_postgres(
    settings: Any,  # noqa: F811 - the fixture imported above
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the probe does not reach the global gate on the real vector arm
    (gate fields stay empty where they must fill), the gate's verdict or margin is
    not recorded, or the evidence stored for an answered and a declined case is
    not what the answer model was given (read by the turn's USER message)."""
    embedder = _embedder(tmp_path)
    shipped = _shipped()
    # The orchestrator's default retriever embeds with the recorded vectors and
    # stamps with their identity, as the judged run does with the real embedder.
    monkeypatch.setattr(orchestrator_mod, "get_embedder", lambda _s: embedder)
    monkeypatch.setattr(
        orchestrator_mod, "configured_embedder_identity", lambda _s: embedder.query_identity
    )

    @contextlib.asynccontextmanager
    async def _shipped_session(_settings: Any, *, postgres: bool) -> AsyncIterator[Any]:
        assert postgres
        async with shipped.shipped_corpus_session(settings, embedder, embedder.query_identity) as (
            session,
            _,
        ):
            yield session

    monkeypatch.setattr(runner_mod, "_judge_session", _shipped_session)
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    base = _golden()["claude_api_lit_ratelimit"]
    plain = {"history": (), "expected_facts": (), "must_not_contain": (), "postgres_only": True}
    global_case = dataclasses.replace(base, id="pg_global", question=GLOBAL_QUESTION, **plain)
    scoped_case = dataclasses.replace(base, id="pg_scoped", question=SCOPED_QUESTION, **plain)
    model = _Model(lambda prompt: "An answer [1]." if GLOBAL_QUESTION in prompt else "No marker.")

    answered, declined = await runner_mod._judge_cases(
        [global_case, scoped_case],
        settings=settings,
        postgres=True,
        answer_llm=runner_mod.AttributingLLM(model),  # type: ignore[arg-type]
        judge_llm=_Judge(),  # type: ignore[arg-type]
        expect_answerer=EXPECT,
    )

    assert len(model.answer_prompts) == 2  # partner: both cases reached the model
    # The global case: the gate ran, passed, and its numbers are in the report.
    assert answered.outcome == "answer"  # type: ignore[attr-defined]
    assert answered.retrieval_global is True  # type: ignore[attr-defined]
    assert answered.gate_passed is True  # type: ignore[attr-defined]
    top, margin = answered.gate_top_score, answered.gate_margin  # type: ignore[attr-defined]
    assert isinstance(top, float) and isinstance(margin, float)
    assert top == pytest.approx(1.0, abs=1e-3)  # the question has its chunk's own vector
    assert 0.0 < margin <= top
    # The scoped case: no gate ran, so no gate fields; declined for its uncited reply.
    assert declined.decline_reason == "uncited_answer"  # type: ignore[attr-defined]
    assert declined.retrieval_global is False  # type: ignore[attr-defined]
    assert declined.gate_top_score is None  # type: ignore[attr-defined]
    assert declined.gate_passed is None  # type: ignore[attr-defined]
    # Both kept the evidence the model was given, in its order.
    assert _stored_evidence(answered) == _prompt_evidence(model.answer_prompts[0])
    assert _stored_evidence(declined) == _prompt_evidence(model.answer_prompts[1])
