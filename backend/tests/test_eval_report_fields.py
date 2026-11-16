"""Per-case report fields for the judged eval (#598 Step 2, Part B).

The judged report has to say, for every case, WHY it came out as it did, so Step 3
can study weak answers without another paid run. Written before the
implementation (test designer); the contract the builder writes to:

* ``JudgedCase`` gains, with defaults: ``answer_errored``, ``outcome`` and
  ``decline_reason`` (read from the run's own ``AuditEvent`` row), ``raw_reply``
  and ``retry_reply`` (the answer model's text for the FINAL turn, ``CallSite.answer``
  only) with ``retry_kept``, ``resolved_query`` (the query of the final turn's FIRST
  retrieval), ``retrieval_global``, ``gate_top_score``, ``gate_margin``,
  ``gate_passed``, ``evidence`` (read through the turn's USER message: ``source``,
  ``heading``, ``rank``, ``score``, ``type``, ``text`` capped at 1500, ``truncated``),
  ``question``, and ``grader_calls`` (one entry per grader call: ``score``,
  ``rationale``, ``duration_seconds``).
* Each pass's ``judge`` section gains ``answer_errors`` (``{"total", "by_kind"}``,
  by case kind) and ``declines_by_reason``.
* ``tests/eval/probe.py``: ``probe_retrieval()``, a context manager that wraps
  ``app.answer.orchestrator._default_retriever`` (recording each retrieval as
  ``first`` or ``retry``, with its ``query``) and
  ``app.retrieval.vector.is_confident_global_result`` (recording ``top_score``,
  ``margin``, ``passed`` and returning the ORIGINAL verdict). It is active only
  inside ``_judge_cases``.
* D1 (owner, 2026-11-11): an errored answer, including one written by the wrong
  model, FAILS the refusal-leak, groundedness and multi-turn gates.
* Configured key values are redacted once over the whole serialized report; the
  report is written after each pass; each pass prints one line per case scoring 2
  or less, marked "not gating until #602".

Everything here is free: the hermetic SQLite catalog and recorded fake clients.
The gate fields only fill on Postgres (the vector arm returns before the gate on
SQLite); ``test_eval_report_fields_pg.py`` proves they fill.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import re
import uuid
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from app.answer import orchestrator as orchestrator_mod
from app.core.config import Settings
from app.cost.call_site import CallSite, get_call_site
from app.llm.errors import LLMUnavailable
from app.llm.prompts import NO_ANSWER_REFUSAL
from app.llm.types import LLMResult
from app.models import RetrievalType
from app.retrieval import vector as vector_mod
from app.retrieval.types import RetrievalResult
from app.routing.intent import Intent
from tests.eval import judge as judge_mod
from tests.eval import runner as runner_mod
from tests.eval.cases import EvalCase, load_cases
from tests.eval.paths import GOLDEN_PATH
from tests.eval.retrieval import seeded_session
from tests.eval.runner import JudgedCase
from tests.orchestrator_double import Orchestrator

ANSWER_MODEL = "gemini-3.6-flash"
FALLBACK = "openai/gpt-4o-mini"
EXPECT = ("gemini", ANSWER_MODEL)
GRADER = "anthropic/claude-haiku-5.5"
EVIDENCE_KEYS = {"source", "heading", "rank", "score", "type", "text", "truncated"}
LOW_SCORE_MARK = "not gating until #602"
_PROMPT_EVIDENCE = re.compile(
    r"^\[(\d+)\] Source: (.*?) \| Title: .*? \| URL: .*? \| Snippet: (.*)$", re.MULTILINE
)


def _probe() -> ModuleType:
    """The module under test. It does not exist until the builder writes it."""
    return importlib.import_module("tests.eval.probe")


def _golden() -> dict[str, EvalCase]:
    return {c.id: c for c in load_cases(GOLDEN_PATH)}


class _Model:
    """A fake answer client. ``reply`` decides the answer text from the prompt;
    every other call site (condense, alias check) gets "" and so declines."""

    def __init__(
        self,
        reply: str | Callable[[str], str],
        *,
        provider: str = "gemini",
        model: str = ANSWER_MODEL,
    ) -> None:
        self.reply, self.provider, self.model = reply, provider, model
        self.answer_prompts: list[str] = []

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:  # noqa: ARG002
        text = ""
        if get_call_site() is CallSite.answer:
            self.answer_prompts.append(user)
            text = self.reply(user) if callable(self.reply) else self.reply
        return LLMResult(
            text=text,
            input_tokens=1,
            output_tokens=1,
            model=self.model,
            provider=self.provider,  # type: ignore[arg-type]
        )

    async def aclose(self) -> None:
        return None


class _Judge:
    """A fake grader: a fixed score, a numbered rationale per call."""

    def __init__(self, score: int = 4) -> None:
        self.score, self.calls = score, 0

    async def complete(self, **_: Any) -> LLMResult:
        self.calls += 1
        return LLMResult(
            text=json.dumps({"score": self.score, "rationale": f"r{self.calls}"}),
            input_tokens=1,
            output_tokens=1,
            model="fake-judge",
            provider="router",
        )

    async def aclose(self) -> None:
        return None


def _run(
    monkeypatch: pytest.MonkeyPatch,
    cases: list[EvalCase],
    model: _Model,
    *,
    judge: _Judge | None = None,
    expect: tuple[str, str] | None = EXPECT,
) -> list[JudgedCase]:
    """The REAL ``_judge_cases`` on the hermetic catalog, with fake clients."""
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    return asyncio.run(
        runner_mod._judge_cases(
            cases,
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            answer_llm=runner_mod.AttributingLLM(model),  # type: ignore[arg-type]
            judge_llm=judge or _Judge(),  # type: ignore[arg-type]
            expect_answerer=expect,
        )
    )


def _prompt_evidence(prompt: str) -> list[tuple[str, str]]:
    """``(source, snippet)`` per evidence line of one answer prompt, in order."""
    found = [(src, text) for _n, src, text in _PROMPT_EVIDENCE.findall(prompt)]
    assert found, f"no evidence lines in the prompt: {prompt[:200]!r}"
    return found


def _stored_evidence(case: JudgedCase) -> list[tuple[str, str]]:
    ordered = sorted(case.evidence, key=lambda e: e["rank"])  # type: ignore[attr-defined]
    return [(e["source"], e["text"]) for e in ordered]


# --- decline reason and outcome, from the run's own audit row --------------------


def test_an_uncited_reply_is_reported_as_uncited_answer_with_its_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the decline reason is dropped from the report (it is in the audit
    row), or the model's own text is lost behind the served no-answer copy."""
    reply = "The limit is fifty requests each minute."
    (case,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(reply))
    assert case.no_answer is True  # partner: it really was declined
    assert case.outcome == "no_answer"  # type: ignore[attr-defined]
    assert case.decline_reason == "uncited_answer"  # type: ignore[attr-defined]
    assert case.raw_reply == reply  # type: ignore[attr-defined]
    assert case.answer != reply


