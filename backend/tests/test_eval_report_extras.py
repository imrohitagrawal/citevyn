"""Builder's additions for #598 Step 2, beside the test designer's files.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.llm.errors import LLMUnavailable
from tests.eval import runner as runner_mod

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
_TIMEOUT_LINE = re.compile(r"^# CITEVYN_(\w+_TIMEOUT_SECONDS)=(\S+)", re.MULTILINE)


class _Down:
    async def complete(self, **_: Any) -> Any:
        raise LLMUnavailable("grader timed out")

    async def aclose(self) -> None:
        return None


def test_a_failed_grader_call_is_recorded_with_its_duration_and_still_raises() -> None:
    """Red when: a grader call that fails (a timeout) leaves no entry, so a stalled
    grader cannot be found in the report, or the failure is swallowed (the case
    must still record ``judge_unavailable``)."""
    grader = runner_mod._RecordingGrader(_Down())  # type: ignore[arg-type]
    with pytest.raises(LLMUnavailable):
        asyncio.run(grader.complete(system="s", user="u", max_tokens=1, temperature=0.0))
    (entry,) = grader.calls
    assert entry["score"] is None
    assert "grader timed out" in entry["rationale"]
    assert isinstance(entry["duration_seconds"], float) and entry["duration_seconds"] >= 0


def test_env_example_timeouts_state_the_code_defaults() -> None:
    """Red when: ``.env.example`` states a provider timeout the code does not use
    (it said 30.0 for Gemini and OpenRouter while the code used 15.0)."""
    found = dict(_TIMEOUT_LINE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))
    stated = {name: value for name, value in found.items() if value[0].isdigit()}
    # Partner: the provider lines are really read, so an empty match cannot pass.
    assert {"GEMINI_TIMEOUT_SECONDS", "OPENROUTER_TIMEOUT_SECONDS"} <= set(stated), found
    for name, value in stated.items():
        default = Settings.model_fields[name.lower()].default
        assert float(value) == default, (name, value, default)
