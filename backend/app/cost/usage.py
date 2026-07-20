"""Provider-reported token usage for the embedding path (#153 Layer 1).

Why this exists at all
----------------------

``LLMResult`` carries ``input_tokens``/``output_tokens``, so the LLM meter reads
what the provider actually billed. The :class:`~app.embeddings.protocol.Embedder`
seam returns bare vectors (``list[float]``) and nothing else — yet the
OpenAI-compatible ``/embeddings`` response DOES carry ``usage.prompt_tokens``, and
``app/embeddings/openrouter.py`` was throwing it away. Metering on a chars/4
estimate when the provider told us the real number would make every embedding row
``tokens_estimated=True`` for no reason, and an estimate that is wrong in the cheap
direction is exactly how a budget under-counts.

Why a contextvar and not a return value
---------------------------------------

Widening the ``Embedder`` protocol to return ``(vectors, usage)`` would change the
signature of every implementation and every call site — the read path, the worker,
the eval harness, the stub — for a bookkeeping concern, and would force the stub to
invent a usage block it has none of. The same argument that keeps the call-site
label in a contextvar (:mod:`app.cost.call_site`) applies here: the metering
decorator sits *below* the protocol and cannot see through it, so the provider
reports upward instead.

``contextvars`` are per-task under asyncio, so two concurrent embed calls cannot
credit tokens to each other's row the way a module-level global would allow. A
provider that reports with no collector installed (an un-metered stub path, a unit
test constructing the client directly) is a silent no-op by design.

Requests, not just tokens
-------------------------

Every successful POST reports, even when the provider omits usage (Gemini's
``embedContent`` returns no token counts at all). That keeps ``attempts`` — the
number of HTTP requests actually issued, retries included — truthful on the
embedding rows, and it is what lets the meter tell "the provider sent no usage
block" apart from "no request was made".
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Generator
from contextvars import ContextVar


@dataclasses.dataclass
class EmbeddingUsage:
    """What one metered embed call actually consumed, as reported by the provider.

    ``requests`` counts every provider HTTP request that returned a usable body,
    including retries and the sub-batches ``embed_documents`` splits into.
    ``reported_requests`` counts the subset that carried a token count, so the
    meter can flag a *partially* reported call as estimated rather than silently
    recording the reported fraction as if it were the whole.
    """

    input_tokens: int = 0
    requests: int = 0
    reported_requests: int = 0

    @property
    def fully_reported(self) -> bool:
        """True only when every request that happened came back with token counts."""
        return self.requests > 0 and self.reported_requests == self.requests


_current_usage: ContextVar[EmbeddingUsage | None] = ContextVar(
    "citevyn_embedding_usage", default=None
)


@contextlib.contextmanager
def collect_embedding_usage() -> Generator[EmbeddingUsage]:
    """Install a collector for the duration of one metered embed call.

    Restores the previous collector on exit — including on an exception — so a
    failed call cannot keep crediting tokens to a record nobody will read.
    """
    usage = EmbeddingUsage()
    token = _current_usage.set(usage)
    try:
        yield usage
    finally:
        _current_usage.reset(token)


def report_embedding_usage(*, input_tokens: int | None, requests: int = 1) -> None:
    """Called by an embedder client after a successful provider request.

    ``input_tokens=None`` means "this provider does not report usage" — the request
    still counts toward ``attempts``, but the call will be metered from an estimate
    and flagged. A no-op when no collector is installed.
    """
    usage = _current_usage.get()
    if usage is None:
        return
    usage.requests += max(1, requests)
    if input_tokens is None or input_tokens <= 0:
        # A provider reporting 0 tokens for text we definitely sent is a missing
        # usage block wearing a number: treat it as unreported so the meter
        # estimates instead of recording a free call. Recording 0 would look
        # exactly like a genuinely free call and vanish from the budget.
        return
    usage.input_tokens += input_tokens
    usage.reported_requests += 1


__all__ = ["EmbeddingUsage", "collect_embedding_usage", "report_embedding_usage"]
