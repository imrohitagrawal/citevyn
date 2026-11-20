"""A total time limit on each answer and grader call in the judged eval.

Written before the implementation (test designer). Free: a REAL asyncio server on
127.0.0.1 with httpx's default transport (``MockTransport`` applies no timeouts,
so it could not show that the per-read timeout alone is no cap), the production
OpenRouter client pointed at it, and scripted clients. No key, no network. The
contract for ``backend/tests/eval/runner.py`` (the eval layer only: nothing in
``app/llm`` changes):

* ``CallTotalTimeout``: a module-level ``Exception`` subclass (never a
  ``CancelledError``).
* ``AttributingLLM(inner, *, total_timeout_seconds=None)`` and
  ``_RecordingGrader(inner, ..., total_timeout_seconds=None)`` run the inner call
  inside ``asyncio.timeout(total)``. Only when THAT timeout expired is the call's
  error ``CallTotalTimeout``; it is recorded as a failed call like any other. An
  outer cancel stays a ``CancelledError``, and an outer timeout stays the outer
  ``TimeoutError``. ``None`` adds no limit.
* A ``CallTotalTimeout`` can cancel the spend meter after the provider answered,
  so its spend row may be lost: every such call is flagged
  ``spend_may_be_unrecorded`` (``True``) on its record (``_CallRecord`` attribute;
  the key in its ``model_calls`` entry; the key in its grader-call entry). The key
  is absent, or false, on every other call, so today's pinned grader-call keys
  hold.
* ``run_eval_async`` gives the answer clients (main and fallback)
  ``Settings.eval_call_total_timeout_seconds`` and the grader's recorders
  ``Settings.eval_judge_timeout_seconds`` as their totals.
* Each pass's ``judge`` section counts the flagged calls of that pass (answer and
  grader calls) as ``spend_may_be_unrecorded`` (an int, 0 when none).

Timings are CI-safe: the drip lasts 2 s, the total is 0.5 s, the bound asserted
is 1.5 s.

Each test carries one ``RED WHEN`` line naming the change that turns it red.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.cost.call_site import CallSite, call_site, get_call_site
from app.llm.openrouter import OpenRouterLLMClient
from app.llm.types import LLMResult
from tests.eval import runner as runner_mod
from tests.test_eval_report_fields import _Model, _two_pass_settings
from tests.test_eval_report_integrity import ANSWERED, _clients

DRIP_SECONDS = 2.0
TOTAL_SECONDS = 0.5
BOUND_SECONDS = 1.5
#: httpx's per-read timeout: far above the drip's gaps, so it never fires.
READ_TIMEOUT_SECONDS = 1.0
REPLY = "dripped reply"
ONE = ["claude_api_lit_ratelimit"]
TWO = ["claude_api_lit_ratelimit", "codex_lit_modelflag"]


def _body() -> bytes:
    return json.dumps(
        {
            "model": "openai/gpt-4o-mini",
            "choices": [{"message": {"role": "assistant", "content": REPLY}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
    ).encode()


@contextlib.asynccontextmanager
async def _server(drip_seconds: float) -> AsyncIterator[str]:
    """An HTTP/1.1 server on 127.0.0.1 that answers every POST with 200 and a valid
    OpenRouter body, sent one byte at a time spread over ``drip_seconds``."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.decode("latin-1").split("\r\n"):
                name, _, value = line.partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            await reader.readexactly(length)
            body = _body()
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                + f"content-length: {len(body)}\r\nconnection: close\r\n\r\n".encode()
            )
            await writer.drain()
            gap = drip_seconds / len(body)
            for i in range(len(body)):
                writer.write(body[i : i + 1])
                await writer.drain()
                if gap:
                    await asyncio.sleep(gap)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=2.0)


def _router(base: str) -> OpenRouterLLMClient:
    return OpenRouterLLMClient(
        model="openai/gpt-4o-mini",
        api_key="placeholder-not-a-key",
        api_base=base,
        timeout_seconds=READ_TIMEOUT_SECONDS,
    )


async def _answer(llm: Any) -> LLMResult:
    with call_site(CallSite.answer):
        return await llm.complete(system="s", user="u", max_tokens=8, temperature=0.0)


