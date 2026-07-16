"""Unit tests for conversation memory (Phase 3b, app/answer/memory.py).

Two surfaces:

* :func:`build_contextual_query` — the PURE rewrite logic (no I/O). These lock the
  three rewrite gates + the two safety properties (single-turn no-op; a self-contained
  off-domain sentence is never contextualized).
* :func:`recent_user_questions` — the DB read (role filter, session scope, ordering,
  limit), exercised against the hermetic SQLite session fixture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.answer.memory import (
    build_contextual_query,
    is_anaphoric_followup,
    recent_user_questions,
)
from app.models import Message, MessageRole

# ---------------------------------------------------------------------------
# build_contextual_query — pure rewrite logic
# ---------------------------------------------------------------------------

_CLAUDE_PRIOR = "What is the rate limit for the Claude API?"
_GEMINI_PRIOR = "Which header carries the Gemini API key?"


def test_single_turn_is_a_noop() -> None:
    """No prior turns → the question is returned verbatim (existing paths unchanged)."""
    assert build_contextual_query("How can I raise it?", []) == "How can I raise it?"


@pytest.mark.parametrize(
    "followup",
    [
        "How can I raise it?",
        "Is there another way to provide it?",
        "How do I see the others?",
        "What about it?",
        "And the rest?",
    ],
)
def test_anaphoric_followup_prepends_product_antecedent(followup: str) -> None:
    """An anaphoric follow-up that names no product is prefixed with the prior turn."""
    assert build_contextual_query(followup, [_CLAUDE_PRIOR]) == f"{_CLAUDE_PRIOR} {followup}"


def test_self_contained_product_question_is_unchanged() -> None:
    """A follow-up that names a product is self-contained — never rewritten."""
    q = "What about the Codex --model flag?"
    assert build_contextual_query(q, [_CLAUDE_PRIOR]) == q


def test_self_contained_offdomain_sentence_is_not_contextualized() -> None:
    """Adversarial R1: a full off-topic sentence names no product but carries no
    anaphora/ellipsis, so it must NOT borrow the prior topic — it reaches the refusal."""
    for offtopic in ["What's the weather in Paris tomorrow?", "How do I reverse a list in Python?"]:
        assert build_contextual_query(offtopic, [_CLAUDE_PRIOR]) == offtopic


def test_anaphoric_followup_with_no_product_prior_is_unchanged() -> None:
    """Anaphora but no product antecedent to resolve against → leave it alone."""
    assert build_contextual_query("How can I raise it?", ["hello there", "thanks"]) == (
        "How can I raise it?"
    )


def test_most_recent_product_turn_is_the_antecedent() -> None:
    """With two product priors, the MOST-RECENT (list head) is chosen; intervening
    non-product turns are skipped to reach a product antecedent."""
    # most-recent first: a greeting, then Gemini, then Claude
    priors = ["hi there", _GEMINI_PRIOR, _CLAUDE_PRIOR]
    out = build_contextual_query("How can I raise it?", priors)
    assert out == f"{_GEMINI_PRIOR} How can I raise it?"


def test_intervening_nonproduct_turn_is_skipped() -> None:
    priors = ["thanks!", _CLAUDE_PRIOR]  # most-recent 'thanks' is not a product turn
    out = build_contextual_query("How can I raise it?", priors)
    assert out == f"{_CLAUDE_PRIOR} How can I raise it?"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("How can I raise it?", True),
        ("Is there another way to provide it?", True),
        ("How do I see the others?", True),
        ("What about that?", True),
        ("And the rest?", True),
        ("how about them", True),
        ("What is the weather in Paris?", False),
        ("How do I reverse a list in Python?", False),
        ("What is the rate limit?", False),
    ],
)
def test_is_anaphoric_followup(text: str, expected: bool) -> None:
    assert is_anaphoric_followup(text) is expected


# ---------------------------------------------------------------------------
# recent_user_questions — DB read
# ---------------------------------------------------------------------------


async def _add_message(session, session_id, *, role, content, created_at) -> None:
    session.add(
        Message(
            session_id=session_id,
            role=role,
            content=content,
            normalized_query=content,
            domain=None,
            intent=None,
            created_at=created_at,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_recent_user_questions_orders_most_recent_first(session) -> None:
    sid = uuid.uuid4()
    now = datetime.now(UTC)
    await _add_message(session, sid, role=MessageRole.user, content="first", created_at=now)
    await _add_message(
        session, sid, role=MessageRole.user, content="second", created_at=now + timedelta(seconds=1)
    )
    got = await recent_user_questions(session, sid, limit=6)
    assert got == ["second", "first"]


@pytest.mark.asyncio
async def test_recent_user_questions_excludes_assistant_rows(session) -> None:
    """Adversarial finding #2: an assistant answer contains product tokens; selecting
    it would mask a missing role filter and pollute the antecedent."""
    sid = uuid.uuid4()
    now = datetime.now(UTC)
    await _add_message(session, sid, role=MessageRole.user, content="user-q", created_at=now)
    await _add_message(
        session,
        sid,
        role=MessageRole.assistant,
        content="assistant-answer",
        created_at=now + timedelta(seconds=1),
    )
    got = await recent_user_questions(session, sid, limit=6)
    assert got == ["user-q"]


@pytest.mark.asyncio
async def test_recent_user_questions_scopes_to_session(session) -> None:
    sid_a, sid_b = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    await _add_message(session, sid_a, role=MessageRole.user, content="a-q", created_at=now)
    await _add_message(session, sid_b, role=MessageRole.user, content="b-q", created_at=now)
    assert await recent_user_questions(session, sid_a, limit=6) == ["a-q"]
    assert await recent_user_questions(session, sid_b, limit=6) == ["b-q"]


@pytest.mark.asyncio
async def test_recent_user_questions_respects_limit(session) -> None:
    sid = uuid.uuid4()
    now = datetime.now(UTC)
    for i in range(5):
        await _add_message(
            session,
            sid,
            role=MessageRole.user,
            content=f"q{i}",
            created_at=now + timedelta(seconds=i),
        )
    got = await recent_user_questions(session, sid, limit=2)
    assert got == ["q4", "q3"]
