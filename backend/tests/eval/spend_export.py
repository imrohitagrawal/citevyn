"""The recorded spend of a judged run, exported for the run's artifacts (#598 Step 3).

Read-only: it reads every ``provider_calls`` row of the database in
``CITEVYN_DATABASE_URL`` (the judged job's database is fresh, so its rows are this
run's), makes no model call and writes nothing to the database. Usage, from
``backend/``::

    python -m tests.eval.spend_export --out ../artifacts/spend.json \\
        [--report ../artifacts/eval_report.json]

The figure is the RECORDED BOOK COST: the cost the meter stored for each row at
the price book's rates. It is not a guaranteed total; ``gaps`` names what it
cannot see. With ``--report``, the failed calls the eval itself recorded (which
leave no row) are counted per model as ``observed_failures``, and the primary
calls the fallback answered, with their causes, as ``primary_failures`` (#576).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
from collections import Counter
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.llm.attempts import cause_key
from app.models.provider_calls import ProviderCall

LABEL = "recorded book cost"
GAPS = (
    "Failed attempts that were never recorded are not in the cost: a primary call "
    "the fallback hid is counted, with its cause, under primary_failures, but any "
    "tokens it used are not in the cost (#496); a turn on which both providers "
    "failed leaves no row.",
    "Meter writes that were lost are not in the cost: the meter never fails a call, "
    "so a row it could not write is silently missing.",
    "Rows with estimated tokens (the provider returned no usage) carry an estimated "
    "cost, which can read low.",
    "Unpriced rows (a model missing from the price book) are counted as zero.",
)


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def _empty() -> dict[str, Any]:
    return {
        "rows": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "book_cost_usd": Decimal(0),
        "priced_rows": 0,
        "unpriced_rows": 0,
        "tokens_estimated_rows": 0,
    }


def _add(total: dict[str, Any], row: ProviderCall) -> None:
    total["rows"] += 1
    total["input_tokens"] += row.input_tokens or 0
    total["output_tokens"] += row.output_tokens or 0
    total["book_cost_usd"] += Decimal(row.cost_usd or 0)
    total["priced_rows" if row.priced else "unpriced_rows"] += 1
    total["tokens_estimated_rows"] += 1 if row.tokens_estimated else 0


_REFUSED = "CostLimitReached"
ATTRIBUTION = (
    "A failed grader call records no model, so it is counted under the configured grader model."
)


def _grader_call_outcome(call: dict[str, Any]) -> str | None:
    """``"refused"`` (the spend cap stopped it before it was sent), ``"failed"``,
    or ``None`` for a call that returned."""
    rationale = str(call.get("rationale", ""))
    if call.get("score") is not None or not rationale.startswith("call failed:"):
        return None
    refused = call.get("refused_by_cap") or rationale.startswith(f"call failed: {_REFUSED}:")
    return "refused" if refused else "failed"


def failed_calls(report: dict[str, Any] | None) -> tuple[dict[str, int], dict[str, int]]:
    """The failed calls the eval recorded, by model, in both passes, and apart from
    them the calls the spend cap refused (never sent, so never billed): answer
    client calls marked ``failed``, and grader calls that raised (blind, passage
    and probe), the latter under the report's grader model (see ``ATTRIBUTION``)."""
    failed, refused, _seconds = _tally(report)
    return failed, refused


NOT_MEASURED = "not measured"


def failure_seconds(report: dict[str, Any] | None) -> dict[str, float | str]:
    """The total duration of the observed failures, by model (as recorded)."""
    return _tally(report)[2]


def _tally(
    report: dict[str, Any] | None,
) -> tuple[dict[str, int], dict[str, int], dict[str, float | str]]:
    if not report:
        return {}, {}, {}
    grader = (report.get("grader") or {}).get("model") or (report.get("models") or {}).get("judge")
    counts: dict[str, Counter[str]] = {"failed": Counter(), "refused": Counter()}
    seconds: dict[str, float] = {}
    failed_models: set[str] = set()

    def note(outcome: str, model: str, call: dict[str, Any]) -> None:
        counts[outcome][model] += 1
        if outcome == "failed":
            failed_models.add(model)
            if call.get("duration_seconds") is not None:
                seconds[model] = seconds.get(model, 0.0) + float(call["duration_seconds"])

    grader_calls: list[dict[str, Any]] = []
    sections = [report.get("judge") or {}, ((report.get("fallback") or {}).get("judge")) or {}]
    for section in sections:
        for case in section.get("cases") or []:
            for call in case.get("model_calls") or []:
                if call.get("failed"):
                    refused = call.get("refused_by_cap") or call.get("error_type") == _REFUSED
                    outcome = "refused" if refused else "failed"
                    note(outcome, str(call.get("model") or "unknown"), call)
            grader_calls += case.get("grader_calls") or []
            grader_calls += case.get("passage_grader_calls") or []
    probe = (report.get("grader_check") or {}).get("probe") or {}
    grader_calls += probe.get("calls") or []
    for call in grader_calls:
        outcome = _grader_call_outcome(call)
        if outcome is not None:
            note(outcome, str(grader or "unknown"), call)
    return (
        dict(sorted(counts["failed"].items())),
        dict(sorted(counts["refused"].items())),
        # A model whose failed calls carry no duration (a run with the grader
        # check off) is "not measured", never 0.0 s.
        {m: seconds.get(m, NOT_MEASURED) for m in sorted(failed_models)},
    )


