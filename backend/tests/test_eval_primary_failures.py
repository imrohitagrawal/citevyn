"""#576 part 1: the judged eval records why each primary (Gemini) call failed.

Production's answer client is Gemini with an OpenRouter fallback. When Gemini
fails, OpenRouter answers, and before this change nothing recorded why. Now:

* every ``model_calls`` entry carries ``primary_failures`` (``[]`` when the
  primary did not fail, so a missing key is detectable), each a bounded record:
  provider, model, error type, kind, HTTP status, Gemini's short error code, the
  finish or block reason, the duration and, on an empty answer, the token counts;
* the wrong-answerer reason on the case names the cause;
* ``judge.primary_failures`` sums them per pass, with ``without_record``: the
  answers another model wrote that carry no cause (a recording gap);
* the console prints one line per pass and one per case with a cause.

Never the response body, never message text. Free and hermetic: fake clients,
or the real clients over ``httpx.MockTransport`` with fake keys. Each test names
the change that turns it red.
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
from app.cost.call_site import CallSite, call_site, get_call_site
from app.cost.metered import MeteredLLMClient
from app.llm import gemini as gemini_mod
from app.llm import openrouter as openrouter_mod
from app.llm.errors import LLMUnavailable
from app.llm.factory import build_metered_llm_client
from app.llm.fallback import FallbackLLMClient
from app.llm.gemini import GeminiLLMClient
from app.llm.openrouter import OpenRouterLLMClient
from app.llm.types import LLMResult
from app.models import Base
from tests.eval import runner as runner_mod
from tests.eval import spend_export
from tests.test_eval_report_fields import ANSWER_MODEL, FALLBACK, _golden, _Judge, _Model, _run
from tests.test_eval_report_round1 import _judge_with

ANSWERED = "The limit is 50 requests per minute [1]."
GRADER = "anthropic/claude-haiku-5.5"


def _marker() -> str:
    """A secret-like marker built at run time (never written as a literal)."""
    return "".join(["leak", "-", "marker", "-", uuid.uuid4().hex])


def _err(kind: str, **fields: Any) -> LLMUnavailable:
    return LLMUnavailable("Gemini failed", kind=kind, **fields)


def _503() -> LLMUnavailable:
    return _err("http_status", status_code=503, provider_status="UNAVAILABLE")


class _Gemini(_Model):
    """A fake primary that names itself as the Gemini client does, and raises the
    error ``fail`` returns for a call (``None``: it answers)."""

    _PROVIDER = "gemini"

    def __init__(
        self,
        reply: Any = ANSWERED,
        fail: Callable[[CallSite, str], LLMUnavailable | None] = lambda _s, _u: None,
    ) -> None:
        super().__init__(reply)
        self._model = ANSWER_MODEL
        self.fail = fail

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        exc = self.fail(get_call_site(), user)
        if exc is not None:
            raise exc
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


class _Down(_Model):
    async def complete(self, **_: Any) -> LLMResult:
        raise LLMUnavailable("OpenRouter returned 503")


def _chain(primary: _Gemini, secondary: Any = None) -> FallbackLLMClient:
    return FallbackLLMClient(
        primary=primary,  # type: ignore[arg-type]
        secondary=secondary or _Model(ANSWERED, provider="router", model=FALLBACK),  # type: ignore[arg-type]
    )


def _asks(question: str) -> Callable[[str], bool]:
    return lambda user: f"Question: {question}\n" in user


def _summary(cases: list[Any], judged: list[Any], **kwargs: Any) -> dict[str, Any]:
    return runner_mod._summarize(  # pyright: ignore[reportPrivateUsage]
        cases, {}, judged, judge_available=True, judgeable=cases, **kwargs
    )


def _notes(judged: Any) -> list[tuple[str, str, list[dict[str, Any]]]]:
    return [(c["turn"], c["site"], c["primary_failures"]) for c in judged.model_calls]


# --- 13. the real stack -------------------------------------------------------------------


async def test_the_real_clients_record_a_503_behind_the_router_answer(
    tmp_path: pathlib.Path,
) -> None:
    """Red when: the collector is opened outside ``AttributingLLM`` (the metered
    wrapper would hide the note), or the factory's wiring changes shape."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    gemini = GeminiLLMClient(
        model=ANSWER_MODEL,
        api_key="g-test",
        api_base="https://generativelanguage.googleapis.com",
        timeout_seconds=5.0,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
            )
        ),
    )
    router = OpenRouterLLMClient(
        model=FALLBACK,
        api_key="or-test",
        api_base="https://openrouter.ai/api/v1",
        timeout_seconds=5.0,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": ANSWERED}}], "model": FALLBACK},
                )
            )
        ),
    )
    client = runner_mod.AttributingLLM(
        MeteredLLMClient(
            FallbackLLMClient(primary=gemini, secondary=router),
            sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
            settings=Settings(_env_file=None, cost_budget_enabled=False),  # type: ignore[call-arg]
        )
    )
    try:
        with call_site(CallSite.answer):
            result = await client.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    finally:
        await engine.dispose()
    assert result.provider == "router"
    (record,) = client.records
    assert record.provider == "router"
    (note,) = record.primary_failures
    assert (note.provider, note.model, note.kind, note.status_code, note.provider_status) == (
        "gemini",
        ANSWER_MODEL,
        "http_status",
        503,
        "UNAVAILABLE",
    )
    # The factory builds exactly this shape for the judged run's main pass.
    built = build_metered_llm_client(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            llm_provider="gemini",
            gemini_api_key="g-test",
            openrouter_api_key="or-test",
        )
    )
    assert isinstance(built, MeteredLLMClient)
    assert isinstance(built.inner, FallbackLLMClient)
    assert isinstance(built.inner.primary, GeminiLLMClient)
    assert isinstance(built.inner.secondary, OpenRouterLLMClient)
    await built.aclose()


