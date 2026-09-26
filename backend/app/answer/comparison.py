"""Cross-vendor comparison answers (ADR-0005 §5, Phase 6E).

Pure helpers, no I/O: plain names for product areas, the instruction added to a
multi-product question, and the check that every product the question named is
actually cited. Wired into the orchestrator behind
``CITEVYN_COMPARISON_ANSWERS`` (default off).

A citation maps to a product through its evidence chunk (``product_area``): the
response citation itself does not carry the area.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.retrieval.types import EvidenceHit

#: How each product area reads in a sentence. A product domain missing here
#: would print its internal id; a test pins that every product domain has one.
_NAMES: dict[str, str] = {
    "claude_api": "the Claude API",
    "claude_code": "Claude Code",
    "codex": "Codex",
    "gemini_api": "the Gemini API",
}


def product_name(area: str) -> str:
    return _NAMES.get(area, area)


def _join(names: list[str], word: str) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" {word} " + names[-1]


def comparison_instruction(areas: Sequence[str]) -> str:
    """The line added to the user prompt when a question names several products."""
    names = [product_name(a) for a in areas]
    return (
        f"This question is about {_join(names, 'and')}. Answer for each of them, "
        "citing that product's own evidence."
    )


def missing_areas(
    named: Sequence[str], evidence: Sequence[EvidenceHit], cited_indices: Sequence[int]
) -> list[str]:
    """The named product areas that no cited evidence chunk belongs to, in the
    question's order. ``cited_indices`` are 1-based markers; one outside the
    evidence list (the validator rejects those first) is ignored."""
    cited = {evidence[i - 1].product_area for i in cited_indices if 1 <= i <= len(evidence)}
    return [a for a in named if a not in cited]


def missing_note(areas: Sequence[str]) -> str:
    """One plain sentence naming the products the answer could not cover, or ""."""
    if not areas:
        return ""
    names = _join([product_name(a) for a in areas], "or")
    return f"The documentation I have does not cover {names} for this question."


__all__ = ["comparison_instruction", "missing_areas", "missing_note", "product_name"]
