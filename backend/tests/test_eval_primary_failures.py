"""#576 part 1: the judged eval records why each primary (Gemini) call failed,
per call, per case, per pass, on the console and in the spend export; never body text.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.cost.call_site import CallSite, get_call_site
from app.llm import gemini as gemini_mod
from app.llm import openrouter as openrouter_mod
from app.llm.attempts import cause_key
from app.llm.errors import LLMUnavailable
from app.llm.fallback import FallbackLLMClient
from app.llm.types import LLMResult
from app.models import Base
from tests.eval import runner as runner_mod
from tests.eval import spend_export
from tests.test_eval_report_fields import ANSWER_MODEL, FALLBACK, _golden, _Judge, _Model, _run
from tests.test_eval_report_round1 import _judge_with

ANSWERED = "The limit is 50 requests per minute [1]."
GRADER = "anthropic/claude-haiku-5.5"
UNAVAILABLE = "gemini http_status 503 UNAVAILABLE"
Fail = Callable[[CallSite, str], LLMUnavailable | None]


def _err(kind: str, **fields: Any) -> LLMUnavailable:
    return LLMUnavailable("Gemini failed", kind=kind, **fields)


def _503() -> LLMUnavailable:
    return _err("http_status", status_code=503, provider_status="UNAVAILABLE")


class _Gemini(_Model):
    """A fake primary named as the Gemini client is; raises what ``fail`` returns."""

    _PROVIDER = "gemini"

    def __init__(self, reply: Any = ANSWERED, fail: Fail = lambda _s, _u: None) -> None:
        super().__init__(reply)
        self._model, self.fail = ANSWER_MODEL, fail

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        exc = self.fail(get_call_site(), user)
        if exc is not None:
            raise exc
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


def _asks(question: str) -> Callable[[str], bool]:
    return lambda user: f"Question: {question}\n" in user


def _judge_chain(
    monkeypatch: pytest.MonkeyPatch, cases: list[Any], primary: _Gemini, secondary: Any = None
) -> list[Any]:
    chain = FallbackLLMClient(
        primary=primary,  # type: ignore[arg-type]
        secondary=secondary or _Model(ANSWERED, provider="router", model=FALLBACK),  # type: ignore[arg-type]
    )
    return _judge_with(monkeypatch, cases, chain)


def _fail_on(question: str) -> _Gemini:
    asks = _asks(question)
    return _Gemini(fail=lambda _s, u: _503() if asks(u) else None)


def _block(cases: list[Any], judged: list[Any], applicable: bool = True) -> dict[str, Any]:
    return runner_mod._summarize(  # pyright: ignore[reportPrivateUsage]
        cases, {}, judged, judge_available=True, judgeable=cases, primary_applicable=applicable
    )["judge"]


def _causes(judged: Any) -> list[tuple[str, str, list[str]]]:
    return [
        (c["turn"], c["site"], [cause_key(n) for n in c["primary_failures"]])
        for c in judged.model_calls
    ]


def test_each_failed_site_is_recorded_on_its_own_call_and_summed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: per-turn slicing drops or misplaces a note, the reason omits the
    cause, or the summary (sites, causes, vague counts, cases, lines) miscounts."""
    golden = _golden()
    followup = golden["followup_claude_api_raise"]
    alias = dataclasses.replace(
        golden["citevyn_lit_free"],
        id="citevyn_two_word_alias",
        question="Is site win free to use right now?",
        expected_facts=(),
    )
    history = _asks(followup.history[0])

    def fail(site: CallSite, user: str) -> LLMUnavailable | None:
        if site is CallSite.answer and history(user):
            return _err("http_status", status_code=429, provider_status="RESOURCE_EXHAUSTED")
        if site is CallSite.condense:
            return LLMUnavailable("untagged")
        if site is CallSite.alias_intent and alias.question in user:
            return _err("empty_answer", reason="MAX TOKENS", thoughts_tokens=70, answer_tokens=0)
        return None

    reply = "Set CLAUDE_API_RATE_LIMIT to raise it [1]."
    primary = _Gemini(lambda p: reply if followup.question in p else ANSWERED, fail=fail)
    judged = _judge_chain(monkeypatch, [followup, alias], primary)
    causes = [c for j in judged for c in _causes(j)]
    quota = "gemini http_status 429 RESOURCE_EXHAUSTED"
    assert ("history", "answer", [quota]) in causes
    assert ("final", "condense", ["gemini unknown"]) in causes
    assert ("final", "alias_intent", ["gemini empty_answer UNRECOGNIZED"]) in causes
    assert sum(1 for *_x, c in causes if c) == 3  # every other call: the key, empty
    (note,) = next(c["primary_failures"] for c in judged[1].model_calls if c["primary_failures"])
    assert (note["thoughts_tokens"], note["answer_tokens"]) == (70, 0)
    assert f"answer by router {FALLBACK} (history turn; {quota})" in (judged[0].error or "")

    judge = _block([followup, alias], judged)
    block = judge["primary_failures"]
    assert block["applicable"] is True
    assert block["calls"] == sum(len(j.model_calls) for j in judged) > 3
    assert (block["count"], block["via_cache"]) == (3, 0)
    assert block["by_site"] == {"alias_intent": 1, "answer": 1, "condense": 1}
    assert block["by_cause"] == {
        "gemini empty_answer UNRECOGNIZED": 1,
        quota: 1,
        "gemini unknown": 1,
    }
    assert block["answer_site"] == {"fallback_answers": 1, "explained": 1, "without_record": 0}
    assert block["vague"] == {"unknown_kind": 1, "unrecognized": 1}
    row = {"case_id": followup.id, "turn": "history", "site": "answer", "cause": quota}
    assert row in block["cases"] and len(block["cases"]) == 3
    lines = runner_mod.pass_report_lines(judge)
    assert any(ln.startswith("primary failures: 3 of ") for ln in lines), lines
    assert f"primary failure: {followup.id} (history turn, answer): {quota}" in lines


