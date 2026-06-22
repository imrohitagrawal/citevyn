"""Live executor that drives each :class:`GoldenCase` against a TestClient.

This is the third (and only stateful) layer of the runner:

* :mod:`.cases`     — parse YAML.
* :mod:`.scoring`   — pure value objects + assertion helpers.
* :mod:`.runner`    — this module: spins up a seeded TestClient,
                      dispatches per-case, collects :class:`CaseResult`.

The entry point is :func:`run_all`, which accepts a case directory,
optional id filter, and a JSON output path.  The function is
reusable from a Makefile target, a CI step, and a pytest plugin
without any external orchestration framework.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import Base, IndexStatus, IndexVersion
from tests.conftest import seed_catalog

from .cases import GoldenCase, filter_cases, load_cases
from .scoring import (
    CaseResult,
    _check_contains_any,
    _check_eq,
    _check_flag,
    assert_answer_response,
    assert_error_envelope,
    assert_search_response,
)

DEMO_BEARER = "Bearer local-demo-key"
# Cases live at <repo>/tests/golden/cases/.  Resolve relative to the
# current file so ``python -m tests.golden.runner`` from any CWD works.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_CASES_DIR = REPO_ROOT / "tests" / "golden" / "cases"
DEFAULT_REPORT_PATH = pathlib.Path("artifacts/golden_report.json")


# ---------------------------------------------------------------------------
# TestClient setup
# ---------------------------------------------------------------------------


def _build_seeded_client(
    monkeypatch_module: Any | None = None,
) -> TestClient:
    """Return a TestClient backed by a fresh SQLite file with the demo seed.

    We use a temp file (not ``:memory:``) because each async
    connection in a pool otherwise sees a separate database and
    the route's :func:`get_session` would not see the seeded rows.
    """
    import pytest  # local import keeps the runner importable without pytest

    monkeypatch = monkeypatch_module or pytest.MonkeyPatch()
    db_module.reset_engine()
    get_settings.cache_clear()
    fd, db_path = tempfile.mkstemp(prefix="golden-", suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    # The runner disables rate limiting so 50 cases don't exhaust the 30 q/h demo limit.
    monkeypatch.setenv("CITEVYN_RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()
    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())

    factory = get_sessionmaker()

    async def _seed() -> None:
        async with factory() as session:
            version = IndexVersion(
                index_version="index_v1",
                status=IndexStatus.active,
                source_version_hash="sha256:index-v1",
                created_at=datetime.now(UTC),
                promoted_at=datetime.now(UTC),
            )
            session.add(version)
            await session.flush()
            await seed_catalog(session)

    asyncio.run(_seed())
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Per-case dispatch
# ---------------------------------------------------------------------------


def _open_session(client: TestClient) -> str:
    create = client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    if create.status_code != 201:
        raise RuntimeError(f"failed to create session: {create.status_code} {create.text}")
    return create.json()["session_id"]


def _post_message(
    client: TestClient, session_id: str, *, message: str, answer_style: str | None
) -> tuple[int, Any]:
    body: dict[str, Any] = {"message": message}
    if answer_style is not None:
        body["answer_style"] = answer_style
    response = client.post(
        f"/v1/sessions/{session_id}/messages",
        json=body,
        headers={"Authorization": DEMO_BEARER},
    )
    try:
        return response.status_code, response.json()
    except Exception:  # noqa: BLE001
        return response.status_code, response.text


def _post_search(
    client: TestClient, *, term: str, product_area: str, term_type: str | None
) -> tuple[int, Any]:
    body: dict[str, Any] = {"term": term, "product_area": product_area}
    if term_type is not None:
        body["term_type"] = term_type
    response = client.post("/v1/search/exact", json=body, headers={"Authorization": DEMO_BEARER})
    try:
        return response.status_code, response.json()
    except Exception:  # noqa: BLE001
        return response.status_code, response.text


def _run_answer_case(client: TestClient, case: GoldenCase) -> CaseResult:
    result = CaseResult(case=case)
    session_id = _open_session(client)
    status, body = _post_message(
        client,
        session_id,
        message=case.question,
        answer_style=case.answer_style,
    )
    result.response_payload = {"status": status, "body": body}
    if case.expect_http_400:
        assert_error_envelope(result, status=status, expected=400, body=body)
        return result
    if case.expect_http_422:
        assert_error_envelope(result, status=status, expected=422, body=body)
        return result
    if status != 200:
        result.fail("http.status", f"expected 200, got {status}")
        return result
    assert_answer_response(case, result, body)
    if case.expect_no_answer:
        _no_answer = body.get("no_answer", False)
        if _no_answer:
            result.ok("response.no_answer", "True")
        else:
            result.fail("response.no_answer", "expected True")
    return result


def _run_search_case(client: TestClient, case: GoldenCase) -> CaseResult:
    result = CaseResult(case=case)
    status, body = _post_search(
        client,
        term=case.search_term or "",
        product_area=case.search_product_area or "",
        term_type=case.search_term_type,
    )
    result.response_payload = {"status": status, "body": body}
    if status != 200:
        result.fail("http.status", f"expected 200, got {status}: {body}")
        return result
    assert_search_response(case, result, body)
    return result


def _run_cache_case(client: TestClient, case: GoldenCase) -> CaseResult:
    """Ask the same question twice; the second call must be a cache hit."""
    result = CaseResult(case=case)
    session_id = _open_session(client)
    status1, body1 = _post_message(
        client,
        session_id,
        message=case.question,
        answer_style=case.answer_style,
    )
    if status1 != 200:
        result.fail("http.status.first", f"expected 200, got {status1}")
        result.response_payload = {"status": status1, "body": body1}
        return result
    status2, body2 = _post_message(
        client,
        session_id,
        message=case.question,
        answer_style=case.answer_style,
    )
    if status2 != 200:
        result.fail("http.status.second", f"expected 200, got {status2}")
        result.response_payload = {"first": body1, "second_status": status2, "second_body": body2}
        return result
    result.response_payload = {"first": body1, "second": body2}
    for key, expected in case.first_assert.items():
        _check_flag(
            result,
            label=f"first.{key}",
            actual=body1.get(key),
            expected=expected,
        )
    for key, expected in case.second_assert.items():
        _check_flag(
            result,
            label=f"second.{key}",
            actual=body2.get(key),
            expected=expected,
        )
    assert_answer_response(case, result, body2)
    return result


def _run_multi_turn_case(client: TestClient, case: GoldenCase) -> CaseResult:
    """POST N user messages; assert on the last one only."""
    result = CaseResult(case=case)
    session_id = _open_session(client)
    bodies: list[Any] = []
    last_status: int | None = None
    for message in case.messages:
        if message.get("role") != "user":
            continue
        last_status, body = _post_message(
            client,
            session_id,
            message=message["content"],
            answer_style=case.answer_style,
        )
        bodies.append(body)
        if last_status != 200:
            result.fail("http.status", f"got {last_status}")
            result.response_payload = bodies
            return result
    result.response_payload = bodies
    final = bodies[-1] if bodies else {}
    if case.expect_last_domain is not None:
        _check_eq(
            result,
            label="last.domain",
            actual=final.get("domain"),
            expected=case.expect_last_domain,
        )
    if case.expect_last_answer_contains:
        _check_contains_any(
            result,
            label="last.answer_contains",
            haystack=final.get("answer", "") or "",
            needles=case.expect_last_answer_contains,
        )
    if case.expect_last_citation_url_contains:
        cites = final.get("citations") or []
        urls = " ".join(str(c.get("url", "")) for c in cites)
        if case.expect_last_citation_url_contains in urls:
            result.ok(
                "last.citation_url_contains",
                f"{case.expect_last_citation_url_contains!r} found",
            )
        else:
            result.fail(
                "last.citation_url_contains",
                f"{case.expect_last_citation_url_contains!r} not in {urls!r}",
            )
    return result


def run_case(client: TestClient, case: GoldenCase) -> CaseResult:
    """Dispatch one case by its ``type`` field."""
    try:
        if case.type == "answer":
            return _run_answer_case(client, case)
        if case.type == "no_answer":
            return _run_answer_case(client, case)
        if case.type == "search":
            return _run_search_case(client, case)
        if case.type == "cache":
            return _run_cache_case(client, case)
        if case.type == "multi_turn":
            return _run_multi_turn_case(client, case)
        if case.type == "unsupported":
            return _run_answer_case(client, case)
        # Unknown type -> fail loudly so the runner doesn't silently skip.
        result = CaseResult(case=case)
        result.fail("dispatch", f"unknown case type {case.type!r}")
        return result
    except Exception as exc:  # noqa: BLE001
        result = CaseResult(case=case)
        result.error = f"{type(exc).__name__}: {exc}"
        return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_all(
    *,
    cases_dir: pathlib.Path = DEFAULT_CASES_DIR,
    ids: list[str] | None = None,
    report_path: pathlib.Path | None = DEFAULT_REPORT_PATH,
    fresh_client_per_case: bool = True,
) -> dict[str, Any]:
    """Execute every case (optionally filtered) and write a JSON report.

    A fresh TestClient is built for every case by default so the
    in-memory answer cache (and any other in-process state) cannot
    leak between cases.  When ``fresh_client_per_case=False`` we
    reuse a single client; useful for debugging but unfit for CI.
    """
    cases = filter_cases(load_cases(cases_dir), ids=ids)
    if not cases:
        raise SystemExit(f"no golden cases found under {cases_dir}")
    shared_client: TestClient | None = None
    if not fresh_client_per_case:
        shared_client = _build_seeded_client()
    results: list[CaseResult] = []
    for case in cases:
        client = shared_client or _build_seeded_client()
        results.append(run_case(client, case))
    from .scoring import summarize

    summary = summarize(results)
    summary["generated_at"] = datetime.now(UTC).isoformat()
    summary["cases_dir"] = str(cases_dir)
    summary["ids_filter"] = ids
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w") as fh:
            json.dump(summary, fh, indent=2, default=str)
    return summary


def main(argv: list[str] | None = None) -> int:
    """Tiny CLI used by ``make golden`` and the nightly workflow."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="golden-runner",
        description="Execute CiteVyn golden cases and emit a JSON report.",
    )
    parser.add_argument(
        "--cases-dir",
        type=pathlib.Path,
        default=DEFAULT_CASES_DIR,
        help="Directory containing golden case YAML files.",
    )
    parser.add_argument(
        "--ids",
        type=str,
        default=None,
        help="Comma-separated case id filter (default: run all).",
    )
    parser.add_argument(
        "--report",
        type=pathlib.Path,
        default=DEFAULT_REPORT_PATH,
        help="Output JSON report path.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-case progress; only print the final summary.",
    )
    args = parser.parse_args(argv)
    ids = [s.strip() for s in args.ids.split(",")] if args.ids else None
    summary = run_all(
        cases_dir=args.cases_dir,
        ids=ids,
        report_path=args.report,
    )
    if not args.quiet:
        for case_summary in summary["cases"]:
            status = "PASS" if case_summary["passed"] else "FAIL"
            print(f"{status} {case_summary['case_id']}: {case_summary['title']}")
    print(
        f"\nGolden runner: {summary['passed']}/{summary['total']} passed "
        f"({summary['failed']} failed). Report: {args.report}"
    )
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
