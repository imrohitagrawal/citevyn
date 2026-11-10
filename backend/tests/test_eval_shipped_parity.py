"""#601: the shipped-corpus eval searches exactly what production searches (free job).

The real ``Orchestrator.ask`` runs every non-follow-up question with a spy in place of
``HybridRetriever``, and the eval's own path runs with the same spy. Both must build the
retriever and call it identically: query text, route, areas, intent, limit, top_k and the
global confidence gate. A question production does not retrieve for must be labelled so.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest

from app.answer import orchestrator as orchestrator_module
from app.answer.orchestrator import Orchestrator
from app.core.config import Settings, get_settings
from app.embeddings.protocol import Embedder
from app.guardrails.domain import Domain
from app.retrieval.types import RetrievalResult, VectorDegrade
from app.routing.intent import Intent, classify_intent
from tests.eval import shipped


class _Spy:
    """Stands in for ``HybridRetriever``: records how it was built and called."""

    calls: list[dict[str, Any]] = []

    def __init__(self, _session: object, **kwargs: Any) -> None:
        self._built = {"global_confidence": kwargs.get("global_confidence")}

    async def retrieve(self, question: str, **kwargs: Any) -> RetrievalResult:
        _Spy.calls.append({**self._built, "method": "retrieve", "question": question, **kwargs})
        return RetrievalResult(hits=[], vector_degrade=VectorDegrade.none)

    async def retrieve_multi(self, question: str, **kwargs: Any) -> RetrievalResult:
        _Spy.calls.append({**self._built, "method": "multi", "question": question, **kwargs})
        return RetrievalResult(hits=[], vector_degrade=VectorDegrade.none)


def _settings(**overrides: Any) -> Settings:
    return get_settings().model_copy(update={"cache_enabled": False, **overrides})


def _classifier_that_ignores_the_domain(question: str, domain: Domain) -> Intent:
    """Today's classifier already answers ``unsupported`` for that domain, which makes the
    orchestrator's own override invisible; this one does not, so the override must exist."""
    return classify_intent(question, Domain.general if domain is Domain.unsupported else domain)


@pytest.mark.parametrize(
    ("overrides", "loose_classifier"),
    [({}, False), ({"answer_when_grounded": False}, False), ({}, True)],
    ids=["default", "no_global_answers", "classifier_ignores_domain"],
)
async def test_the_eval_calls_the_retriever_exactly_as_production_does(
    session: Any,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    loose_classifier: bool,
) -> None:
    """Red when: the eval's route, query, intent, limit, top_k or confidence gate drifts.

    Also red when a question production short-circuits is not labelled as skipped, or the
    eval drops the override that makes an unsupported domain's intent ``unsupported``.
    """
    settings = _settings(**overrides)
    if loose_classifier:
        monkeypatch.setattr(
            orchestrator_module, "classify_intent", _classifier_that_ignores_the_domain
        )
        monkeypatch.setattr(shipped, "classify_intent", _classifier_that_ignores_the_domain)
    monkeypatch.setattr(orchestrator_module, "HybridRetriever", _Spy)
    monkeypatch.setattr(shipped, "HybridRetriever", _Spy)
    cases = [case for case in shipped.all_shipped_cases() if not case.follow_up]
    assert len(cases) > 100
    served = 0
    for case in cases:
        _Spy.calls = []
        await Orchestrator(settings, session, create_missing_session=True).ask(
            question=case.question, request_id=f"req_{case.id}", session_id=uuid.uuid4()
        )
        production = _Spy.calls[:1]
        routed = shipped.route_case(case, settings)
        if routed.production_skip is not None:
            assert production == [], case.id
            continue
        _Spy.calls = []
        await shipped.retrieve_routed(
            session, routed, embedder=cast("Embedder", None), identity=None, settings=settings
        )
        assert _Spy.calls == production, case.id
        served += 1
    # Partner: most questions really reached the retriever on both sides.
    assert served > len(cases) // 2
