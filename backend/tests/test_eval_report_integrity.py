"""#598 Step 2b: report integrity before Step 3's one paid judged run.

Written before the implementation (test designer), from the plan's REVISION 2
(items A-H). Free and hermetic: the SQLite catalog and fake clients. The Postgres
half (a DB error that poisons the transaction) is ``test_eval_report_integrity_pg.py``.

The contract the builder writes to:

* ``JudgedCase.harness_error``: ``None``, or ``{"stage", "type", "message"}``, with
  ``stage`` one of ``answer``, ``report`` or ``grade`` for a per-case failure. Every
  failure that is not "an answer call failed" or "another model wrote an answer"
  lands here and never in ``answer_errored`` (B). These tests also accept a list of
  such entries.
* ``model_calls`` entries gain ``failed`` (bool) and ``error_type`` (the exception's
  type name; ``None`` for a call that returned) (C).
* ``grader_calls`` entries gain ``provider`` and ``model``: what served the call (F).
* ``JudgedCase.cache_origin``: on a cache hit, ``{"case_id", "turn", "evidence"}`` of
  the turn that wrote the cached answer; ``None`` otherwise. The hit's own
  retrieval fields stay empty (G).
* The report gains ``in_progress``: the pass being run when it was written mid-pass,
  ``None`` once the run finished (E).
* The harness-integrity check prints a gate failure line holding the word
  "harness" and the case id, once per pass; the fallback pass's line also says
  "fallback".

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import json
import os
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.answer import orchestrator as orchestrator_mod
from app.cache import answer_cache as answer_cache_mod
from app.cost.call_site import CallSite, get_call_site
from app.llm.errors import LLMUnavailable
from app.llm.types import LLMResult
from app.models.enums import Confidence
from app.retrieval.types import RetrievalResult, VectorDegrade
from tests.eval import judge as judge_mod
from tests.eval import runner as runner_mod
from tests.eval.cases import EvalCase, load_cases
from tests.eval.paths import GOLDEN_PATH
from tests.eval.runner import JudgedCase
from tests.test_eval_fallback_wiring import _Answers, _DownFor, _run_cases
from tests.test_eval_report_fields import (
    ANSWER_MODEL,
    EXPECT,
    FALLBACK,
    _exact_first_retriever,
    _fillers,
    _gate,
    _golden,
    _Judge,
    _judged_case,
    _Model,
    _run,
    _spy_passes,
    _two_pass_settings,
)
from tests.test_eval_report_round1 import _CondenseBy, _judge_with

ANSWERED = "The limit is 50 requests per minute [1]."
JUDGE = "anthropic/claude-haiku-5.5"
KEY = "or-SECRET-router-2468013579"


def _harness(case: Any) -> list[dict[str, Any]]:
    """A case's harness errors as a list (a dict, a list of dicts, or none)."""
    value = (
        case.get("harness_error")
        if isinstance(case, dict)
        else getattr(case, "harness_error", None)
    )
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _stages(case: Any) -> list[str]:
    return [h.get("stage") for h in _harness(case)]


def _comparable(case: JudgedCase) -> dict[str, Any]:
    """A case's report minus what differs between two healthy runs (durations,
    and the fake grader's call-numbered rationales)."""
    data = case.as_dict()
    data.pop("rationale", None)
    data["grader_calls"] = [
        {k: v for k, v in c.items() if k not in ("duration_seconds", "rationale")}
        for c in data["grader_calls"]
    ]
    return data


def _answer_calls(case: JudgedCase, turn: str | None = None) -> list[dict[str, Any]]:
    return [
        c for c in case.model_calls if c["site"] == "answer" and (turn is None or c["turn"] == turn)
    ]


class _SiteFails(_Model):
    """``_Model``, except calls at ``site`` whose prompt passes ``when`` raise ``exc``."""

    def __init__(self, reply: Any, *, site: CallSite, exc: BaseException, when: Any = None) -> None:
        super().__init__(reply)
        self.site, self.exc = site, exc
        self.when = when or (lambda _user: True)

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if get_call_site() is self.site and self.when(user):
            raise self.exc
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


def _asks(question: str) -> Any:
    """True for the answer prompt that asks exactly ``question``."""
    return lambda user: f"Question: {question}\n" in user


def _pair() -> list[EvalCase]:
    golden = _golden()
    return [golden["claude_api_lit_ratelimit"], golden["codex_lit_modelflag"]]


def _solo_second(monkeypatch: pytest.MonkeyPatch) -> JudgedCase:
    """The second case of ``_pair`` run on its own, healthy: the baseline a case
    after a failure must equal."""
    (solo,) = _run(monkeypatch, [_pair()[1]], _Model("An answer [1]."))
    assert solo.score == 4 and solo.error is None  # partner: the baseline is healthy
    return solo


# --- A. one case's failure never aborts the pass --------------------------------------


