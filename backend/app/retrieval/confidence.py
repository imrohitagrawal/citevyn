"""Global-retrieval confidence gate (Phase 2 — "answer when grounded").

When a question does not name a product, the domain router returns ``unsupported``
and — before Phase 2 — the orchestrator refused it outright, even when the docs
actually cover it ("how do I restrict which tools the *coding assistant* may run?"
is really a Claude Code question). Phase 2 instead retrieves **globally** (across
all product areas) for those questions and answers when the evidence is real.

The risk is refusal safety: a genuinely off-corpus question ("how do I deploy to
Kubernetes?") also retrieves *some* nearest chunk. An **absolute** cosine floor
cannot separate the two — empirically a competitor-API refusal ("call the OpenAI
GPT-4 API", top sim 0.40) outscores a valid in-corpus paraphrase ("premium paid
tier", 0.39). What DOES separate them is a **relative** signal: an in-corpus query
has one chunk that clearly stands out, while an off-corpus query yields a muddle of
roughly-equal weak matches. Measured on the real ingested corpus (33 chunks):

    answerable margins (top1 − top2): all ≥ 0.070
    refusal    margins             : all ≤ 0.027

So the gate is: the global vector result is trusted only when its best hit both
clears a loose floor (a cheap cost-guard) AND stands out from the runner-up by a
margin. This is a first filter that also bounds cost (obvious off-topic queries
never reach the LLM); the LLM grounding-refusal remains the authoritative final net
for anything that slips through. Thresholds are tuned on a small corpus and are
config-overridable; the eval harness is what validates any change to them.

This module is deliberately pure (no I/O, no DB) so the gate is unit-tested without
pgvector — the retrieval arms are dead on the hermetic SQLite engine.
"""

from __future__ import annotations

from collections.abc import Sequence


def is_confident_global_result(
    scores: Sequence[float],
    *,
    min_top_score: float,
    min_margin: float,
) -> bool:
    """Whether a globally-retrieved vector result is confident enough to trust.

    ``scores`` are the vector hits' similarity scores (higher = closer), assumed
    sorted descending as the vector arm returns them.

    Returns ``True`` only when the best hit both clears ``min_top_score`` (a loose
    cost-guard that rejects a query whose nearest chunk is barely related at all)
    AND beats the second-best by at least ``min_margin`` (the discriminating signal
    that the query is about one specific chunk, not a muddle). A lone hit (no
    runner-up to compare against) passes on the floor check alone.

    An empty result is never confident.
    """
    if not scores:
        return False
    top = scores[0]
    if top < min_top_score:
        return False
    if len(scores) == 1:
        return True
    return (top - scores[1]) >= min_margin


__all__ = ["is_confident_global_result"]
