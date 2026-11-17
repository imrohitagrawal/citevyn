"""#598 Step 4: the judged answer half runs on the shipped corpus (``postgres`` job).

Written before the implementation (test designer). The contract the builder writes to:

* ``runner._judge_session(settings, postgres=True)`` yields a session over the 44
  shipped passages (``shipped.shipped_judge_session``): chunk vectors from the
  committed recording, inside ``shipped_corpus_session``, with the eval users seeded,
  rolled back afterwards. It embeds no document live. The SQLite branch and the
  retrieval half (``postgres_session`` over the fixture) are unchanged.
* Each evidence entry carries ``content_checksum`` (read from the chunk), equal to
  ``shipped.shipped_heading_keys()[source#heading]`` on this path.
* ``run_eval_async(postgres=True)`` runs a preflight BEFORE the retrieval half: the
  chunk recording's identity equals ``configured_embedder_identity(settings)`` and
  passes ``check_production_identity``; ``check_chunk_vectors(recording,
  shipped_heading_keys())`` holds; the catalog is empty. A failure raises
  ``SystemExit`` with one line saying nothing was spent, before any embedder or
  model call.
* The report says ``corpus == {"retrieval": "fixture", "answers": "shipped"}`` and
  ``index_chunks == 44``; the printed retrieval lines say "fixture corpus".

No provider is called: queries embed through a fake that serves recorded chunk
vectors, the answer and grader clients are scripted, and the retrieval half's live
embedder is a deterministic fake. Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import random
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.answer import orchestrator as orchestrator_mod
from app.core.config import get_settings
from app.cost.call_site import CallSite, get_call_site
from app.cost.meter import build_call, record_call
from app.embeddings.factory import EmbedderIdentity
from app.llm.types import LLMResult
from app.models import Chunk, Document, IndexVersion, Message, ProviderCall, User
from tests.conftest import seed_catalog
from tests.eval import retrieval as retrieval_mod
from tests.eval import runner as runner_mod
from tests.eval import shipped as shipped_mod
from tests.eval.paths import RECORDED_CHUNKS_PATH
from tests.eval.recorded import load_chunk_vectors
from tests.test_eval_report_fields import (
    ANSWER_MODEL,
    EXPECT,
    _golden,
    _Judge,
    _Model,
    _two_pass_settings,
)
from tests.test_eval_report_integrity import _clients
from tests.test_eval_shipped_pg import (  # noqa: F401 - pg_schema and settings are fixtures
    GLOBAL_KEY,
    GLOBAL_QUESTION,
    _url_with_schema,
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

DIM = 1536
SHIPPED_PASSAGES = 44
RATE_KEY = "claude_api#Rate limits"
ANSWERED = "The limit is 50 requests per minute [1]."
IDENTITY = {
    "embedding_provider": "gemini",
    "embedding_model": "gemini-embedding-001",
    "embedding_dim": DIM,
}


def _recorded(key: str) -> list[float]:
    return list(load_chunk_vectors(RECORDED_CHUNKS_PATH).rows[key].vector)


class _QueryEmbedder:
    """The orchestrator's query embedder: every query gets one recorded chunk's vector,
    so that chunk ranks first by cosine. It must never embed a document."""

    def __init__(self, key: str = RATE_KEY) -> None:
        self.vector = _recorded(key)
        self.calls = 0

    @property
    def dim(self) -> int:
        return DIM

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return list(self.vector)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("the query embedder was asked to embed documents")

    async def aclose(self) -> None:
        return None


class _LiveEmbedder:
    """Stands in for the real (paid) embedder ``build_embedder`` would return: stable
    pseudo-random vectors, and a count of every call."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def dim(self) -> int:
        return DIM

    @staticmethod
    def _vector(text: str) -> list[float]:
        rng = random.Random(hashlib.sha256(text.encode("utf-8")).digest())
        return [rng.uniform(-1.0, 1.0) for _ in range(DIM)]

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return self._vector(text)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(t) for t in texts]

    async def aclose(self) -> None:
        return None


def _wire(monkeypatch: pytest.MonkeyPatch, query: _QueryEmbedder | None = None) -> dict[str, Any]:
    """Fake every embedder and the process-wide clients; nothing reaches a provider."""
    live = _LiveEmbedder()
    query = query or _QueryEmbedder()
    monkeypatch.setattr(retrieval_mod, "build_embedder", lambda _s: live)
    monkeypatch.setattr(orchestrator_mod, "get_embedder", lambda _s=None: query)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda *_a, **_k: _Judge())
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    return {"live": live, "query": query}