def test_a_keyerror_from_the_answer_client_errors_its_case_and_the_pass_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: an exception that is not ``OrchestratorError``/``LLMUnavailable``
    escaping ``ask()`` aborts the pass (every later case is lost), or the failed
    answer call is not recorded and does not error its case (A, C)."""
    first, second = _pair()

    def reply(prompt: str) -> str:
        if f"Question: {first.question}\n" in prompt:
            raise KeyError("candidates")
        return "An answer [1]."

    judged = _run(monkeypatch, [first, second], _Model(reply))
    assert [j.case_id for j in judged] == [first.id, second.id]
    broken, healthy = judged
    assert broken.answer_errored is True and broken.score is None
    (call,) = _answer_calls(broken)
    assert call["failed"] is True and call["error_type"] == "KeyError"
    assert "answer call failed" in (broken.error or "")
    # Partner: the case after it is graded exactly as when run alone.
    assert _comparable(healthy) == _comparable(_solo_second(monkeypatch))


class _HugeScoreFor(_Judge):
    """A grader whose verdict for one question carries a score too large for a
    float (``int`` 10**400: ``math.isfinite`` raises ``OverflowError``)."""

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    async def complete(self, **kwargs: Any) -> LLMResult:
        if self.question in kwargs["user"]:
            self.calls += 1
            return LLMResult(
                text='{"score": 1' + "0" * 400 + ', "rationale": "huge"}',
                input_tokens=1,
                output_tokens=1,
                model="fake-judge",
                provider="router",
            )
        return await super().complete(**kwargs)


def test_a_grader_score_too_large_for_a_float_is_a_grade_harness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the ``OverflowError`` from a grader score of 10**400 aborts the
    pass, or is not recorded as a ``grade`` harness error on its case (A, B)."""
    first, second = _pair()
    judged = _run(
        monkeypatch, [first, second], _Model("An answer [1]."), judge=_HugeScoreFor(first.question)
    )
    assert [j.case_id for j in judged] == [first.id, second.id]
    broken, healthy = judged
    assert broken.answer_errored is False  # the answer itself was fine
    assert broken.score is None
    assert "grade" in _stages(broken)
    assert any(h["type"] == "OverflowError" for h in _harness(broken))
    assert broken.outcome == "answer" and broken.raw_reply == "An answer [1]."  # records kept
    assert healthy.score == 4 and not _harness(healthy)


class _BrokenCondense(_Model):
    """The condense call returns an object without ``text``: the attributing
    client sees provider and model, then fails reading the text."""

    async def complete(self, *, system: str, user: str, max_tokens: int, temperature: float) -> Any:
        if get_call_site() is CallSite.condense:

            class _NoText:
                provider = "gemini"
                model = ANSWER_MODEL

            return _NoText()
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


def test_a_broken_helper_result_keeps_the_call_pairing_and_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a call that recorded its provider but not its reply leaves
    ``calls`` and ``replies`` unequal, so the strict ``zip`` raises ``ValueError``
    and aborts the pass (A, C: the pairing must stay valid)."""
    golden = _golden()
    followup, literal = golden["followup_claude_api_raise"], golden["claude_api_lit_ratelimit"]
    judged = _judge_with(
        monkeypatch, [followup, literal], _BrokenCondense(lambda p: "An answer [1].")
    )
    assert [j.case_id for j in judged] == [followup.id, literal.id]
    broken, healthy = judged
    # The condense failure is a helper failure: reported, never an answer error.
    assert broken.answer_errored is False
    assert any(c["site"] == "condense" and c.get("failed") for c in broken.model_calls)
    assert healthy.score == 4 and healthy.error is None


# --- C. failed calls are recorded and an answer-site failure errors the case -----------


def test_a_failed_208_retry_answer_call_errors_the_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the #208 retry's ``LLMUnavailable`` (swallowed by the orchestrator,
    which keeps its first refusal) disappears: the case is graded as a normal
    decline, the failed call is missing from ``model_calls``, or it is reported as
    a ``retry_reply`` (C)."""
    _exact_first_retriever(monkeypatch)
    replies = iter(["No markers."])

    def reply(_prompt: str) -> str:
        try:
            return next(replies)
        except StopIteration:
            raise LLMUnavailable("provider down on the retry") from None

    (judged,) = _run(monkeypatch, [_golden()["codex_lit_modelflag"]], _Model(reply))
    calls = _answer_calls(judged, "final")
    assert [(c["failed"], c["error_type"]) for c in calls] == [
        (False, None),
        (True, "LLMUnavailable"),
    ]
    assert judged.answer_errored is True and judged.score is None
    assert "answer call failed" in (judged.error or "") and "final" in (judged.error or "")
    assert judged.raw_reply == "No markers."
    assert judged.retry_reply is None and judged.retry_kept is False


def test_a_retry_that_answers_uncited_is_still_graded_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner of the failed retry. Red when: recording failed calls marks a retry
    that RETURNED (uncited) as failed, so a healthy decline is errored."""
    _exact_first_retriever(monkeypatch)
    replies = iter(["No markers.", "Still no markers."])
    (judged,) = _run(
        monkeypatch, [_golden()["codex_lit_modelflag"]], _Model(lambda _p: next(replies))
    )
    assert [c["failed"] for c in _answer_calls(judged, "final")] == [False, False]
    assert judged.answer_errored is False and judged.error is None
    assert judged.score == 4 and judged.retry_reply == "Still no markers."


def test_a_failed_answer_after_a_successful_condense_keeps_both_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the calls of a turn that raised are lost (harvested only after
    ``ask`` returns), so the case says neither which helper ran nor which call
    failed (C: harvest in a ``finally``)."""
    case = _golden()["followup_claude_api_raise"]

    def reply(prompt: str) -> str:
        if "CONDENSED" in prompt:
            raise LLMUnavailable("provider down on the final answer")
        return ANSWERED

    model = _CondenseBy(
        reply,
        "CONDENSED: how can I raise the Claude API rate limit?",
        provider="gemini",
        model=ANSWER_MODEL,
    )
    (judged,) = _judge_with(monkeypatch, [case], model)
    got = [(c["turn"], c["site"], c["failed"]) for c in judged.model_calls]
    assert got == [
        ("history", "answer", False),
        ("final", "condense", False),
        ("final", "answer", True),
    ]
    assert judged.answer_errored is True and judged.score is None
    assert "answer call failed" in (judged.error or "") and "final" in (judged.error or "")


