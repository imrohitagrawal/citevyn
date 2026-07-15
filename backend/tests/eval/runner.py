"""Eval runner + CLI (Phase 0, issue #96).

Composes the two metrics into one report and a pass/fail exit code:

* **retrieval hit-rate** (:mod:`.retrieval`) — always run, fully hermetic.
* **answer quality** (:mod:`.judge`) — run only when a real LLM provider is
  configured; each answerable case is driven end-to-end through
  :class:`~app.answer.orchestrator.Orchestrator` against the seeded corpus and
  the produced answer is judged 1–5 vs its expected gist. Under the stub the
  section is marked ``unavailable`` (never faked).

Usage::

    python -m tests.eval.runner                 # retrieval + judge (if key set)
    python -m tests.eval.runner --no-judge       # retrieval only
    python -m tests.eval.runner --report out.json --quiet

Exit code is non-zero when the retrieval gate regresses, or when the judge ran
and its mean score fell below the floor — so ``make eval`` gates locally too.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import pathlib
import uuid
from typing import Any

from app.answer.orchestrator import Orchestrator, OrchestratorError
from app.core.config import Settings, get_settings
from app.llm.errors import LLMUnavailable
from app.llm.factory import get_llm_client
from app.llm.stub import StubLLMClient

from .cases import EvalCase, filter_cases, load_cases
from .judge import JudgeParseError, score_answer_async
from .paths import DEFAULT_REPORT_PATH, GOLDEN_PATH
from .retrieval import evaluate_retrieval, seeded_session
from .thresholds import (
    MAX_REFUSAL_LEAKS,
    MIN_JUDGE_COVERAGE,
    MIN_LITERAL_HIT_RATE,
    MIN_MEAN_JUDGE,
    MIN_OVERALL_HIT_RATE,
)


@dataclasses.dataclass
class JudgedCase:
    """One case driven end-to-end and (optionally) judged."""

    case_id: str
    kind: str
    answer: str
    no_answer: bool
    score: int | None = None
    rationale: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


async def _judge_cases(cases: list[EvalCase], *, settings: Settings) -> list[JudgedCase]:
    """Drive each case through the orchestrator and judge the produced answer.

    Returns one :class:`JudgedCase` per input case. A per-case failure (LLM
    outage, unparseable judge output) is captured in ``error`` and never
    silently dropped — the case simply contributes no numeric score to the
    mean. Returns an empty list when no real provider is configured.
    """
    if isinstance(get_llm_client(settings), StubLLMClient):
        return []
    judged: list[JudgedCase] = []
    async with seeded_session() as session:
        for case in cases:
            # Step 1: produce the answer. A provider outage (429/5xx) surfaces as
            # OrchestratorError; ``LLMUnavailable`` can also escape the generate
            # path in some arms. Either way the case records a loud error and the
            # batch keeps going — one flaky call must not abort the whole run.
            try:
                response = await Orchestrator(settings, session).ask(
                    question=case.question,
                    request_id=f"eval-{case.id}",
                    session_id=uuid.uuid4(),
                )
            except (OrchestratorError, LLMUnavailable) as exc:
                judged.append(
                    JudgedCase(
                        case_id=case.id,
                        kind=case.kind,
                        answer="",
                        no_answer=True,
                        error=f"answer: {type(exc).__name__}: {exc}",
                    )
                )
                continue
            answer = str(response.get("answer", ""))
            no_answer = bool(response.get("no_answer") or response.get("unsupported"))
            entry = JudgedCase(case_id=case.id, kind=case.kind, answer=answer, no_answer=no_answer)
            # Step 2: judge it. An unparseable verdict or a provider outage on the
            # judge call is recorded per-case (never a fabricated score, never a
            # crash) so the run still emits a report and the retrieval gate holds.
            try:
                verdict = await score_answer_async(
                    question=case.question,
                    answer=answer,
                    expected_gist=case.expected_gist,
                    settings=settings,
                )
            except JudgeParseError as exc:
                entry.error = f"judge_parse: {exc}"
            except LLMUnavailable as exc:
                entry.error = f"judge_unavailable: {exc}"
            else:
                if verdict is not None:
                    entry.score = verdict.score
                    entry.rationale = verdict.rationale
                else:
                    # ``None`` only if the provider went stub mid-run (guarded
                    # against above). Record it as an error rather than letting
                    # the case vanish from ``scored`` without a trace.
                    entry.error = "judge_unavailable: verdict was None (stub mid-run)"
            judged.append(entry)
    return judged


def _summarize(
    cases: list[EvalCase],
    retrieval: dict[str, Any],
    judged: list[JudgedCase],
    *,
    judge_available: bool,
) -> dict[str, Any]:
    scores = [j.score for j in judged if j.score is not None]
    mean_score = (sum(scores) / len(scores)) if scores else None
    return {
        "total_cases": len(cases),
        "retrieval": retrieval,
        "judge": {
            "available": judge_available,
            "judged": len(judged),
            "scored": len(scores),
            "mean_score": mean_score,
            "min_mean_threshold": MIN_MEAN_JUDGE,
            "errors": [j.as_dict() for j in judged if j.error],
            "cases": [j.as_dict() for j in judged],
        },
    }


def gate_failures(summary: dict[str, Any]) -> list[str]:
    """Return human-readable reasons the run should fail the build (empty = pass).

    Guards against *degenerate* inputs, not just low numbers: a golden file with
    zero answerable cases, or a run where the literal bucket is absent, must FAIL
    rather than sail through on the empty-pool ``hit_rate`` convention of 1.0 (a
    silent success is the one outcome this harness exists to prevent).
    """
    failures: list[str] = []
    r = summary["retrieval"]
    by_kind = r["hit_rate_by_kind"]
    if r["answerable_total"] == 0:
        failures.append("no answerable cases in the golden set (zero coverage)")
    if "literal" not in by_kind:
        failures.append("no literal cases in the golden set (cannot enforce literal gate)")
    else:
        literal_rate = by_kind["literal"]
        if literal_rate < MIN_LITERAL_HIT_RATE:
            failures.append(f"literal hit-rate {literal_rate:.3f} < {MIN_LITERAL_HIT_RATE}")
    if r["overall_hit_rate"] < MIN_OVERALL_HIT_RATE:
        failures.append(f"overall hit-rate {r['overall_hit_rate']:.3f} < {MIN_OVERALL_HIT_RATE}")
    if r["refusal_leaks"] > MAX_REFUSAL_LEAKS:
        failures.append(f"{r['refusal_leaks']} refusal leak(s) > {MAX_REFUSAL_LEAKS}")
    j = summary["judge"]
    if j["available"]:
        judged = j.get("judged", 0)
        scored = j.get("scored", 0)
        # A total outage (nothing scored) or a material partial outage (a low
        # score-coverage ratio) must FAIL, not pass on the mean of the lucky
        # survivors — an inflated mean over a biased subset is a silent success.
        if judged > 0 and scored == 0:
            failures.append("judge ran but produced no usable scores (total judge outage)")
        elif judged > 0 and (scored / judged) < MIN_JUDGE_COVERAGE:
            failures.append(
                f"judge scored only {scored}/{judged} cases "
                f"(coverage {scored / judged:.2f} < {MIN_JUDGE_COVERAGE}); mean not trustworthy"
            )
        elif j["mean_score"] is not None and j["mean_score"] < MIN_MEAN_JUDGE:
            failures.append(f"mean judge score {j['mean_score']:.2f} < {MIN_MEAN_JUDGE}")
    return failures


async def run_eval_async(
    *,
    golden_path: pathlib.Path = GOLDEN_PATH,
    ids: list[str] | None = None,
    with_judge: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run the full eval and return a JSON-friendly summary."""
    settings = settings or get_settings()
    cases = filter_cases(load_cases(golden_path), ids=ids)
    if not cases:
        raise SystemExit(f"no eval cases found in {golden_path}")
    retrieval_report = await evaluate_retrieval(cases, settings=settings)
    judge_available = with_judge and not isinstance(get_llm_client(settings), StubLLMClient)
    judged = await _judge_cases(cases, settings=settings) if judge_available else []
    return _summarize(cases, retrieval_report.as_dict(), judged, judge_available=judge_available)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval-runner",
        description="Run the CiteVyn RAG eval harness (retrieval hit-rate + LLM judge).",
    )
    parser.add_argument("--golden", type=pathlib.Path, default=GOLDEN_PATH)
    parser.add_argument("--ids", type=str, default=None, help="Comma-separated id filter.")
    parser.add_argument("--report", type=pathlib.Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--no-judge",
        dest="with_judge",
        action="store_false",
        help="Skip the LLM-judge metric even when a provider key is set.",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    ids = [s.strip() for s in args.ids.split(",")] if args.ids else None
    summary = asyncio.run(
        run_eval_async(golden_path=args.golden, ids=ids, with_judge=args.with_judge)
    )

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("w") as fh:
            json.dump(summary, fh, indent=2, default=str)

    r = summary["retrieval"]
    j = summary["judge"]
    if not args.quiet:
        print("Retrieval hit-rate:")
        print(
            f"  overall (answerable): {r['answerable_hits']}/{r['answerable_total']} "
            f"= {r['overall_hit_rate']:.3f}"
        )
        for kind, rate in sorted(r["hit_rate_by_kind"].items()):
            print(f"  {kind:11s}: {rate:.3f}")
        print(f"  refusal leaks: {r['refusal_leaks']}/{r['refusal_total']}")
        if j["available"]:
            mean = j["mean_score"]
            print(
                f"Judge: mean {mean:.2f} over {j['scored']} scored ({len(j['errors'])} error(s))"
                if mean is not None
                else "Judge: no scores"
            )
        else:
            print("Judge: unavailable (stub provider) — set CITEVYN_LLM_PROVIDER + key to run")

    failures = gate_failures(summary)
    if failures:
        print("\nEVAL GATE FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    if not args.quiet:
        print(f"\nEval gate passed. Report: {args.report}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