def primary_failures(report: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The primary calls the fallback answered (or failed after), by primary model,
    in both passes: their count, causes and total duration (#576). Never folded
    into ``observed_failures``: those calls left no row and are reported apart."""
    found: dict[str, dict[str, Any]] = {}
    sections = [(report or {}).get("judge") or {}]
    sections.append((((report or {}).get("fallback") or {}).get("judge")) or {})
    for section in sections:
        for case in section.get("cases") or []:
            for call in case.get("model_calls") or []:
                for note in call.get("primary_failures") or []:
                    model = str(note.get("model") or "unknown")
                    entry = found.setdefault(
                        model, {"calls": 0, "by_cause": Counter(), "seconds": 0.0}
                    )
                    entry["calls"] += 1
                    entry["by_cause"][cause_key(note)] += 1
                    entry["seconds"] += float(note.get("duration_seconds") or 0.0)
    return {
        model: {**entry, "by_cause": dict(sorted(entry["by_cause"].items()))}
        for model, entry in sorted(found.items())
    }


def observed_failures(report: dict[str, Any] | None) -> dict[str, int]:
    """The failed calls the eval recorded, by model (cap refusals excluded)."""
    return failed_calls(report)[0]


def summarize(rows: list[ProviderCall], report: dict[str, Any] | None = None) -> dict[str, Any]:
    """The export from rows already read and the eval report (or ``None``)."""
    failures, refused = failed_calls(report)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    totals = _empty()
    for row in rows:
        key = (row.provider, row.model, row.kind)
        _add(groups.setdefault(key, _empty()), row)
        _add(totals, row)
    return {
        "label": LABEL,
        "groups": [
            {
                "provider": provider,
                "model": model,
                "kind": kind,
                **group,
                "book_cost_usd": _money(group["book_cost_usd"]),
            }
            for (provider, model, kind), group in sorted(groups.items())
        ],
        "totals": {
            "rows": totals["rows"],
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "book_cost_usd": _money(totals["book_cost_usd"]),
            "priced_rows": totals["priced_rows"],
            "unpriced_rows": totals["unpriced_rows"],
            "tokens_estimated_rows": totals["tokens_estimated_rows"],
        },
        "observed_failures": failures,
        # How long the observed failures took, by model (failed answer calls carry
        # a duration only with the grader check on).
        "observed_failure_seconds": failure_seconds(report),
        # Calls the spend cap stopped before they were sent: not spend, not failures.
        "refused_by_cap": refused,
        # Primary calls the fallback answered, with their causes (#576); their
        # tokens are not in the cost (``gaps``).
        "primary_failures": primary_failures(report),
        "attribution": ATTRIBUTION,
        "gaps": list(GAPS),
        # The run's cap record, copied from the report (#598 Step 4): this step does
        # not see the eval step's settings. ``None`` without a report or the record.
        "spend_cap": ((report or {}).get("grader") or {}).get("spend_cap"),
    }


async def read_rows(database_url: str) -> list[ProviderCall]:
    """Every ``provider_calls`` row, read in one SELECT."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(select(ProviderCall))
            return [ProviderCall(**dict(m)) for m in result.mappings()]
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spend-export", description="Export a judged run's recorded provider spend."
    )
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--report", type=pathlib.Path, default=None)
    args = parser.parse_args(argv)
    # The rows first: they are the spend, and a bad report must not cost them.
    rows = asyncio.run(read_rows(get_settings().database_url))
    report: dict[str, Any] | None = None
    if args.report is not None:
        try:
            report = json.loads(args.report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(
                f"spend export: the eval report at {args.report} could not be read "
                f"({type(exc).__name__}); failures are not counted"
            )
    try:
        export = summarize(rows, report)
    except (AttributeError, TypeError, KeyError, ValueError) as exc:
        # A report that parses but has another shape: the rows still go out.
        print(
            f"spend export: the eval report at {args.report} could not be read "
            f"({type(exc).__name__}); failures are not counted"
        )
        export = summarize(rows, None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(export, indent=2), encoding="utf-8")
    totals = export["totals"]
    print(
        f"Spend ({LABEL}): {totals['rows']} row(s), {totals['input_tokens']} input and "
        f"{totals['output_tokens']} output token(s), ${totals['book_cost_usd']}; "
        f"unpriced rows {totals['unpriced_rows']}, estimated-token rows "
        f"{totals['tokens_estimated_rows']}; observed failures {export['observed_failures']}; "
        f"refused by the cap {export['refused_by_cap']}; primary failures "
        f"{ {m: p['calls'] for m, p in export['primary_failures'].items()} }"
    )
    for model, found in export["primary_failures"].items():
        print(f"  primary failures of {model}: {found['by_cause']}")
    print(f"  note: {ATTRIBUTION}")
    for gap in GAPS:
        print(f"  gap: {gap}")
    print(f"Written to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