def test_the_refusal_paragraph_is_reported_as_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: a model refusal and an uncited claim are reported alike (the audit
    row tells them apart: ``no_answer`` vs ``uncited_answer``)."""
    (case,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(NO_ANSWER_REFUSAL))
    assert case.outcome == "no_answer"  # type: ignore[attr-defined]
    assert case.decline_reason == "no_answer"  # type: ignore[attr-defined]
    assert case.raw_reply == NO_ANSWER_REFUSAL  # type: ignore[attr-defined]


def test_an_invalid_citation_is_reported_as_citation_validation_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a reply citing a source that does not exist is not reported with
    its own outcome and the validator's reason."""
    (case,) = _run(
        monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model("Fifty a minute [7].")
    )
    assert case.outcome == "citation_validation_failed"  # type: ignore[attr-defined]
    assert case.decline_reason  # type: ignore[attr-defined]


def test_an_unsupported_question_is_reported_with_its_reason_and_no_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: an unsupported refusal carries no outcome, or is given a reply or
    evidence it never had (no answer call is made for it)."""
    model = _Model("unused [1]")
    (case,) = _run(monkeypatch, [_golden()["refusal_k8s"]], model)
    assert model.answer_prompts == []  # partner: no answer call was made
    assert case.outcome == "unsupported"  # type: ignore[attr-defined]
    assert case.decline_reason == "unsupported_domain"  # type: ignore[attr-defined]
    assert case.raw_reply is None  # type: ignore[attr-defined]
    assert case.evidence == []  # type: ignore[attr-defined]


def test_an_answered_case_has_outcome_answer_and_no_decline_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: an answered case is reported without its outcome, or with a
    decline reason, or with a retry it never made."""
    reply = "The limit is 50 requests per minute [1]."
    (case,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(reply))
    assert case.no_answer is False
    assert case.outcome == "answer"  # type: ignore[attr-defined]
    assert case.decline_reason is None  # type: ignore[attr-defined]
    assert case.raw_reply == reply  # type: ignore[attr-defined]
    assert case.retry_reply is None  # type: ignore[attr-defined]
    assert case.retry_kept is False  # type: ignore[attr-defined]