def test_answers_by_another_model_with_no_cause_are_counted_without_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: ``without_record`` is a constant or counts the wrong population."""
    golden = _golden()
    cases = [golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]]
    judged = _run(monkeypatch, cases, _Model(ANSWERED, provider="router", model=FALLBACK))
    n = sum(1 for j in judged for c in j.model_calls if c["site"] == "answer")
    assert n >= 2  # partner: the population exists
    judge = _block(cases, judged)
    assert judge["primary_failures"]["answer_site"] == {
        "fallback_answers": n,
        "explained": 0,
        "without_record": n,
    }
    assert any(f"without record {n}" in ln for ln in runner_mod.pass_report_lines(judge))


def test_a_failed_retry_is_recorded_on_the_retry_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the #208 retry's failure is lost or lands on the first answer call."""
    from tests.test_eval_report_fields import _exact_first_retriever  # pyright: ignore

    _exact_first_retriever(monkeypatch)
    answers = iter([None, _err("http_status", status_code=500)])
    primary = _Gemini(
        "No markers.", fail=lambda s, _u: next(answers) if s is CallSite.answer else None
    )
    secondary = _Model("Retry grounded [1].", provider="router", model=FALLBACK)
    (judged,) = _judge_chain(monkeypatch, [_golden()["codex_lit_modelflag"]], primary, secondary)
    calls = [c for _t, site, c in _causes(judged) if site == "answer"]
    assert calls == [[], ["gemini http_status 500"]]  # partner: the retry really ran


def test_when_both_providers_fail_the_failed_record_keeps_its_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the failed path drops the note, or the case's answer error changes."""
    case = _golden()["claude_api_lit_ratelimit"]
    primary = _Gemini(fail=lambda s, _u: _503() if s is CallSite.answer else None)
    (judged,) = _judge_chain(
        monkeypatch,
        [case],
        primary,
        _Gemini(fail=lambda *_: LLMUnavailable("OpenRouter returned 503")),
    )
    assert ("final", "answer", [UNAVAILABLE]) in _causes(judged)
    assert (judged.error or "").startswith(
        "answer: answer call failed (final, answer: LLMUnavailable: OpenRouter returned 503)"
    )
    assert "UNAVAILABLE" not in (judged.error or "")


