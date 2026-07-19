#!/usr/bin/env python
"""Count the judged eval's PAID model calls — without making any (#153 Layer 6).

`docs/COST_CONTROLS.md` §6 justifies the CI cost policy with a call-volume table.
This is the harness that produced it, committed so the numbers are reproducible
rather than asserted. It swaps in a counting fake LLM client, so it makes **no
network requests and needs no API key**.

Usage (from the repo root)::

    cd backend && PYTHONPATH=. CITEVYN_EVAL_JUDGE_PANEL=1 \
        uv run python ../scripts/measure_eval_spend.py

What it does and does NOT measure
---------------------------------

* **Measured exactly:** the number of paid calls, split by call site, and the
  *input* token footprint of the prompts the harness actually builds.
* **NOT measured:** realistic output lengths — the fake returns a stub string, so
  its ``out_tok`` is far below a real answer (~300 tokens) or judge verdict (~30).
  It also runs HERMETICALLY (SQLite), where the dead vector arm means many cases
  refuse before ever reaching the LLM; on the live ``--postgres`` path more cases
  reach the answer generator. Both effects mean the printed dollar figure is a
  FLOOR. §6 states the extrapolated estimate separately.

Token counts use the repo's own 4-chars/token convention (``app/llm/stub.py``).
"""

from __future__ import annotations

import asyncio
import collections

from app.llm.types import LLMResult

CHARS_PER_TOKEN = 4

# gpt-4o-mini list price per 1M tokens — the model CI's judged eval runs on
# (``openrouter_model`` default, with CITEVYN_LLM_PROVIDER=router).
PRICE_IN_PER_1M = 0.15
PRICE_OUT_PER_1M = 0.60

calls: collections.Counter[str] = collections.Counter()
in_tok: collections.Counter[str] = collections.Counter()
out_tok: collections.Counter[str] = collections.Counter()
_JUDGE_SYSTEMS: set[str] = set()


def _classify(system: str) -> str:
    """Attribute a call to its site by matching the exact system prompt.

    Deliberately identity-based rather than keyword-based: an earlier keyword
    heuristic mis-attributed judge calls as answer calls and produced a table that
    did not even vary with the panel size.
    """
    if system in _JUDGE_SYSTEMS:
        return "judge"
    if "standalone" in system.lower():
        return "condense"
    if "CiteVyn" in system and "intent" in system.lower():
        return "alias_intent"
    return "answer"


class CountingLLM:
    """An LLMClient that records instead of calling. Not a StubLLMClient, so the
    judge does not self-skip."""

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        del max_tokens, temperature
        site = _classify(system)
        text = '{"score": 5, "rationale": "ok"}' if site == "judge" else "Answer [1]."
        i = max(1, (len(system) + len(user)) // CHARS_PER_TOKEN)
        o = max(1, len(text) // CHARS_PER_TOKEN)
        calls[site] += 1
        in_tok[site] += i
        out_tok[site] += o
        return LLMResult(
            text=text, input_tokens=i, output_tokens=o, model="counting-fake", provider="router"
        )

    async def aclose(self) -> None:
        return None


async def main() -> None:
    import app.answer.orchestrator as orch_mod
    from app.core.config import Settings
    from tests.eval import judge as judge_mod
    from tests.eval import runner as runner_mod
    from tests.eval.cases import load_cases
    from tests.eval.paths import GOLDEN_PATH

    fake = CountingLLM()
    # Patch every import site. Patching only the factory module leaves the
    # orchestrator holding its own reference and issuing REAL requests.
    for mod in (orch_mod, judge_mod, runner_mod):
        mod.get_llm_client = lambda settings=None: fake  # type: ignore[assignment]

    _JUDGE_SYSTEMS.update(judge_mod._STANDARD_FRAMINGS)
    _JUDGE_SYSTEMS.add(judge_mod._ADVERSARIAL_SYSTEM)

    cases = load_cases(GOLDEN_PATH)
    settings = Settings(llm_provider="router", openrouter_api_key="unused", embedding_provider="stub")
    judged = await runner_mod._judge_cases(cases, settings=settings, postgres=False)

    print(f"golden cases: {len(cases)}   judged (hermetic, excl. postgres_only): {len(judged)}")
    print(f"{'site':14s} {'calls':>7s} {'in_tok':>10s} {'out_tok':>9s}")
    for site in sorted(calls):
        print(f"{site:14s} {calls[site]:7d} {in_tok[site]:10d} {out_tok[site]:9d}")
    print(
        f"{'TOTAL':14s} {sum(calls.values()):7d} "
        f"{sum(in_tok.values()):10d} {sum(out_tok.values()):9d}"
    )
    cost = (
        sum(in_tok.values()) / 1e6 * PRICE_IN_PER_1M
        + sum(out_tok.values()) / 1e6 * PRICE_OUT_PER_1M
    )
    print(f"\nfloor cost @ gpt-4o-mini (${PRICE_IN_PER_1M}/${PRICE_OUT_PER_1M} per 1M): ${cost:.4f}")
    print("NB: a FLOOR — the fake's outputs are far shorter than real ones, and this")
    print("    is the hermetic run. See docs/COST_CONTROLS.md §6 for the extrapolation.")


if __name__ == "__main__":
    asyncio.run(main())
