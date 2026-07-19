"""Per-call cost metering (#153 Layer 1).

The budget in ``RELEASE_PLAN`` §9 is only as trustworthy as this layer. Three
properties matter more than the happy path and are pinned hardest here:

1. **The stub is never wrapped.** Wrapping it would hide ``StubLLMClient`` behind
   the decorator, which is the identity the eval judge checks to self-skip on the
   free path — re-enabling the judge on every stub run and spending real money on
   the very path chosen to avoid it.
2. **Metering never breaks an answer.** A DB failure must lose the row, not the
   response the user already paid for.
3. **An unknown model is recorded as unpriced, not as free and not dropped.**
   Guessing corrupts the budget; dropping makes the call invisible.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.cost.call_site import CallSite, call_site, get_call_site
from app.cost.meter import build_call
from app.cost.metered import MeteredLLMClient
from app.cost.pricing import known_models, price_for
from app.llm.factory import build_llm_client, get_llm_client, reset_llm_client
from app.llm.stub import StubLLMClient
from app.llm.types import LLMResult


class _FakeLLM:
    def __init__(self, result: LLMResult | None = None) -> None:
        self.result = result or LLMResult(
            text="hi", input_tokens=1000, output_tokens=500, model="m", provider="router"
        )
        self.closed = False
        self.calls = 0

    async def complete(self, *, system: str, user: str, max_tokens: int, temperature: float):
        del system, user, max_tokens, temperature
        self.calls += 1
        return self.result

    async def aclose(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_cost_is_computed_per_million_tokens_for_input_and_output_separately() -> None:
    price = price_for(provider="router", model="openai/gpt-4o-mini")
    assert price is not None
    # 1M input @ $0.15 + 1M output @ $0.60.
    assert price.cost_for(input_tokens=1_000_000, output_tokens=1_000_000) == Decimal("0.75")
    # Asymmetric on purpose: swapping the two rates must change the answer.
    assert price.cost_for(input_tokens=1_000_000, output_tokens=0) == Decimal("0.15")
    assert price.cost_for(input_tokens=0, output_tokens=1_000_000) == Decimal("0.60")


def test_cost_uses_decimal_not_float() -> None:
    """Spend is summed over thousands of rows and compared to a dollar threshold.

    Float accumulation drifts in exactly that pattern, so a budget could trip late.
    """
    price = price_for(provider="router", model="openai/gpt-4o-mini")
    assert price is not None
    total = sum(
        (price.cost_for(input_tokens=333, output_tokens=333) for _ in range(3000)),
        Decimal(0),
    )
    assert isinstance(total, Decimal)


def test_pricing_is_keyed_by_provider_AND_model() -> None:
    """A model swap must not silently mis-bill.

    gpt-4o costs ~16x gpt-4o-mini on the same provider; if the key were the
    provider alone, changing CITEVYN_OPENROUTER_MODEL would keep reporting the
    cheap rate.
    """
    mini = price_for(provider="router", model="openai/gpt-4o-mini")
    full = price_for(provider="router", model="openai/gpt-4o")
    assert mini is not None and full is not None
    assert full.input_per_1m > mini.input_per_1m * 10


def test_unknown_model_has_no_price() -> None:
    assert price_for(provider="router", model="some/model-shipped-tomorrow") is None
    assert price_for(provider="not-a-provider", model="openai/gpt-4o-mini") is None


def test_the_stub_provider_is_free_whatever_its_model_string() -> None:
    """The stub's model is derived from config, so no fixed key can match it."""
    for model in ("stub-claude-opus-4-8", "stub-anything", "stub-deterministic-v1"):
        price = price_for(provider="stub", model=model)
        assert price is not None
        assert price.cost_for(input_tokens=10**9, output_tokens=10**9) == Decimal(0)


def test_every_configured_default_model_is_priced() -> None:
    """A shipped default that is not in the price book under-counts from day one."""
    from app.core.config import Settings

    s = Settings(llm_provider="stub")
    assert price_for(provider="router", model=s.openrouter_model) is not None, (
        f"default openrouter_model {s.openrouter_model!r} is not in the price book"
    )
    assert price_for(provider="gemini", model=s.gemini_model) is not None, (
        f"default gemini_model {s.gemini_model!r} is not in the price book"
    )


def test_known_models_is_non_empty_and_sorted() -> None:
    models = known_models()
    assert models
    assert models == sorted(models)


# ---------------------------------------------------------------------------
# build_call — the pricing decision, without a database
# ---------------------------------------------------------------------------


