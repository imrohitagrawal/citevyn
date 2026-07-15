"""LLM-as-judge answer-quality metric (Phase 0, issue #96).

Scores a produced answer 1–5 against an expected gist, using the *configured*
LLM (`get_llm_client`) — Gemini (free) in the dev/eval loop per
``docs/RAG_QUALITY_PLAN.md`` §11a. One call per case; the golden set is tiny
(~20) so a full run stays well under the 30/hr demo limit.

**No silent stubs.** When the configured client is the deterministic
:class:`~app.llm.stub.StubLLMClient` (the no-key path the factory falls back
to), the judge returns ``None`` — an explicit "unavailable" — rather than a
fabricated score. That keeps hermetic CI honest: a run with no real provider
reports "judge did not run", never a meaningless pass. Likewise a malformed
model response raises :class:`JudgeParseError` loudly instead of defaulting to
a middling score.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from typing import Any, cast

from app.core.config import Settings, get_settings
from app.llm.factory import get_llm_client
from app.llm.protocol import LLMClient
from app.llm.stub import StubLLMClient

# Bounded budget for a single judge call. The rubric + answer are short and the
# expected output is a one-line JSON object, so this is generous.
_JUDGE_MAX_TOKENS = 512
_JUDGE_TEMPERATURE = 0.0

_JUDGE_SYSTEM = (
    "You are a strict, impartial grader of a documentation assistant's answers. "
    "You are given a user QUESTION, the EXPECTED gist of a correct answer, and the "
    "assistant's ACTUAL answer. Score how well the ACTUAL answer satisfies the "
    "question relative to the expected gist, on an integer scale of 1 to 5:\n"
    "5 = fully correct and grounded; matches the expected gist.\n"
    "4 = correct but missing a minor detail.\n"
    "3 = partially correct or vague.\n"
    "2 = mostly wrong or unsupported.\n"
    "1 = wrong, empty, a refusal when an answer was expected, or hallucinated.\n"
    "When the EXPECTED gist says the assistant SHOULD decline (out of scope), a "
    "correct refusal scores 5 and a fabricated answer scores 1.\n"
    'Respond with ONLY a JSON object: {"score": <int 1-5>, "rationale": "<one sentence>"}. '
    "No markdown, no code fence, no extra text."
)


class JudgeParseError(RuntimeError):
    """The judge model returned output that could not be parsed into a score."""


@dataclasses.dataclass(frozen=True)
class JudgeVerdict:
    """One judged answer."""

    score: int
    rationale: str

    def as_dict(self) -> dict[str, object]:
        return {"score": self.score, "rationale": self.rationale}


def _build_user_prompt(*, question: str, answer: str, expected_gist: str) -> str:
    return (
        f"QUESTION:\n{question}\n\n"
        f"EXPECTED gist of a correct answer:\n{expected_gist}\n\n"
        f"ACTUAL answer to grade:\n{answer}\n\n"
        'Return only the JSON object {"score": <int 1-5>, "rationale": "<one sentence>"}.'
    )


def parse_verdict(text: str) -> JudgeVerdict:
    """Extract a {score, rationale} verdict from the model's raw output.

    Tolerant of a stray code fence or surrounding prose (grabs the first JSON
    object), but *strict* on the essentials: a missing / non-integer / out-of-
    range score raises :class:`JudgeParseError` rather than being coerced to a
    default, so a broken judge is loud, not silently averaged in.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise JudgeParseError(f"no JSON object in judge output: {text!r}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"invalid JSON in judge output: {text!r}") from exc
    if not isinstance(parsed, dict) or "score" not in parsed:
        raise JudgeParseError(f"judge output missing 'score': {text!r}")
    obj = cast("dict[str, Any]", parsed)
    raw_score = obj["score"]
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        raise JudgeParseError(f"judge score is not numeric: {raw_score!r}")
    # ``json.loads`` accepts the non-standard literals NaN/Infinity; ``int()`` on
    # them raises ValueError/OverflowError. Reject them as a parse error (loud,
    # per-case) instead of letting an uncaught exception abort the whole run.
    if not math.isfinite(raw_score):
        raise JudgeParseError(f"judge score is not finite: {raw_score!r}")
    score = int(raw_score)
    if not 1 <= score <= 5:
        raise JudgeParseError(f"judge score {score} out of range 1-5")
    rationale = str(obj.get("rationale", "")).strip()
    return JudgeVerdict(score=score, rationale=rationale)


async def score_answer_async(
    *,
    question: str,
    answer: str,
    expected_gist: str,
    settings: Settings | None = None,
    client: LLMClient | None = None,
) -> JudgeVerdict | None:
    """Judge one answer. Returns ``None`` when no real LLM provider is configured.

    ``None`` means *the judge could not run* (stub provider) — it is never a
    score. Callers must treat it as "unavailable", not as a pass.
    """
    settings = settings or get_settings()
    client = client or get_llm_client(settings)
    if isinstance(client, StubLLMClient):
        return None
    result = await client.complete(
        system=_JUDGE_SYSTEM,
        user=_build_user_prompt(question=question, answer=answer, expected_gist=expected_gist),
        max_tokens=_JUDGE_MAX_TOKENS,
        temperature=_JUDGE_TEMPERATURE,
    )
    return parse_verdict(result.text)


def score_answer(
    *,
    question: str,
    answer: str,
    expected_gist: str,
    settings: Settings | None = None,
    client: LLMClient | None = None,
) -> JudgeVerdict | None:
    """Synchronous wrapper around :func:`score_answer_async`."""
    import asyncio

    return asyncio.run(
        score_answer_async(
            question=question,
            answer=answer,
            expected_gist=expected_gist,
            settings=settings,
            client=client,
        )
    )
