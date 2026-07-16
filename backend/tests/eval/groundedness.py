"""Deterministic groundedness / faithfulness metric (Item 1c, judge hardening).

A single LLM judge can over-score a *plausible-but-wrong* answer — the classic
failure being a confidently-worded reply that fumbles a hard fact ("the Claude
API allows 500 requests per minute"). This module is the judge-INDEPENDENT net:
a cheap, exact check that every hard fact a correct answer must state actually
appears in the produced answer. It has no LLM in the loop, so it cannot be
fooled by fluent prose — a wrong or missing fact fails regardless of the judge's
opinion.

Design (hardened against the plan-review findings):

* **Word-boundary matching, not raw substring.** ``"50 requests per minute"`` must
  NOT be considered present in ``"150 requests per minute"`` — the exact
  digit-prefix error the metric exists to catch. A fact matches only when it is
  not flanked by an alphanumeric character, so a wrong number formed by prefixing
  digits scores 0.
* **Any-of alternatives.** A fact entry may list surface forms separated by ``|``
  (e.g. ``"50 requests per minute|50 req/min|50 requests/minute"``); ANY one
  present counts the fact as covered. This tolerates legitimate answer paraphrase
  without loosening the wrong-number guard.
* **Groundability is a separate, corpus-anchored invariant.** A golden-integrity
  test (in ``test_eval_harness``) asserts at least one alternative of every fact
  appears in the seed corpus, so a fact can never assert something the corpus
  cannot support. This module only measures whether the *answer* surfaced it.

The metric is intentionally narrow and honest: it catches a MISSING or WRONG hard
fact, not arbitrary hallucination (that needs NLI, out of scope). Facts are best
chosen as verbatim identifiers a correct answer cannot paraphrase — env-var names,
header names, CLI commands — plus numeric facts guarded by the boundary rule.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# Characters that, if they flank a candidate match, mean it is part of a larger
# token and therefore NOT a real occurrence of the fact. Alphanumerics plus the
# identifier joiners ``-``/``_`` so ``150`` cannot satisfy a ``50`` fact AND
# ``x-goog-api-key-v2`` / ``CLAUDE_API_RATE_LIMIT_V2`` cannot satisfy the shorter
# identifier, while symbol-bearing facts still match at their natural word edges
# (``-`` is placed last in the class so it is a literal, not a range).
_WORD_CHAR = "0-9a-z_-"


def normalize(text: str) -> str:
    """Casefold and collapse all whitespace runs to a single space.

    Applied to BOTH the answer and each fact so matching is insensitive to case
    and to the incidental line wrapping an LLM emits, without touching token
    identity (letters/digits/symbols are preserved).
    """
    return re.sub(r"\s+", " ", text).strip().casefold()


def _fact_present(normalized_answer: str, fact_alternative: str) -> bool:
    """True when ``fact_alternative`` occurs in ``normalized_answer`` as a whole
    token run — i.e. not immediately flanked by an alphanumeric character.

    The flank guard is what makes ``"50 requests per minute"`` absent from
    ``"150 requests per minute"`` (the ``5`` is preceded by the word char ``1``)
    while still matching ``"a default of 50 requests per minute."``.
    """
    needle = normalize(fact_alternative)
    if not needle:
        return False
    pattern = rf"(?<![{_WORD_CHAR}]){re.escape(needle)}(?![{_WORD_CHAR}])"
    return re.search(pattern, normalized_answer) is not None


def fact_covered(answer: str, fact: str) -> bool:
    """True when ANY ``|``-separated alternative of ``fact`` is present in ``answer``."""
    normalized_answer = normalize(answer)
    return any(_fact_present(normalized_answer, alt) for alt in fact.split("|") if alt.strip())


def missing_facts(answer: str, expected_facts: Sequence[str]) -> list[str]:
    """Return the expected facts NOT present in ``answer`` (each in its raw form)."""
    return [f for f in expected_facts if not fact_covered(answer, f)]


def fact_coverage(answer: str, expected_facts: Sequence[str]) -> float:
    """Fraction of ``expected_facts`` present in ``answer`` (1.0 when none required).

    An empty ``expected_facts`` returns 1.0 (vacuously grounded) — the runner only
    aggregates over cases that actually declare facts, so this convention never
    inflates the gated metric.
    """
    if not expected_facts:
        return 1.0
    covered = sum(1 for f in expected_facts if fact_covered(answer, f))
    return covered / len(expected_facts)
