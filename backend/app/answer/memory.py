"""Conversation memory (Phase 3b).

Resolves an anaphoric follow-up ("How can I raise it?") against the recent turns of
a session so BOTH retrieval and the generated answer see the topic the pronoun refers
to. Without this, a follow-up that names no product routes to ``unsupported`` and the
global confidence gate finds nothing — the user gets a refusal to a perfectly
answerable question (see ``tests/eval`` ``followup`` bucket).

Two pieces, deliberately split so the load-bearing logic is a pure function:

* :func:`build_contextual_query` — PURE: given the current question and the prior
  user questions (most-recent FIRST), return the query to retrieve/answer with. It
  rewrites ONLY when the current question is a genuine anaphoric/elliptical follow-up
  that names no product, prepending the most-recent prior user question that DID name
  a product. Everything else is returned unchanged.
* :func:`recent_user_questions` — the thin DB read that supplies those prior turns.

Two safety properties matter and are tested:

1. **A self-contained off-domain sentence is never contextualized.** "what's the
   weather?" names no product but carries no anaphora/ellipsis, so it is returned
   unchanged and still reaches the unsupported refusal. Only a fragment that leans on
   prior context ("how about it?", "and the others?") is rewritten. (Adversarial
   review R1: without this gate, every off-topic follow-up would be hijacked into the
   prior product and bypass the refusal.)
2. **Single-turn is a no-op.** With no prior turns the function returns the question
   verbatim, so every existing single-turn path is byte-for-byte unchanged.

Design limitations (documented, not bugs):

* The antecedent is the MOST-RECENT prior product turn. Deep coreference — "it"
  referring two topics back past an intervening product turn — is out of scope; a
  follow-up refers to the immediately-preceding topic in the overwhelming common case,
  and resolving true coreference needs an LLM the hermetic retrieval path cannot call.
* An off-corpus PIVOT that opens with an anaphor/ellipsis ("and how do I do that on
  Kubernetes?") is contextualized like a genuine follow-up, so it retrieves the prior
  product's chunk. The LLM grounding-refusal net (Phase 2) is the authoritative gate
  there: a pivot to a clearly off-corpus topic finds no support in the routed chunk and
  is declined (verified in the judged eval + a hermetic test).

* CONTENT-NOUN follow-ups ("is there a credentials file option?", "what are the different
  models?") name no product and carry no bare anaphora, so the regex above leaves them and
  they would route ``unsupported`` and refuse. :func:`condense_question_llm` (#112) resolves
  these via the LLM as an ENTITY-AWARE rewrite. It is wired ONLY on the orchestrator's
  answer-when-grounded (global, confidence-gated) path — a PURE RECALL improver that changes
  only the retrieval/generation TEXT, never the routing — so it can never hijack a pivot onto
  the scoped, un-gated path; the confidence gate + grounding-refusal net stay the sole refusal
  authority. Kept OUT of the pure regex path so the deterministic hermetic followup gate is
  unchanged (proven on the judged run; see docs/RAG_QUALITY_PLAN.md §8a-10).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.guardrails.domain import Domain, classify_domain
from app.llm.protocol import LLMClient
from app.models import Message, MessageRole

# Anaphora markers: a bare pronoun / determiner that points at something named in a
# prior turn ("raise IT", "block THOSE", "see THE OTHERS"). Word-bounded so "itemize"
# does not match "it".
_ANAPHORA_RE = re.compile(
    r"""
    \b(?:
        it | its | it's | that | those | these | them | they | their | this |
        the\s+other | the\s+others | another | the\s+rest | the\s+same |
        one\s+of\s+(?:them|those)
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Elliptical continuation openers: a fragment that only makes sense as a continuation
# of the prior turn ("and X?", "what about X?", "how about …", "what else?").
_ELLIPSIS_RE = re.compile(
    r"""^\s*(?:and|or|but|so|what\s+about|how\s+about|what\s+else|any\s+others?)\b""",
    re.IGNORECASE | re.VERBOSE,
)


def is_anaphoric_followup(question: str) -> bool:
    """True when ``question`` reads as a follow-up leaning on prior context.

    Either it contains an anaphoric pronoun/determiner (:data:`_ANAPHORA_RE`) or it
    opens with an elliptical continuation (:data:`_ELLIPSIS_RE`). This is the gate
    that keeps a self-contained off-domain sentence ("what's the weather?") from being
    contextualized — such a sentence has neither marker, so it flows on to the
    unsupported refusal instead of being hijacked into the prior product's topic.
    """
    return bool(_ANAPHORA_RE.search(question) or _ELLIPSIS_RE.search(question))


def build_contextual_query(question: str, prior_user_questions: Sequence[str]) -> str:
    """Return the query to retrieve/answer with, resolving an anaphoric follow-up.

    ``prior_user_questions`` are the session's prior USER turns, MOST-RECENT FIRST.

    Rewrite rules (each is a hard gate; failing any returns ``question`` unchanged):

    1. The current question must name NO product — ``classify_domain(question)`` is
       ``unsupported``. A question that already names a product (or CiteVyn) is
       self-contained and answered as-is.
    2. It must be a genuine anaphoric/elliptical follow-up
       (:func:`is_anaphoric_followup`) — a self-contained off-domain sentence is left
       alone so it reaches the refusal.
    3. There must be a prior user turn that DID name a product; the most-recent such
       turn is the antecedent and is prepended.

    When all three hold, returns ``f"{antecedent} {question}"`` — a self-contained
    query whose domain routing, retrieval, and generated answer all resolve the
    pronoun. Otherwise returns ``question`` verbatim (single-turn is always a no-op).
    """
    if classify_domain(question) is not Domain.unsupported:
        return question
    if not is_anaphoric_followup(question):
        return question
    for prior in prior_user_questions:  # most-recent first
        if classify_domain(prior) is not Domain.unsupported:
            return f"{prior} {question}"
    return question


_CONDENSE_SYSTEM = (
    "You rewrite a user's latest question into a single self-contained question, using the "
    "earlier turns of the conversation to resolve anything under-specified (a pronoun, or a "
    "bare noun like 'the models' / 'a credentials file' that only makes sense in context). "
    "TWO HARD RULES: (1) If the latest question already stands on its own, OR changes the "
    "subject to something the earlier turns did not establish, return it UNCHANGED — never "
    "invent a topic it doesn't ask about. (2) Reply with ONLY the rewritten question — no "
    "preamble, quotes, or explanation."
)

# A rewrite longer than this is almost certainly the model ignoring the "only the question"
# instruction (adding preamble/explanation) — fall back to the original rather than feed a
# runaway string into retrieval.
_MAX_REWRITE_CHARS = 300


async def condense_question_llm(
    question: str, prior_user_questions: Sequence[str], llm: LLMClient
) -> str:
    """Rewrite a context-dependent follow-up into a standalone question via the LLM.

    This is the entity-aware companion to :func:`build_contextual_query` (#112): it resolves a
    CONTENT-NOUN follow-up ("what are the different models?", "is there a credentials file
    option?") that carries no bare anaphora and so the deterministic regex leaves unchanged.

    It is a PURE RECALL IMPROVER and is called by the orchestrator ONLY inside the
    "answer when grounded" (global, confidence-gated) path — after routing is already fixed
    from the un-rewritten query — so it can NEVER flip a pivot onto the scoped, un-gated
    retrieval path. The confidence gate + the LLM grounding-refusal net remain the sole
    authority on whether an off-corpus pivot is declined; this only changes the TEXT fed to
    the global retrieval + generation. Any empty history, empty/overlong output, or LLM error
    falls back to the original ``question`` (the caller also wraps the call defensively).
    """
    if not prior_user_questions:
        return question
    # Oldest-first for the model to read as a conversation (the store hands them newest-first).
    convo = "\n".join(f"- {q}" for q in reversed(list(prior_user_questions)))
    user = (
        f"Earlier questions in this conversation (oldest first):\n{convo}\n\n"
        f"Latest question: {question}\n\nStandalone question:"
    )
    result = await llm.complete(system=_CONDENSE_SYSTEM, user=user, max_tokens=80, temperature=0.0)
    rewritten = result.text.strip().strip('"').strip()
    if not rewritten or len(rewritten) > _MAX_REWRITE_CHARS:
        return question
    return rewritten


async def recent_user_questions(
    session: AsyncSession, session_id: uuid.UUID, *, limit: int
) -> list[str]:
    """Return the session's prior USER message contents, MOST-RECENT FIRST.

    Filters to ``role == user`` (an assistant turn contains product tokens and would
    mask a missing filter) and scopes to ``session_id``. Ordered ``created_at DESC``;
    ``message_id DESC`` is a deterministic — if temporally arbitrary — tiebreaker for
    the rare case where two turns share a timestamp (``_persist_messages`` stamps both
    messages of a turn with one ``now()``, but different turns differ by microseconds).

    Called from :meth:`Orchestrator.ask` BEFORE the current turn's user message is
    persisted, so the current question is never included in its own antecedents.
    """
    stmt = (
        select(Message.content)
        .where(Message.session_id == session_id, Message.role == MessageRole.user)
        .order_by(Message.created_at.desc(), Message.message_id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


__all__ = [
    "build_contextual_query",
    "condense_question_llm",
    "is_anaphoric_followup",
    "recent_user_questions",
]