# --- 14-18. the real _judge_cases ---------------------------------------------------------


def _sites_case() -> tuple[Any, Any]:
    golden = _golden()
    followup = golden["followup_claude_api_raise"]
    alias = dataclasses.replace(
        golden["citevyn_lit_free"],
        id="citevyn_two_word_alias",
        question="Is site win free to use right now?",
        expected_facts=(),
    )
    return followup, alias


def _fail_on_sites(followup: Any, alias: Any) -> Callable[[CallSite, str], LLMUnavailable | None]:
    def fail(site: CallSite, user: str) -> LLMUnavailable | None:
        if site is CallSite.answer and _asks(followup.history[0])(user):
            return _err("http_status", status_code=429, provider_status="RESOURCE_EXHAUSTED")
        if site is CallSite.condense:
            return _err("timeout", reason="ReadTimeout")
        if site is CallSite.alias_intent and alias.question in user:
            return _err("empty_answer", reason="MAX_TOKENS", thoughts_tokens=70, answer_tokens=0)
        return None

    return fail


def test_each_failed_site_is_recorded_on_its_own_call_and_summed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: per-turn slicing drops an entry, a note lands on the wrong call,
    or the summary miscounts."""
    followup, alias = _sites_case()
    primary = _Gemini(
        lambda p: (
            "Set CLAUDE_API_RATE_LIMIT to raise it [1]." if followup.question in p else ANSWERED
        ),
        fail=_fail_on_sites(followup, alias),
    )
    judged = _judge_with(monkeypatch, [followup, alias], _chain(primary))
    by_call = [_notes(j) for j in judged]
    causes = [
        (turn, site, [runner_mod.cause_key(n) for n in notes])
        for calls in by_call
        for turn, site, notes in calls
    ]
    assert ("history", "answer", ["gemini http_status 429 RESOURCE_EXHAUSTED"]) in causes
    assert ("final", "condense", ["gemini timeout ReadTimeout"]) in causes
    assert ("final", "alias_intent", ["gemini empty_answer MAX_TOKENS"]) in causes
    # Every other call carries the key, empty.
    assert sum(1 for _t, _s, c in causes if c) == 3
    assert all(isinstance(n, list) for calls in by_call for _t, _s, n in calls)
    (alias_note,) = next(n for _t, s, n in by_call[1] if s == "alias_intent")
    assert (alias_note["thoughts_tokens"], alias_note["answer_tokens"]) == (70, 0)
    # The wrong-answerer reason names the cause.
    assert (
        f"answer by router {FALLBACK} (history turn; gemini http_status 429 RESOURCE_EXHAUSTED)"
        in (judged[0].error or "")
    )

    block = _summary([followup, alias], judged, primary_applicable=True)["judge"][
        "primary_failures"
    ]
    assert block["applicable"] is True
    assert block["calls"] == sum(len(j.model_calls) for j in judged) > 3
    assert block["count"] == 3
    assert block["by_site"] == {"alias_intent": 1, "answer": 1, "condense": 1}
    assert block["by_cause"] == {
        "gemini empty_answer MAX_TOKENS": 1,
        "gemini http_status 429 RESOURCE_EXHAUSTED": 1,
        "gemini timeout ReadTimeout": 1,
    }
    assert block["answer_site"] == {"fallback_answers": 1, "explained": 1, "without_record": 0}
    assert block["via_cache"] == 0
    assert block["vague"] == {"unknown_kind": 0, "unrecognized": 0}
    assert {
        "case_id": followup.id,
        "turn": "history",
        "site": "answer",
        "cause": "gemini http_status 429 RESOURCE_EXHAUSTED",
    } in block["cases"]
    assert len(block["cases"]) == 3

    lines = runner_mod.pass_report_lines(
        _summary([followup, alias], judged, primary_applicable=True)["judge"]
    )
    assert any(ln.startswith("primary failures: 3 of ") for ln in lines), lines
    assert any(
        ln.startswith(f"primary failure: {followup.id} (history turn, answer): gemini http_status")
        for ln in lines
    ), lines


def test_a_failed_retry_is_recorded_on_the_retry_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the #208 retry's failure is lost or lands on the first answer call."""
    from tests.test_eval_report_fields import _exact_first_retriever, _replies

    _exact_first_retriever(monkeypatch)
    case = _golden()["codex_lit_modelflag"]
    calls = {"n": 0}

    def fail(site: CallSite, _user: str) -> LLMUnavailable | None:
        if site is CallSite.answer:
            calls["n"] += 1
            if calls["n"] == 2:
                return _err("http_status", status_code=500)
        return None

    primary = _Gemini(_replies("No markers."), fail=fail)
    secondary = _Model("Retry grounded [1].", provider="router", model=FALLBACK)
    (judged,) = _judge_with(monkeypatch, [case], _chain(primary, secondary))
    answers = [n for _t, s, n in _notes(judged) if s == "answer"]
    assert len(answers) == 2  # partner: the retry really ran
    assert answers[0] == []
    assert [runner_mod.cause_key(n) for n in answers[1]] == ["gemini http_status 500"]