def _pg(settings: Any) -> Any:  # noqa: F811 - the fixture imported above
    return settings.model_copy(update={**IDENTITY, "gemini_api_key": "g-test"})


def _judge(cases: list[Any], settings: Any, model: Any) -> Any:  # noqa: F811
    return runner_mod._judge_cases(
        cases,
        settings=settings,
        postgres=True,
        answer_llm=runner_mod.AttributingLLM(model),
        judge_llm=_Judge(),  # type: ignore[arg-type]
        expect_answerer=EXPECT,
    )


def _key(entry: dict[str, Any]) -> str:
    return f"{entry['source']}#{entry['heading']}"


async def _counts(settings: Any) -> dict[str, int]:  # noqa: F811
    """Committed rows only: read over a separate connection."""
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            return {
                model.__tablename__: int(
                    await session.scalar(select(func.count()).select_from(model)) or 0
                )
                for model in (Chunk, IndexVersion, Document, User, Message, ProviderCall)
            }
    finally:
        await engine.dispose()


# --- 1. the judge session holds the shipped passages ------------------------------------------


async def test_the_judge_session_holds_exactly_the_44_shipped_passages(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the judged answer half still seeds the conftest fixture (or any other
    chunk set), or embeds its documents live instead of from the recording."""
    wired = _wire(monkeypatch)
    pg = _pg(settings)
    async with runner_mod._judge_session(pg, postgres=True) as session:
        rows = (
            await session.execute(
                select(Document.source_name, Chunk.heading, Chunk.content_checksum).join(
                    Document, Chunk.document_id == Document.document_id
                )
            )
        ).all()
    found = {f"{source}#{heading}": checksum for source, heading, checksum in rows}
    assert len(rows) == SHIPPED_PASSAGES, sorted(found)
    assert found == shipped_mod.shipped_heading_keys()
    assert wired["live"].calls == 0  # no document was embedded live


async def test_a_judged_answer_is_made_from_a_shipped_passage(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the evidence an answer was made from is not a shipped passage (its
    ``source#heading`` and ``content_checksum`` must match the shipped map), or the
    evidence entry does not carry the checksum."""
    wired = _wire(monkeypatch)
    model = _Model(ANSWERED)
    (judged,) = await _judge([_golden()["claude_api_lit_ratelimit"]], _pg(settings), model)
    assert model.answer_prompts  # partner: the answer model was asked
    assert wired["query"].calls > 0  # partner: the query went through the vector arm
    assert judged.evidence, judged.error
    shipped = shipped_mod.shipped_heading_keys()
    texts = shipped_mod.shipped_chunk_texts()
    first = min(judged.evidence, key=lambda e: e["rank"])
    assert _key(first) == RATE_KEY
    assert first["text"] == texts[RATE_KEY]
    for entry in judged.evidence:
        assert "content_checksum" in entry, entry
        assert entry["content_checksum"] == shipped[_key(entry)], entry


# --- 2. nothing stays behind but the spend, and a second pass runs ----------------------------


class _Metered(_Model):
    """An answer client that writes a spend row the way the real meter does (its own
    committed session), and fails one case's answer call."""

    def __init__(self, settings: Any, fail_question: str) -> None:  # noqa: F811
        super().__init__(ANSWERED)
        self.settings = settings
        self.fail_question = fail_question

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if get_call_site() is CallSite.answer:
            engine = create_async_engine(self.settings.database_url)
            try:
                await record_call(
                    async_sessionmaker(engine, expire_on_commit=False),
                    build_call(
                        kind="llm",
                        provider="gemini",
                        model=ANSWER_MODEL,
                        input_tokens=10,
                        output_tokens=2,
                    ),
                )
            finally:
                await engine.dispose()
            if self.fail_question in user:
                raise KeyError("candidates")
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


async def test_two_passes_leave_only_the_spend_rows(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the shipped seam commits catalog or session rows (a later run would
    find a non-empty catalog), its rollback also erases the spend rows, a failed case's
    savepoint breaks the session for the cases after it, or a second pass collides with
    the first."""
    _wire(monkeypatch)
    pg = _pg(settings)
    golden = _golden()
    failing = golden["claude_api_lit_envvar"]
    cases = [golden["claude_api_lit_ratelimit"], failing, golden["codex_lit_modelflag"]]
    shipped = shipped_mod.shipped_heading_keys()
    for _pass in range(2):
        judged = await _judge(cases, pg, _Metered(pg, failing.question))
        assert [j.case_id for j in judged] == [c.id for c in cases]
        ok_first, broken, ok_last = judged
        assert broken.answer_errored  # partner: the failure happened mid-pass
        for ok in (ok_first, ok_last):
            assert ok.harness_error is None and ok.score == 4, ok.error
            assert ok.evidence
            # Partner: the pass really answered from the shipped seam.
            assert all(e.get("content_checksum") == shipped[_key(e)] for e in ok.evidence)
    counts = await _counts(pg)
    assert counts["provider_calls"] >= 6, counts  # the spend survives the rollback
    assert {k: v for k, v in counts.items() if k != "provider_calls"} == {
        "chunks": 0,
        "index_versions": 0,
        "documents": 0,
        "users": 0,
        "messages": 0,
    }


# --- 3. the vector arm is live on the judged path --------------------------------------------


async def test_a_global_question_reaches_the_live_vector_arm(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: judged answers do not reach vector search over the recorded shipped
    vectors (the gate fields stay empty, or the top score is not the recorded chunk's
    own cosine of 1.0), or the case reports a degraded vector arm."""
    _wire(monkeypatch, _QueryEmbedder(GLOBAL_KEY))
    base = _golden()["claude_api_lit_ratelimit"]
    plain = {"history": (), "expected_facts": (), "must_not_contain": (), "postgres_only": True}
    case = dataclasses.replace(base, id="pg_global", question=GLOBAL_QUESTION, **plain)
    (judged,) = await _judge([case], _pg(settings), _Model("An answer [1]."))
    assert judged.retrieval_global is True  # partner: it took the global path
    top, margin = judged.gate_top_score, judged.gate_margin
    assert isinstance(top, float) and isinstance(margin, float)
    assert top == pytest.approx(1.0, abs=1e-3)
    assert isinstance(judged.gate_passed, bool)
    assert judged.harness_error is None


# --- 4. the preflight refuses before anything is spent ---------------------------------------


class _Reached(Exception):
    """Raised in place of the retrieval half: the preflight let the run through."""


def _run_settings(settings: Any, **overrides: Any) -> Any:  # noqa: F811
    return _two_pass_settings(
        database_url=settings.database_url,
        environment="test",
        **{**IDENTITY, **overrides},
    )


def _preflight(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,  # noqa: F811
    **overrides: Any,
) -> tuple[str | None, dict[str, Any]]:
    """Run ``run_eval_async(postgres=True)`` up to the retrieval half. Returns the
    refusal line (``None`` when the run reached the retrieval half) and the fakes."""
    wired = _wire(monkeypatch)
    wired["built"] = _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")

    async def _retrieval(*_args: Any, **_kwargs: Any) -> Any:
        raise _Reached

    monkeypatch.setattr(runner_mod, "evaluate_retrieval", _retrieval)
    try:
        asyncio.run(
            runner_mod.run_eval_async(
                ids=["claude_api_lit_ratelimit"],
                settings=_run_settings(settings, **overrides),
                postgres=True,
            )
        )
    except SystemExit as exc:
        return str(exc.code if exc.code is not None else ""), wired
    except _Reached:
        return None, wired
    pytest.fail("run_eval_async neither refused nor reached the retrieval half")


def _assert_nothing_spent(line: str | None, wired: dict[str, Any]) -> str:
    assert line is not None, "the retrieval half ran: the preflight did not refuse first"
    assert "\n" not in line.strip(), line
    assert "nothing was spent" in line.lower(), line
    assert wired["live"].calls == 0 and wired["query"].calls == 0
    clients = [c for built in wired["built"].values() for c in built]
    assert all(getattr(c, "calls", 0) == 0 for c in clients)
    assert all(not getattr(c, "answer_prompts", []) for c in clients)
    return line


def test_the_preflight_lets_the_committed_recording_through(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner of the refusals. Red when: the preflight refuses the committed recording
    and the real shipped corpus, e.g. because it passes the passage TEXT to
    ``check_chunk_vectors`` instead of the checksum map (44 changed)."""
    line, wired = _preflight(monkeypatch, settings)
    assert line is None, line
    assert wired["live"].calls == 0  # nothing was embedded before the retrieval half


def test_the_preflight_refuses_a_recording_from_another_configured_embedder(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a recording whose identity differs from
    ``configured_embedder_identity(settings)`` is not refused, or is refused only
    after the retrieval half has spent."""
    line, wired = _preflight(monkeypatch, settings, embedding_model="gemini-embedding-002")
    line = _assert_nothing_spent(line, wired)
    assert "gemini-embedding-002" in line


def test_the_preflight_refuses_a_recording_that_is_not_productions(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: ``check_production_identity`` is not part of the preflight."""
    other = EmbedderIdentity("openrouter", "openai/text-embedding-3-small", DIM)
    monkeypatch.setattr(shipped_mod, "production_embedder_identity", lambda: other)
    monkeypatch.setattr(runner_mod, "production_embedder_identity", lambda: other, raising=False)
    line, wired = _preflight(monkeypatch, settings)
    line = _assert_nothing_spent(line, wired)
    assert "openrouter" in line


def test_the_preflight_refuses_a_changed_chunk_and_names_it(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a shipped chunk whose text no longer matches its recorded checksum is
    not refused before any spend, or the refusal does not name the chunk."""
    real = shipped_mod.shipped_chunk_texts

    def _changed() -> dict[str, str]:
        texts = dict(real())
        texts[RATE_KEY] = texts[RATE_KEY] + " Edited."
        return texts

    monkeypatch.setattr(shipped_mod, "shipped_chunk_texts", _changed)
    monkeypatch.setattr(runner_mod, "shipped_chunk_texts", _changed, raising=False)
    assert (
        shipped_mod.shipped_heading_keys()[RATE_KEY]
        != load_chunk_vectors(RECORDED_CHUNKS_PATH).rows[RATE_KEY].content_checksum
    )  # partner: the checksum really changed
    line, wired = _preflight(monkeypatch, settings)
    line = _assert_nothing_spent(line, wired)
    assert RATE_KEY in line


def test_the_preflight_refuses_a_non_empty_catalog(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a database that already holds a catalog is not refused before the
    retrieval half spends."""

    async def _seed() -> None:
        engine = create_async_engine(settings.database_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
            async with factory() as session:
                await seed_catalog(session, index_version="pre-existing")
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    assert asyncio.run(_counts(settings))["chunks"] > 0  # partner: the catalog holds rows
    line, wired = _preflight(monkeypatch, settings)
    line = _assert_nothing_spent(line, wired)
    assert "catalog" in line.lower()


# --- 5 and 7. the whole run: answers on shipped, retrieval on the fixture ---------------------


def test_a_postgres_run_answers_on_shipped_and_retrieves_on_the_fixture(
    settings: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Red when: the report lacks ``corpus`` / ``index_chunks`` or misstates them, the
    retrieval half moves off the fixture (``postgres_session`` with the fixture's
    ``gold_chunks``) or the judged half stays on it, a judged case reports a degraded
    vector arm, or the printed retrieval lines do not say they ran on the fixture."""
    wired = _wire(monkeypatch)
    _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    pg = _run_settings(settings)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: pg)
    real_session = retrieval_mod.postgres_session
    entered: list[int] = []

    @contextlib.asynccontextmanager
    async def _counted(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        entered.append(1)
        async with real_session(*args, **kwargs) as pair:
            yield pair

    monkeypatch.setattr(retrieval_mod, "postgres_session", _counted)
    monkeypatch.setattr(runner_mod, "postgres_session", _counted, raising=False)
    report = tmp_path / "r.json"
    runner_mod.main(["--postgres", "--ids", "claude_api_lit_ratelimit", "--report", str(report)])
    out = capsys.readouterr().out
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert summary["passes_completed"] == ["main", "fallback"]  # partner: both passes ran
    assert summary.get("corpus") == {"retrieval": "fixture", "answers": "shipped"}
    assert summary.get("index_chunks") == SHIPPED_PASSAGES
    # The retrieval half alone opened the fixture session, and it hit the fixture gold.
    assert entered == [1], entered
    (outcome,) = summary["retrieval"]["outcomes"]
    assert outcome["hit"] is True, outcome
    assert outcome["gold_chunks"] == ["claude_api#0"]  # the fixture's own key form
    assert "claude_api#0" in outcome["retrieved_chunk_keys"], outcome
    shipped = shipped_mod.shipped_heading_keys()
    for section in (summary["judge"], summary["fallback"]["judge"]):
        (case,) = section["cases"]
        assert case["evidence"], case["error"]
        assert all(e["content_checksum"] == shipped[_key(e)] for e in case["evidence"])
        assert section["harness_errors"] == []  # the vector arm did not degrade
    assert wired["query"].calls > 0  # partner: the judged queries were embedded
    retrieval_lines = [ln for ln in out.splitlines() if ln.startswith("Retrieval hit-rate")]
    assert retrieval_lines and all("fixture corpus" in ln for ln in retrieval_lines), out