def test_a_failed_history_answer_errors_the_case_with_its_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a follow-up whose HISTORY answer call failed is errored without
    the failed call in ``model_calls``, or without naming the history turn (C)."""
    case = _golden()["followup_claude_api_raise"]
    model = _SiteFails(
        ANSWERED,
        site=CallSite.answer,
        exc=LLMUnavailable("provider down on the history turn"),
        when=_asks(case.history[0]),
    )
    (judged,) = _run(monkeypatch, [case], model)
    (call,) = judged.model_calls
    assert (call["turn"], call["site"], call["failed"]) == ("history", "answer", True)
    assert call["error_type"] == "LLMUnavailable"
    assert judged.answer_errored is True and judged.score is None
    assert "answer call failed" in (judged.error or "") and "history" in (judged.error or "")


def test_a_failed_condense_call_is_reported_not_gated_and_never_by_none_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a condense call that raised (the orchestrator swallows it and
    retrieves with the concatenation) is missing from ``model_calls``, is not
    reported in the pass's lines, errors or gates the case, or is printed as
    "by None None" (C)."""
    case = _golden()["followup_claude_api_raise"]
    model = _SiteFails(
        lambda p: "Set CLAUDE_API_RATE_LIMIT to raise it [1]." if case.question in p else ANSWERED,
        site=CallSite.condense,
        exc=LLMUnavailable("condense provider down"),
    )
    (judged,) = _judge_with(monkeypatch, [case], model)
    final = [(c["site"], c["failed"]) for c in judged.model_calls if c["turn"] == "final"]
    assert final == [("condense", True), ("answer", False)]
    assert judged.answer_errored is False and judged.error is None and not _harness(judged)
    assert judged.score == 4
    summary = runner_mod._summarize([case], {}, [judged], judge_available=True, judgeable=[case])
    lines = runner_mod.pass_report_lines(summary["judge"])
    assert any(case.id in ln and "condense" in ln for ln in lines), lines
    assert not any("None None" in ln for ln in lines), lines
    assert _gate([*_fillers(), judged], [case]) == []


def test_a_failed_alias_check_is_reported_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the alias-intent call that raised (swallowed; the question keeps
    its refusal) leaves no record, errors the case, or prints "None None" (C)."""
    case = dataclasses.replace(
        _golden()["citevyn_lit_free"],
        id="citevyn_two_word_alias",
        question="Is site win free to use right now?",
        expected_facts=(),
    )
    model = _SiteFails(ANSWERED, site=CallSite.alias_intent, exc=LLMUnavailable("alias down"))
    (judged,) = _judge_with(monkeypatch, [case], model)
    assert [(c["site"], c["failed"]) for c in judged.model_calls] == [("alias_intent", True)]
    assert judged.answer_errored is False and not _harness(judged)
    assert judged.score == 4
    summary = runner_mod._summarize([case], {}, [judged], judge_available=True, judgeable=[case])
    lines = runner_mod.pass_report_lines(summary["judge"])
    assert any(case.id in ln and "alias_intent" in ln for ln in lines), lines
    assert not any("None None" in ln for ln in lines), lines


# --- B. what is an answer error and what is a harness error --------------------------


def test_an_embedding_outage_before_generation_is_a_harness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: an ``OrchestratorError`` raised with no answer call (the embedding
    provider was down, so retrieval found nothing) is counted as an answer error;
    the owner's rule makes it a harness error (B)."""
    first, second = _pair()
    real = orchestrator_mod._default_retriever

    class _Outage:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def retrieve(self, question: str, **kwargs: Any) -> RetrievalResult:
            if question == first.question:
                return RetrievalResult(hits=[], vector_degrade=VectorDegrade.unavailable)
            return await self._inner.retrieve(question, **kwargs)

        async def retrieve_multi(self, *args: Any, **kwargs: Any) -> RetrievalResult:
            return await self._inner.retrieve_multi(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator_mod, "_default_retriever", lambda *a, **k: _Outage(real(*a, **k))
    )
    model = _Model("An answer [1].")
    broken, healthy = _run(monkeypatch, [first, second], model)
    assert broken.model_calls == []  # partner: no answer call was made
    assert broken.answer_errored is False
    assert [(h["stage"], h["type"]) for h in _harness(broken)] == [("answer", "OrchestratorError")]
    assert broken.score is None
    assert healthy.score == 4 and not _harness(healthy)
    summary = runner_mod._summarize(
        _pair(), {}, [broken, healthy], judge_available=True, judgeable=_pair()
    )
    assert summary["judge"]["answer_errors"] == {"total": 0, "by_kind": {}}