def test_build_call_prices_a_known_model() -> None:
    call = build_call(
        kind="llm",
        provider="router",
        model="openai/gpt-4o-mini",
        input_tokens=1_000_000,
        output_tokens=0,
        call_site=CallSite.answer,
    )
    assert call.priced is True
    assert call.cost_usd == Decimal("0.15")
    assert call.input_price_per_1m == Decimal("0.15")
    assert call.output_price_per_1m == Decimal("0.60")
    assert call.call_site == "answer"


def test_build_call_records_an_unknown_model_as_unpriced_not_free() -> None:
    """The distinction is the whole point: unpriced means 'budget is under-counting'."""
    call = build_call(
        kind="llm",
        provider="router",
        model="brand/new-model",
        input_tokens=999_999,
        output_tokens=999_999,
    )
    assert call.priced is False
    assert call.cost_usd == Decimal(0)
    # NULL rates, so a row is never mistaken for one priced at zero.
    assert call.input_price_per_1m is None
    assert call.output_price_per_1m is None
    # ...but the call still EXISTS. Dropping it would make the spend invisible.
    assert call.provider == "router"
    assert call.model == "brand/new-model"
    assert call.input_tokens == 999_999


def test_build_call_defaults_the_site_to_the_ambient_context() -> None:
    with call_site(CallSite.condense):
        call = build_call(
            kind="llm",
            provider="router",
            model="openai/gpt-4o-mini",
            input_tokens=1,
            output_tokens=1,
        )
    assert call.call_site == "condense"


def test_build_call_falls_back_to_unknown_outside_any_context() -> None:
    call = build_call(
        kind="llm",
        provider="router",
        model="openai/gpt-4o-mini",
        input_tokens=1,
        output_tokens=1,
    )
    assert call.call_site == "unknown"


def test_attempts_defaults_to_one_and_never_records_zero() -> None:
    """Attempts counts provider HTTP requests INCLUDING retries.

    A flaky provider costs up to 3x (embedders retry twice), and a budget that
    under-counts during an outage under-counts exactly when it matters.
    """
    assert (
        build_call(
            kind="llm",
            provider="router",
            model="openai/gpt-4o-mini",
            input_tokens=1,
            output_tokens=1,
        ).attempts
        == 1
    )
    assert (
        build_call(
            kind="llm",
            provider="router",
            model="openai/gpt-4o-mini",
            input_tokens=1,
            output_tokens=1,
            attempts=3,
        ).attempts
        == 3
    )
    assert (
        build_call(
            kind="llm",
            provider="router",
            model="openai/gpt-4o-mini",
            input_tokens=1,
            output_tokens=1,
            attempts=0,
        ).attempts
        == 1
    )


def test_negative_token_counts_are_clamped() -> None:
    call = build_call(
        kind="llm",
        provider="router",
        model="openai/gpt-4o-mini",
        input_tokens=-5,
        output_tokens=-5,
    )
    assert call.input_tokens == 0
    assert call.output_tokens == 0


# ---------------------------------------------------------------------------
# The call-site contextvar
# ---------------------------------------------------------------------------


def test_call_site_restores_the_previous_label_on_exception() -> None:
    """A failed call must not leak its label onto the next thing in the task."""
    with pytest.raises(RuntimeError), call_site(CallSite.alias_intent):
        raise RuntimeError("boom")
    assert get_call_site() is CallSite.unknown


def test_call_site_nests() -> None:
    with call_site(CallSite.answer):
        assert get_call_site() is CallSite.answer
        with call_site(CallSite.condense):
            assert get_call_site() is CallSite.condense
        assert get_call_site() is CallSite.answer
    assert get_call_site() is CallSite.unknown


def test_concurrent_tasks_do_not_share_a_call_site() -> None:
    """A module global would let one request's label bleed into another's."""

    async def _labelled(site: CallSite, delay: float) -> CallSite:
        with call_site(site):
            await asyncio.sleep(delay)
            return get_call_site()

    async def _run() -> list[CallSite]:
        return list(
            await asyncio.gather(
                _labelled(CallSite.answer, 0.02),
                _labelled(CallSite.condense, 0.01),
                _labelled(CallSite.alias_intent, 0.0),
            )
        )

    assert asyncio.run(_run()) == [CallSite.answer, CallSite.condense, CallSite.alias_intent]


# ---------------------------------------------------------------------------
# The factory seam — the safety-critical part
# ---------------------------------------------------------------------------


