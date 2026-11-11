"""Record what retrieval did during a judged run, without changing it (#598).

:func:`probe_retrieval` wraps two module globals for as long as it is open:

* ``app.answer.orchestrator._default_retriever``: each retriever it builds is
  wrapped in a recorder. The first retrieval on one retriever is tagged
  ``first``; any later one on the same retriever is the #208 retry (``retry``),
  whose query has its flag tokens stripped.
* ``app.retrieval.vector.is_confident_global_result``: each call of the global
  confidence gate is recorded (top score, margin to the runner-up, verdict) and
  the ORIGINAL verdict is returned unchanged.

Nothing here changes an answer. The runner opens it only inside
``_judge_cases``, so no other code retrieves through a recorder.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Iterator, Sequence
from typing import Any

from app.answer import orchestrator as orchestrator_mod
from app.retrieval import vector as vector_mod


@dataclasses.dataclass(frozen=True)
class Retrieval:
    """One retrieval call: ``first`` or ``retry``, its query, and whether it
    was the unscoped (global) path."""

    tag: str
    query: str
    is_global: bool


@dataclasses.dataclass(frozen=True)
class Gate:
    """One call of the global confidence gate. ``margin`` is top minus the
    runner-up (``None`` for a lone hit); ``top_score`` is ``None`` for no hits."""

    top_score: float | None
    margin: float | None
    passed: bool


@dataclasses.dataclass
class Probe:
    retrievals: list[Retrieval] = dataclasses.field(default_factory=list)
    gates: list[Gate] = dataclasses.field(default_factory=list)


class _RecordingRetriever:
    def __init__(self, inner: Any, probe: Probe) -> None:
        self._inner = inner
        self._probe = probe
        self._calls = 0

    def _record(self, query: str, *, is_global: bool) -> None:
        tag = "first" if self._calls == 0 else "retry"
        self._calls += 1
        self._probe.retrievals.append(Retrieval(tag=tag, query=query, is_global=is_global))

    async def retrieve(self, question: str, **kwargs: Any) -> Any:
        self._record(question, is_global=kwargs.get("product_area") is None)
        return await self._inner.retrieve(question, **kwargs)

    async def retrieve_multi(self, question: str, **kwargs: Any) -> Any:
        self._record(question, is_global=False)
        return await self._inner.retrieve_multi(question, **kwargs)


@contextlib.contextmanager
def probe_retrieval() -> Iterator[Probe]:
    """Record retrievals and gate calls while open; restore both names on exit."""
    probe = Probe()
    real_retriever = orchestrator_mod._default_retriever
    real_gate = vector_mod.is_confident_global_result

    def _retriever(*args: Any, **kwargs: Any) -> Any:
        return _RecordingRetriever(real_retriever(*args, **kwargs), probe)

    def _gate(scores: Sequence[float], *, min_top_score: float, min_margin: float) -> bool:
        passed = real_gate(scores, min_top_score=min_top_score, min_margin=min_margin)
        top = float(scores[0]) if scores else None
        margin = float(scores[0]) - float(scores[1]) if len(scores) > 1 else None
        probe.gates.append(Gate(top_score=top, margin=margin, passed=passed))
        return passed

    orchestrator_mod._default_retriever = _retriever  # type: ignore[assignment]
    vector_mod.is_confident_global_result = _gate  # type: ignore[assignment]
    try:
        yield probe
    finally:
        orchestrator_mod._default_retriever = real_retriever  # type: ignore[assignment]
        vector_mod.is_confident_global_result = real_gate  # type: ignore[assignment]
