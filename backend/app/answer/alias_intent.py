"""Intent check for the two-word CiteVyn homophones (#84 follow-up).

The owner dictates questions, and speech-to-text renders "CiteVyn" as **"site win"** more
often than anything else. Single-token manglings ("sitewin", "citevin") are safe to match
on sight — no language spells a word that way — and the guardrail handles them. The
two-word forms are different: they are ordinary English, and three adversarial review
rounds established that no surrounding-token rule separates the two readings:

* a blocklist of metric nouns fell to ``"site win data"`` and ``"did the site win the
  award?"`` (``win`` as a VERB, a reading it never modelled);
* a fail-closed allowlist with a determiner guard fell to ``"may the best site win!"`` —
  Python's lookbehind is fixed-width, so it only ever sees the token adjacent to the
  alias, and one adjective walks straight through.

Both attempts were also REWRITING the query, so a false positive did not merely misroute:
it fed ``"may the best CiteVyn!"`` to retrieval and the generator.

What actually distinguishes the two readings is the meaning of the whole sentence, so that
is what this asks about. It is the "intent detection rather than loosening the keyword"
that issue #84 called for in the first place.

**Two independent things must agree before an aliased question is treated as CiteVyn**,
and that is the whole safety argument:

1. this check must return ``True``; and
2. the answer must still clear the global confidence gate and the LLM grounding-refusal
   net, because the caller deliberately leaves the question on the answer-when-grounded
   path instead of flipping it onto the scoped, un-gated ``citevyn`` route.

A single wrong ``True`` therefore costs a refusal at worst, not a confidently-cited wrong
answer. Every failure mode — provider outage, unparseable reply, stub LLM — resolves to
``False``, which is the pre-existing refusal.
"""

from __future__ import annotations

from app.llm.protocol import LLMClient

# Kept deliberately narrow. The model is NOT asked "could this be about CiteVyn?" — under
# that framing an obliging model says yes to anything containing the letters. It is asked
# to pick the reading a normal English speaker would, and told the product exists, so the
# ordinary-English answer is available to it and is the documented default.
_INTENT_SYSTEM = (
    "You are triaging messages typed into the chat box of a product called CiteVyn — a "
    "documentation question-answering assistant. Users often DICTATE their questions, and "
    "speech-to-text transcribes 'CiteVyn' as 'site win', 'cite win' or 'sight win'.\n\n"
    "Decide which reading the message intends.\n\n"
    "YES — the message asks about the product itself (what it is, what it covers, what it "
    "costs, whether it is accurate, how it works). Remember the context: a question of the "
    "form 'what is site win?' typed INTO CiteVyn is asking about CiteVyn.\n"
    "NO — any ordinary-English use: a website or team winning something, a sales or "
    "analytics 'site win' figure, a set phrase like 'may the best site win', or any "
    "statement that is not a question about the product.\n\n"
    "Examples:\n"
    "  'what is site win?' -> YES\n"
    "  'is site win free?' -> YES\n"
    "  'does site win cover codex?' -> YES\n"
    "  'what does site win cost?' -> YES\n"
    "  'tell me about site win' -> YES\n"
    "  'may the best site win!' -> NO\n"
    "  'did the site win the award?' -> NO\n"
    "  'what is our site win rate?' -> NO\n"
    "  'the recent site win cost us the deal' -> NO\n"
    "  'site win data for Q3' -> NO\n\n"
    "Reply with exactly one word: YES or NO."
)


async def is_citevyn_intent_llm(question: str, llm: LLMClient) -> bool:
    """True when ``question`` is asking about CiteVyn under a mis-transcribed name.

    Strict parse: only a reply whose first word is ``YES`` counts. Anything else — ``NO``,
    a hedge, an explanation, empty output — is ``False``. The bias is deliberate and
    matches the guardrail's: a miss costs the user a rephrase, a false hit costs
    trustworthiness.

    Raises nothing of its own; the caller wraps the call so a provider outage degrades to
    the pre-existing refusal rather than a 500.
    """
    if not question or not question.strip():
        return False
    result = await llm.complete(
        system=_INTENT_SYSTEM,
        user=f"Message: {question}\n\nIs this asking about the CiteVyn product? YES or NO:",
        max_tokens=4,
        temperature=0.0,
    )
    # Take the first word only: a model that ignores the one-word instruction and replies
    # "NO — this is about a sales figure" must not be read as a YES because the string
    # happens to contain other text.
    first = result.text.strip().upper().lstrip("*_ \"'").split(maxsplit=1)
    return bool(first) and first[0].rstrip(".,!:;\"'*_") == "YES"


__all__ = ["is_citevyn_intent_llm"]