def test_the_stub_client_is_NOT_wrapped() -> None:
    """Wrapping the stub would silently re-enable the paid eval judge.

    ``tests/eval/runner.py`` decides whether to run the LLM judge with
    ``isinstance(get_llm_client(settings), StubLLMClient)``. Behind a decorator that
    check goes False, the judge activates on every hermetic run, and the free
    development path starts costing money — the exact failure the stub exists to
    prevent.
    """
    from app.core.config import Settings

    reset_llm_client()
    client = get_llm_client(Settings(llm_provider="stub"))
    reset_llm_client()
    assert isinstance(client, StubLLMClient)
    assert not isinstance(client, MeteredLLMClient)


def test_the_keyless_gemini_FALLBACK_stub_is_NOT_wrapped_either() -> None:
    """The riskiest stub is the one nobody asked for.

    ``CITEVYN_LLM_PROVIDER=gemini`` with neither a Gemini nor an OpenRouter key
    silently degrades to a bare stub inside ``_build_gemini_with_fallback`` — which
    is exactly the config a developer lands in after the owner set ``.env`` back to
    a keyless provider. If ``_metered`` ever wrapped that one, the judge's
    ``isinstance(..., StubLLMClient)`` check would go False on the very path chosen
    to spend nothing, and every hermetic eval run would start billing.
    """
    from app.core.config import Settings

    reset_llm_client()
    try:
        client = get_llm_client(
            Settings(llm_provider="gemini", gemini_api_key=None, openrouter_api_key=None)
        )
    finally:
        reset_llm_client()
    assert isinstance(client, StubLLMClient)
    assert not isinstance(client, MeteredLLMClient)


def test_paid_providers_ARE_wrapped() -> None:
    """Metering is applied at the single construction site, so a new call site
    cannot forget to meter."""
    from app.core.config import Settings

    for provider, kwargs in (
        ("router", {"openrouter_api_key": "k"}),
        ("anthropic", {"anthropic_api_key": "k"}),
        ("gemini", {"gemini_api_key": "k", "openrouter_api_key": "k"}),
    ):
        reset_llm_client()
        client = get_llm_client(Settings(llm_provider=provider, **kwargs))  # type: ignore[arg-type]
        assert isinstance(client, MeteredLLMClient), f"{provider} client is not metered"
        # build_llm_client stays PURE — it is provider selection, not wiring.
        bare = build_llm_client(Settings(llm_provider=provider, **kwargs))  # type: ignore[arg-type]
        assert not isinstance(bare, MeteredLLMClient)
    reset_llm_client()


# ---------------------------------------------------------------------------
# MeteredLLMClient behaviour
# ---------------------------------------------------------------------------


def test_metered_client_returns_the_inner_result_unchanged() -> None:
    inner = _FakeLLM()
    recorded: list[object] = []

    async def _fake_record(sm, call):  # type: ignore[no-untyped-def]
        recorded.append(call)

    client = MeteredLLMClient(inner, sessionmaker=object())  # type: ignore[arg-type]
    import app.cost.metered as metered_mod

    original = metered_mod.record_call
    metered_mod.record_call = _fake_record  # type: ignore[assignment]
    try:
        result = asyncio.run(client.complete(system="s", user="u", max_tokens=10, temperature=0.0))
    finally:
        metered_mod.record_call = original  # type: ignore[assignment]

    assert result is inner.result
    assert len(recorded) == 1


def test_metering_failure_does_not_break_the_answer() -> None:
    """The user already paid for this call; losing the ROW is the acceptable loss."""
    inner = _FakeLLM()
    client = MeteredLLMClient(inner, sessionmaker=object())  # type: ignore[arg-type]
    import app.cost.metered as metered_mod

    async def _boom(sm, call):  # type: ignore[no-untyped-def]
        raise RuntimeError("database is on fire")

    original = metered_mod.record_call
    metered_mod.record_call = _boom  # type: ignore[assignment]
    try:
        result = asyncio.run(client.complete(system="s", user="u", max_tokens=10, temperature=0.0))
    finally:
        metered_mod.record_call = original  # type: ignore[assignment]

    assert result is inner.result


def test_metered_client_delegates_aclose() -> None:
    """The wrapper must not strand the inner httpx connection pool."""
    inner = _FakeLLM()
    client = MeteredLLMClient(inner, sessionmaker=object())  # type: ignore[arg-type]
    asyncio.run(client.aclose())
    assert inner.closed is True


def test_a_failed_completion_is_not_metered() -> None:
    """A call that errored before generation was not billed; recording an estimate
    for it would inflate the budget with money that was never spent."""
    recorded: list[object] = []

    class _Failing:
        async def complete(self, **kwargs: object) -> LLMResult:
            raise RuntimeError("provider 500")

        async def aclose(self) -> None:
            return None

    async def _fake_record(sm, call):  # type: ignore[no-untyped-def]
        recorded.append(call)

    import app.cost.metered as metered_mod

    client = MeteredLLMClient(_Failing(), sessionmaker=object())  # type: ignore[arg-type]
    original = metered_mod.record_call
    metered_mod.record_call = _fake_record  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(client.complete(system="s", user="u", max_tokens=1, temperature=0.0))
    finally:
        metered_mod.record_call = original  # type: ignore[assignment]

    assert recorded == []