def test_the_raw_reply_differs_from_the_served_one_when_the_text_is_rewritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: ``raw_reply`` is filled from the served answer, so a rewrite the
    product made (#554 "the provided evidence") is invisible in the report."""
    reply = "According to the provided evidence, the limit is 50 requests per minute [1]."
    (case,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model(reply))
    assert case.raw_reply == reply  # type: ignore[attr-defined]
    assert "provided evidence" not in case.answer  # partner: the product did rewrite it
    assert case.raw_reply != case.answer  # type: ignore[attr-defined]


def test_declines_are_counted_by_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the summary does not count the declines by their audit reason."""
    golden = _golden()
    cases = [golden["claude_api_lit_ratelimit"], golden["refusal_k8s"]]
    judged = _run(monkeypatch, cases, _Model("No marker in this reply."))
    summary = runner_mod._summarize(cases, {}, judged, judge_available=True, judgeable=cases)
    assert summary["judge"]["declines_by_reason"] == {
        "uncited_answer": 1,
        "unsupported_domain": 1,
    }


# --- follow-ups: the final turn's reply and its resolved query --------------------


def test_a_followup_reports_the_final_turns_reply_and_its_resolved_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the reply is taken from a history turn (indexes not captured
    before the final ``ask``), or the resolved query is the bare follow-up without
    its antecedent. The history turn here is NOT a cache hit: it calls the model."""
    case = _golden()["followup_claude_api_raise"]

    def reply(prompt: str) -> str:
        return "FINAL turn reply [1]." if case.question in prompt else "HISTORY turn reply [1]."

    model = _Model(reply)
    (judged,) = _run(monkeypatch, [case], model)
    # Partner: the history turn really called the model, so a wrong index would
    # pick up its reply instead of the final one.
    assert len(model.answer_prompts) == 2
    assert judged.raw_reply == "FINAL turn reply [1]."  # type: ignore[attr-defined]
    resolved = judged.resolved_query  # type: ignore[attr-defined]
    assert resolved != case.question
    assert case.history[0] in resolved and case.question in resolved
    assert _stored_evidence(judged) == _prompt_evidence(model.answer_prompts[-1])


# --- evidence: what the answer model was given ------------------------------------


@pytest.mark.parametrize(
    ("reply", "answered"),
    [("The limit is 50 requests per minute [1].", True), ("No marker here.", False)],
    ids=["answered", "declined"],
)
def test_evidence_is_what_the_answer_model_was_given(
    monkeypatch: pytest.MonkeyPatch, reply: str, answered: bool
) -> None:
    """Red when: evidence is read by the assistant message id (the rows are keyed to
    the turn's USER message, so it comes back empty), or is not what the model saw,
    or a declined case loses its evidence."""
    model = _Model(reply)
    (case,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], model)
    assert case.no_answer is (not answered)
    evidence = case.evidence  # type: ignore[attr-defined]
    assert evidence, "no evidence stored"
    assert all(set(e) >= EVIDENCE_KEYS for e in evidence), evidence
    assert all(len(e["text"]) <= 1500 and e["truncated"] is False for e in evidence)
    assert _stored_evidence(case) == _prompt_evidence(model.answer_prompts[-1])


# --- the #208 retry ---------------------------------------------------------------