def test_a_cache_replay_of_a_fallback_answer_carries_the_writers_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a cached fallback answer is not traced to its writer's cause, or is
    counted as a new failure."""
    first = _golden()["claude_api_lit_ratelimit"]
    twin = dataclasses.replace(first, id=f"{first.id}_twin")
    judged = _judge_chain(monkeypatch, [first, twin], _fail_on(first.question))
    assert "cache" in (judged[1].error or "") and UNAVAILABLE in (judged[1].error or "")
    block = _block([first, twin], judged)["primary_failures"]
    assert (block["via_cache"], block["count"]) == (1, 1)
    row = {"case_id": twin.id, "turn": "final", "site": "cache", "cause": UNAVAILABLE}
    assert row in block["cases"]


# --- end to end: the real factory and clients over MockTransport ----------------------------

IDS = ["claude_api_lit_ratelimit", "codex_lit_modelflag"]


def _gemini_reply(request: httpx.Request, failures: dict[str, httpx.Response]) -> httpx.Response:
    user = str(json.loads(request.content)["contents"][0]["parts"][0]["text"])
    for question, response in failures.items():
        if f"Question: {question}\n" in user:
            return response
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": ANSWERED}]}}]})


def test_end_to_end_the_causes_reach_every_output_and_no_body_text_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: any link from the real Gemini client to the report, the console or
    the spend export drops a cause, the fallback pass counts one, or body or message
    text reaches a mid-run save, the report, the console or the export."""
    marker = "".join(["leak", "-", "marker", "-", uuid.uuid4().hex])  # built at run time
    key = "g-" + uuid.uuid4().hex
    error = {"status": "INVALID_ARGUMENT", "message": f"{marker} {key}", "details": [marker]}
    empty = {
        "candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
        "usageMetadata": {"thoughtsTokenCount": 70, "candidatesTokenCount": 0},
    }
    golden = _golden()
    rejected = httpx.Response(400, json={"error": error})
    failures = {
        golden[IDS[0]].question: rejected,
        golden[IDS[1]].question: httpx.Response(200, json=empty),
    }
    # Every real client the run builds talks to a mock transport.
    db = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="gemini",
        gemini_api_key=key,
        openrouter_api_key="or-" + uuid.uuid4().hex,
        gemini_model=ANSWER_MODEL,
        openrouter_model=FALLBACK,
        eval_fallback_model=FALLBACK,
        eval_judge_model=GRADER,
        eval_judge_timeout_seconds=60.0,
        cost_budget_enabled=False,
        database_url=db,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "generateContent" in str(request.url):
            return _gemini_reply(request, failures)
        model = json.loads(request.content)["model"]
        text = json.dumps({"score": 5, "rationale": "fake"}) if model == GRADER else ANSWERED
        return httpx.Response(
            200, json={"choices": [{"message": {"content": text}}], "model": model}
        )

    transport = httpx.MockTransport(handler)
    shim = SimpleNamespace(
        AsyncClient=lambda timeout=None: httpx.AsyncClient(transport=transport, timeout=timeout)
    )
    from app.core import config as config_mod
    from app.core import db as db_mod

    async def _tables() -> None:
        engine = create_async_engine(db)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_tables())
    for module in (gemini_mod, openrouter_mod):
        monkeypatch.setattr(module, "httpx", shim)
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    # Only the stub check reads the process-wide client; the run builds its own.
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    for module in (config_mod, runner_mod, spend_export):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    # The meter writes to the test's own database, never the process-wide engine.
    monkeypatch.setattr(
        db_mod, "get_sessionmaker", lambda: async_sessionmaker(create_async_engine(db))
    )
    saved: list[str] = []
    real_write = runner_mod.write_report

    def _capture(path: pathlib.Path, summary: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        saved.append(json.dumps(summary))
        real_write(path, summary, *args, **kwargs)

    monkeypatch.setattr(runner_mod, "write_report", _capture)
    report = tmp_path / "eval_report.json"
    runner_mod.main(["--ids", ",".join(IDS), "--report", str(report)])
    written = json.loads(report.read_text(encoding="utf-8"))
    invalid = "gemini http_status 400 INVALID_ARGUMENT"

    block = written["judge"]["primary_failures"]
    assert block["applicable"] is True and block["calls"] > 0
    assert block["by_cause"][invalid] >= 1
    assert block["by_cause"]["gemini empty_answer MAX_TOKENS"] >= 1
    assert block["answer_site"]["fallback_answers"] >= 2
    assert block["answer_site"]["without_record"] == 0
    notes = [
        n
        for case in written["judge"]["cases"]
        for c in case["model_calls"]
        for n in c["primary_failures"]
        if n["kind"] == "empty_answer"
    ]
    assert notes and notes[0]["thoughts_tokens"] == 70 and notes[0]["max_output_tokens"]
    fallback = written["fallback"]["judge"]
    assert fallback["primary_failures"]["applicable"] is False
    assert fallback["primary_failures"]["calls"] > 0
    assert fallback["primary_failures"]["count"] == 0
    assert all(c["primary_failures"] == [] for k in fallback["cases"] for c in k["model_calls"])

    console = capsys.readouterr().out
    assert f"main pass primary failure: {IDS[0]} (final turn, answer): {invalid}" in console
    assert "fallback pass primary failures: not applicable" in console

    out = tmp_path / "spend.json"
    assert spend_export.main(["--out", str(out), "--report", str(report)]) == 0
    export = out.read_text(encoding="utf-8")
    per_model = json.loads(export)["primary_failures"][ANSWER_MODEL]
    assert (per_model["by_cause"], per_model["calls"]) == (block["by_cause"], block["count"])
    assert f"primary failures of {ANSWER_MODEL}" in capsys.readouterr().out

    # The leak check, with its partners: the marker was in the body, saves were seen.
    assert marker in rejected.text and len(saved) >= 2
    for text in [*saved, report.read_text(encoding="utf-8"), console, export]:
        assert marker not in text
