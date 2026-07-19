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
import contextlib
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
from .groundedness import fact_coverage, forbidden_present, missing_facts
from .judge import JudgeParseError, score_answer_panel_async
from .paths import DEFAULT_REPORT_PATH, GOLDEN_PATH
from .retrieval import evaluate_retrieval, postgres_session, seeded_session
from .subset import select_judge_subset
from .thresholds import (
    MAX_REFUSAL_LEAKS,
    MIN_FOLLOWUP_HIT_RATE,
    MIN_JUDGE_COVERAGE,
    MIN_LITERAL_HIT_RATE,
    MIN_MEAN_JUDGE,
    MIN_MRR,
    MIN_MULTIHOP_HIT_RATE,
    MIN_OVERALL_HIT_RATE,
    MIN_PRECISION_AT_1,
)


@dataclasses.dataclass
class JudgedCase:
    """One case driven end-to-end and (optionally) judged.

    ``score`` is the ROBUST panel score — ``min(standard_median, adversarial)`` — so
    a skeptic that catches a plausible-but-wrong answer vetoes an over-scored median.
    ``fact_coverage`` is the judge-independent deterministic groundedness signal
    (``None`` when the case declares no ``expected_facts``).
    """

    case_id: str
    kind: str
    answer: str
    no_answer: bool
    score: int | None = None
    rationale: str = ""
    error: str | None = None
    standard_scores: tuple[int, ...] = ()
    standard_median: int | None = None
    adversarial_score: int | None = None
    spread: int | None = None
    contested: bool = False
    fact_coverage: float | None = None
    missing_facts: tuple[str, ...] = ()
    # Prompt-injection resistance (Item 2): forbidden substrings the answer emitted (a
    # non-empty tuple = the model complied with an injection → a leak). ``None`` when
    # the case declares no ``must_not_contain``.
    injection_hits: tuple[str, ...] | None = None
    # Multi-turn echo oracle (#169). ``True`` when this ``followup`` case's answer came
    # back BYTE-IDENTICAL to the answer of the turn before it — the signature of a
    # follow-up that was never actually answered. ``None`` for a single-turn case (no
    # prior turn to compare against). See ``_judge_cases`` for why this exists.
    echoed_prior: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@contextlib.asynccontextmanager
async def _judge_session(settings: Settings, *, postgres: bool):
    """Yield the DB session the judge drives the orchestrator against.

    Hermetic SQLite by default; the real Postgres+pgvector catalog (with a real
    embedder, rolled back for zero CATALOG residue) when ``postgres=True`` so the
    judged answers reflect the live semantic retrieval path.

    The rollback covers seeded catalog/session rows only. ``provider_calls`` rows
    written by the cost meter (#153 Layer 1) are committed on a separate session and
    intentionally OUTLIVE the run — spend is a fact about money that already left the
    account, so our own teardown must not erase it. See :func:`postgres_session` and
    ``docs/COST_CONTROLS.md`` §1.
    """
    if postgres:
        async with postgres_session(settings) as (session, _embedder):
            yield session
    else:
        async with seeded_session() as session:
            yield session


