"""Dataclasses + JSONL loader for RAG eval cases (Phase 0, issue #96).

This module is intentionally pure-Python: it depends on the stdlib
``json`` and nothing from the backend.  That keeps the golden schema
documented in one place and lets the loader be imported before the
heavy FastAPI / retrieval machinery is pulled in.

The eval harness is *distinct* from the assertion-based golden runner
in :mod:`tests.golden`.  Where that runner checks fixed substrings and
enum values, this harness measures two outcome metrics from
``docs/RAG_QUALITY_PLAN.md`` §8:

* **retrieval hit-rate** — does any top-k retrieved chunk come from the
  expected source? (:mod:`tests.eval.retrieval`)
* **answer quality** — an LLM-as-judge 1–5 score vs an expected gist.
  (:mod:`tests.eval.judge`)

Each line of ``tests/eval/golden.jsonl`` is one :class:`EvalCase`.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from collections.abc import Iterable
from typing import Any, cast

# The case kinds. ``literal`` questions share vocabulary with the seed corpus
# (keyword/exact arms can hit); ``paraphrase`` questions carry zero literal token
# overlap so they isolate the semantic/vector arm (expected ~0 hit-rate until #97
# revives it); ``refusal`` questions are out-of-corpus / off-domain and MUST
# retrieve nothing and be refused; ``multihop`` questions name >=2 products and hit
# only when EVERY named area is retrieved (Phase 3a); ``followup`` questions are the
# final turn of a multi-turn conversation whose ``history`` establishes the topic —
# the anaphoric final question ("How can I raise it?") names no product on its own
# and only resolves once conversation memory (Phase 3b) rewrites it against the prior
# turn. Unlike ``multihop`` (which needs the live vector arm), the ``followup``
# rewrite resolves DETERMINISTICALLY (domain re-routing + keyword), so it hits on
# hermetic SQLite too and is gated on the hermetic run once the feature lands; it
# lives in its own bucket only because a multi-turn conversation is a distinct
# concern from the single-turn literal+paraphrase overall gate.
KINDS = frozenset({"literal", "paraphrase", "refusal", "multihop", "followup"})


@dataclasses.dataclass(frozen=True)
class EvalCase:
    """One parsed eval case.

    Attributes mirror the JSONL schema documented in
    ``tests/eval/README.md``.  Unknown extra keys are preserved in
    :attr:`raw` so the runner can surface them in a report.
    """

    id: str
    area: str
    kind: str
    question: str
    expected_source: str | None
    expected_gist: str
    expect_no_answer: bool
    raw: dict[str, Any]
    # Multi-hop (Phase 3): a cross-product question is a HIT only when EVERY listed
    # source area is retrieved. ``None`` for single-source cases (they use
    # ``expected_source``). Appended last with a default so the frozen dataclass
    # stays valid and every existing case parses unchanged.
    expected_sources: tuple[str, ...] | None = None
    # Conversation memory (Phase 3b): the prior USER turns (most-recent turn LAST,
    # in chronological order) that precede this ``question`` in one session. A
    # ``followup`` case carries a non-empty ``history``; every other kind leaves it
    # empty. The runner replays these turns before the asserted final turn so the
    # orchestrator's memory can resolve the anaphora.
    history: tuple[str, ...] = ()

    @property
    def is_refusal(self) -> bool:
        """True when the correct outcome is a refusal / no-answer.

        Refusal cases have no expected source (there is no correct chunk
        to retrieve) and the answer path must decline.  Both signals are
        checked for consistency at parse time.
        """
        return self.kind == "refusal"

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, origin: str) -> EvalCase:
        required = ("id", "area", "kind", "question", "expected_gist")
        missing = [k for k in required if k not in d]
        if missing:
            raise ValueError(f"{origin}: missing required keys {missing}; got keys {sorted(d)}")
        kind = d["kind"]
        if kind not in KINDS:
            raise ValueError(f"{origin}: unknown kind {kind!r}; expected one of {sorted(KINDS)}")
        expected_source = d.get("expected_source")
        raw_sources = d.get("expected_sources")
        expected_sources = tuple(raw_sources) if raw_sources else None
        expect_no_answer = bool(d.get("expect_no_answer", False))
        raw_history = d.get("history")
        # Guard against a stringly-typed history ("prior turn") silently
        # char-splitting into a tuple of letters — it must be a JSON array of turns.
        if raw_history is not None and not isinstance(raw_history, list):
            raise ValueError(f"{origin}: case {d['id']!r} history must be a list of strings")
        history = tuple(raw_history) if raw_history else ()
        # ``history`` belongs only to a ``followup`` case (it drives the multi-turn
        # replay). Rejecting it elsewhere keeps a stray key from silently doing
        # nothing on a single-turn case.
        if history and kind != "followup":
            raise ValueError(f"{origin}: case {d['id']!r} sets history; only kind='followup' may")
        # A refusal case has no source and expects a no-answer; a multihop case names
        # >=2 expected_sources (and no single expected_source); a followup case names
        # exactly one expected_source AND a non-empty history; every other answerable
        # case names exactly one expected_source. This keeps the golden data
        # internally consistent so the metrics can trust a case's shape.
        if kind == "refusal":
            if expected_source is not None or expected_sources is not None:
                raise ValueError(
                    f"{origin}: refusal case {d['id']!r} must not set expected_source(s)"
                )
            if not expect_no_answer:
                raise ValueError(
                    f"{origin}: refusal case {d['id']!r} must set expect_no_answer=true"
                )
        elif kind == "multihop":
            if expected_source is not None:
                raise ValueError(
                    f"{origin}: multihop case {d['id']!r} uses expected_sources (a list), "
                    "not expected_source"
                )
            if not expected_sources or len(expected_sources) < 2:
                raise ValueError(
                    f"{origin}: multihop case {d['id']!r} must set expected_sources with >=2 areas"
                )
            if expect_no_answer:
                raise ValueError(f"{origin}: multihop case {d['id']!r} must not expect_no_answer")
        elif kind == "followup":
            if not expected_source:
                raise ValueError(
                    f"{origin}: followup case {d['id']!r} must set a non-empty expected_source"
                )
            if expected_sources is not None:
                raise ValueError(
                    f"{origin}: followup case {d['id']!r} sets expected_sources; use one "
                    "expected_source (the final turn resolves to one area)"
                )
            if not history:
                raise ValueError(
                    f"{origin}: followup case {d['id']!r} must set a non-empty history "
                    "(the prior turns that establish the topic)"
                )
            if expect_no_answer:
                raise ValueError(f"{origin}: followup case {d['id']!r} must not expect_no_answer")
        else:
            if not expected_source:
                raise ValueError(
                    f"{origin}: answerable case {d['id']!r} must set a non-empty expected_source"
                )
            if expected_sources is not None:
                raise ValueError(
                    f"{origin}: case {d['id']!r} sets expected_sources; use kind='multihop'"
                )
            if expect_no_answer:
                raise ValueError(
                    f"{origin}: answerable case {d['id']!r} must not set expect_no_answer=true"
                )
        return cls(
            id=d["id"],
            area=d["area"],
            kind=kind,
            question=d["question"],
            expected_source=expected_source,
            expected_gist=d["expected_gist"],
            expect_no_answer=expect_no_answer,
            raw=d,
            expected_sources=expected_sources,
            history=history,
        )


def load_cases(path: pathlib.Path) -> list[EvalCase]:
    """Parse every non-blank line of a JSONL golden file into an EvalCase.

    Blank lines and ``#``-prefixed comment lines are skipped so the file
    stays human-annotatable.  A duplicate id is a hard error — silently
    overwriting one case with another would shrink coverage unnoticed,
    exactly the failure mode this harness exists to prevent.
    """
    if not path.exists():
        raise FileNotFoundError(f"golden file not found: {path}")
    cases: list[EvalCase] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            origin = f"{path}:{lineno}"
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{origin}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"{origin}: each line must be a JSON object")
            case = EvalCase.from_dict(cast("dict[str, Any]", obj), origin=origin)
            if case.id in seen:
                raise ValueError(f"{origin}: duplicate case id {case.id!r}")
            seen.add(case.id)
            cases.append(case)
    return cases


def filter_cases(cases: Iterable[EvalCase], *, ids: list[str] | None = None) -> list[EvalCase]:
    """Optionally filter the case list to specific ids (preserves input order)."""
    if not ids:
        return list(cases)
    wanted = set(ids)
    return [c for c in cases if c.id in wanted]