def _total_timeout() -> type[BaseException]:
    cls = runner_mod.CallTotalTimeout  # type: ignore[attr-defined]
    assert issubclass(cls, Exception) and not issubclass(cls, asyncio.CancelledError)
    return cls


# --- AttributingLLM on a real loopback server ---------------------------------------


def test_a_drip_under_the_read_timeout_with_no_total_succeeds() -> None:
    # RED WHEN: AttributingLLM takes no total_timeout_seconds, or None adds a limit.
    async def scenario() -> tuple[LLMResult, float, Any]:
        async with _server(DRIP_SECONDS) as base:
            inner = _router(base)
            llm = runner_mod.AttributingLLM(inner, total_timeout_seconds=None)  # type: ignore[call-arg]
            start = time.monotonic()
            try:
                result = await _answer(llm)
            finally:
                await inner.aclose()
            return result, time.monotonic() - start, llm

    result, elapsed, llm = asyncio.run(scenario())
    assert result.text == REPLY
    assert elapsed >= DRIP_SECONDS * 0.9  # the drip really lasted
    assert [r.failed for r in llm.records] == [False]


def test_the_same_drip_with_a_total_raises_call_total_timeout_in_time() -> None:
    # RED WHEN: AttributingLLM's asyncio.timeout wrapper is removed.
    total_timeout = _total_timeout()

    async def scenario() -> tuple[BaseException | None, float, Any]:
        async with _server(DRIP_SECONDS) as base:
            inner = _router(base)
            llm = runner_mod.AttributingLLM(inner, total_timeout_seconds=TOTAL_SECONDS)  # type: ignore[call-arg]
            start = time.monotonic()
            caught: BaseException | None = None
            try:
                await _answer(llm)
            except BaseException as exc:  # noqa: BLE001 - the type is the assertion
                caught = exc
            finally:
                await inner.aclose()
            return caught, time.monotonic() - start, llm

    caught, elapsed, llm = asyncio.run(scenario())
    assert isinstance(caught, total_timeout), repr(caught)
    assert elapsed < BOUND_SECONDS, elapsed
    (record,) = llm.records
    assert record.failed and record.error_type == "CallTotalTimeout"
    assert record.spend_may_be_unrecorded is True  # type: ignore[attr-defined]


def test_an_outer_cancel_during_the_call_stays_a_cancel() -> None:
    # RED WHEN: an outer cancel during an answer call is relabelled CallTotalTimeout.
    async def scenario() -> tuple[BaseException | None, Any]:
        async with _server(DRIP_SECONDS) as base:
            inner = _router(base)
            llm = runner_mod.AttributingLLM(inner, total_timeout_seconds=5.0)  # type: ignore[call-arg]
            task = asyncio.ensure_future(_answer(llm))
            await asyncio.sleep(0.2)
            task.cancel()
            caught: BaseException | None = None
            try:
                await task
            except BaseException as exc:  # noqa: BLE001 - the type is the assertion
                caught = exc
            finally:
                await inner.aclose()
            return caught, llm

    caught, llm = asyncio.run(scenario())
    assert isinstance(caught, asyncio.CancelledError), repr(caught)
    assert not isinstance(caught, _total_timeout())
    (record,) = llm.records
    assert record.error_type == "CancelledError"
    assert not getattr(record, "spend_may_be_unrecorded", False)


def test_an_outer_timeout_shorter_than_the_total_stays_the_outer_timeout() -> None:
    # RED WHEN: an outer timeout is relabelled as the call's own total.
    async def scenario() -> BaseException | None:
        async with _server(DRIP_SECONDS) as base:
            inner = _router(base)
            llm = runner_mod.AttributingLLM(inner, total_timeout_seconds=5.0)  # type: ignore[call-arg]
            try:
                async with asyncio.timeout(0.3):
                    await _answer(llm)
            except BaseException as exc:  # noqa: BLE001 - the type is the assertion
                return exc
            finally:
                await inner.aclose()
            return None

    caught = asyncio.run(scenario())
    assert isinstance(caught, TimeoutError), repr(caught)
    assert not isinstance(caught, _total_timeout())


