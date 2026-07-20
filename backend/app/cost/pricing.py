"""The price book — cost per token, keyed by **provider + model** (#153 Layer 1).

Why keyed by both
-----------------

Pricing differs by an order of magnitude between models on the *same* provider
(``openai/gpt-4o-mini`` at $0.15/$0.60 per 1M vs ``openai/gpt-4o`` at $2.50/$10.00).
Keying on the provider alone would mean an operator swapping
``CITEVYN_OPENROUTER_MODEL`` silently mis-bills by ~16x while every meter, budget
check and dashboard keeps reporting confident, wrong numbers. The budget is only as
trustworthy as this table's key.

Unknown models are NOT free
---------------------------

:func:`price_for` returns ``None`` for a model it does not know, and the meter
records the call with ``priced=False`` and a zero cost rather than inventing one.
That combination is deliberate:

* Guessing a price would silently under- or over-charge the daily budget.
* Silently *skipping* the row would make the call invisible to the meter entirely.
* Raising would take the demo down over a bookkeeping gap.

So the call is recorded, its cost is honestly zero-and-flagged, and
``unpriced_calls`` on the spend summary is what an operator watches. A non-zero
value there means "the budget is now under-counting — add the model here."

Rates are **USD per 1,000,000 tokens**, list price, taken from the provider's public
pricing page. They are a snapshot: providers change prices, and this table does not
auto-update. It is used for *budget enforcement*, not billing reconciliation — the
provider-side cap (``docs/COST_CONTROLS.md`` §0) is the authority on actual spend.
"""

from __future__ import annotations

import dataclasses
import re
from decimal import Decimal

# Verified against provider pricing pages on 2026-07-20. Re-check when adding a
# model; a stale rate here quietly skews every budget decision downstream.
_PRICES_VERIFIED_ON = "2026-07-20"


@dataclasses.dataclass(frozen=True)
class TokenPrice:
    """USD per 1M input / output tokens for one provider+model pair."""

    input_per_1m: Decimal
    output_per_1m: Decimal

    def cost_for(self, *, input_tokens: int, output_tokens: int) -> Decimal:
        """Return the USD cost of one call.

        Uses :class:`~decimal.Decimal` end-to-end: these values are summed across
        thousands of calls and compared against a dollar threshold, and binary
        floats accumulate error in exactly that pattern. A budget that trips a cent
        late is a bug that only shows up in production.
        """
        million = Decimal(1_000_000)
        return (
            Decimal(input_tokens) * self.input_per_1m + Decimal(output_tokens) * self.output_per_1m
        ) / million


# (provider, model) -> price. ``provider`` matches ``LLMResult.provider``.
_PRICE_BOOK: dict[tuple[str, str], TokenPrice] = {
    # OpenRouter. The configured fallback/eval model (Settings.openrouter_model).
    ("router", "openai/gpt-4o-mini"): TokenPrice(Decimal("0.15"), Decimal("0.60")),
    ("router", "openai/gpt-4o"): TokenPrice(Decimal("2.50"), Decimal("10.00")),
    ("router", "google/gemini-2.5-flash"): TokenPrice(Decimal("0.30"), Decimal("2.50")),
    # Gemini direct. ``gemini-flash-latest`` is an ALIAS that tracks the current
    # Flash GA model, so its price can change under us without the model string
    # changing — priced at the current Flash rate and re-checked on the date above.
    ("gemini", "gemini-flash-latest"): TokenPrice(Decimal("0.30"), Decimal("2.50")),
    ("gemini", "gemini-2.5-flash"): TokenPrice(Decimal("0.30"), Decimal("2.50")),
    # Anthropic direct. Wired in the factory but not the configured provider today.
    ("anthropic", "claude-opus-4-8"): TokenPrice(Decimal("5.00"), Decimal("25.00")),
    # Keyed on the UNDATED base name. Anthropic's API echoes both forms, and the
    # dated snapshot resolves here via the snapshot-suffix rule below; keying on the
    # dated string instead would leave the plain alias unpriced.
    ("anthropic", "claude-haiku-4-5"): TokenPrice(Decimal("1.00"), Decimal("5.00")),
    # Cheaper sibling tiers, priced EXPLICITLY. They share a prefix with the models
    # above and must never be collapsed onto them (gemini-2.5-flash-lite at Flash
    # rates is 6.25x over on output).
    ("gemini", "gemini-2.5-flash-lite"): TokenPrice(Decimal("0.10"), Decimal("0.40")),
    ("router", "google/gemini-2.5-flash-lite"): TokenPrice(Decimal("0.10"), Decimal("0.40")),
    # --- EMBEDDING models (kind="embedding") -------------------------------
    # Embeddings bill INPUT ONLY — there are no output tokens to charge for — so
    # the output rate is a true zero rather than a placeholder. The meter passes
    # output_tokens=0 on this path, so the two agree either way; the zero is here
    # so a future non-zero output rate cannot be introduced by accident.
    #
    # OpenAI text-embedding-3-*, reached through OpenRouter's OpenAI-compatible
    # /embeddings endpoint, keyed by the OpenRouter model id we actually send.
    ("router", "openai/text-embedding-3-small"): TokenPrice(Decimal("0.02"), Decimal(0)),
    ("router", "openai/text-embedding-3-large"): TokenPrice(Decimal("0.13"), Decimal(0)),
    # The same two under their BARE OpenAI names. OpenRouter wants the "openai/"
    # prefix, but ``CITEVYN_EMBEDDING_MODEL`` is free-form and an operator who sets
    # the bare name would otherwise land every ingest row in ``unpriced_calls``.
    ("router", "text-embedding-3-small"): TokenPrice(Decimal("0.02"), Decimal(0)),
    ("router", "text-embedding-3-large"): TokenPrice(Decimal("0.13"), Decimal(0)),
    # Gemini direct — the shipped default ``CITEVYN_EMBEDDING_MODEL``. Priced at
    # the PAID tier: the free tier is a quota, not a discount, and a budget built
    # on "it might be free today" under-counts the moment the quota is raised.
    ("gemini", "gemini-embedding-001"): TokenPrice(Decimal("0.15"), Decimal(0)),
}