def _exact_first_retriever(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the hermetic catalog's ``exact_lookup`` answer with exact-arm hits, as
    the shipped corpus does, so an uncited reply reaches the #208 retry."""
    real = orchestrator_mod._default_retriever

    class _ExactFirst:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def retrieve(self, question: str, *, product_area, intent, limit, top_k):  # type: ignore[no-untyped-def]
            asked = Intent.faq if intent is Intent.exact_lookup else intent
            result = await self._inner.retrieve(
                question, product_area=product_area, intent=asked, limit=limit, top_k=top_k
            )
            hits = result.hits
            if intent is Intent.exact_lookup:
                hits = [h.model_copy(update={"retrieval_type": RetrievalType.exact}) for h in hits]
            return RetrievalResult(hits=hits, vector_degrade=result.vector_degrade)

        async def retrieve_multi(self, *args: Any, **kwargs: Any) -> RetrievalResult:
            return await self._inner.retrieve_multi(*args, **kwargs)

    def _default(settings: Settings, session: Any, *, active_index_version=None):  # type: ignore[no-untyped-def]
        return _ExactFirst(real(settings, session, active_index_version=active_index_version))

    monkeypatch.setattr(orchestrator_mod, "_default_retriever", _default)


def _replies(*texts: str) -> Callable[[str], str]:
    it = iter(texts)
    return lambda _prompt: next(it)


def test_a_kept_retry_reports_both_replies_and_the_retrys_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the retry's reply or ``retry_kept`` is not recorded, the resolved
    query is the retry's flag-stripped query rather than the first retrieval's, or
    the evidence stored is the first retrieval's although the answer used the retry's."""
    _exact_first_retriever(monkeypatch)
    case = _golden()["codex_lit_modelflag"]
    model = _Model(_replies("No markers.", "Retry grounded [1]."))
    (judged,) = _run(monkeypatch, [case], model)
    assert len(model.answer_prompts) == 2  # partner: the retry really ran
    assert judged.outcome == "answer"  # type: ignore[attr-defined]
    assert judged.raw_reply == "No markers."  # type: ignore[attr-defined]
    assert judged.retry_reply == "Retry grounded [1]."  # type: ignore[attr-defined]
    assert judged.retry_kept is True  # type: ignore[attr-defined]
    assert judged.resolved_query == case.question  # type: ignore[attr-defined]
    types = {e["type"] for e in judged.evidence}  # type: ignore[attr-defined]
    assert types and "exact" not in types


def test_a_dropped_retry_reports_its_reply_and_the_first_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner of the kept retry. Red when: a retry that did not ground is reported
    as kept, or its evidence replaces the evidence the decline was made on."""
    _exact_first_retriever(monkeypatch)
    model = _Model(_replies("No markers.", "Still no markers."))
    (judged,) = _run(monkeypatch, [_golden()["codex_lit_modelflag"]], model)
    assert len(model.answer_prompts) == 2
    assert judged.decline_reason == "uncited_answer"  # type: ignore[attr-defined]
    assert judged.retry_reply == "Still no markers."  # type: ignore[attr-defined]
    assert judged.retry_kept is False  # type: ignore[attr-defined]
    assert {e["type"] for e in judged.evidence} == {"exact"}  # type: ignore[attr-defined]


# --- no leak across cases ---------------------------------------------------------


def test_a_cache_hit_final_turn_is_labelled_and_inherits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a cache hit is left unlabelled, or carries the reply, evidence or
    query of the case that wrote the cached answer (per-case state not reset)."""
    golden = _golden()
    pair = [golden["citevyn_lit_free"], golden["citevyn_lit_alias_sitewin"]]
    model = _Model("CiteVyn is free right now [1].")
    writer, hit = _run(monkeypatch, pair, model)
    # Partner: the first case wrote the answer the second was served from the cache.
    assert len(model.answer_prompts) == 1
    assert hit.answer == writer.answer
    assert writer.raw_reply and writer.evidence and writer.resolved_query  # type: ignore[attr-defined]
    assert hit.outcome == "cache_hit"  # type: ignore[attr-defined]
    assert hit.raw_reply is None  # type: ignore[attr-defined]
    assert hit.evidence == []  # type: ignore[attr-defined]
    assert hit.resolved_query is None  # type: ignore[attr-defined]
    assert hit.retrieval_global is None  # type: ignore[attr-defined]


def test_a_decline_after_an_answer_inherits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: one case's reply, reason or evidence is carried into the next."""
    golden = _golden()
    first, second = _run(
        monkeypatch,
        [golden["claude_api_lit_ratelimit"], golden["refusal_k8s"]],
        _Model("The limit is 50 requests per minute [1]."),
    )
    assert first.raw_reply and first.evidence  # type: ignore[attr-defined]
    assert second.outcome == "unsupported"  # type: ignore[attr-defined]
    assert second.raw_reply is None  # type: ignore[attr-defined]
    assert second.evidence == []  # type: ignore[attr-defined]


# --- gate fields on SQLite ---------------------------------------------------------


def test_the_global_route_is_recorded_and_the_gate_fields_stay_empty_on_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: ``retrieval_global`` is not recorded, or gate fields are invented
    on SQLite, where the vector arm returns before the gate (they fill on
    Postgres: ``test_eval_report_fields_pg.py``)."""
    golden = _golden()
    scoped, global_ = _run(
        monkeypatch,
        [golden["claude_api_lit_ratelimit"], golden["claude_code_par_toolgate"]],
        _Model("An answer [1]."),
    )
    assert scoped.retrieval_global is False  # type: ignore[attr-defined]
    assert global_.retrieval_global is True  # type: ignore[attr-defined]
    assert global_.resolved_query == golden["claude_code_par_toolgate"].question  # type: ignore[attr-defined]
    for case in (scoped, global_):
        assert case.gate_top_score is None  # type: ignore[attr-defined]
        assert case.gate_margin is None  # type: ignore[attr-defined]
        assert case.gate_passed is None  # type: ignore[attr-defined]


# --- answer errors -----------------------------------------------------------------

_ONE_PER_KIND = [
    "claude_api_lit_ratelimit",
    "claude_code_par_toolgate",
    "refusal_k8s",
    "multihop_auth_compare",
    "followup_claude_api_raise",
]


def test_an_orchestrator_error_without_an_answer_call_is_a_harness_error_on_every_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: an ``OrchestratorError`` raised before any answer call is marked
    ``answer_errored`` for some case kind, or leaves no ``answer`` harness error
    (#598 Step 2b, B: a case errors only when an answer call failed or another model
    wrote an answer; everything else is a harness error). Was the answer-error
    test of Step 2; REVISION 2 reverses it. Answer-call failures are counted by
    kind in ``test_eval_report_integrity.py``."""

    class _Down(Orchestrator):
        async def ask(self, **_: Any) -> Any:
            raise orchestrator_mod.OrchestratorError("provider down")

    monkeypatch.setattr(runner_mod, "Orchestrator", _Down)
    golden = _golden()
    cases = [golden[i] for i in _ONE_PER_KIND]
    assert {c.kind for c in cases} == {"literal", "paraphrase", "refusal", "multihop", "followup"}
    judged = _run(monkeypatch, cases, _Model("unused"))
    assert len(judged) == 5  # partner: every kind came back
    assert [j.answer_errored for j in judged] == [False] * 5  # type: ignore[attr-defined]
    errors = [getattr(j, "harness_error", None) for j in judged]
    firsts = [e[0] if isinstance(e, list) and e else e for e in errors]
    assert [e.get("stage") if isinstance(e, dict) else None for e in firsts] == ["answer"] * 5
    summary = runner_mod._summarize(cases, {}, judged, judge_available=True, judgeable=cases)
    assert summary["judge"]["answer_errors"] == {"total": 0, "by_kind": {}}


def test_a_healthy_run_has_no_answer_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partner of the above. Red when: a case that answered or declined normally is
    marked errored, or the count is not zero on a healthy run."""
    golden = _golden()
    cases = [golden[i] for i in _ONE_PER_KIND]
    judged = _run(monkeypatch, cases, _Model("An answer [1]."))
    assert len(judged) == 5
    assert [j.answer_errored for j in judged] == [False] * 5  # type: ignore[attr-defined]
    summary = runner_mod._summarize(cases, {}, judged, judge_available=True, judgeable=cases)
    assert summary["judge"]["answer_errors"] == {"total": 0, "by_kind": {}}


@pytest.mark.parametrize(
    ("provider", "model", "errored"),
    [("router", FALLBACK, True), ("gemini", ANSWER_MODEL, False)],
    ids=["wrong-model", "expected-model"],
)
def test_an_answer_by_the_wrong_model_is_an_answer_error(
    monkeypatch: pytest.MonkeyPatch, provider: str, model: str, errored: bool
) -> None:
    """Red when: an answer written by another model (D1: an answer error) is not
    marked ``answer_errored``, or the expected model's answer is."""
    (case,) = _run(
        monkeypatch,
        [_golden()["claude_api_lit_ratelimit"]],
        _Model("The limit is 50 requests per minute [1].", provider=provider, model=model),
    )
    assert case.answer_errored is errored  # type: ignore[attr-defined]
    assert ("answered by" in (case.error or "")) is errored


# --- D1: an errored answer fails the gates it used to drop out of --------------------

_HEALTHY_RETRIEVAL = {
    "answerable_total": 5,
    "hit_rate_by_kind": {"literal": 1.0},
    "overall_hit_rate": 1.0,
    "refusal_leaks": 0,
}


def _fillers(n: int = 10) -> list[JudgedCase]:
    """Scored cases, so the judge-coverage gate stays clean beside one error."""
    return [
        JudgedCase(case_id=f"filler_{i}", kind="literal", answer="a", no_answer=False, score=5)
        for i in range(n)
    ]


def _gate(judged: list[JudgedCase], cases: list[EvalCase]) -> list[str]:
    summary = runner_mod._summarize(
        cases, _HEALTHY_RETRIEVAL, judged, judge_available=True, judgeable=cases
    )
    summary["embedder"] = {"mode": "postgres"}
    return runner_mod.gate_failures(summary)


def _errored(case_id: str, kind: str) -> JudgedCase:
    return JudgedCase(
        case_id=case_id,
        kind=kind,
        answer="",
        no_answer=True,
        error="answer: OrchestratorError: provider down",
        answer_errored=True,  # type: ignore[call-arg]
    )


def test_an_errored_refusal_case_fails_the_refusal_gate() -> None:
    """Red when (D1): an errored refusal case still counts as a correct decline."""
    refusal = _golden()["refusal_k8s"]
    failures = _gate([*_fillers(), _errored(refusal.id, "refusal")], [refusal])
    assert any("refusal" in f for f in failures), failures


def test_an_errored_followup_fails_the_multi_turn_gate() -> None:
    """Red when (D1): an errored follow-up drops out of the multi-turn pool."""
    followup = _golden()["followup_claude_api_raise"]
    failures = _gate([*_fillers(), _errored(followup.id, "followup")], [followup])
    assert any(followup.id in f for f in failures), failures


def test_an_errored_fact_case_fails_groundedness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when (D1): a fact-bearing case whose answer errored drops out of the
    groundedness pool instead of failing it. Driven end to end, so the case's
    facts are known to the runner exactly as on a real run."""

    def down(_prompt: str) -> str:
        raise LLMUnavailable("provider down")

    case = _golden()["claude_api_lit_ratelimit"]
    assert case.expected_facts  # partner: the case really is fact-bearing
    judged = _run(monkeypatch, [case], _Model(down))
    assert judged[0].answer_errored is True  # type: ignore[attr-defined]
    failures = _gate([*_fillers(), *judged], [case])
    assert any(case.id in f and "grounded" in f for f in failures), failures


def test_a_grounded_fact_case_passes_groundedness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partner of the errored fact case: the same case answered with its fact is
    scored exactly as before. Red when: the groundedness change hits every case."""
    case = _golden()["claude_api_lit_ratelimit"]
    judged = _run(monkeypatch, [case], _Model("The limit is 50 requests per minute [1]."))
    assert judged[0].fact_coverage == 1.0
    assert not any("grounded" in f for f in _gate([*_fillers(), *judged], [case]))


def test_cases_without_an_error_are_gated_exactly_as_before() -> None:
    """Partner of the D1 changes. Red when: a case that did not error is gated
    differently: a declined refusal and a non-echoing follow-up pass, and an
    answered refusal fails with the same text as before."""
    golden = _golden()
    refusal, followup = golden["refusal_k8s"], golden["followup_claude_api_raise"]
    declined = JudgedCase(case_id=refusal.id, kind="refusal", answer="no", no_answer=True, score=5)
    answered_followup = JudgedCase(
        case_id=followup.id, kind="followup", answer="b", no_answer=False, score=5
    )
    answered_followup.echoed_prior = False
    assert _gate([*_fillers(), declined, answered_followup], [refusal, followup]) == []
    leaked = dataclasses.replace(declined, no_answer=False)
    assert _gate([*_fillers(), leaked], [refusal]) == [
        "1 refusal leak(s) — orchestrator answered a refusal (judged) > 0"
    ]


# --- the probe ----------------------------------------------------------------------


def test_the_probe_keeps_the_verdict_and_computes_the_margin() -> None:
    """Red when: the probe changes the gate's verdict, or records a wrong score or
    margin (margin = top - second; none for a lone hit or an empty result)."""
    with _probe().probe_retrieval() as probe:
        verdicts = [
            vector_mod.is_confident_global_result([0.9, 0.5], min_top_score=0.3, min_margin=0.1),
            vector_mod.is_confident_global_result([0.5, 0.48], min_top_score=0.3, min_margin=0.1),
            vector_mod.is_confident_global_result([0.2, 0.0], min_top_score=0.3, min_margin=0.1),
            vector_mod.is_confident_global_result([0.8], min_top_score=0.3, min_margin=0.1),
            vector_mod.is_confident_global_result([], min_top_score=0.3, min_margin=0.1),
        ]
    assert verdicts == [True, False, False, True, False]
    got = [(g.top_score, g.margin, g.passed) for g in probe.gates]
    assert got[0] == (0.9, pytest.approx(0.4), True)
    assert got[1] == (0.5, pytest.approx(0.02), False)
    assert got[2] == (0.2, pytest.approx(0.2), False)
    assert got[3] == (0.8, None, True)
    assert got[4] == (None, None, False)


def test_the_probe_restores_both_names_on_exit_even_after_an_error() -> None:
    """Red when: the probe leaves its wrappers installed, so a later run or test
    retrieves through a stale recorder."""
    retriever = orchestrator_mod._default_retriever
    gate = vector_mod.is_confident_global_result
    with pytest.raises(RuntimeError, match="inside"), _probe().probe_retrieval():
        assert orchestrator_mod._default_retriever is not retriever  # partner: it wrapped
        assert vector_mod.is_confident_global_result is not gate
        raise RuntimeError("inside")
    assert orchestrator_mod._default_retriever is retriever
    assert vector_mod.is_confident_global_result is gate


async def _ask(question: str) -> Any:
    async with seeded_session() as session:
        return await Orchestrator(
            Settings(_env_file=None),  # type: ignore[call-arg]
            session,
            llm=_Model("An answer [1]."),  # type: ignore[arg-type]
        ).ask(question=question, request_id="probe", session_id=uuid.uuid4())


def test_wiring_the_orchestrator_still_builds_its_retriever_by_the_spied_name() -> None:
    """Red when: the orchestrator stops calling ``_default_retriever`` through its
    module global (inlined, renamed or imported under another name), so the
    probe records nothing and every resolved query and gate field goes empty."""
    question = _golden()["claude_api_lit_ratelimit"].question
    with _probe().probe_retrieval() as probe:
        asyncio.run(_ask(question))
    assert [(r.tag, r.query) for r in probe.retrievals] == [("first", question)]


def test_wiring_the_vector_arm_still_calls_the_gate_by_the_spied_name() -> None:
    """Red when: ``app.retrieval.vector`` stops calling ``is_confident_global_result``
    through its module global, so the probe never sees the gate. Driven with a
    session that reports the Postgres dialect, so it runs without a database."""

    class _Rows:
        def all(self) -> list[tuple[Any, Any, float]]:
            return [(object(), object(), 0.5), (object(), object(), 0.52)]

    class _Session:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, _stmt: Any) -> _Rows:
            return _Rows()

    class _Embedder:
        async def embed(self, _text: str) -> list[float]:
            return [0.0] * 1536

    retriever = vector_mod.VectorRetriever(
        _Session(),  # type: ignore[arg-type]
        embedder=_Embedder(),  # type: ignore[arg-type]
        global_confidence=(0.3, 0.05),
    )
    with _probe().probe_retrieval() as probe:
        hits = asyncio.run(retriever.retrieve("q", product_area=None, limit=10))
    assert hits == []
    assert [(g.top_score, g.passed) for g in probe.gates] == [(0.5, False)]
    assert probe.gates[0].margin == pytest.approx(0.02)