class _RaisesItsOwnTimeout:
    """A client whose call raises a TimeoutError of its own (a database wait inside
    the meter, say) long before the total limit."""

    async def complete(self, **_: Any) -> LLMResult:
        raise TimeoutError("the meter's own wait timed out")

    async def aclose(self) -> None:
        return None


def test_a_timeout_raised_inside_the_call_is_not_relabelled_as_the_total() -> None:
    # RED WHEN: the wrapper skips its cm.expired() check.
    async def scenario() -> tuple[BaseException | None, Any, Any]:
        llm = runner_mod.AttributingLLM(_RaisesItsOwnTimeout(), total_timeout_seconds=5.0)  # type: ignore[call-arg]
        grader = runner_mod._RecordingGrader(_RaisesItsOwnTimeout(), total_timeout_seconds=5.0)  # type: ignore[call-arg]
        caught: list[BaseException] = []
        for call in (
            _answer(llm),
            grader.complete(system="s", user="u", max_tokens=8, temperature=0.0),
        ):
            try:
                await call
            except BaseException as exc:  # noqa: BLE001 - the type is the assertion
                caught.append(exc)
        return caught[0] if len(caught) == 2 else None, llm, grader

    caught, llm, grader = asyncio.run(scenario())
    assert isinstance(caught, TimeoutError), repr(caught)
    assert not isinstance(caught, _total_timeout())
    (record,) = llm.records
    assert record.error_type == "TimeoutError"
    assert not getattr(record, "spend_may_be_unrecorded", False)
    (entry,) = grader.calls
    assert entry["rationale"].startswith("call failed: TimeoutError")
    assert not entry.get("spend_may_be_unrecorded")


class _StallsAfterTheProvider:
    """The provider answers at once; then the spend meter never returns."""

    def __init__(self, provider: OpenRouterLLMClient) -> None:
        self.provider, self.provider_answered = provider, False

    async def complete(self, **kwargs: Any) -> LLMResult:
        await self.provider.complete(**kwargs)
        self.provider_answered = True
        await asyncio.Event().wait()  # the meter's record_call stalls
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        await self.provider.aclose()


def test_a_total_hit_after_the_provider_answered_is_flagged_spend_may_be_unrecorded() -> None:
    # RED WHEN: a CallTotalTimeout record is not flagged.
    async def scenario() -> tuple[BaseException | None, Any, _StallsAfterTheProvider]:
        async with _server(0.0) as base:
            inner = _StallsAfterTheProvider(_router(base))
            llm = runner_mod.AttributingLLM(inner, total_timeout_seconds=TOTAL_SECONDS)  # type: ignore[call-arg]
            caught: BaseException | None = None
            try:
                await asyncio.wait_for(_answer(llm), timeout=10.0)
            except BaseException as exc:  # noqa: BLE001 - the type is the assertion
                caught = exc
            finally:
                await inner.aclose()
            return caught, llm, inner

    caught, llm, inner = asyncio.run(scenario())
    assert inner.provider_answered is True  # partner: a spend row was owed
    assert isinstance(caught, _total_timeout()), repr(caught)
    (record,) = llm.records
    assert record.spend_may_be_unrecorded is True  # type: ignore[attr-defined]


# --- _RecordingGrader on the same server ------------------------------------------------


def test_the_grader_recorder_raises_call_total_timeout_and_flags_the_entry() -> None:
    # RED WHEN: _RecordingGrader does not wrap, record or flag the total.
    total_timeout = _total_timeout()

    async def scenario() -> tuple[BaseException | None, float, Any]:
        async with _server(DRIP_SECONDS) as base:
            inner = _router(base)
            grader = runner_mod._RecordingGrader(inner, total_timeout_seconds=TOTAL_SECONDS)  # type: ignore[call-arg]
            start = time.monotonic()
            caught: BaseException | None = None
            try:
                await grader.complete(system="s", user="u", max_tokens=8, temperature=0.0)
            except BaseException as exc:  # noqa: BLE001 - the type is the assertion
                caught = exc
            finally:
                await inner.aclose()
            return caught, time.monotonic() - start, grader

    caught, elapsed, grader = asyncio.run(scenario())
    assert isinstance(caught, total_timeout), repr(caught)
    assert elapsed < BOUND_SECONDS, elapsed
    (entry,) = grader.calls
    assert "CallTotalTimeout" in entry["rationale"]
    assert entry["spend_may_be_unrecorded"] is True


