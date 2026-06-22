"""Dataclasses + YAML loader for golden cases.

This module is intentionally pure-Python: it depends on PyYAML and
nothing from the backend.  That keeps the schema documented in one
place and lets the runner import the loader before it pulls in the
heavy FastAPI machinery.
"""
from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Iterable
from typing import Any

import yaml


@dataclasses.dataclass(frozen=True)
class GoldenCase:
    """One parsed golden case.

    Attributes mirror the YAML schema in ``tests/golden/cases/README.md``.
    Unknown extra keys are preserved in :attr:`raw` so the runner can
    surface them in a regression report.
    """

    id: str
    title: str
    type: str
    area: str
    question: str
    answer_style: str | None
    messages: tuple[dict[str, str], ...]
    search_term: str | None
    search_term_type: str | None
    search_product_area: str | None
    first_assert: dict[str, Any]
    second_assert: dict[str, Any]
    expect_no_answer: bool | None
    expect_http_400: bool | None
    expect_http_422: bool | None
    expect_domain: str | None
    expect_intent: str | None
    expect_confidence: str | None
    expect_retrieval_strategy: str | None
    expect_citations_min: int | None
    expect_search_total_ge: int | None
    expect_search_hit_url_contains: str | None
    expect_last_domain: str | None
    expect_last_answer_contains: tuple[str, ...]
    expect_answer_contains: tuple[str, ...]
    raw: dict[str, Any]
    path: pathlib.Path

    @classmethod
    def from_dict(cls, d: dict[str, Any], path: pathlib.Path) -> "GoldenCase":
        if "id" not in d or "type" not in d or "area" not in d:
            raise ValueError(
                f"{path}: missing required keys id/type/area; got keys {sorted(d)}"
            )
        return cls(
            id=d["id"],
            title=d.get("title", d["id"]),
            type=d["type"],
            area=d["area"],
            question=d.get("question", ""),
            answer_style=d.get("answer_style"),
            messages=tuple({k: v for k, v in m.items()} for m in d.get("messages", ())),
            search_term=d.get("search_term"),
            search_term_type=d.get("search_term_type"),
            search_product_area=d.get("search_product_area"),
            first_assert=dict(d.get("first_assert", {})),
            second_assert=dict(d.get("second_assert", {})),
            expect_no_answer=d.get("expect_no_answer"),
            expect_http_400=d.get("expect_http_400"),
            expect_http_422=d.get("expect_http_422"),
            expect_domain=d.get("expect_domain"),
            expect_intent=d.get("expect_intent"),
            expect_confidence=d.get("expect_confidence"),
            expect_retrieval_strategy=d.get("expect_retrieval_strategy"),
            expect_citations_min=d.get("expect_citations_min"),
            expect_search_total_ge=d.get("expect_search_total_ge"),
            expect_search_hit_url_contains=d.get("expect_search_hit_url_contains"),
            expect_last_domain=d.get("expect_last_domain"),
            expect_last_answer_contains=tuple(d.get("expect_last_answer_contains", ())),
            expect_answer_contains=tuple(d.get("expect_answer_contains", ())),
            raw=d,
            path=path,
        )

    @classmethod
    def from_yaml(cls, path: pathlib.Path) -> "GoldenCase":
        with path.open() as fh:
            d = yaml.safe_load(fh) or {}
        if not isinstance(d, dict):
            raise ValueError(f"{path}: top-level YAML must be a mapping")
        return cls.from_dict(d, path)


def load_cases(directory: pathlib.Path) -> list[GoldenCase]:
    """Return every ``.yml`` / ``.yaml`` case under ``directory`` sorted by id."""
    if not directory.exists():
        return []
    out: list[GoldenCase] = []
    for ext in ("*.yml", "*.yaml"):
        for p in sorted(directory.glob(ext)):
            out.append(GoldenCase.from_yaml(p))
    return out


def filter_cases(
    cases: Iterable[GoldenCase], *, ids: list[str] | None = None
) -> list[GoldenCase]:
    """Optionally filter the case list to specific ids (preserves input order)."""
    if not ids:
        return list(cases)
    wanted = set(ids)
    return [c for c in cases if c.id in wanted]