def test_when_gemini_answers_every_entry_has_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner for the zero. Red when: the key is missing (absence would read as
    "no failure") or a healthy pass counts a failure."""
    followup, alias = _sites_case()
    judged = _judge_with(monkeypatch, [followup, alias], _chain(_Gemini()))
    entries = [c for j in judged for c in j.model_calls]
    assert entries and all(c["primary_failures"] == [] for c in entries)
    block = _summary([followup, alias], judged, primary_applicable=True)["judge"][
        "primary_failures"
    ]
    assert block["calls"] == len(entries) > 0
    assert block["count"] == 0 and block["cases"] == []
    assert block["answer_site"] == {"fallback_answers": 0, "explained": 0, "without_record": 0}


def test_answers_by_another_model_with_no_cause_are_counted_without_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: ``without_record`` is a constant or counts the wrong population
    (a recording gap would read as fully explained)."""
    golden = _golden()
    cases = [golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]]
    judged = _run(monkeypatch, cases, _Model(ANSWERED, provider="router", model=FALLBACK))
    answers = [c for j in judged for c in j.model_calls if c["site"] == "answer"]
    assert len(answers) >= 2  # partner: the population exists
    block = _summary(cases, judged, primary_applicable=True)["judge"]["primary_failures"]
    assert block["answer_site"] == {
        "fallback_answers": len(answers),
        "explained": 0,
        "without_record": len(answers),
    }
    lines = runner_mod.pass_report_lines(_summary(cases, judged, primary_applicable=True)["judge"])
    assert any(f"without record {len(answers)}" in ln for ln in lines), lines


