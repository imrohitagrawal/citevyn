"""#576 part 1: the fallback client notes why its primary failed.

The note goes to a collector the judged eval installs around each call
(:func:`collect_primary_failures`); with none installed it does nothing, so
production behaviour is unchanged. The note is taken BEFORE the secondary is
called, so it survives the secondary failing too. Free and hermetic. Each test
names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from app.llm.errors import LLMUnavailable
from app.llm.fallback import FallbackLLMClient
from app.llm.types import LLMResult

MODEL = "gemini-3.6-flash"


def _attempts() -> Any:
    """The new module, imported per test so each test is red on its own before it exists."""
    return importlib.import_module("app.llm.attempts")


class _Primary:
    """A primary that names itself the way the Gemini client does."""

    _PROVIDER = "gemini"

    def __init__(self, exc: BaseException | None = None) -> None:
        self._model = MODEL
        self.exc = exc
        self.calls = 0

    async def complete(self, **_: Any) -> LLMResult:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return LLMResult(
            text="gemini", input_tokens=1, output_tokens=1, model=MODEL, provider="gemini"
        )

    async def aclose(self) -> None:
        return None


class _Secondary:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc
        self.calls = 0

    async def complete(self, **_: Any) -> LLMResult:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return LLMResult(
            text="router", input_tokens=1, output_tokens=1, model="m", provider="router"
        )

    async def aclose(self) -> None:
        return None


async def _ask(client: FallbackLLMClient) -> LLMResult:
    return await client.complete(system="s", user="u", max_tokens=8, temperature=0.0)


def _tagged_429() -> LLMUnavailable:
    return LLMUnavailable(
        "Gemini returned 429",
        kind="http_status",
        status_code=429,
        provider_status="RESOURCE_EXHAUSTED",
    )


async def test_a_fallback_inside_a_collector_notes_one_bounded_cause() -> None:
    """Red when: the fallback stops noting, or names another provider or model."""
    client = FallbackLLMClient(primary=_Primary(_tagged_429()), secondary=_Secondary())  # type: ignore[arg-type]
    with _attempts().collect_primary_failures() as notes:
        result = await _ask(client)
    assert result.text == "router"
    (note,) = notes
    assert (note.provider, note.model, note.kind, note.status_code, note.provider_status) == (
        "gemini",
        MODEL,
        "http_status",
        429,
        "RESOURCE_EXHAUSTED",
    )
    assert note.error_type == "LLMUnavailable"
    assert note.duration_seconds >= 0.0


async def test_outside_a_collector_the_fallback_works_and_records_nothing() -> None:
    """Red when: noting raises with no collector installed (production has none)."""
    client = FallbackLLMClient(primary=_Primary(_tagged_429()), secondary=_Secondary())  # type: ignore[arg-type]
    assert (await _ask(client)).text == "router"
    with _attempts().collect_primary_failures() as notes:
        pass
    assert notes == []  # a fresh collector starts empty: nothing leaked in


async def test_a_primary_that_answers_leaves_no_note() -> None:
    """Red when: a success notes a stale failure."""
    client = FallbackLLMClient(primary=_Primary(), secondary=_Secondary())  # type: ignore[arg-type]
    with _attempts().collect_primary_failures() as notes:
        assert (await _ask(client)).text == "gemini"
    assert notes == []


async def test_when_both_fail_the_note_is_kept_and_the_secondarys_error_propagates() -> None:
    """Red when: the note is taken after the secondary call, or the secondary's
    error is replaced."""
    secondary_error = LLMUnavailable("OpenRouter returned 503")
    client = FallbackLLMClient(
        primary=_Primary(_tagged_429()),
        secondary=_Secondary(secondary_error),  # type: ignore[arg-type]
    )
    with _attempts().collect_primary_failures() as notes, pytest.raises(LLMUnavailable) as info:
        await _ask(client)
    assert info.value is secondary_error
    assert [n.status_code for n in notes] == [429]


@pytest.mark.parametrize(
    "exc",
    [asyncio.CancelledError(), TimeoutError("t"), ValueError("v")],
    ids=lambda e: type(e).__name__,
)
async def test_other_exceptions_propagate_with_no_secondary_call_and_no_note(
    exc: BaseException,
) -> None:
    """Red when: the catch is widened (a cancellation or a bug is masked by a
    fallback answer, or noted as a provider failure)."""
    secondary = _Secondary()
    client = FallbackLLMClient(primary=_Primary(exc), secondary=secondary)  # type: ignore[arg-type]
    with _attempts().collect_primary_failures() as notes, pytest.raises(type(exc)):
        await _ask(client)
    assert secondary.calls == 0
    assert notes == []


async def test_a_note_made_inside_a_task_lands_in_the_callers_collector() -> None:
    """Red when: the collector is replaced rather than appended to, so a note made in
    a task (which runs on a copy of the context) is lost."""
    client = FallbackLLMClient(primary=_Primary(_tagged_429()), secondary=_Secondary())  # type: ignore[arg-type]
    with _attempts().collect_primary_failures() as notes:
        await asyncio.create_task(_ask(client))
    assert [n.status_code for n in notes] == [429]


def test_collectors_are_reset_on_exit_even_after_an_error() -> None:
    """Red when: the collector's token is not reset, so a later call outside any
    collector (or in the next one) would see an earlier call's notes."""
    failure = _attempts().PrimaryFailure.from_error(
        LLMUnavailable("x"), provider="gemini", model=MODEL, duration_seconds=0.0
    )
    with pytest.raises(RuntimeError), _attempts().collect_primary_failures() as first:
        _attempts().note_primary_failure(failure)
        raise RuntimeError("boom")
    assert len(first) == 1
    _attempts().note_primary_failure(failure)  # no collector: a no-op
    with _attempts().collect_primary_failures() as second:
        pass
    assert second == [] and len(first) == 1
