"""Grader latency of one judged pass (#598 Step 4), from its grader-call records.

Pure: it reads the records the runner already keeps (``duration_seconds`` and
``rationale`` per call) and makes no call. A call that raised (its rationale starts
"call failed:", a timeout included) is counted apart with its seconds and kept out of
the percentiles; every other call completed, a malformed verdict included (it took its
time and came back). The percentiles are ``statistics.quantiles(method="inclusive")``
over the completed calls, the method Step 3's p50 1.04 s / p90 2.09 s were computed
with. With no completed call they are ``None`` ("not available"), never 0: a 0 s
grader would read as a fast one.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from typing import Any

FAILED_PREFIX = "call failed:"
NOT_AVAILABLE = "not available"


def _failed(call: Mapping[str, Any]) -> bool:
    return str(call.get("rationale") or "").startswith(FAILED_PREFIX)


def _seconds(call: Mapping[str, Any]) -> float:
    return float(call.get("duration_seconds") or 0.0)


def grader_latency(calls: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """``calls``, ``completed``, ``failed``, ``failed_seconds``, ``p50_seconds``,
    ``p90_seconds`` and ``max_seconds`` over the given grader-call records."""
    records = list(calls)
    failed = [c for c in records if _failed(c)]
    done = sorted(_seconds(c) for c in records if not _failed(c))
    p50: float | None = None
    p90: float | None = None
    if len(done) == 1:
        # ``quantiles`` needs two points on Python before 3.13; one point is every
        # percentile.
        p50 = p90 = done[0]
    elif done:
        cuts = statistics.quantiles(done, n=10, method="inclusive")
        p50, p90 = cuts[4], cuts[8]
    return {
        "calls": len(records),
        "completed": len(done),
        "failed": len(failed),
        "failed_seconds": round(sum(_seconds(c) for c in failed), 3),
        "p50_seconds": None if p50 is None else round(p50, 3),
        "p90_seconds": None if p90 is None else round(p90, 3),
        "max_seconds": round(done[-1], 3) if done else None,
    }


def latency_line(by_kind: Mapping[str, Mapping[str, Any]]) -> str:
    """One log line for a pass: each kind's counts and percentiles."""

    def part(kind: str, figures: Mapping[str, Any]) -> str:
        head = (
            f"{kind} {figures['calls']} call(s), {figures['completed']} completed, "
            f"{figures['failed']} failed ({figures['failed_seconds']} s)"
        )
        if figures["p50_seconds"] is None:
            return f"{head}: {NOT_AVAILABLE}"
        return (
            f"{head}: p50 {figures['p50_seconds']} s, p90 {figures['p90_seconds']} s, "
            f"max {figures['max_seconds']} s"
        )

    return "grader latency: " + "; ".join(part(k, v) for k, v in by_kind.items())
