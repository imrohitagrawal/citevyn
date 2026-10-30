"""A product cited ONLY inside a grouped marker is still called uncovered (#554).

"Split, don't count": an index that came only from a group gets its marker and
source card, but never decides a grounding outcome. The comparison note (Phase
6E, "The documentation I have does not cover X") is one of those outcomes, so it
is computed from the markers written singly, exactly as before #554.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.llm.types import LLMResult
from tests.orchestrator_double import Orchestrator
from tests.test_answer_orchestrator import (
    _COMPARE_Q,
    _evidence,
    _FakeRetriever,
    _seed_index_version,
    _settings,
)

_NOTE = "The documentation I have does not cover the Gemini API for this question."


class _FixedTextLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, **_kwargs: Any) -> LLMResult:
        return LLMResult(
            text=self._text, input_tokens=1, output_tokens=1, model="stub", provider="stub"
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_product_cited_only_in_a_group_still_gets_the_missing_note(session: Any) -> None:
    # Red when: missing_areas is given the shown indices (cited_indices) instead of the
    # singly written ones, so a group-only [2] hides the "does not cover" note.
    await _seed_index_version(session)
    evidence = await _evidence(session, count=2)
    evidence[0].product_area, evidence[0].source_name = "claude_api", "claude_api"
    evidence[1].product_area, evidence[1].source_name = "gemini_api", "gemini_api"
    text = "Claude limits are X [1]. Gemini limits are not explicitly listed [1, 2]."
    orchestrator = Orchestrator(
        _settings(comparison_answers=True),
        session,
        llm=_FixedTextLLM(text),
        retriever=_FakeRetriever(evidence),
    )

    response = await orchestrator.ask(
        question=_COMPARE_Q, request_id="req_cmp_group", session_id=uuid.uuid4()
    )

    assert response["no_answer"] is False
    assert response["answer"] == (
        "Claude limits are X [1]. Gemini limits are not explicitly listed [1][2].\n\n" + _NOTE
    )
    # Cards still come from every shown index; confidence from the single [1] only.
    assert [c["marker"] for c in response["citations"]] == [1, 2]
    assert response["confidence"] == "medium"
