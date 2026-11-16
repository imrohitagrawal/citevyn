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
leave no row) are counted per model as ``observed_failures``.
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
from app.models.provider_calls import ProviderCall

LABEL = "recorded book cost"
GAPS = (
    "Failed attempts that were never recorded are not in the cost: a primary call "
    "the fallback hid, and a turn on which both providers failed, leave no row.",
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


def _failed_grader_call(call: dict[str, Any]) -> bool:
    return call.get("score") is None and str(call.get("rationale", "")).startswith("call failed:")


def observed_failures(report: dict[str, Any] | None) -> dict[str, int]:
    """The failed calls the eval recorded, by model, in both passes: answer-client
    calls marked ``failed`` and grader calls that raised (blind, passage and
    probe), the latter under the report's grader model."""
    if not report:
        return {}
    grader = (report.get("grader") or {}).get("model") or (report.get("models") or {}).get("judge")
    counts: Counter[str] = Counter()
    sections = [report.get("judge") or {}, ((report.get("fallback") or {}).get("judge")) or {}]
    for section in sections:
        for case in section.get("cases") or []:
            for call in case.get("model_calls") or []:
                if call.get("failed"):
                    counts[str(call.get("model") or "unknown")] += 1
            for call in [
                *(case.get("grader_calls") or []),
                *(case.get("passage_grader_calls") or []),
            ]:
                if _failed_grader_call(call):
                    counts[str(grader or "unknown")] += 1
    probe = (report.get("grader_check") or {}).get("probe") or {}
    for call in probe.get("calls") or []:
        if _failed_grader_call(call):
            counts[str(grader or "unknown")] += 1
    return dict(sorted(counts.items()))


def summarize(rows: list[ProviderCall], report: dict[str, Any] | None = None) -> dict[str, Any]:
    """The export from rows already read and the eval report (or ``None``)."""
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
            "unpriced_rows": totals["unpriced_rows"],
            "tokens_estimated_rows": totals["tokens_estimated_rows"],
        },
        "observed_failures": observed_failures(report),
        "gaps": list(GAPS),
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
    report: dict[str, Any] | None = None
    if args.report is not None:
        if args.report.exists():
            report = json.loads(args.report.read_text(encoding="utf-8"))
        else:
            print(f"spend export: no eval report at {args.report}; failures not counted")
    rows = asyncio.run(read_rows(get_settings().database_url))
    export = summarize(rows, report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(export, indent=2), encoding="utf-8")
    totals = export["totals"]
    print(
        f"Spend ({LABEL}): {totals['rows']} row(s), {totals['input_tokens']} input and "
        f"{totals['output_tokens']} output token(s), ${totals['book_cost_usd']}; "
        f"unpriced rows {totals['unpriced_rows']}, estimated-token rows "
        f"{totals['tokens_estimated_rows']}; observed failures {export['observed_failures']}"
    )
    for gap in GAPS:
        print(f"  gap: {gap}")
    print(f"Written to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