def test_when_both_providers_fail_the_failed_record_keeps_its_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the failed path drops the note (notes copied only on success), or
    the case's answer error changes."""
    case = _golden()["claude_api_lit_ratelimit"]
    primary = _Gemini(fail=lambda s, _u: _503() if s is CallSite.answer else None)
    (judged,) = _judge_with(monkeypatch, [case], _chain(primary, _Down(ANSWERED)))
    (answer,) = [c for c in judged.model_calls if c["site"] == "answer"]
    assert answer["failed"] is True
    assert [runner_mod.cause_key(n) for n in answer["primary_failures"]] == [
        "gemini http_status 503 UNAVAILABLE"
    ]
    assert (judged.error or "").startswith(
        "answer: answer call failed (final, answer: LLMUnavailable: OpenRouter returned 503)"
    )
    assert "UNAVAILABLE" not in (judged.error or "")


def test_notes_stay_with_their_own_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: notes leak across calls or cases."""
    golden = _golden()
    first, second = golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]
    asks_first = _asks(first.question)
    primary = _Gemini(fail=lambda _s, u: _503() if asks_first(u) else None)
    judged = _judge_with(monkeypatch, [first, second], _chain(primary))
    assert any(n for _t, _s, n in _notes(judged[0]))  # partner: case 0 really failed over
    assert all(n == [] for _t, _s, n in _notes(judged[1]))
    assert judged[1].error is None


def test_a_cache_replay_of_a_fallback_answer_carries_the_writers_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: an answer served from the cache that the fallback wrote is not
    traced to the writer's cause, or is counted as a new failure."""
    first = _golden()["claude_api_lit_ratelimit"]
    twin = dataclasses.replace(first, id=f"{first.id}_twin")
    asks_first = _asks(first.question)
    primary = _Gemini(fail=lambda _s, u: _503() if asks_first(u) else None)
    judged = _judge_with(monkeypatch, [first, twin], _chain(primary))
    assert "cache" in (judged[1].error or "")  # partner: the twin really was a cache hit
    assert "gemini http_status 503 UNAVAILABLE" in (judged[1].error or "")
    block = _summary([first, twin], judged, primary_applicable=True)["judge"]["primary_failures"]
    assert block["via_cache"] == 1
    assert block["count"] == 1
    assert {
        "case_id": twin.id,
        "turn": "final",
        "site": "cache",
        "cause": "gemini http_status 503 UNAVAILABLE",
    } in block["cases"]