def test_the_probe_tags_the_retry_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the #208 retry's retrieval is not told apart from the first, so
    the resolved query could be the retry's flag-stripped one."""
    _exact_first_retriever(monkeypatch)
    question = _golden()["codex_lit_modelflag"].question

    async def _ask_twice() -> None:
        async with seeded_session() as session:
            await Orchestrator(
                Settings(_env_file=None),  # type: ignore[call-arg]
                session,
                llm=_Model(_replies("No markers.", "Retry grounded [1].")),  # type: ignore[arg-type]
            ).ask(question=question, request_id="probe", session_id=uuid.uuid4())

    with _probe().probe_retrieval() as probe:
        asyncio.run(_ask_twice())
    tags = [r.tag for r in probe.retrievals]
    assert tags == ["first", "retry"]
    assert probe.retrievals[0].query == question
    assert "--model" not in probe.retrievals[1].query


def test_the_probe_is_active_only_inside_judge_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: ``_judge_cases`` leaves the probe installed after it returns (or
    never installs it: the partner below then finds no resolved query)."""
    retriever = orchestrator_mod._default_retriever
    gate = vector_mod.is_confident_global_result
    (case,) = _run(monkeypatch, [_golden()["claude_api_lit_ratelimit"]], _Model("An answer [1]."))
    assert case.resolved_query == _golden()["claude_api_lit_ratelimit"].question  # type: ignore[attr-defined]
    assert orchestrator_mod._default_retriever is retriever
    assert vector_mod.is_confident_global_result is gate


# --- grader calls -------------------------------------------------------------------


def test_each_grader_call_is_stored_with_its_score_rationale_and_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: the report keeps only the aggregated panel score, so a stalled or
    disagreeing grader call cannot be found, or drops the question text."""
    case = _golden()["claude_api_lit_ratelimit"]
    judge = _Judge(score=4)
    (judged,) = _run(
        monkeypatch, [case], _Model("The limit is 50 requests per minute [1]."), judge=judge
    )
    calls = judged.grader_calls  # type: ignore[attr-defined]
    assert judge.calls == 4  # partner: the default panel is 3 framings + 1 adversarial
    assert [c["score"] for c in calls] == [4, 4, 4, 4]
    assert [c["rationale"] for c in calls] == ["r1", "r2", "r3", "r4"]
    assert all(
        isinstance(c["duration_seconds"], float) and c["duration_seconds"] >= 0 for c in calls
    )
    assert judged.question == case.question  # type: ignore[attr-defined]