def _report_read_fails_once(monkeypatch: pytest.MonkeyPatch) -> None:
    real = runner_mod._final_turn
    calls = {"n": 0}

    async def _flaky(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away")
        return await real(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "_final_turn", _flaky)


def test_a_report_read_failure_is_a_harness_error_and_the_answer_is_still_graded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a failed read of the report fields is classed as an answer error
    (the answer is fine; only the harness failed), or the case is no longer graded
    (B: graded when its answer exists and attribution permits)."""
    _report_read_fails_once(monkeypatch)
    first, second = _pair()
    broken, healthy = _run(monkeypatch, [first, second], _Model(ANSWERED))
    assert broken.answer_errored is False
    assert [(h["stage"], h["type"]) for h in _harness(broken)] == [("report", "RuntimeError")]
    assert "database went away" in _harness(broken)[0]["message"]
    assert broken.score == 4 and broken.fact_coverage == 1.0
    assert healthy.score == 4 and not _harness(healthy)


def test_a_report_read_failure_still_applies_the_wrong_model_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner. Red when: a report-read failure skips attribution, so an answer
    another model wrote is graded as the expected model's (B)."""
    _report_read_fails_once(monkeypatch)
    (case,) = _run(
        monkeypatch,
        [_golden()["claude_api_lit_ratelimit"]],
        _Model(ANSWERED, provider="router", model=FALLBACK),
    )
    assert case.answer_errored is True and case.score is None
    assert "answer by router" in (case.error or "")
    assert _stages(case) == ["report"]


def _plant_cache_hit(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    """The pass's FIRST cache read returns an entry no turn of this pass wrote."""
    real = answer_cache_mod.PostgresAnswerCacheStore.get
    state = {"planted": False}

    async def _get(self: Any, *, cache_key: str) -> Any:
        if not state["planted"]:
            state["planted"] = True
            now = datetime.now(UTC)
            return answer_cache_mod.CachedAnswer(
                answer=answer,
                citations=[],
                confidence=Confidence.high,
                source_version_hash="planted",
                answer_policy_version="planted",
                created_at=now,
                ttl_expires_at=now + timedelta(days=1),
            )
        return await real(self, cache_key=cache_key)

    monkeypatch.setattr(answer_cache_mod.PostgresAnswerCacheStore, "get", _get)


def test_a_cache_hit_from_an_unknown_writer_is_a_harness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a cache hit whose key no turn of this pass wrote is counted as an
    answer error (no model call failed and no other model is known to have
    written it), or is graded as the expected model's answer (B, G)."""
    _plant_cache_hit(monkeypatch, "Planted [1].")
    first, second = _pair()
    broken, healthy = _run(monkeypatch, [first, second], _Model("An answer [1]."))
    assert broken.outcome == "cache_hit"  # partner: the planted entry was served
    assert broken.answer_errored is False
    assert _harness(broken), "an unknown cache writer left no harness error"
    assert broken.score is None
    assert healthy.score == 4 and not _harness(healthy)


# --- D. an errored answer keeps its real no_answer -----------------------------------


def test_a_wrong_model_answer_to_a_refusal_counts_as_a_leak_and_as_errored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: an answer another model wrote to a refusal case is recorded as
    ``no_answer`` (it did answer), so the refusal-leak figures hide it; it must
    count there as what it was AND fail the errored line (D)."""
    refusal = dataclasses.replace(
        _golden()["claude_api_lit_ratelimit"],
        id="refusal_answered_by_fallback",
        kind="refusal",
        expected_facts=(),
        must_not_contain=(),
    )
    (case,) = _run(monkeypatch, [refusal], _Model(ANSWERED, provider="router", model=FALLBACK))
    assert case.outcome == "answer"  # partner: the product did answer
    assert case.answer_errored is True
    assert case.no_answer is False
    summary = runner_mod._summarize(
        [refusal], {}, [case], judge_available=True, judgeable=[refusal]
    )
    assert summary["judge"]["refusal_leaks_judged"] == 1
    assert [e["case_id"] for e in summary["judge"]["refusal_errored"]] == [refusal.id]
    failures = _gate([*_fillers(), case], [refusal])
    assert any("refusal leak" in f for f in failures), failures
    assert any(f"errored: {refusal.id}" in f for f in failures), failures


# --- F. the grader --------------------------------------------------------------------


class _ServedJudge(_Judge):
    """A grader that reports which provider and model served it, optionally
    returning an unparseable verdict."""

    def __init__(
        self, *, provider: str = "router", model: str = "fake-judge", text: str | None = None
    ) -> None:
        super().__init__()
        self.provider, self.model, self.text = provider, model, text

    async def complete(self, **_: Any) -> LLMResult:
        self.calls += 1
        text = self.text or json.dumps({"score": self.score, "rationale": f"r{self.calls}"})
        return LLMResult(
            text=text,
            input_tokens=1,
            output_tokens=1,
            model=self.model,
            provider=self.provider,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("text", [None, "not a verdict"], ids=["parsed", "malformed"])
def test_each_grader_call_stores_its_served_provider_and_model(
    monkeypatch: pytest.MonkeyPatch, text: str | None
) -> None:
    """Red when: a grader call record drops the provider and model that served it,
    including when its verdict could not be parsed (F)."""
    judge = _ServedJudge(model="anthropic/claude-haiku-5.5-20261001", text=text)
    (case,) = _run(
        monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(ANSWERED), judge=judge
    )
    assert case.grader_calls, "no grader call recorded"
    assert all(
        (c.get("provider"), c.get("model")) == ("router", "anthropic/claude-haiku-5.5-20261001")
        for c in case.grader_calls
    ), case.grader_calls


@pytest.mark.parametrize(
    ("served", "self_graded"),
    [
        ("google/gemini-3.6-flash", True),  # vendor prefix stripped
        ("gemini-3.6-flash-2026-09-30", True),  # date suffix matched
        ("anthropic/claude-haiku-5.5", False),
        ("google/gemini-3.6-flash-lite", False),  # a longer id is another model
    ],
)
def test_a_grader_call_served_by_the_answer_model_is_a_harness_error(
    monkeypatch: pytest.MonkeyPatch, served: str, self_graded: bool
) -> None:
    """Red when: a grader call that OpenRouter served with the run's own answer
    model (named with a vendor prefix or a date suffix) is not a ``grade`` harness
    error, or a different model (a longer id) is flagged (F)."""
    (case,) = _run(
        monkeypatch,
        [_golden()["claude_api_lit_ratelimit"]],
        _Model(ANSWERED),
        judge=_ServedJudge(model=served),
    )
    assert case.answer_errored is False
    assert ("grade" in _stages(case)) is self_graded
    if not self_graded:
        assert case.score == 4 and not _harness(case)  # partner: graded as before


# --- run-level helpers ----------------------------------------------------------------


def _run_settings(**overrides: Any) -> Any:
    return _two_pass_settings(eval_judge_model=JUDGE, eval_judge_timeout_seconds=60.0, **overrides)


def _clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    main: Any = None,
    fallback: Any = None,
    grader: Any = None,
) -> dict[str, list[Any]]:
    """Fake every client ``run_eval_async`` builds; record what was built."""
    built: dict[str, list[Any]] = {"main": [], "fallback": [], "grader": []}

    def _build(settings: Any) -> Any:
        if settings.llm_provider == "router" and settings.openrouter_model == JUDGE:
            role, client = "grader", grader or _Judge()
        elif settings.llm_provider == "router":
            role = "fallback"
            client = fallback or _Model(
                ANSWERED, provider="router", model=settings.openrouter_model
            )
        else:
            role, client = "main", main or _Model(ANSWERED)
        built[role].append(client)
        return client

    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "build_metered_llm_client", _build)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    monkeypatch.setattr(judge_mod, "get_llm_client", lambda settings=None: _Judge())
    return built


_IDS = ["claude_api_lit_ratelimit", "codex_lit_modelflag"]


def _ordered_ids() -> list[str]:
    """``_IDS`` in the order the run drives them (golden-file order)."""
    return [c.id for c in load_cases(GOLDEN_PATH) if c.id in _IDS]


def _question(case_id: str) -> str:
    return _golden()[case_id].question


def _report(path: pathlib.Path) -> dict[str, Any]:
    assert path.exists(), f"no report was written at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _case_ids(section: dict[str, Any]) -> list[str]:
    return [c["case_id"] for c in section["cases"]]


@pytest.mark.parametrize(
    ("served", "self_graded"),
    [
        ("openai/gpt-4o-mini-2024-07-18", True),  # the fallback model, dated
        ("google/gemini-3.6-flash", True),  # the main model, vendor-prefixed
        ("openai/gpt-4o-mini-search-preview", False),
    ],
)
def test_self_grading_is_a_harness_error_in_both_passes(
    monkeypatch: pytest.MonkeyPatch, served: str, self_graded: bool
) -> None:
    """Red when: a grader call served by ANY of the run's answer models (main or
    fallback) is not a harness error in both passes, or another model is (F)."""
    _clients(monkeypatch, grader=_ServedJudge(model=served))
    summary = asyncio.run(runner_mod.run_eval_async(ids=_IDS, settings=_run_settings()))
    for section in (summary["judge"], summary["fallback"]["judge"]):
        assert len(section["cases"]) == 2  # partner: both cases ran in each pass
        for case in section["cases"]:
            assert ("grade" in _stages(case)) is self_graded, case


def test_a_judged_run_without_a_grader_model_is_refused_before_any_paid_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a judged run starts without ``CITEVYN_EVAL_JUDGE_MODEL`` (the
    answers would be graded by their own model) or spends anything first: the
    retrieval half (paid embeddings on Postgres), a built client, a model call
    (F, owner decision 2026-11-13)."""
    built = _clients(monkeypatch)
    retrievals: list[Any] = []

    async def _retrieval(*args: Any, **kwargs: Any) -> Any:
        retrievals.append(args)
        raise AssertionError("retrieval ran before the refusal")

    monkeypatch.setattr(runner_mod, "evaluate_retrieval", _retrieval)
    settings = _two_pass_settings(eval_judge_model=None, eval_judge_timeout_seconds=None)
    with pytest.raises(SystemExit, match="CITEVYN_EVAL_JUDGE_MODEL"):
        asyncio.run(runner_mod.run_eval_async(ids=_IDS, settings=settings))
    assert retrievals == []
    assert built == {"main": [], "fallback": [], "grader": []}


def test_an_unjudged_run_without_a_grader_model_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partner. Red when: the refusal also stops a run that judges nothing."""
    _clients(monkeypatch)
    settings = _two_pass_settings(eval_judge_model=None, eval_judge_timeout_seconds=None)
    summary = asyncio.run(runner_mod.run_eval_async(ids=_IDS, settings=settings, with_judge=False))
    assert summary["judge"]["available"] is False


def test_an_invalid_panel_size_is_refused_before_the_paid_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: ``CITEVYN_EVAL_JUDGE_PANEL`` is read only inside the first case's
    grading, after its answer was paid for (and the ``ValueError`` then aborts the
    pass) (F)."""
    main = _Model(ANSWERED)
    built = _clients(monkeypatch, main=main)
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_PANEL", "three")
    with pytest.raises((SystemExit, ValueError), match="CITEVYN_EVAL_JUDGE_PANEL"):
        asyncio.run(runner_mod.run_eval_async(ids=_IDS, settings=_run_settings()))
    assert main.answer_prompts == []
    assert built["main"] == [] and built["fallback"] == []


# --- E. the report is saved after every case, atomically -----------------------------


def test_the_report_is_replaced_atomically_from_a_temp_file_in_its_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: the report is written in place (a write cut short leaves a
    truncated file) instead of a synced temp file in the same folder moved over
    it with ``os.replace`` (E)."""
    report = tmp_path / "reports" / "eval_report.json"
    moves: list[tuple[str, str]] = []
    synced: list[int] = []
    real_replace, real_fsync = os.replace, os.fsync

    def _replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        moves.append((os.fspath(src), os.fspath(dst)))
        real_replace(src, dst, *args, **kwargs)

    def _fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "replace", _replace)
    monkeypatch.setattr(os, "fsync", _fsync)
    runner_mod.write_report(report, {"ok": 1}, _run_settings())
    assert _report(report) == {"ok": 1}
    assert synced, "the temp file was not synced before the move"
    ((src, dst),) = moves
    assert pathlib.Path(dst) == report
    assert pathlib.Path(src).parent == report.parent and pathlib.Path(src) != report


@pytest.mark.parametrize("cut_at", ["fsync", "replace"])
def test_an_interrupted_write_keeps_the_previous_valid_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, cut_at: str
) -> None:
    """Red when: a second write cut short (before the move) damages the report the
    first write left: it must still be the first report, valid JSON (E)."""
    report = tmp_path / "eval_report.json"
    settings = _run_settings()
    runner_mod.write_report(report, {"cases": ["first"]}, settings)

    def _cut(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(f"cut at {cut_at}")

    monkeypatch.setattr(os, cut_at, _cut)
    with contextlib.suppress(OSError):
        runner_mod.write_report(report, {"cases": ["first", "second"]}, settings)
    assert _report(report) == {"cases": ["first"]}


class _InterruptAt(_Model):
    """Answers like ``_Model``, but raises ``exc`` on the answer call that asks
    ``question``: the run is cut off there."""

    def __init__(self, question: str, exc: BaseException, **kwargs: Any) -> None:
        super().__init__(ANSWERED, **kwargs)
        self.question, self.exc = question, exc

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        if get_call_site() is CallSite.answer and f"Question: {self.question}\n" in user:
            raise self.exc
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


@pytest.mark.parametrize(
    "exc", [KeyboardInterrupt(), asyncio.CancelledError()], ids=["ctrl-c", "cancelled"]
)
def test_a_run_cut_off_mid_pass_keeps_every_finished_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, exc: BaseException
) -> None:
    """Red when: the report is written only after a whole pass, so a run cut off
    (Ctrl-C, a cancelled task, a step timeout) during the main pass keeps nothing,
    or the report does not say which pass was in progress (E)."""
    first, second = _ordered_ids()
    _clients(monkeypatch, main=_InterruptAt(_question(second), exc))
    report = tmp_path / "eval_report.json"
    with pytest.raises(type(exc)):
        asyncio.run(
            runner_mod.run_eval_async(ids=_IDS, settings=_run_settings(), report_path=report)
        )
    written = _report(report)
    assert _case_ids(written["judge"]) == [first]
    assert written["in_progress"] == "main"
    assert written["passes_completed"] == []


def test_a_run_cut_off_in_the_fallback_pass_keeps_the_main_pass_and_its_finished_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red when: a run cut off during the fallback pass keeps only the main pass,
    losing the fallback answers already paid for (E)."""
    first, second = _ordered_ids()
    fallback = _InterruptAt(
        _question(second), KeyboardInterrupt(), provider="router", model=FALLBACK
    )
    _clients(monkeypatch, fallback=fallback)
    report = tmp_path / "eval_report.json"
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            runner_mod.run_eval_async(ids=_IDS, settings=_run_settings(), report_path=report)
        )
    written = _report(report)
    assert _case_ids(written["judge"]) == [first, second]
    assert _case_ids(written["fallback"]["judge"]) == [first]
    assert written["in_progress"] == "fallback"
    assert written["passes_completed"] == ["main"]