# ``CITEVYN_EMBEDDING_PROVIDER`` spells OpenRouter "openrouter" while
# ``LLMResult.provider`` spells it "router". Left alone, one vendor would occupy two
# rows in every spend-by-provider report and an operator adding a price would have
# to guess which spelling the meter used. Canonicalised on the way in, so the column
# has exactly one name per vendor and the price book has exactly one key.
_PROVIDER_ALIASES: dict[str, str] = {"openrouter": "router"}


def canonical_provider(provider: str) -> str:
    """The single spelling of a vendor used by both the meter and the price book."""
    return _PROVIDER_ALIASES.get(provider, provider)


# The stub provider makes no network call and costs nothing. It is priced by
# PROVIDER rather than by an entry above, because the stub's model string is
# derived from config (``f"stub-{settings.llm_model}"``), so no fixed key matches
# it. It should never reach the meter anyway — the factory does not wrap the stub
# (see app.llm.factory) — but pricing it at zero means that if it ever does, it
# reports as a free priced call rather than polluting ``unpriced_calls``, which
# must stay a signal that a REAL model is going unbilled.
_FREE_PROVIDERS: frozenset[str] = frozenset({"stub"})
_FREE = TokenPrice(Decimal(0), Decimal(0))


def price_for(*, provider: str, model: str) -> TokenPrice | None:
    """Return the price for ``provider``+``model``, or ``None`` if unknown.

    ``None`` is a first-class outcome — see the module docstring. Callers must
    record the call as unpriced rather than treating it as free.
    """
    provider = canonical_provider(provider)
    if provider in _FREE_PROVIDERS:
        return _FREE
    exact = _PRICE_BOOK.get((provider, model))
    if exact is not None:
        return exact
    return _price_for_variant(provider=provider, model=model)


# A trailing DATED SNAPSHOT: -2024-07-18, -20241120, -preview-06-17.
# Anchored at the end, because only a trailing date identifies a pinned release of
# the SAME model. Anything else after a "-" is a DIFFERENT model.
_SNAPSHOT_SUFFIX = re.compile(r"-(?:preview-)?\d{4}(?:-\d{2}-\d{2}|-\d{2}|\d{4})?$")

# OpenRouter ROUTING suffixes after ":". These select how a request is routed, not
# what model runs, so the base model's price applies — except ":free", which is
# genuinely free and must not consume budget it never spends.
_ROUTING_SUFFIXES = frozenset({"floor", "nitro", "beta", "extended", "thinking", "online"})
_FREE_ROUTING_SUFFIX = "free"


def _price_for_variant(*, provider: str, model: str) -> TokenPrice | None:
    """Resolve a provider's *response* model string back to a priced base model.

    ``LLMResult.model`` carries what the provider SAYS it served, not what we asked
    for (``app/llm/openrouter.py`` uses ``data.get("model", self._model)``).
    OpenRouter routinely echoes a resolved variant — a dated snapshot
    ``openai/gpt-4o-mini-2024-07-18`` or a routing suffix ``…:floor``. Keyed
    exactly, each of those falls through to "unpriced" and contributes $0.

    **Only two suffix shapes are collapsed onto a base model**, and the strictness
    is the entire point. An earlier attempt matched any longest known prefix at a
    ``-`` boundary, on the assumption that a ``-`` continuation meant a snapshot.
    It does not — it usually means a *different model at a different price*:

    * ``gemini-2.5-flash-lite`` would have been billed at ``gemini-2.5-flash``
      rates: **6.25x over** on output. The budget trips ~6x early and takes the
      demo down, with ``priced=True`` so nothing surfaces the mistake.
    * ``openai/gpt-4o-mini-realtime-preview`` would have been billed at the text
      tier: **4x under**. The budget never trips, which is the failure this whole
      layer exists to prevent.
    * ``gemini-2.5-flash-image`` would have been **12x under** on output.

    So an unrecognised suffix returns ``None`` — the call lands in
    ``unpriced_calls``, which is exactly the alarm that says "a real model is going
    unbilled; add it to the book". A wrong price is worse than an admitted gap: the
    gap is visible and the wrong price is not.
    """
    base = model
    # 1. Routing suffix after ":" — same model, different routing.
    if ":" in model:
        base, _, suffix = model.partition(":")
        if suffix == _FREE_ROUTING_SUFFIX:
            return _FREE
        if suffix not in _ROUTING_SUFFIXES:
            return None
    exact = _PRICE_BOOK.get((provider, base))
    if exact is not None:
        return exact
    # 2. Trailing dated snapshot — a pinned release of the same model.
    undated = _SNAPSHOT_SUFFIX.sub("", base)
    if undated != base:
        return _PRICE_BOOK.get((provider, undated))
    return None


def known_models() -> list[tuple[str, str]]:
    """Every priced (provider, model) pair. Used by the admin surface and tests."""
    return sorted(_PRICE_BOOK)


__all__ = ["TokenPrice", "canonical_provider", "known_models", "price_for"]