# --- the run: both passes, the written report, redaction, printed low scores --------

_ANSWERED = "The limit is 50 requests per minute [1]."


def _two_pass_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_provider": "gemini",
        "gemini_api_key": "g-test",
        "openrouter_api_key": "or-test",
        "gemini_model": ANSWER_MODEL,
        "eval_fallback_model": FALLBACK,
        # A judged run is refused without a separate grader (#598 Step 2b, F).
        "eval_judge_model": GRADER,
        "eval_judge_timeout_seconds": 60.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_both_passes_carry_the_new_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: only the main pass fills the new per-case fields or summary counts
    (the fallback pass is gated and read the same way)."""

    def _build(settings: Settings) -> Any:
        if settings.openrouter_model == GRADER:
            return _Judge()
        if settings.llm_provider == "router":
            return _Model(_ANSWERED, provider="router", model=settings.openrouter_model)
        return _Model(_ANSWERED)

    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "build_metered_llm_client", _build)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    # No grader model here, so the grader is the process-wide client: a fake too.
    monkeypatch.setattr(judge_mod, "get_llm_client", lambda settings=None: _Judge())
    summary = asyncio.run(
        runner_mod.run_eval_async(ids=["claude_api_lit_ratelimit"], settings=_two_pass_settings())
    )
    for section in (summary["judge"], summary["fallback"]["judge"]):
        (case,) = section["cases"]
        assert case["error"] is None  # partner: each pass answered with its own model
        assert case["outcome"] == "answer"
        assert case["raw_reply"] == _ANSWERED
        assert case["evidence"]
        assert section["answer_errors"] == {"total": 0, "by_kind": {}}
        assert "declines_by_reason" in section


def _spy_passes(monkeypatch: pytest.MonkeyPatch, passes: list[Any], settings: Settings) -> None:
    """Replace each pass with a canned result (a list) or a failure (an exception)."""
    it = iter(passes)

    async def _spy(
        cases,
        *,
        settings,
        postgres=False,
        answer_llm=None,
        judge_llm=None,
        expect_answerer=None,
        **_new,
    ):  # type: ignore[no-untyped-def]
        result = next(it)
        if isinstance(result, BaseException):
            raise result
        return result

    class _Closable:
        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(runner_mod, "_judge_cases", _spy)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: object())
    monkeypatch.setattr(runner_mod, "build_metered_llm_client", lambda s: _Closable())
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)


def _judged_case(case_id: str, score: int, rationale: str = "fine", **extra: Any) -> JudgedCase:
    return JudgedCase(
        case_id=case_id,
        kind="literal",
        answer=extra.pop("answer", "a"),
        no_answer=False,
        score=score,
        rationale=rationale,
        **extra,
    )


def test_the_report_is_written_after_the_first_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Red when: the report is written only at the end, so a job cut by its time
    limit during the fallback pass loses the main pass's paid answers too."""
    _spy_passes(
        monkeypatch,
        [[_judged_case("first_pass_case", 5)], RuntimeError("fallback pass died")],
        _two_pass_settings(),
    )
    report = tmp_path / "report.json"
    with pytest.raises(RuntimeError, match="fallback pass died"):
        runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--report", str(report)])
    assert report.exists(), "no report was written before the second pass failed"
    written = json.loads(report.read_text(encoding="utf-8"))
    assert [c["case_id"] for c in written["judge"]["cases"]] == ["first_pass_case"]


