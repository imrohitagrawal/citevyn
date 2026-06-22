"""Result + scoring primitives for the golden runner.

These dataclasses are intentionally simple value objects:

* :class:`Check` is one assertion that produced a pass or a fail.
* :class:`CaseResult` aggregates every check for one YAML case and
  captures the live response payload for forensic inspection.
* :func:`summarize` collapses a list of ``CaseResult`` into a JSON-
  friendly summary suitable for the CI smoke test.

The runner uses :func:`assert_response_matches` to convert a case's
expectations into a list of :class:`Check`. Each check is additive;
a case with 6 expectations produces 6 checks. A single failure
flags the case as failed but the runner keeps going so a CI report
shows every regression in one pass.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from typing import Any

from .cases import GoldenCase


@dataclasses.dataclass(frozen=True)
class Check:
    """One pass/fail assertion."""

    name: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclasses.dataclass
class CaseResult:
    """All checks for one case, plus the captured response payload."""

    case: GoldenCase
    checks: list[Check] = dataclasses.field(default_factory=list)
    response_payload: Any = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks) and self.error is None

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name=name, passed=passed, detail=detail))

    def fail(self, name: str, detail: str) -> None:
        self.add(name, False, detail)

    def ok(self, name: str, detail: str = "") -> None:
        self.add(name, True, detail)

    def skip(self, name: str, detail: str = "") -> None:
        # Skipped checks count as passed (the case asserts nothing).
        self.add(name, True, f"SKIP: {detail}" if detail else "SKIP")

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case.id,
            "title": self.case.title,
            "area": self.case.area,
            "type": self.case.type,
            "passed": self.passed,
            "error": self.error,
            "checks": [c.as_dict() for c in self.checks],
            "response": self.response_payload,
        }


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _check_contains_any(
    result: CaseResult, *, label: str, haystack: str, needles: Iterable[str]
) -> None:
    """Pass if every needle appears in haystack (case-insensitive)."""
    if not needles:
        return
    low = haystack.lower()
    missing = [n for n in needles if n.lower() not in low]
    if missing:
        result.fail(label, f"missing substrings {missing!r} in response")
    else:
        result.ok(label, f"all {len(list(needles))} substrings present")


def _check_eq(result: CaseResult, *, label: str, actual: Any, expected: Any) -> None:
    if expected is None:
        result.skip(label, "no expectation set")
        return
    if actual == expected:
        result.ok(label, f"{actual!r} == {expected!r}")
    else:
        result.fail(label, f"got {actual!r}, expected {expected!r}")


def _check_ge(result: CaseResult, *, label: str, actual: int | float, minimum: int | float) -> None:
    if actual >= minimum:
        result.ok(label, f"{actual} >= {minimum}")
    else:
        result.fail(label, f"got {actual}, expected >= {minimum}")


def _check_flag(result: CaseResult, *, label: str, actual: bool, expected: bool) -> None:
    if actual is expected:
        result.ok(label, f"{actual!r}")
    else:
        result.fail(label, f"got {actual!r}, expected {expected!r}")


# ---------------------------------------------------------------------------
# High-level assertion routines
# ---------------------------------------------------------------------------


def assert_answer_response(case: GoldenCase, result: CaseResult, body: Mapping[str, Any]) -> None:
    """Apply every ``expect_*`` field relevant to a grounded answer body."""
    _check_eq(
        result, label="response.domain", actual=body.get("domain"), expected=case.expect_domain
    )
    if case.expect_intent is not None:
        _check_eq(
            result, label="response.intent", actual=body.get("intent"), expected=case.expect_intent
        )
    if case.expect_confidence is not None:
        _check_eq(
            result,
            label="response.confidence",
            actual=body.get("confidence"),
            expected=case.expect_confidence,
        )
    if case.expect_retrieval_strategy is not None:
        _check_eq(
            result,
            label="response.retrieval_strategy",
            actual=body.get("retrieval_strategy"),
            expected=case.expect_retrieval_strategy,
        )
    if case.expect_citations_min is not None:
        cites = body.get("citations") or []
        _check_ge(
            result,
            label="response.citations_min",
            actual=len(cites),
            minimum=case.expect_citations_min,
        )
    if case.expect_answer_contains:
        answer = body.get("answer", "") or ""
        _check_contains_any(
            result,
            label="response.answer_contains",
            haystack=answer,
            needles=case.expect_answer_contains,
        )
    if case.expect_citation_url_contains:
        cites = body.get("citations") or []
        if not cites:
            result.fail(
                "response.citation_url_contains",
                "no citations to inspect",
            )
        else:
            urls = " ".join(str(c.get("url", "")) for c in cites)
            if case.expect_citation_url_contains in urls:
                result.ok(
                    "response.citation_url_contains",
                    f"{case.expect_citation_url_contains!r} found in citation URLs",
                )
            else:
                result.fail(
                    "response.citation_url_contains",
                    f"{case.expect_citation_url_contains!r} not in {urls!r}",
                )
    if case.expect_answer_cites_index is not None:
        answer = body.get("answer", "") or ""
        has_bracket_one = "[1]" in answer
        if case.expect_answer_cites_index and has_bracket_one:
            result.ok("response.answer_cites_index", "answer references [1]")
        elif case.expect_answer_cites_index and not has_bracket_one:
            result.fail("response.answer_cites_index", "answer missing [1]")
        elif not case.expect_answer_cites_index and not has_bracket_one:
            result.ok("response.answer_cites_index", "answer correctly omits [1]")
        else:
            result.fail(
                "response.answer_cites_index",
                "answer unexpectedly contains [1]",
            )


def assert_search_response(case: GoldenCase, result: CaseResult, body: Mapping[str, Any]) -> None:
    """Apply every ``expect_search_*`` field."""
    if case.expect_search_total_ge is not None:
        total = body.get("total", 0)
        _check_ge(
            result,
            label="search.total_ge",
            actual=total,
            minimum=case.expect_search_total_ge,
        )
    if case.expect_search_hit_url_contains:
        hits = body.get("hits") or []
        if not hits:
            result.fail("search.url_contains", "no hits to inspect")
        else:
            joined = " ".join(str(h.get("url", "")) for h in hits)
            if case.expect_search_hit_url_contains in joined:
                result.ok("search.url_contains", joined)
            else:
                result.fail(
                    "search.url_contains",
                    f"substring {case.expect_search_hit_url_contains!r} not in {joined!r}",
                )
    if case.expect_search_hit_product_area:
        hits = body.get("hits") or []
        if not hits:
            result.fail("search.product_area", "no hits to inspect")
        else:
            areas = [str(h.get("product_area", "")) for h in hits]
            if case.expect_search_hit_product_area in areas:
                result.ok(
                    "search.product_area",
                    f"{case.expect_search_hit_product_area!r} in {areas}",
                )
            else:
                result.fail(
                    "search.product_area",
                    f"{case.expect_search_hit_product_area!r} not in {areas!r}",
                )


def assert_error_envelope(result: CaseResult, *, status: int, expected: int, body: Any) -> None:
    _check_eq(result, label="http.status", actual=status, expected=expected)
    if isinstance(body, dict) and "code" in body:
        # The standard error envelope must have a non-empty request_id.
        request_id = body.get("request_id", "")
        if request_id:
            result.ok("envelope.request_id", request_id)
        else:
            result.fail("envelope.request_id", "missing")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    """Collapse a list of case results into a JSON-friendly summary."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / total) if total else 1.0,
        "cases": [r.as_dict() for r in results],
    }