def test_a_vague_cause_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: an untagged or odd-shaped cause is not counted as vague (a gap in
    the recording would read as a cause)."""
    golden = _golden()
    first, second = golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]
    asks_first = _asks(first.question)

    def fail(site: CallSite, user: str) -> LLMUnavailable | None:
        if site is not CallSite.answer:
            return None
        if asks_first(user):
            return LLMUnavailable("untagged")
        return _err("empty_answer", reason="SAFETY <b>")

    judged = _judge_with(monkeypatch, [first, second], _chain(_Gemini(fail=fail)))
    block = _summary([first, second], judged, primary_applicable=True)["judge"]["primary_failures"]
    assert block["vague"] == {"unknown_kind": 1, "unrecognized": 1}
    assert block["by_cause"] == {"gemini unknown": 1, "gemini empty_answer UNRECOGNIZED": 1}


# --- 20. the fallback pass ----------------------------------------------------------------


def test_the_fallback_pass_is_not_applicable_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a main-pass collector leaks into the fallback pass, or the fallback
    pass reads as if its primary could fail."""
    case = _golden()["claude_api_lit_ratelimit"]
    asks = _asks(case.question)
    _judge_with(
        monkeypatch, [case], _chain(_Gemini(fail=lambda _s, u: _503() if asks(u) else None))
    )
    judged = _run(
        monkeypatch,
        [case],
        _Model(ANSWERED, provider="router", model=FALLBACK),
        expect=("router", FALLBACK),
    )
    entries = [c for j in judged for c in j.model_calls]
    assert entries and all(c["primary_failures"] == [] for c in entries)
    block = _summary([case], judged)["judge"]["primary_failures"]
    assert block["applicable"] is False
    assert block["calls"] == len(entries) > 0 and block["count"] == 0
    lines = runner_mod.pass_report_lines(_summary([case], judged)["judge"])
    assert any(ln.startswith("primary failures: not applicable") for ln in lines), lines


# --- 10, 19. end to end, the real factory over MockTransport ------------------------------


def _e2e_settings(db: pathlib.Path, gemini_key: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="gemini",
        gemini_api_key=gemini_key,
        openrouter_api_key="or-" + uuid.uuid4().hex,
        gemini_model=ANSWER_MODEL,
        openrouter_model=FALLBACK,
        eval_fallback_model=FALLBACK,
        eval_judge_model=GRADER,
        eval_judge_timeout_seconds=60.0,
        cost_budget_enabled=False,
        database_url=f"sqlite+aiosqlite:///{db}",
    )


def _user_text(request: httpx.Request) -> str:
    payload = json.loads(request.content)
    if "contents" in payload:
        return str(payload["contents"][0]["parts"][0]["text"])
    return str(payload["messages"][-1]["content"])


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, gemini: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Every real client the factory builds talks to this handler instead of the
    network: ``gemini`` for Gemini, a fixed answer or verdict for OpenRouter."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "generateContent" in str(request.url):
            return gemini(request)
        model = json.loads(request.content)["model"]
        text = json.dumps({"score": 5, "rationale": "fake"}) if model == GRADER else ANSWERED
        return httpx.Response(
            200, json={"choices": [{"message": {"content": text}}], "model": model}
        )

    transport = httpx.MockTransport(handler)
    shim = SimpleNamespace(
        AsyncClient=lambda timeout=None: httpx.AsyncClient(transport=transport, timeout=timeout)
    )
    monkeypatch.setattr(gemini_mod, "httpx", shim)
    monkeypatch.setattr(openrouter_mod, "httpx", shim)
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    # Only the stub check reads the process-wide client; the run builds its own.
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())