def test_the_grader_recorder_lets_an_outer_cancel_through_unflagged() -> None:
    # RED WHEN: _RecordingGrader relabels or flags an outer cancel.
    async def scenario() -> tuple[BaseException | None, Any]:
        async with _server(DRIP_SECONDS) as base:
            inner = _router(base)
            grader = runner_mod._RecordingGrader(inner, total_timeout_seconds=5.0)  # type: ignore[call-arg]
            task = asyncio.ensure_future(
                grader.complete(system="s", user="u", max_tokens=8, temperature=0.0)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            caught: BaseException | None = None
            try:
                await task
            except BaseException as exc:  # noqa: BLE001 - the type is the assertion
                caught = exc
            finally:
                await inner.aclose()
            return caught, grader

    caught, grader = asyncio.run(scenario())
    assert isinstance(caught, asyncio.CancelledError), repr(caught)
    (entry,) = grader.calls
    assert not entry.get("spend_may_be_unrecorded")


# --- the run wires the settings into each wrapper ----------------------------------------


class _StallingAnswer(_Model):
    """Answers helper calls like ``_Model``; every answer call never returns."""

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if get_call_site() is CallSite.answer:
            await asyncio.Event().wait()
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


class _StallingGrader:
    """A grader whose calls never return."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_: Any) -> LLMResult:
        self.calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        return None


def _flagged(case: dict[str, Any]) -> int:
    calls = [*(case.get("model_calls") or []), *(case.get("grader_calls") or [])]
    return sum(1 for c in calls if c.get("spend_may_be_unrecorded"))


def _bounded_run(settings: Any, ids: list[str]) -> dict[str, Any]:
    # A missing wrapper would hang forever; the outer bound turns that into a failure.
    return asyncio.run(
        asyncio.wait_for(runner_mod.run_eval_async(ids=ids, settings=settings), timeout=10.0)
    )


def test_the_run_gives_answer_calls_the_call_total_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RED WHEN: run_eval_async does not pass the call total to AttributingLLM.
    settings = _two_pass_settings(eval_fallback_model=None, eval_call_total_timeout_seconds=0.3)
    assert settings.eval_call_total_timeout_seconds == 0.3  # type: ignore[attr-defined]
    _clients(monkeypatch, main=_StallingAnswer(ANSWERED))
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    summary = _bounded_run(settings, TWO)
    cases = summary["judge"]["cases"]
    assert [c["case_id"] for c in cases] == TWO
    for case in cases:
        assert "CallTotalTimeout" in (case["error"] or ""), case["error"]
        assert _flagged(case) >= 1  # partner of the count: the flagged calls exist
    assert summary["judge"]["spend_may_be_unrecorded"] == sum(_flagged(c) for c in cases)


def test_the_run_gives_grader_calls_the_judge_timeout_as_their_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RED WHEN: the grader recorder does not get the judge timeout as total.
    settings = _two_pass_settings(eval_fallback_model=None, eval_judge_timeout_seconds=0.3)
    grader = _StallingGrader()
    _clients(monkeypatch, grader=grader)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    summary = _bounded_run(settings, ONE)
    (case,) = summary["judge"]["cases"]
    assert grader.calls >= 1  # partner: the grader was really called
    assert "CallTotalTimeout" in (case["error"] or ""), case["error"]
    assert case["score"] is None
    assert any("CallTotalTimeout" in c["rationale"] for c in case["grader_calls"])
    assert summary["judge"]["spend_may_be_unrecorded"] == _flagged(case) >= 1


def test_a_healthy_run_counts_no_unrecorded_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    # RED WHEN: the judge section loses the count, or flags a healthy call.
    settings = _two_pass_settings(eval_fallback_model=None, eval_call_total_timeout_seconds=5.0)
    _clients(monkeypatch)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "1")
    summary = _bounded_run(settings, TWO)
    assert [c["score"] for c in summary["judge"]["cases"]] == [4, 4]
    assert summary["judge"]["spend_may_be_unrecorded"] == 0