# ---------------------------------------------------------------------------
# Response-model variant resolution
#
# A provider's RESPONSE model is not the string we asked for, so these paths
# decide the price of real calls. An adversarial review found the first attempt
# at this (longest-prefix at a "-" boundary) mis-billed sibling tiers by 4-12x in
# BOTH directions, so every case below is a regression test for a real defect.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "model", "base"),
    [
        # Dated snapshots are the SAME model, pinned.
        ("router", "openai/gpt-4o-mini-2024-07-18", "openai/gpt-4o-mini"),
        ("router", "openai/gpt-4o-2024-11-20", "openai/gpt-4o"),
        ("anthropic", "claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        # Routing suffixes select HOW a request is routed, not what runs.
        ("router", "openai/gpt-4o-mini:floor", "openai/gpt-4o-mini"),
        ("router", "openai/gpt-4o-mini:nitro", "openai/gpt-4o-mini"),
    ],
)
def test_a_variant_resolves_to_its_base_models_price(provider: str, model: str, base: str) -> None:
    resolved = price_for(provider=provider, model=model)
    expected = price_for(provider=provider, model=base)
    assert resolved is not None, f"{model} went unpriced"
    assert expected is not None
    assert resolved == expected


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        # A "-lite" tier is a DIFFERENT, cheaper model. Collapsing it onto Flash
        # billed it 6.25x over on output and tripped the budget ~6x early.
        ("gemini", "gemini-2.5-flash-lite-preview"),
        # Modality variants cost MUCH more. Collapsing them onto the text tier
        # under-counted 4-12x, so the budget would never trip at all.
        ("router", "openai/gpt-4o-mini-realtime-preview"),
        ("router", "openai/gpt-4o-realtime-preview"),
        ("gemini", "gemini-2.5-flash-image"),
        ("gemini", "gemini-2.5-flash-audio"),
        # A future family member must land in unpriced_calls, which is the alarm
        # that says "a real model is going unbilled" — not be silently guessed at.
        ("router", "openai/gpt-4o-mini-something-new"),
        ("gemini", "gemini-2.5-flash-turbo"),
        # An unrecognised routing suffix is not a licence to guess.
        ("router", "openai/gpt-4o-mini:unknown-route"),
    ],
)
def test_a_sibling_or_modality_variant_is_NOT_priced_as_its_prefix(
    provider: str, model: str
) -> None:
    """A wrong price is worse than an admitted gap: the gap is visible."""
    assert price_for(provider=provider, model=model) is None


def test_explicitly_priced_sibling_tiers_keep_their_own_rate() -> None:
    lite = price_for(provider="gemini", model="gemini-2.5-flash-lite")
    flash = price_for(provider="gemini", model="gemini-2.5-flash")
    assert lite is not None and flash is not None
    assert lite.output_per_1m < flash.output_per_1m


def test_a_free_openrouter_route_costs_nothing() -> None:
    """A ':free' route must not consume budget it never spends."""
    price = price_for(provider="router", model="openai/gpt-4o-mini:free")
    assert price is not None
    assert price.cost_for(input_tokens=10**6, output_tokens=10**6) == Decimal(0)


# ---------------------------------------------------------------------------
# Token estimation when the provider omits its usage block
# ---------------------------------------------------------------------------


def _result(**kw: object) -> LLMResult:
    base = {
        "text": "answer",
        "input_tokens": 0,
        "output_tokens": 0,
        "model": "openai/gpt-4o-mini",
        "provider": "router",
    }
    base.update(kw)
    return LLMResult(**base)  # type: ignore[arg-type]


def test_reported_tokens_are_used_verbatim_and_not_flagged_estimated() -> None:
    from app.cost.metered import _resolve_tokens

    tokens = _resolve_tokens(
        _result(input_tokens=1234, output_tokens=56), prompt_chars=8000, max_tokens=1024
    )
    assert tokens == (1234, 56, False)