def test_configured_keys_are_redacted_from_the_whole_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Red when: a key value a model echoed (or an error message carried) reaches
    the stored report, which CI uploads as an artifact."""
    gemini_key, router_key = "g-SECRET-gemini-0123456789", "or-SECRET-router-9876543210"
    settings = _two_pass_settings(gemini_api_key=gemini_key, openrouter_api_key=router_key)
    leaky = _judged_case(
        "leaky_case",
        5,
        rationale=f"the grader saw {router_key} here",
        answer=f"before {gemini_key} after",
    )
    leaky.error = f"judge_unavailable: 401 for key {router_key}"
    _spy_passes(monkeypatch, [[leaky], [dataclasses.replace(leaky)]], settings)
    report = tmp_path / "report.json"
    runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--report", str(report), "--quiet"])
    text = report.read_text(encoding="utf-8")
    assert "leaky_case" in text and "before" in text and "after" in text  # partner
    assert gemini_key not in text
    assert router_key not in text


def test_each_pass_prints_its_low_scores_as_not_gating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: a case scoring 2 or less is not printed with its id, score and
    rationale, or a pass's low scores are missing, or a good case is printed."""
    _spy_passes(
        monkeypatch,
        [
            [_judged_case("low_main", 2, "misses the limit"), _judged_case("fine_main", 5)],
            [_judged_case("low_fallback", 1, "wrong header"), _judged_case("fine_fallback", 3)],
        ],
        _two_pass_settings(),
    )
    runner_mod.main(["--ids", "claude_api_lit_ratelimit", "--report", str(tmp_path / "r.json")])
    marked = [line for line in capsys.readouterr().out.splitlines() if LOW_SCORE_MARK in line]
    assert len(marked) == 2, marked
    main_line, fallback_line = sorted(marked, key=lambda line: "low_fallback" in line)
    assert "low_main" in main_line and "2" in main_line and "misses the limit" in main_line
    assert "low_fallback" in fallback_line and "1" in fallback_line
    assert "wrong header" in fallback_line
    assert not any("fine_" in line for line in marked)
