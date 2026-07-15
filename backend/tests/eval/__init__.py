"""RAG eval harness for CiteVyn (Phase 0, issue #96).

This package measures two *outcome* metrics over the golden set at
``tests/eval/golden.jsonl`` — the measurement foundation for all RAG
quality work in ``docs/RAG_QUALITY_PLAN.md``:

* :mod:`.retrieval` — retrieval hit-rate (does any top-k chunk come from
  the expected source?).  Fully hermetic: seeded SQLite + the real
  :class:`~app.retrieval.hybrid.HybridRetriever`, no network, no LLM.
* :mod:`.judge` — LLM-as-judge answer quality (1–5 vs an expected gist).
  Requires a real LLM provider; returns "unavailable" (never a faked
  score) under the stub so hermetic CI cannot silently pass it.

It is deliberately separate from :mod:`tests.golden` (the assertion-based
regression runner) and reuses the backend's own seams —
:func:`tests.conftest.seed_catalog`, ``HybridRetriever``,
``classify_domain`` / ``classify_intent``, and ``get_llm_client`` —
rather than re-implementing them.
"""

from .cases import EvalCase, filter_cases, load_cases
from .judge import JudgeParseError, JudgeVerdict, score_answer, score_answer_async
from .retrieval import RetrievalOutcome, RetrievalReport, evaluate_retrieval

# NOTE: :mod:`.runner` is deliberately NOT imported here. It is the CLI entry
# point (``python -m tests.eval.runner``); importing it from the package
# ``__init__`` triggers a ``runpy`` double-import warning under ``-m``. Import
# ``run_eval_async`` from ``tests.eval.runner`` directly when needed.

__all__ = [
    "EvalCase",
    "JudgeParseError",
    "JudgeVerdict",
    "RetrievalOutcome",
    "RetrievalReport",
    "evaluate_retrieval",
    "filter_cases",
    "load_cases",
    "score_answer",
    "score_answer_async",
]
