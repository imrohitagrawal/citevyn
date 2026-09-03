"""The system prompt's formatting contract (#303).

The client parses a deliberately tiny markdown subset
(``frontend/src/lib/answerFormat.ts``) and renders anything else as literal
text. These guard the two halves of that contract that are easy to break by
editing the prompt: the subset instruction being present, and the trailing
refusal clause staying LAST and byte-exact.
"""

from __future__ import annotations

from app.llm.prompts import NO_ANSWER_REFUSAL, SYSTEM_PROMPT


def test_the_refusal_clause_is_still_the_last_thing_the_prompt_says() -> None:
    """``_is_no_answer_refusal`` compares against this and ``knowledgeBase.ts``
    mirrors it byte-for-byte, so anything appended after it changes what the
    model emits for a no-answer and silently breaks both."""
    tail = f'respond with exactly: "{NO_ANSWER_REFUSAL}" and nothing else.'
    assert SYSTEM_PROMPT.endswith(tail), SYSTEM_PROMPT[-160:]


def test_the_prompt_constrains_the_model_to_the_subset_the_client_parses() -> None:
    assert "**bold**" in SYSTEM_PROMPT
    assert "`backticks`" in SYSTEM_PROMPT
    assert '"- "' in SYSTEM_PROMPT
    # Both bullet markers, because the client accepts both — the first live answer
    # after #303 shipped used "* " and rendered literal asterisks.
    assert '"* "' in SYSTEM_PROMPT
    # ...and names the things it must NOT emit, which is the half that keeps
    # unconstrained markdown from reaching the reader as visible punctuation.
    for banned in ("headings", "tables", "links", "images", "code fences"):
        assert banned in SYSTEM_PROMPT, banned


def test_the_formatting_instruction_comes_before_the_refusal_clause() -> None:
    """Order is load-bearing: the refusal clause says "respond with exactly ...
    and nothing else", so any instruction placed after it is inside the scope of
    that sentence rather than a separate rule."""
    assert SYSTEM_PROMPT.index("Formatting:") < SYSTEM_PROMPT.index("respond with exactly")


def test_the_citation_contract_survives_the_formatting_addition() -> None:
    # Partner assertion: the formatting rules must not have displaced the thing
    # the prompt exists for.
    assert "bracketed citation marker like [1]" in SYSTEM_PROMPT
    assert "Answer ONLY using the evidence bullets" in SYSTEM_PROMPT