def test_a_finished_run_says_nothing_is_in_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Partner. Red when: a complete report still says a pass is in progress, or
    the per-case writes change what the final report holds."""
    _clients(monkeypatch)
    report = tmp_path / "eval_report.json"
    summary = asyncio.run(
        runner_mod.run_eval_async(ids=_IDS, settings=_run_settings(), report_path=report)
    )
    written = _report(report)
    assert written.get("in_progress") is None
    assert written["passes_completed"] == ["main", "fallback"]
    assert _case_ids(written["judge"]) == _case_ids(written["fallback"]["judge"]) == _ordered_ids()
    assert all(c["score"] == 4 and c["error"] is None for c in summary["judge"]["cases"])


def _main(report: pathlib.Path) -> int:
    return runner_mod.main(["--ids", ",".join(_IDS), "--report", str(report), "--quiet"])


def test_a_session_teardown_failure_keeps_both_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: the main pass's DB session failing to close loses that pass's
    answers (they were collected inside the closing context) or the fallback pass,
    or the failure is silent: it must be a harness error that fails the run (E)."""
    settings = _run_settings()
    _clients(monkeypatch)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)
    real = runner_mod._judge_session
    state = {"closed": 0}

    @contextlib.asynccontextmanager
    async def _fails_on_close(s: Any, *, postgres: bool) -> Any:
        async with real(s, postgres=postgres) as session:
            yield session
        state["closed"] += 1
        if state["closed"] == 1:
            raise RuntimeError("session teardown failed")

    monkeypatch.setattr(runner_mod, "_judge_session", _fails_on_close)
    report = tmp_path / "eval_report.json"
    code = _main(report)
    out = capsys.readouterr().out
    written = _report(report)
    assert _case_ids(written["judge"]) == _ordered_ids()
    assert _case_ids(written["fallback"]["judge"]) == _ordered_ids()
    assert "session teardown failed" in json.dumps(written)
    assert code == 1
    assert any("harness" in ln for ln in out.splitlines() if ln.startswith("  - ")), out