async def _judge_cases(
    cases: list[EvalCase], *, settings: Settings, postgres: bool = False
) -> list[JudgedCase]:
    """Drive each case through the orchestrator and judge the produced answer.

    Returns one :class:`JudgedCase` per input case. A per-case failure (LLM
    outage, unparseable judge output) is captured in ``error`` and never
    silently dropped — the case simply contributes no numeric score to the
    mean. Returns an empty list when no real provider is configured.
    """
    if isinstance(get_llm_client(settings), StubLLMClient):
        return []
    # Exclude postgres-only cases from a hermetic judged run (Item 2) — they need the
    # live vector arm; on SQLite they would misfire and pollute the judged metrics.
    cases = [c for c in cases if postgres or not c.postgres_only]
    judged: list[JudgedCase] = []
    async with _judge_session(settings, postgres=postgres) as session:
        for case in cases:
            # Step 1: produce the answer. A provider outage (429/5xx) surfaces as
            # OrchestratorError; ``LLMUnavailable`` can also escape the generate
            # path in some arms. Either way the case records a loud error and the
            # batch keeps going — one flaky call must not abort the whole run.
            #
            # A ``followup`` case is MULTI-TURN: replay its ``history`` through the
            # orchestrator on ONE session first, so the orchestrator's DB-backed
            # conversation memory (Phase 3b) resolves the anaphoric final turn exactly
            # as it would in production. Non-followup cases have empty history → the
            # replay loop is a no-op and the case is driven single-turn as before.
            session_id = uuid.uuid4()
            # Keep the LAST history turn's answer so the echo oracle below can compare
            # against it. Only the immediately-preceding turn matters: that is the one a
            # concatenating rewrite would cause the final turn to re-answer.
            prior_answer = ""
            try:
                for prior in case.history:
                    prior_response = await Orchestrator(settings, session).ask(
                        question=prior,
                        request_id=f"eval-{case.id}-history",
                        session_id=session_id,
                    )
                    prior_answer = str(prior_response.get("answer", ""))
                response = await Orchestrator(settings, session).ask(
                    question=case.question,
                    request_id=f"eval-{case.id}",
                    session_id=session_id,
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
            # Multi-turn echo oracle (#169) — judge-independent, and the single assertion
            # that catches this whole bug class. The existing followup metrics measure
            # RETRIEVAL hit-rate and non-refusal; a rewrite that concatenates the prior
            # turn onto the follow-up ("What is Codex CLI? who built it?") PASSES both — it
            # retrieves the right chunk and returns a fluent cited answer — while the LLM
            # silently answers only the leading clause and re-emits the previous turn's
            # answer verbatim. The bug was invisible BY CONSTRUCTION until this check.
            # Compared on stripped text so trailing-whitespace noise is not a false pass.
            if prior_answer.strip():
                entry.echoed_prior = answer.strip() == prior_answer.strip()
            # Deterministic groundedness (Item 1c) — judge-independent. Computed for
            # every case that declares hard facts, whether or not the LLM judge runs,
            # so a plausible-but-wrong answer that fumbles a fact fails regardless of
            # the judge's opinion.
            if case.expected_facts:
                entry.fact_coverage = fact_coverage(answer, case.expected_facts)
                entry.missing_facts = tuple(missing_facts(answer, case.expected_facts))
            # Prompt-injection resistance (Item 2) — judge-independent. Any forbidden
            # sentinel present in the answer means the model obeyed an injection.
            if case.must_not_contain:
                entry.injection_hits = tuple(forbidden_present(answer, case.must_not_contain))
            # Step 2: judge it with the robust panel (N framings + adversarial veto).
            # An unparseable verdict or a provider outage on any judge call is recorded
            # per-case (never a fabricated score, never a crash) so the run still emits
            # a report and the retrieval gate holds.
            try:
                verdict = await score_answer_panel_async(
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
                    entry.standard_scores = verdict.standard_scores
                    entry.standard_median = verdict.standard_median
                    entry.adversarial_score = verdict.adversarial_score
                    entry.spread = verdict.spread
                    entry.contested = verdict.contested
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
    judge_subset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scores = [j.score for j in judged if j.score is not None]
    mean_score = (sum(scores) / len(scores)) if scores else None
    # Judged refusal safety: a refusal "leaks" only when the ORCHESTRATOR actually
    # answered it (``no_answer`` False). Under "answer when grounded" (Phase 2) a
    # refusal may retrieve a nearest chunk globally that the LLM grounding-refusal
    # then correctly declines — so the retrieval-only leak count over-counts, and
    # the orchestrator's decision is the authoritative measure when the LLM ran.
    refusal_judged = [j for j in judged if j.kind == "refusal"]
    refusal_leaks_judged = sum(1 for j in refusal_judged if not j.no_answer)
    # Deterministic groundedness (Item 1c): aggregate ONLY over cases that declared
    # facts (fact_coverage is not None). Independent of the judge — reported even
    # when the panel could not run — so a wrong hard fact is caught on its own axis.
    grounded = [j for j in judged if j.fact_coverage is not None]
    grounded_rate = (
        (sum(j.fact_coverage or 0.0 for j in grounded) / len(grounded)) if grounded else None
    )
    contested = [j.case_id for j in judged if j.contested]
    # Prompt-injection resistance (Item 2): a case OBEYED an injection only when the
    # forbidden sentinel is present AND the answer failed to actually answer (its hard
    # facts are not fully grounded). A resistant answer that names the sentinel while
    # DECLINING ("I won't reply with PWNED — the limit is 50 requests per minute") still
    # grounds its facts, so it is NOT a leak. When a case declares no expected_facts,
    # bare sentinel presence is the (conservative) signal. Any real leak fails the run.
    multi_turn = [j for j in judged if j.echoed_prior is not None]
    injection_cases = [j for j in judged if j.injection_hits is not None]
    injection_leaks = [
        {"case_id": j.case_id, "hits": list(j.injection_hits or ())}
        for j in injection_cases
        if j.injection_hits and (j.fact_coverage is None or j.fact_coverage < 1.0)
    ]
    return {
        "total_cases": len(cases),
        "retrieval": retrieval,
        "judge": {
            "available": judge_available,
            # Cost bounding (#153 Layer 6). ``None`` on a full run; on a bounded run
            # it names exactly which cases were NOT judged, so a reader can never
            # mistake a subset report for full coverage.
            "subset": judge_subset,
            "judged": len(judged),
            "scored": len(scores),
            "mean_score": mean_score,
            "min_mean_threshold": MIN_MEAN_JUDGE,
            "refusal_total": len(refusal_judged),
            "refusal_leaks_judged": refusal_leaks_judged,
            "contested_cases": contested,
            "errors": [j.as_dict() for j in judged if j.error],
            "cases": [j.as_dict() for j in judged],
        },
        "injection": {
            "cases": len(injection_cases),
            "leaks": injection_leaks,
        },
        # Multi-turn echo oracle (#169). ``cases`` counts the multi-turn cases that had a
        # non-empty prior answer to compare against (so a 0 here means the oracle was
        # VACUOUS, not that it passed); ``echoes`` names every case whose follow-up came
        # back byte-identical to the previous turn.
        "multi_turn": {
            "cases": len(multi_turn),
            "echoes": [j.case_id for j in multi_turn if j.echoed_prior],
        },
        "groundedness": {
            "cases_with_facts": len(grounded),
            "grounded_fact_rate": grounded_rate,
            "under_grounded": [
                {
                    "case_id": j.case_id,
                    "coverage": j.fact_coverage,
                    "missing": list(j.missing_facts),
                }
                for j in grounded
                if (j.fact_coverage or 0.0) < 1.0
            ],
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
    j = summary["judge"]
    # Refusal-safety gate. When the LLM ran (judged), the authoritative measure is
    # whether the orchestrator DECLINED — under "answer when grounded" a refusal can
    # retrieve a nearest chunk (a retrieval "leak") that the LLM correctly refuses,
    # so the retrieval-only count over-counts. Fall back to the retrieval metric only
    # when no LLM ran: on hermetic SQLite the dead vector arm keeps refusals empty,
    # so the retrieval count is exact there (and is the CI gate).
    #
    # NB this is an ``elif``: on a judged run the JUDGED count is the only refusal
    # gate that runs. That is sound only while the judged run covers every refusal
    # case — which is why ``tests.eval.subset.is_priority`` retains all of them, and
    # why the assertion below fails loudly rather than quietly narrowing the gate if
    # that ever stops being true. Without it, a bounded run would check the dropped
    # refusal cases in NEITHER branch.
    if j["available"] and j.get("judged", 0) > 0:
        sub = j.get("subset") or {}
        for oracle, ids in (sub.get("dropped_zero_tolerance") or {}).items():
            if ids:
                failures.append(
                    f"{len(ids)} case(s) carrying the ZERO-TOLERANCE '{oracle}' oracle "
                    f"were excluded from the judged run, so that gate did not cover "
                    f"them: {ids}. Such cases must stay in the subset priority pool "
                    "(tests/eval/subset.py:is_priority)."
                )
        judged_leaks = j.get("refusal_leaks_judged", 0)
        if judged_leaks > MAX_REFUSAL_LEAKS:
            failures.append(
                f"{judged_leaks} refusal leak(s) — orchestrator answered a refusal "
                f"(judged) > {MAX_REFUSAL_LEAKS}"
            )
    elif r["refusal_leaks"] > MAX_REFUSAL_LEAKS:
        failures.append(f"{r['refusal_leaks']} refusal leak(s) (retrieval) > {MAX_REFUSAL_LEAKS}")
    # Multi-hop is Postgres-only-provable (needs the live vector arm to hit both
    # areas); gate it ONLY on the --postgres run. On hermetic SQLite it is reported
    # but not gated, so it never drags the standard CI gate.
    if summary.get("embedder", {}).get("mode") == "postgres":
        mh_total = r.get("multihop_total", 0)
        if mh_total and r.get("multihop_hit_rate", 0.0) < MIN_MULTIHOP_HIT_RATE:
            failures.append(
                f"multihop hit-rate {r['multihop_hit_rate']:.3f} < {MIN_MULTIHOP_HIT_RATE}"
            )
        # Chunk-level rank-sensitive metric (#125), Postgres-only (live vector arm). Guarded
        # on a non-empty single-relevant pool so a golden set without gold_chunks (or an
        # older summary lacking the keys) never KeyErrors or fails vacuously.
        ranked_total = r.get("ranked_total", 0)
        if ranked_total:
            if r.get("precision_at_1", 0.0) < MIN_PRECISION_AT_1:
                failures.append(
                    f"precision@1 {r['precision_at_1']:.3f} < {MIN_PRECISION_AT_1} "
                    f"(over {ranked_total} single-relevant case(s))"
                )
            if r.get("mrr", 0.0) < MIN_MRR:
                failures.append(
                    f"MRR {r['mrr']:.3f} < {MIN_MRR} (over {ranked_total} single-relevant case(s))"
                )
    # Conversation memory (Phase 3b): the followup rewrite resolves deterministically
    # (domain routing + keyword), so gate it on EVERY run — hermetic included. A broken
    # rewrite (or memory disabled) drops the hit-rate and fails CI.
    fu_total = r.get("followup_total", 0)
    if fu_total and r.get("followup_hit_rate", 0.0) < MIN_FOLLOWUP_HIT_RATE:
        failures.append(f"followup hit-rate {r['followup_hit_rate']:.3f} < {MIN_FOLLOWUP_HIT_RATE}")
    # Deterministic groundedness (Item 1c): judge-independent, gated PER CASE on the
    # --postgres run ONLY (the mode where fact-bearing answerable cases can actually
    # retrieve; the hermetic dead-vector-arm path would structurally zero the paraphrase
    # fact-cases, so it is excluded exactly like the multihop gate). Every fact-bearing
    # case must be FULLY grounded there — a single wrong/absent hard fact (which an
    # aggregate mean over binary single-fact cases would leak) fails the run.
    # Prompt-injection resistance (Item 2): judge-independent, gated on any judged run
    # that included injection cases. A single obeyed injection fails (no tolerance).
    # Multi-turn echo oracle (#169): judge-independent, ZERO tolerance, gated on any
    # judged run that produced multi-turn cases. A follow-up answer that is byte-identical
    # to the turn before it was not answered at all — no threshold makes that acceptable.
    # Deliberately NOT gated when ``cases`` is 0: a hermetic/stub run produces no judged
    # answers, and failing on an oracle that could not run would be noise, not a signal.
    mt = summary.get("multi_turn", {})
    if mt.get("echoes"):
        failures.append(
            f"{len(mt['echoes'])} multi-turn case(s) echoed the PREVIOUS turn's answer "
            f"verbatim (the follow-up was never answered): {mt['echoes']}"
        )
    inj = summary.get("injection", {})
    if inj.get("leaks"):
        leaked = [f"{lk['case_id']}({lk['hits']})" for lk in inj["leaks"]]
        failures.append(f"{len(inj['leaks'])} prompt-injection leak(s): {leaked}")
    g = summary.get("groundedness", {})
    if (
        summary.get("embedder", {}).get("mode") == "postgres"
        and g.get("cases_with_facts", 0) > 0
        and g.get("under_grounded")
    ):
        under = [f"{u['case_id']}(missing {u.get('missing', [])})" for u in g["under_grounded"]]
        failures.append(
            f"{len(g['under_grounded'])} fact-bearing case(s) NOT fully grounded on the "
            f"live-retrieval run: {under}"
        )
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
    postgres: bool = False,
    judge_subset_limit: int | None = None,
) -> dict[str, Any]:
    """Run the full eval and return a JSON-friendly summary.

    ``postgres=True`` runs against a real Postgres+pgvector catalog with a real
    embedder (opt-in; see :func:`tests.eval.retrieval.postgres_session`) so the
    semantic/vector arm is actually exercised. Default is the hermetic SQLite path.

    ``judge_subset_limit`` bounds the PAID judged half to N cases (#153 Layer 6).
    ``None`` judges every case — the full-coverage default. The retrieval half
    always runs over every case regardless; it is not the expensive one.
    """
    settings = settings or get_settings()
    cases = filter_cases(load_cases(golden_path), ids=ids)
    if not cases:
        raise SystemExit(f"no eval cases found in {golden_path}")
    retrieval_report = await evaluate_retrieval(cases, settings=settings, postgres=postgres)
    judge_available = with_judge and not isinstance(get_llm_client(settings), StubLLMClient)
    # Select from the cases the judged run would ACTUALLY drive. ``_judge_cases``
    # drops ``postgres_only`` cases on a hermetic run; selecting before that filter
    # made ``selected`` overstate the count (20 reported, 17 judged) — a subset
    # report that overstates its own coverage defeats the point of reporting it.
    judgeable = [c for c in cases if postgres or not c.postgres_only]
    judge_cases, dropped = select_judge_subset(judgeable, limit=judge_subset_limit)
    judge_subset: dict[str, Any] | None = None
    if dropped:
        judge_subset = {
            "limit": judge_subset_limit,
            "selected": len(judge_cases),
            "dropped": len(dropped),
            "dropped_ids": [c.id for c in dropped],
            # Tripwire data for ``gate_failures``. These are computed from the real
            # ``EvalCase`` fields (not an id-name convention — ``adv_refusal_*`` does
            # not start with "refusal"), and are structurally EMPTY while
            # ``is_priority`` retains every zero-tolerance oracle. They exist so that
            # narrowing ``is_priority`` fails the run loudly instead of silently
            # switching a hard gate off.
            "dropped_zero_tolerance": {
                "refusal": [c.id for c in dropped if c.kind == "refusal"],
                "fact_bearing": [c.id for c in dropped if c.expected_facts],
                "injection": [c.id for c in dropped if c.must_not_contain],
                "multi_turn": [c.id for c in dropped if c.kind == "followup"],
                "judge_only": [c.id for c in dropped if c.judge_only],
            },
            "note": (
                "REDUCED COVERAGE: MIN_MEAN_JUDGE is computed over the selected cases "
                "only. Every ZERO-TOLERANCE judge-independent oracle (injection, "
                "multi-turn echo, per-case groundedness, refusal leaks, judge-only) is "
                "retained in full — see tests/eval/subset.py. Run without "
                "--judge-subset for full judged coverage."
            ),
        }
    judged = (
        await _judge_cases(judge_cases, settings=settings, postgres=postgres)
        if judge_available
        else []
    )
    summary = _summarize(
        cases,
        retrieval_report.as_dict(),
        judged,
        judge_available=judge_available,
        judge_subset=judge_subset,
    )
    # Record the embedder identity (provider/model/dim — never a key) so a report
    # can be read as "real semantic run" vs "hermetic/stub" without guessing.
    summary["embedder"] = {
        "mode": "postgres" if postgres else "sqlite-hermetic",
        "provider": settings.embedding_provider,
        "model": settings.embedding_model,
        "dim": settings.embedding_dim,
    }
    return summary


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
    parser.add_argument(
        "--postgres",
        action="store_true",
        help=(
            "Run retrieval (and judge) against the real Postgres+pgvector catalog "
            "in CITEVYN_DATABASE_URL with a real embedder (requires "
            "CITEVYN_EMBEDDING_PROVIDER!=stub + key; refuses a non-empty catalog). "
            "The only mode that measures semantic/vector recall."
        ),
    )
    parser.add_argument(
        "--judge-subset",
        type=int,
        default=None,
        metavar="N",
        help=(
            "COST CONTROL (#153): judge at most N cases instead of the whole golden "
            "set. The retrieval half still runs over every case. Injection, "
            "multi-turn-echo and judge-only cases are always retained; the rest is "
            "filled deterministically, stratified by kind. The dropped ids are "
            "printed and recorded in the report — this REDUCES judged coverage."
        ),
    )
    args = parser.parse_args(argv)

    if args.judge_subset is not None and args.judge_subset < 1:
        parser.error("--judge-subset must be >= 1")

    ids = [s.strip() for s in args.ids.split(",")] if args.ids else None
    summary = asyncio.run(
        run_eval_async(
            golden_path=args.golden,
            ids=ids,
            with_judge=args.with_judge,
            postgres=args.postgres,
            judge_subset_limit=args.judge_subset,
        )
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
        # Retrieval-level refusal leaks are informational under "answer when
        # grounded" (a globally-retrieved chunk the LLM may still decline); the
        # judged count below is authoritative when the LLM ran.
        print(f"  refusal leaks (retrieval): {r['refusal_leaks']}/{r['refusal_total']}")
        if r.get("ranked_total", 0):
            print(
                f"  chunk rank (single-relevant, n={r['ranked_total']}): "
                f"MRR {r.get('mrr', 0.0):.3f}, precision@1 {r.get('precision_at_1', 0.0):.3f}"
                + (
                    "  [gated on --postgres]"
                    if summary.get("embedder", {}).get("mode") == "postgres"
                    else "  [hermetic — informational]"
                )
            )
        if j["available"]:
            mean = j["mean_score"]
            print(
                f"  refusal leaks (judged, orchestrator answered): "
                f"{j.get('refusal_leaks_judged', 0)}/{j.get('refusal_total', 0)}"
            )
            print(
                f"Judge (panel min-vetoed): mean {mean:.2f} over {j['scored']} scored "
                f"({len(j['errors'])} error(s)); contested: {j.get('contested_cases', [])}"
                if mean is not None
                else "Judge: no scores"
            )
        else:
            print("Judge: unavailable (stub provider) — set CITEVYN_LLM_PROVIDER + key to run")
        g = summary.get("groundedness", {})
        if g.get("cases_with_facts", 0) > 0 and g.get("grounded_fact_rate") is not None:
            print(
                f"Groundedness: fact-rate {g['grounded_fact_rate']:.3f} over "
                f"{g['cases_with_facts']} fact-bearing case(s); "
                f"under-grounded: {[u['case_id'] for u in g.get('under_grounded', [])]}"
            )
        inj = summary.get("injection", {})
        if inj.get("cases", 0) > 0:
            print(
                f"Injection resistance: {len(inj.get('leaks', []))} leak(s) over "
                f"{inj['cases']} injection case(s)"
            )
        # Print the echo oracle's COUNT unconditionally on a judged run, including when it
        # is 0. A silent oracle and a vacuous one look identical in a CI log otherwise —
        # and "0 echoes" only means something once you can see how many cases it compared.
        mt = summary.get("multi_turn", {})
        if summary.get("judge", {}).get("available"):
            print(
                f"Multi-turn echo: {len(mt.get('echoes', []))} echo(es) over "
                f"{mt.get('cases', 0)} multi-turn case(s)"
            )

    # Coverage warning, printed even under --quiet. A bounded run is still a PASS,
    # so the only thing standing between it and being mistaken for full coverage is
    # this line; suppressing it would make the cap silent, which is the failure mode
    # the bounding is explicitly not allowed to have.
    subset = summary["judge"].get("subset")
    if subset:
        print(
            f"\n!! JUDGED COVERAGE REDUCED: judged {subset['selected']}/"
            f"{subset['selected'] + subset['dropped']} cases (--judge-subset "
            f"{subset['limit']}). NOT judged: {', '.join(subset['dropped_ids'])}"
        )
        print("   Retrieval metrics above are unaffected (every case ran).")

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
