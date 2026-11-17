"""#576 part 1: the fallback notes its primary's failure before the secondary is called,
only into a collector the caller installed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.llm.attempts import PrimaryFailure, collect_primary_failures, note_primary_failure
from app.llm.errors import LLMUnavailable
from app.llm.fallback import FallbackLLMClient
from app.llm.types import LLMResult

MODEL = "gemini-3.6-flash"


class _Client:
    """Answers as ``provider``, or raises ``exc``; named the way the Gemini client is."""

    _PROVIDER = "gemini"

    def __init__(self, provider: str, exc: BaseException | None = None) -> None:
        self._model, self.provider, self.exc, self.calls = MODEL, provider, exc, 0

    async def complete(self, **_: Any) -> LLMResult:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return LLMResult(
            text=self.provider, input_tokens=1, output_tokens=1, model="m", provider="router"
        )

    async def aclose(self) -> None:
        return None


def _429() -> LLMUnavailable:
    return LLMUnavailable(
        "Gemini returned 429", kind="http_status", status_code=429, provider_status="QUOTA"
    )


async def _ask(primary: _Client, secondary: _Client | None = None) -> LLMResult:
    client = FallbackLLMClient(primary=primary, secondary=secondary or _Client("router"))  # type: ignore[arg-type]
    return await client.complete(system="s", user="u", max_tokens=8, temperature=0.0)


async def test_a_fallback_inside_a_collector_notes_one_bounded_cause() -> None:
    """Red when: the fallback stops noting, or names another provider or model."""
    with collect_primary_failures() as notes:
        assert (await _ask(_Client("gemini", _429()))).text == "router"
    (note,) = notes
    assert (note.provider, note.model, note.kind, note.status_code, note.provider_status) == (
        *("gemini", MODEL, "http_status", 429, "QUOTA"),
    )
    assert note.error_type == "LLMUnavailable" and note.duration_seconds >= 0.0


async def test_no_note_on_success_and_none_without_a_collector() -> None:
    """Red when: a success notes a stale failure, or noting raises with no collector."""
    assert (await _ask(_Client("gemini", _429()))).text == "router"
    with collect_primary_failures() as notes:
        assert (await _ask(_Client("gemini"))).text == "gemini"
    assert notes == []


async def test_when_both_fail_the_note_is_kept_and_the_secondarys_error_propagates() -> None:
    """Red when: the note is taken after the secondary call, or its error is replaced."""
    error = LLMUnavailable("OpenRouter returned 503")
    with collect_primary_failures() as notes, pytest.raises(LLMUnavailable) as info:
        await _ask(_Client("gemini", _429()), _Client("router", error))
    assert info.value is error and [n.status_code for n in notes] == [429]


@pytest.mark.parametrize(
    "exc", [asyncio.CancelledError(), TimeoutError("t"), ValueError("v")], ids=repr
)
async def test_other_exceptions_propagate_with_no_secondary_call_and_no_note(
    exc: BaseException,
) -> None:
    """Red when: the catch is widened (a cancellation or a bug masked or noted)."""
    secondary = _Client("router")
    with collect_primary_failures() as notes, pytest.raises(type(exc)):
        await _ask(_Client("gemini", exc), secondary)
    assert secondary.calls == 0 and notes == []


async def test_a_note_made_inside_a_task_lands_in_the_callers_collector() -> None:
    """Red when: the collector is replaced rather than appended to (a task runs on a copy)."""
    with collect_primary_failures() as notes:
        await asyncio.create_task(_ask(_Client("gemini", _429())))
    assert [n.status_code for n in notes] == [429]


def test_collectors_are_reset_on_exit_even_after_an_error() -> None:
    """Red when: the collector's token is not reset, so a later note lands in an old one."""
    failure = PrimaryFailure.from_error(
        LLMUnavailable("x"), provider="gemini", model=MODEL, duration_seconds=0.0
    )
    with pytest.raises(RuntimeError), collect_primary_failures() as first:
        note_primary_failure(failure)
        raise RuntimeError("boom")
    note_primary_failure(failure)  # no collector: a no-op
    with collect_primary_failures() as second:
        pass
    assert len(first) == 1 and second == []