def test_a_missing_usage_block_is_ESTIMATED_not_recorded_as_zero() -> None:
    """OpenRouter really does return 200 with no usage on some routed upstreams.

    Taken at face value that is 0 tokens / $0 / priced=True — indistinguishable
    from a genuinely free call, absent from unpriced_calls, and invisible to the
    §9 budget while real money is being spent.
    """
    from app.cost.metered import _resolve_tokens

    in_tok, out_tok, estimated = _resolve_tokens(
        _result(text="x" * 400), prompt_chars=4000, max_tokens=1024
    )
    assert estimated is True
    assert in_tok == 1000  # 4000 chars / 4
    assert out_tok == 100  # 400 chars / 4
    assert in_tok > 0 and out_tok > 0


def test_an_empty_completion_with_no_usage_falls_back_to_the_authorised_ceiling() -> None:
    """Deliberately conservative-HIGH: under-counting is the failure that costs
    money, over-counting only trips the limit early."""
    from app.cost.metered import _resolve_tokens

    in_tok, out_tok, estimated = _resolve_tokens(
        _result(text=""), prompt_chars=4000, max_tokens=777
    )
    assert estimated is True
    assert out_tok == 777


def test_a_partial_usage_block_is_still_trusted_not_re_estimated() -> None:
    """input>0 with output==0 is a legitimate shape (an empty completion), so it
    must NOT be overwritten by a guess."""
    from app.cost.metered import _resolve_tokens

    assert _resolve_tokens(
        _result(input_tokens=500, output_tokens=0), prompt_chars=9999, max_tokens=1024
    ) == (500, 0, False)


def test_the_estimated_flag_reaches_the_persisted_row() -> None:
    """An estimate that is not FLAGGED is worse than no estimate — it reads as fact."""
    call = build_call(
        kind="llm",
        provider="router",
        model="openai/gpt-4o-mini",
        input_tokens=1000,
        output_tokens=100,
        tokens_estimated=True,
    )
    assert call.tokens_estimated is True
    assert (
        build_call(
            kind="llm",
            provider="router",
            model="openai/gpt-4o-mini",
            input_tokens=1000,
            output_tokens=100,
        ).tokens_estimated
        is False
    )


def test_request_id_is_None_not_empty_string_outside_a_request() -> None:
    """`WHERE request_id IS NULL` is the natural 'spend not caused by a user
    request' query; storing '' would make it return nothing."""
    from app.cost.metered import _current_request_id

    assert _current_request_id() is None


def test_meter_wiring_flags_an_estimated_call_on_the_recorded_row() -> None:
    """End-to-end through MeteredLLMClient, not just build_call.

    Testing ``build_call(tokens_estimated=True)`` in isolation proves the column
    works but NOT that ``_meter`` passes the flag through — a mutation dropping
    ``tokens_estimated=estimated`` survived the isolated test. This drives the real
    wrapper with a usage-less result and inspects the row that would be persisted.
    """
    import app.cost.metered as metered_mod

    captured: list[object] = []

    class _NoUsage:
        async def complete(self, *, system, user, max_tokens, temperature):  # type: ignore[no-untyped-def]
            del system, user, max_tokens, temperature
            return _result(text="hello world")  # 0/0 tokens: no usage block

        async def aclose(self) -> None:
            return None

    async def _capture(sm, call):  # type: ignore[no-untyped-def]
        captured.append(call)

    original = metered_mod.record_call
    metered_mod.record_call = _capture  # type: ignore[assignment]
    try:
        asyncio.run(
            metered_mod.MeteredLLMClient(_NoUsage(), sessionmaker=object()).complete(  # type: ignore[arg-type]
                system="s" * 100, user="u" * 300, max_tokens=512, temperature=0.0
            )
        )
    finally:
        metered_mod.record_call = original  # type: ignore[assignment]

    assert len(captured) == 1
    row = captured[0]
    assert row.tokens_estimated is True, "the estimated flag never reached the row"
    assert row.input_tokens == 100  # (100 + 300) chars / 4
    assert row.cost_usd > 0, "an estimated call must still contribute to the budget"


def test_meter_wiring_does_NOT_flag_a_call_that_reported_real_usage() -> None:
    """The mirror: a real usage block must not be mislabelled as a guess."""
    import app.cost.metered as metered_mod

    captured: list[object] = []

    async def _capture(sm, call):  # type: ignore[no-untyped-def]
        captured.append(call)

    original = metered_mod.record_call
    metered_mod.record_call = _capture  # type: ignore[assignment]
    try:
        asyncio.run(
            metered_mod.MeteredLLMClient(_FakeLLM(), sessionmaker=object()).complete(  # type: ignore[arg-type]
                system="s", user="u", max_tokens=10, temperature=0.0
            )
        )
    finally:
        metered_mod.record_call = original  # type: ignore[assignment]

    assert captured[0].tokens_estimated is False
    assert captured[0].input_tokens == 1000