async def _seed(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


IDS = ["claude_api_lit_ratelimit", "codex_lit_modelflag"]


def test_end_to_end_the_report_and_the_spend_export_carry_the_causes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: any link from the real Gemini client to the written report, the
    console or the spend export drops the cause (a stub dry run proves nothing: the
    judge skips itself there)."""
    golden = _golden()
    q503, qempty = golden[IDS[0]].question, golden[IDS[1]].question

    def gemini(request: httpx.Request) -> httpx.Response:
        user = _user_text(request)
        if f"Question: {q503}\n" in user:
            return httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE"}})
        if f"Question: {qempty}\n" in user:
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
                    "usageMetadata": {"thoughtsTokenCount": 70, "candidatesTokenCount": 0},
                },
            )
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": ANSWERED}]}}]}
        )

    _install_transport(monkeypatch, gemini)
    db = tmp_path / "eval.db"
    asyncio.run(_seed(f"sqlite+aiosqlite:///{db}"))
    settings = _e2e_settings(db, "g-" + uuid.uuid4().hex)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(spend_export, "get_settings", lambda: settings)
    report = tmp_path / "eval_report.json"
    runner_mod.main(["--ids", ",".join(IDS), "--report", str(report)])
    written = json.loads(report.read_text(encoding="utf-8"))

    main_block = written["judge"]["primary_failures"]
    assert main_block["applicable"] is True
    assert main_block["calls"] > 0
    assert main_block["by_cause"]["gemini http_status 503 UNAVAILABLE"] >= 1
    assert main_block["by_cause"]["gemini empty_answer MAX_TOKENS"] >= 1
    assert main_block["answer_site"]["without_record"] == 0
    assert main_block["answer_site"]["fallback_answers"] >= 2
    empty = [
        n
        for case in written["judge"]["cases"]
        for c in case["model_calls"]
        for n in c["primary_failures"]
        if n["kind"] == "empty_answer"
    ]
    assert empty and empty[0]["thoughts_tokens"] == 70 and empty[0]["max_output_tokens"]
    fallback_block = written["fallback"]["judge"]["primary_failures"]
    assert fallback_block["applicable"] is False
    assert fallback_block["calls"] > 0 and fallback_block["count"] == 0

    console = capsys.readouterr().out
    assert "main pass primary failures: " in console
    assert (
        f"main pass primary failure: {IDS[0]} (final turn, answer): gemini http_status 503"
        in console
    )

    out = tmp_path / "spend.json"
    assert spend_export.main(["--out", str(out), "--report", str(report)]) == 0
    export = json.loads(out.read_text(encoding="utf-8"))
    per_model = export["primary_failures"][ANSWER_MODEL]
    assert per_model["by_cause"] == main_block["by_cause"]
    assert per_model["calls"] == main_block["count"]
    assert "primary failures" in capsys.readouterr().out


def test_no_body_text_reaches_any_report_or_the_spend_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: body or message text reaches any new field, in a mid-run save, the
    final report, the summary or the spend export."""
    marker = _marker()
    gemini_key = "g-" + uuid.uuid4().hex
    question = _golden()[IDS[0]].question
    bodies: list[str] = []

    def gemini(request: httpx.Request) -> httpx.Response:
        if f"Question: {question}\n" in _user_text(request):
            response = httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": f"{marker} {gemini_key}",
                        "details": [{"reason": marker}],
                    }
                },
            )
            bodies.append(response.text)
            return response
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": ANSWERED}]}}]}
        )

    _install_transport(monkeypatch, gemini)
    db = tmp_path / "eval.db"
    asyncio.run(_seed(f"sqlite+aiosqlite:///{db}"))
    settings = _e2e_settings(db, gemini_key)
    saved: list[str] = []
    real_write = runner_mod.write_report

    def _capture(path: pathlib.Path, summary: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        saved.append(json.dumps(summary))
        real_write(path, summary, *args, **kwargs)

    monkeypatch.setattr(runner_mod, "write_report", _capture)
    summary = asyncio.run(
        runner_mod.run_eval_async(
            ids=IDS[:1], settings=settings, report_path=tmp_path / "report.json"
        )
    )
    export = spend_export.summarize([], summary)
    texts = [*saved, json.dumps(summary), json.dumps(export)]
    assert bodies and marker in bodies[0]  # partner: the marker was in the response
    assert len(saved) >= 2  # partner: mid-run saves were captured
    for text in texts:
        assert marker not in text
    # Partner: the bounded cause itself is present in the report and the export.
    assert '"status_code": 400' in json.dumps(summary)
    assert "gemini http_status 400 INVALID_ARGUMENT" in json.dumps(summary)
    assert "gemini http_status 400 INVALID_ARGUMENT" in json.dumps(export)
