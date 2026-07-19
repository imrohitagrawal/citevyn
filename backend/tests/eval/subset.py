"""Bounded judged-eval case selection (cost control, #153 Layer 6).

The judged half of the eval harness is the project's single largest recurring
spend line: every case drives one real orchestrator answer (plus, for a
``followup`` case, one answer per replayed history turn) and then
``CITEVYN_EVAL_JUDGE_PANEL + 1`` judge calls. Across the 58-case golden set that
is ~200 paid model calls **per CI run**, and the judged job runs on every pull
request. With zero users, CI *is* the spend.

This module bounds that without pretending the coverage did not change. The
retrieval half — hit-rate, MRR/precision@1, refusal leaks — is unaffected and
still runs over every case: it is essentially free (one short query embedding
per case).

Selection contract
------------------

:func:`select_judge_subset` is **deterministic** (no RNG, no clock) so the same
golden file and the same limit always pick the same cases — a subset run that
fails is reproducible locally with the printed ``--ids``.

It is also **priority-preserving**. Some cases carry a **zero-tolerance,
judge-independent** oracle — a gate that fails the run on a single occurrence and
does not depend on the LLM judge's opinion. Dropping one of those does not reduce
a mean, it *switches the gate off*, so they are ALWAYS retained regardless of the
limit:

* ``must_not_contain`` — prompt-injection resistance (any leak fails).
* ``kind == "followup"`` — the multi-turn echo oracle (#169), which caught a bug
  that passed every other metric by construction (any echo fails).
* ``expected_facts`` — deterministic groundedness, gated PER CASE at coverage 1.0
  on the ``--postgres`` run (a single wrong install command or auth header fails).
* ``kind == "refusal"`` — the judged refusal-leak gate. This one is load-bearing
  in a second way: ``gate_failures`` falls back to the *retrieval* leak count only
  when the judge did NOT run (an ``elif``), so on a judged run the judged count is
  the only refusal gate there is. A dropped refusal case is checked by neither.
* ``judge_only`` — a case that has no other validation path at all.

Only the judge's own 1–5 *mean* (``MIN_MEAN_JUDGE``) is a genuine average that
degrades gracefully under sampling. Everything else on the judged run is
all-or-nothing, which is why the priority pool is large.

**Consequence, stated plainly:** on the current golden set that pool is 42 of 58
cases, so the maximum saving from bounding is ~28%, not the ~65% a naive read of
``--judge-subset 20`` suggests. That is why CI does **not** use this to sample
every PR (it bounds judged-run *frequency* instead — see ``docs/COST_CONTROLS.md``
§6). This remains a useful local tool for iterating on a handful of cases.

A limit smaller than the priority pool does not drop an oracle; the pool is kept
and the effective size is reported. Everything beyond it is filled **stratified by
kind**, round-robin in a stable kind order, taking cases in golden-file order.

The caller is expected to report the dropped ids loudly. A silent cap reads as
"we covered everything" when we did not.
"""

from __future__ import annotations

from collections.abc import Iterable

from .cases import EvalCase

# Stable round-robin order for the stratified fill, most-informative kind first.
#
# ``followup`` and ``refusal`` are deliberately absent: every case of either kind
# is already in the priority pool, so a lane for them would always be empty. Any
# kind not named here still gets a lane appended at selection time, so a future
# kind is never silently excluded.
_KIND_ORDER: tuple[str, ...] = ("literal", "paraphrase", "multihop")


def is_priority(case: EvalCase) -> bool:
    """True when ``case`` carries a zero-tolerance judge-independent oracle.

    See the module docstring for why each class qualifies. The test suite pins
    each one separately, so adding a new zero-tolerance gate to the runner without
    adding it here fails a test rather than silently narrowing the gate.
    """
    return (
        bool(case.must_not_contain)
        or bool(case.expected_facts)
        or case.kind in ("followup", "refusal")
        or case.judge_only
    )


def select_judge_subset(
    cases: Iterable[EvalCase], *, limit: int | None
) -> tuple[list[EvalCase], list[EvalCase]]:
    """Split ``cases`` into ``(selected, dropped)`` for the judged run.

    ``limit`` is the target number of judged cases. ``None`` (or a limit at or
    above the case count) selects everything and drops nothing — the full-run
    path, which must stay byte-identical to the pre-subset behaviour.

    The returned lists preserve the input (golden-file) order so a report reads
    in the same sequence as the golden file.
    """
    ordered = list(cases)
    # Validate BEFORE the full-run short-circuit: with the checks the other way
    # round, ``select_judge_subset([], limit=-5)`` would take the ``limit >= 0``
    # branch and return quietly instead of rejecting a nonsense limit.
    if limit is not None and limit < 1:
        raise ValueError(f"judge subset limit must be >= 1 (got {limit})")
    if limit is None or limit >= len(ordered):
        return ordered, []

    keep: set[str] = {c.id for c in ordered if is_priority(c)}

    # Stratified round-robin fill over the non-priority remainder.
    by_kind: dict[str, list[EvalCase]] = {}
    for case in ordered:
        if case.id in keep:
            continue
        by_kind.setdefault(case.kind, []).append(case)
    # Any kind not named in _KIND_ORDER still gets a lane (appended in first-seen
    # order) so a future kind is never silently excluded from every subset.
    lanes = [k for k in _KIND_ORDER if k in by_kind] + [k for k in by_kind if k not in _KIND_ORDER]
    cursors = dict.fromkeys(lanes, 0)
    while len(keep) < limit:
        progressed = False
        for kind in lanes:
            if len(keep) >= limit:
                break
            idx = cursors[kind]
            if idx >= len(by_kind[kind]):
                continue
            keep.add(by_kind[kind][idx].id)
            cursors[kind] = idx + 1
            progressed = True
        if not progressed:  # pragma: no cover - unreachable while limit < len(ordered)
            break

    selected = [c for c in ordered if c.id in keep]
    dropped = [c for c in ordered if c.id not in keep]
    return selected, dropped


__all__ = ["is_priority", "select_judge_subset"]