class _FailsToClose(_Model):
    async def aclose(self) -> None:
        raise RuntimeError("client close failed")


def test_a_client_cleanup_failure_keeps_both_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: a client whose ``aclose`` raises makes the run crash after both
    passes, so the final report (and the gate) never happen; it must be a harness
    error that does not drop results (E)."""
    settings = _run_settings()
    _clients(monkeypatch, main=_FailsToClose(ANSWERED))
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)
    report = tmp_path / "eval_report.json"
    code = _main(report)
    out = capsys.readouterr().out
    written = _report(report)
    assert written["passes_completed"] == ["main", "fallback"]
    assert _case_ids(written["fallback"]["judge"]) == _ordered_ids()
    assert "client close failed" in json.dumps(written)
    assert code == 1
    assert any("harness" in ln for ln in out.splitlines() if ln.startswith("  - ")), out


# --- B/E. the harness-integrity check, in both passes ----------------------------------


def _with_harness(case_id: str, message: str = "database went away") -> JudgedCase:
    case = _judged_case(case_id, 5)
    case.harness_error = {"stage": "report", "type": "RuntimeError", "message": message}  # type: ignore[attr-defined]
    return case


def _gate_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith("  - ")]


@pytest.mark.parametrize("broken_pass", ["main", "fallback"])
def test_a_harness_error_fails_the_run_in_either_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    broken_pass: str,
) -> None:
    """Red when: a harness error in either pass is only recorded (the run passes
    on an incomplete record), is checked in one pass only, or fails an ANSWER gate
    instead of the separate harness-integrity check (B). The report keeps it."""
    healthy, broken = _judged_case("healthy_case", 5), _with_harness("broken_case")
    passes = [[healthy, broken], [healthy, _judged_case("broken_case", 5)]]
    if broken_pass == "fallback":
        passes.reverse()
    _spy_passes(monkeypatch, passes, _run_settings())
    report = tmp_path / "r.json"
    code = runner_mod.main(
        ["--ids", "claude_api_lit_ratelimit", "--report", str(report), "--quiet"]
    )
    lines = _gate_lines(capsys.readouterr().out)
    assert code == 1, lines
    named = [ln for ln in lines if "broken_case" in ln]
    assert named and all("harness" in ln for ln in named), lines
    assert all(("fallback" in ln) is (broken_pass == "fallback") for ln in named), lines
    assert not any("errored: broken_case" in ln for ln in lines), lines  # not an answer gate
    written = _report(report)
    section = written["judge"] if broken_pass == "main" else written["fallback"]["judge"]
    (stored,) = [c for c in section["cases"] if c["case_id"] == "broken_case"]
    assert _stages(stored) == ["report"]


def test_a_run_without_harness_errors_passes_the_harness_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Partner. Red when: the harness check fails a run that has no harness error."""
    _spy_passes(
        monkeypatch,
        [[_judged_case("healthy_case", 5)], [_judged_case("healthy_case", 5)]],
        _run_settings(),
    )
    code = runner_mod.main(
        ["--ids", "claude_api_lit_ratelimit", "--report", str(tmp_path / "r.json"), "--quiet"]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert not any("harness" in ln for ln in _gate_lines(out))


def test_a_printed_harness_line_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: a harness error's message carrying a configured key reaches the
    CI log in the clear."""
    broken = _with_harness("broken_case", f"401 for key {KEY}")
    _spy_passes(
        monkeypatch,
        [[broken], [dataclasses.replace(broken)]],
        _run_settings(openrouter_api_key=KEY),
    )
    runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--report", str(tmp_path / "r.json")])
    out = capsys.readouterr().out
    assert any("harness" in ln and "broken_case" in ln for ln in _gate_lines(out)), out
    assert KEY not in out


# --- G. cache provenance, by key ------------------------------------------------------


def test_a_cache_hit_records_its_origin_and_the_original_passages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a cache hit does not say which case and turn wrote it, or does not
    carry that turn's passages (Step 3 grades with stored passages), or the hit's
    own retrieval fields are filled from the writer (G)."""
    golden = _golden()
    pair = [golden["citevyn_lit_free"], golden["citevyn_lit_alias_sitewin"]]
    model = _Model("CiteVyn is free right now [1].")
    writer, hit = _run(monkeypatch, pair, model)
    assert len(model.answer_prompts) == 1 and hit.outcome == "cache_hit"  # partner
    assert writer.evidence and getattr(writer, "cache_origin", "missing") is None
    assert getattr(hit, "cache_origin", None) == {
        "case_id": writer.case_id,
        "turn": "final",
        "evidence": writer.evidence,
    }
    assert hit.evidence == [] and hit.resolved_query is None and hit.raw_reply is None
    assert hit.score == 4 and hit.error is None


def test_identical_text_under_different_keys_is_attributed_by_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: attribution is by answer text, so the fallback model writing the
    same text under ANOTHER key marks a correct cached answer as another model's
    (G: a hit is attributed by its own key)."""
    golden = _golden()
    order = [
        golden["claude_api_lit_ratelimit"],  # key A, written by the fallback model
        golden["citevyn_lit_free"],  # key B, same text, written by Gemini
        golden["citevyn_lit_alias_sitewin"],  # a hit on key B
    ]
    primary = _DownFor("gemini", "gemini-3.6-flash", order[0].question)
    judged = _run_cases(monkeypatch, order, primary)
    assert judged[0].error and "answered by" in judged[0].error  # partner
    assert judged[1].score == 5
    assert judged[2].outcome == "cache_hit"
    assert judged[2].error is None and judged[2].score == 5


class _UpOrDown(_Answers):
    """Gemini that fails on the answer calls whose 0-based index is in ``down``."""

    def __init__(self, down: set[int]) -> None:
        super().__init__("gemini", "gemini-3.6-flash")
        self.down, self.answer_calls = down, 0

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        index, self.answer_calls = self.answer_calls, self.answer_calls + 1
        if index in self.down:
            self.calls += 1
            raise LLMUnavailable("primary down for this call")
        return await super().complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


@pytest.mark.parametrize(
    ("down", "graded"),
    [({0}, True), ({1}, False)],
    ids=["fallback-then-gemini", "gemini-then-fallback"],
)
def test_the_last_write_to_a_key_decides_who_wrote_a_hit(
    monkeypatch: pytest.MonkeyPatch, down: set[int], graded: bool
) -> None:
    """Red when: a key written twice (the second time after a cache miss) keeps
    the FIRST writer's verdict, so a hit on an answer Gemini rewrote is refused, or
    one the fallback model rewrote is graded (G: last write wins)."""
    golden = _golden()
    first = golden["citevyn_lit_free"]
    order = [
        first,
        dataclasses.replace(first, id=f"{first.id}_again"),
        golden["citevyn_lit_alias_sitewin"],
    ]
    real = answer_cache_mod.PostgresAnswerCacheStore.get
    reads: dict[str, int] = {}

    async def _second_read_misses(self: Any, *, cache_key: str) -> Any:
        reads[cache_key] = reads.get(cache_key, 0) + 1
        if reads[cache_key] == 2:
            return None  # the twin misses, so it writes the same key again
        return await real(self, cache_key=cache_key)

    monkeypatch.setattr(answer_cache_mod.PostgresAnswerCacheStore, "get", _second_read_misses)
    judged = _run_cases(monkeypatch, order, _UpOrDown(down))
    assert [j.outcome for j in judged] == ["answer", "answer", "cache_hit"]  # partner
    assert sorted(reads.values()) == [3]  # all three read one key
    assert (judged[2].score == 5) is graded
    assert (judged[2].error is None) is graded


def test_a_helper_fallback_does_not_poison_the_cached_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner (green today; guards the key-based rewrite). Red when: a turn whose
    CONDENSE call another model made marks its cached answer as another model's,
    so a later hit on it is refused (owner rule: helper calls never gate)."""
    case = _golden()["followup_claude_api_raise"]
    twin = dataclasses.replace(case, id=f"{case.id}_again")
    condensed = "How can I raise the Claude API rate limit?"
    model = _CondenseBy(
        lambda p: "Set CLAUDE_API_RATE_LIMIT to raise it [1]." if condensed in p else ANSWERED,
        condensed,
        provider="router",
        model=FALLBACK,
    )
    writer, hit = _judge_with(monkeypatch, [case, twin], model)
    assert writer.score == 4 and writer.error is None
    assert hit.outcome == "cache_hit"  # partner: the twin's final turn was a hit
    assert hit.answer_errored is False and hit.error is None and not _harness(hit)
    assert hit.score == 4


# --- H. the comments say "an answer call" ---------------------------------------------


def test_the_comments_describe_the_owner_rule() -> None:
    """Red when: ``runner.py`` still says ANY call by another model errors a case
    (the owner's rule since 2026-11-12: only an answer call does) (H)."""
    source = inspect.getsource(runner_mod)
    assert "another model made one of its calls" not in source
    assert "any of whose calls came from another model" not in source
    assert "an answer call" in source  # partner: the rule is still written down


# --- A partner: a healthy run is graded exactly as before ------------------------------


def test_a_healthy_pass_is_graded_exactly_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partner of every change above. Red when: savepoints, call records or
    per-case writes change a healthy case's grading, its error fields, or add a
    harness error."""
    judged = _run(monkeypatch, _pair(), _Model(ANSWERED))
    assert [j.score for j in judged] == [4, 4]
    assert [j.error for j in judged] == [None, None]
    assert [j.answer_errored for j in judged] == [False, False]
    assert [_harness(j) for j in judged] == [[], []]
    assert all(not c["failed"] and c["error_type"] is None for j in judged for c in j.model_calls)
    assert judged[0].fact_coverage == 1.0 and judged[0].outcome == "answer"
    assert EXPECT == ("gemini", ANSWER_MODEL)
