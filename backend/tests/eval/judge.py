"""LLM-as-judge answer-quality metric + robustness panel (Phase 0 #96; hardened Item 1).

Scores a produced answer 1–5 against an expected gist, using the *configured*
LLM (`get_llm_client`). Two entry points:

* :func:`score_answer_async` — the original SINGLE judge call (temp 0.0,
  deterministic). Kept for backward-compat and the opt-in smoke test; its mean
  is the historical §8a baseline.
* :func:`score_answer_panel_async` — the ROBUST metric (Item 1). A single judge
  can be noisy or over-score a plausible-but-wrong answer, so this:

  1. **Prompt-ensemble panel** — scores with N *distinct rubric framings* at
     temperature 0.0 and takes the MEDIAN. Diversity comes from framing, not
     temperature sampling: temp 0.0 keeps every score reproducible (no run-to-run
     gate flake) while different framings probe rubric-interpretation bias — the
     real noise source (plan-review finding). N is odd so the median is a single
     member (no averaging / rounding ambiguity).
  2. **Adversarial veto** — one skeptical fact-checker pass (temp 0.0) that
     actively hunts for why the answer is wrong/ungrounded. It is NOT folded into
     the median (a lone low vote can never move a median — it would be discarded on
     exactly the plausible-but-wrong case it targets); instead the final score is
     ``min(standard_median, adversarial)`` so the skeptic can VETO an over-scored
     answer. ``contested`` is measured over the standard members only, so it flags
     genuine same-rubric disagreement rather than the constant adversarial gap.

**No silent stubs.** When the configured client is :class:`StubLLMClient` (the
no-key path) the judge returns ``None`` — an explicit "unavailable", never a
fabricated score. A malformed model response raises :class:`JudgeParseError`.

**Budget.** The panel costs ``N + 1`` judge calls per case (default 4). Against the
~26-case golden set that is ~104 judge calls on the opt-in ``--postgres`` judged
run; use a provider without a tight daily cap (OpenRouter, per the eval env).
``CITEVYN_EVAL_JUDGE_PANEL`` caps N (clamped to the available framings) so CI can
trade robustness for cost.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import statistics
from typing import Any, cast

from app.core.config import Settings, get_settings
from app.llm.factory import get_llm_client
from app.llm.protocol import LLMClient
from app.llm.stub import StubLLMClient

from .thresholds import CONTESTED_SPREAD as _CONTESTED_SPREAD

# Bounded budget for a single judge call. The rubric + answer are short and the
# expected output is a one-line JSON object, so this is generous.
_JUDGE_MAX_TOKENS = 512
# Temperature 0.0 for EVERY member: panel diversity comes from distinct rubric
# framings, not sampling noise, so each score is reproducible and the gate never
# flakes run-to-run (plan-review finding on temp-0.4).
_JUDGE_TEMPERATURE = 0.0

_SCALE_RUBRIC = (
    "Score on an integer scale of 1 to 5:\n"
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

# The canonical single-call rubric (also the historical baseline framing).
_JUDGE_SYSTEM = (
    "You are a strict, impartial grader of a documentation assistant's answers. "
    "You are given a user QUESTION, the EXPECTED gist of a correct answer, and the "
    "assistant's ACTUAL answer. Score how well the ACTUAL answer satisfies the "
    "question relative to the expected gist. " + _SCALE_RUBRIC
)

# STANDARD panel framings — same 1-5 scale, DIFFERENT rubric emphasis, so the median
# smooths one framing's interpretation bias rather than sampling noise. Ordered so a
# smaller CITEVYN_EVAL_JUDGE_PANEL takes a sensible prefix; the count must stay ODD.
_STANDARD_FRAMINGS: tuple[str, ...] = (
    _JUDGE_SYSTEM,
    # Framing 2 — information-equivalence emphasis.
    (
        "You grade a documentation assistant by INFORMATION CONTENT. Compare the "
        "assistant's ACTUAL answer to the EXPECTED gist for the QUESTION: does it "
        "convey the same key facts, without missing points or adding unsupported "
        "claims? " + _SCALE_RUBRIC
    ),
    # Framing 3 — user-usefulness emphasis.
    (
        "You grade a documentation assistant from the USER's perspective. For the "
        "QUESTION, would the assistant's ACTUAL answer actually help the user and be "
        "correct, judged against the EXPECTED gist? Reward specific, accurate answers; "
        "penalize vague, wrong, or evasive ones. " + _SCALE_RUBRIC
    ),
)

# ADVERSARIAL framing — a skeptic prompted to REFUTE. Used as a veto floor, never a
# median member (see module docstring).
_ADVERSARIAL_SYSTEM = (
    "You are a skeptical fact-checker auditing a documentation assistant's answer. "
    "You are given the QUESTION, the EXPECTED gist of a correct answer, and the "
    "assistant's ACTUAL answer. Actively look for reasons the ACTUAL answer is WRONG, "
    "unsupported by the expected gist, internally inconsistent, or hallucinated — a "
    "wrong number, an invented detail, a confident answer that does not actually match "
    "the expected gist. Award a high score ONLY if you cannot find a real flaw; when in "
    "doubt, score DOWN. " + _SCALE_RUBRIC
)

_DEFAULT_PANEL_SIZE = 3
_PANEL_ENV_VAR = "CITEVYN_EVAL_JUDGE_PANEL"


class JudgeParseError(RuntimeError):
    """The judge model returned output that could not be parsed into a score."""


@dataclasses.dataclass(frozen=True)
class JudgeVerdict:
    """One judged answer (single-call path)."""

    score: int
    rationale: str

    def as_dict(self) -> dict[str, object]:
        return {"score": self.score, "rationale": self.rationale}


@dataclasses.dataclass(frozen=True)
class PanelVerdict:
    """Aggregate of a prompt-ensemble panel plus an adversarial veto.

    ``score`` is the GATED number: ``min(standard_median, adversarial_score)`` so a
    skeptic that catches a plausible-but-wrong answer can veto an over-scored median.
    """

    score: int
    standard_scores: tuple[int, ...]
    standard_median: int
    adversarial_score: int | None
    spread: int  # max-min over STANDARD members only (same-rubric disagreement)
    contested: bool
    rationale: str

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


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


def panel_size() -> int:
    """Resolve the standard-panel size: ``CITEVYN_EVAL_JUDGE_PANEL`` or the default.

    Clamped to the available framings and forced ODD (an even panel would make the
    median a fractional average).
    """
    raw = os.getenv(_PANEL_ENV_VAR)
    n = _DEFAULT_PANEL_SIZE
    if raw is not None:
        try:
            n = int(raw)
        except ValueError as exc:
            raise ValueError(f"{_PANEL_ENV_VAR} must be an integer, got {raw!r}") from exc
    n = max(1, min(n, len(_STANDARD_FRAMINGS)))
    if n % 2 == 0:  # keep the median unambiguous (n==2 -> 1)
        n -= 1
    return n


async def _score_with_system(
    *,
    system: str,
    question: str,
    answer: str,
    expected_gist: str,
    client: LLMClient,
) -> JudgeVerdict:
    result = await client.complete(
        system=system,
        user=_build_user_prompt(question=question, answer=answer, expected_gist=expected_gist),
        max_tokens=_JUDGE_MAX_TOKENS,
        temperature=_JUDGE_TEMPERATURE,
    )
    return parse_verdict(result.text)


async def score_answer_async(
    *,
    question: str,
    answer: str,
    expected_gist: str,
    settings: Settings | None = None,
    client: LLMClient | None = None,
) -> JudgeVerdict | None:
    """Judge one answer with the SINGLE canonical rubric. ``None`` under the stub.

    The historical Phase-0/1/2 baseline metric; kept for backward-compat and the
    opt-in smoke test.
    """
    settings = settings or get_settings()
    client = client or get_llm_client(settings)
    if isinstance(client, StubLLMClient):
        return None
    return await _score_with_system(
        system=_JUDGE_SYSTEM,
        question=question,
        answer=answer,
        expected_gist=expected_gist,
        client=client,
    )


async def score_answer_panel_async(
    *,
    question: str,
    answer: str,
    expected_gist: str,
    settings: Settings | None = None,
    client: LLMClient | None = None,
) -> PanelVerdict | None:
    """Robust panel score. ``None`` when no real provider is configured (stub).

    Runs ``panel_size()`` standard framings (median) + one adversarial veto; the
    gated ``score`` is ``min(standard_median, adversarial)``. A per-member parse
    failure propagates (loud), matching the single-call contract.
    """
    settings = settings or get_settings()
    client = client or get_llm_client(settings)
    if isinstance(client, StubLLMClient):
        return None
    n = panel_size()
    standard = [
        await _score_with_system(
            system=framing,
            question=question,
            answer=answer,
            expected_gist=expected_gist,
            client=client,
        )
        for framing in _STANDARD_FRAMINGS[:n]
    ]
    adversarial = await _score_with_system(
        system=_ADVERSARIAL_SYSTEM,
        question=question,
        answer=answer,
        expected_gist=expected_gist,
        client=client,
    )
    return aggregate_panel(standard, adversarial)


def aggregate_panel(standard: list[JudgeVerdict], adversarial: JudgeVerdict | None) -> PanelVerdict:
    """Pure aggregation (hermetically testable): median of standard members, vetoed
    by the adversarial floor.

    * ``standard_median`` — median over the (odd-count) standard members: a single
      member's score, so no fractional averaging.
    * ``score`` — ``min(standard_median, adversarial)`` when an adversarial member
      ran, else the median. The skeptic can only pull the score DOWN, which is the
      whole point (catch an over-scored plausible-but-wrong answer).
    * ``spread`` / ``contested`` — computed over the STANDARD members only, so they
      flag same-rubric disagreement, not the adversarial's built-in pessimism.
    """
    if not standard:
        raise ValueError("panel needs at least one standard member")
    scores = [v.score for v in standard]
    median = (
        int(statistics.median_low(scores))
        if len(scores) % 2 == 0
        else int(statistics.median(scores))
    )
    spread = max(scores) - min(scores)
    adv_score = adversarial.score if adversarial is not None else None
    final = min(median, adv_score) if adv_score is not None else median
    # Prefer the adversarial's rationale when it vetoed (it explains the flaw);
    # otherwise the rationale of the member whose score IS the reported median (by
    # value, not list position — ``standard`` is unsorted, so an index would describe
    # a different score than ``standard_median``).
    rationale = (
        adversarial.rationale
        if adversarial is not None and adv_score is not None and adv_score < median
        else next(v.rationale for v in standard if v.score == median)
    )
    return PanelVerdict(
        score=final,
        standard_scores=tuple(scores),
        standard_median=median,
        adversarial_score=adv_score,
        spread=spread,
        contested=spread >= _CONTESTED_SPREAD,
        rationale=rationale,
    )


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
