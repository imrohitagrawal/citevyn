from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# ---------------------------------------------------------------------------
# Defaults exposed as module constants so tests can pin them.
# ---------------------------------------------------------------------------

DEFAULT_NO_ANSWER_FALLBACK: str = (
    "I couldn't find a grounded answer for that question. "
    "Try rephrasing with more specific terms, or ask about "
    "Claude, Claude Code, Codex, or the Gemini API."
)

# The refusal names the CiteVyn meta-domain alongside the four products (#84 item 5).
# A near-miss meta question ("what is Pro?") routes to ``unsupported`` because it never
# says "CiteVyn"; without the hint the user has no way to learn that naming the product
# is the phrasing that works, and reads the refusal as "this tool cannot answer that at
# all". Additive only — the four products stay first, so the refusal still reads as a
# scope statement rather than an upsell.
DEFAULT_UNSUPPORTED_REFUSAL: str = (
    "I can answer questions about Claude, Claude Code, Codex, and Gemini using "
    "their official documentation — or about CiteVyn itself. I do not have "
    "credible source material in this assistant to answer that."
)

# Sensible default origins for local development. Production deploys
# MUST override ``CITEVYN_CORS_ALLOWED_ORIGINS`` to the approved
# frontend host. The default is intentionally a single localhost
# origin (per ``docs/SECURITY_MODEL.md §11``) — no wildcards.
DEFAULT_CORS_ALLOWED_ORIGINS: tuple[str, ...] = ("http://localhost:3000",)


def _is_weak_secret(value: str, *, default: str) -> bool:
    """True when ``value`` is the published default, a trivial variant of it, or short.

    Raw ``==`` was not enough. Verified against a production ``Settings``:
    ``'local-demo-key '``, ``' local-demo-key'`` and ``'LOCAL-DEMO-KEY'`` all
    PASSED a plain equality check, leaving the effective bearer guessable in one
    or two attempts. Docker compose's env-file parser happens to strip quotes and
    trailing whitespace, so the compose path was incidentally safe — but these
    guards exist for the NON-compose entry points (a bare ``uvicorn``, ``alembic``,
    a one-off script), where an exported ``KEY='local-demo-key '`` sails through.
    ``_env_guard.sh`` grew its own ``_strip`` helper for exactly this class of
    bypass; this is the Python-side equivalent.

    The length floor is the second half: rejecting only the known default would
    still accept ``x``. 16 chars is well below any real generated secret and well
    above anything typed by hand in a hurry.
    """
    normalised = value.strip().lower()
    return normalised == default or len(value.strip()) < 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CITEVYN_",
        env_file=".env",
        extra="ignore",
    )

    # --- Application / transport ---
    app_name: str = "CiteVyn Backend"
    environment: str = "local"
    demo_api_key: str = Field(default="local-demo-key", min_length=1)
    request_id_header: str = "X-Request-ID"

    # --- Admin auth (Slice 8) ---
    admin_api_key: str = Field(default="local-admin-key", min_length=1)
    admin_api_key_header: str = "X-Admin-API-Key"

    # --- CORS (Slice 8) ---
    # ``NoDecode`` tells pydantic-settings NOT to JSON-decode the env
    # string — the field validator below splits the comma-separated
    # value into a list. Without ``NoDecode`` the env loader would
    # try to parse the whole string as a single JSON list and fail.
    cors_allowed_origins: Annotated[list[str], NoDecode] = list(DEFAULT_CORS_ALLOWED_ORIGINS)

    # --- Rate limiting (Slice 8 + Slice 9a) ---
    # Sliding-window limits from ``docs/SECURITY_MODEL.md §6``:
    # demo_user 30 q/h, admin 100 q/h. The limiter is **Redis-backed**
    # when ``CITEVYN_REDIS_URL`` is set, in-process otherwise. The
    # in-process path is retained for hermetic tests and single-worker
    # development; production deploys MUST set ``CITEVYN_REDIS_URL``.
    rate_limit_enabled: bool = True
    rate_limit_demo_user_per_hour: int = Field(default=30, ge=1)
    rate_limit_admin_per_hour: int = Field(default=100, ge=1)
    rate_limit_window_seconds: int = Field(default=3600, ge=1)
    redis_url: str | None = None

    # --- Persistence (Slice 2+) ---
    database_url: str = Field(
        default="postgresql+psycopg://citevyn:citevyn@localhost:5432/citevyn",
        min_length=1,
    )
    database_echo: bool = False
    database_pool_size: int = Field(default=5, ge=1)
    index_session_ttl_seconds: int = Field(default=60 * 60 * 24, ge=1)
    pg_test_url: str | None = None

    # --- LLM (Slice 4+) ---
    llm_provider: str = "stub"  # "stub" | "anthropic" | "gemini" | "router"
    # Model for the anthropic + stub providers only. gemini/router read their
    # own gemini_model / openrouter_model below and ignore this field.
    llm_model: str = "claude-opus-4-8"
    llm_max_tokens: int = Field(default=1024, ge=1)
    llm_temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    anthropic_api_key: str | None = None
    anthropic_api_base: str = "https://api.anthropic.com"
    anthropic_api_version: str = "2023-06-01"
    anthropic_timeout_seconds: float = Field(default=30.0, gt=0.0)

    # --- LLM: Gemini + OpenRouter (Slice 9b; models refreshed #99) ---
    # Primary provider is Gemini (CITEVYN_LLM_PROVIDER=gemini); the factory
    # transparently falls back to OpenRouter when the Gemini call fails or no
    # Gemini key is set but an OpenRouter key is. Set CITEVYN_LLM_PROVIDER=router
    # to route straight to OpenRouter. Keys come from the environment only.
    #
    # Model choice is cost-driven (#99): the Gemini primary runs on the Google AI
    # Studio FREE tier (rate-limited, ~$0), so it is priority-1; GPT-4o-mini on
    # OpenRouter is the PAID backstop (priority-2), used only when Gemini errors.
    # Even on Gemini's paid tier, Flash ($0.15/$0.60 per 1M) undercuts GPT-4o-mini,
    # so the free-primary / paid-fallback ordering is the cheaper arrangement.
    gemini_api_key: str | None = None
    gemini_api_base: str = "https://generativelanguage.googleapis.com"
    # ``gemini-flash-latest`` auto-tracks the current Flash GA model. The previous
    # pin ``gemini-2.5-flash`` was retired for new API projects (404 "no longer
    # available to new users", #99); ``gemini-2.0-flash`` is being shut down. The
    # alias avoids re-pinning a soon-to-retire snapshot.
    gemini_model: str = "gemini-flash-latest"
    # 15s (not 30) so the sequential Gemini→OpenRouter fallback has a ~30s
    # worst-case ceiling rather than 60s. Flash answers return in a few seconds.
    gemini_timeout_seconds: float = Field(default=15.0, gt=0.0)
    # Gemini "thinking" budget. 0 disables thinking (right for Flash doc answers —
    # spends the token budget on the answer, not reasoning). Set -1 for dynamic,
    # or a positive value for a model that requires thinking (e.g. a Pro tier, or
    # a future Flash that mandates it) if you switch gemini_model.
    gemini_thinking_budget: int = Field(default=0, ge=-1)
    openrouter_api_key: str | None = None
    openrouter_api_base: str = "https://openrouter.ai/api/v1"
    # Paid fallback (priority-2). GPT-4o-mini is the resilience backstop when the
    # free Gemini primary is unavailable; it was chosen over pinning another
    # Gemini snapshot on OpenRouter so a single Google-side retirement cannot take
    # out both arms at once (the prior ``google/gemini-2.5-flash`` shared the #99
    # retirement with the primary).
    openrouter_model: str = "openai/gpt-4o-mini"
    openrouter_timeout_seconds: float = Field(default=15.0, gt=0.0)

    # --- Retrieval / embeddings (Slice 4+ / #51) ---
    # Provider seam mirroring the LLM factory. "stub" is the deterministic,
    # keyless offline default (hermetic tests, local dev); "gemini" uses
    # gemini-embedding-001 via CITEVYN_GEMINI_API_KEY (the same key as the LLM).
    # See docs/ADR/0003-embeddings-provider.md for the provider decision.
    # "stub" | "gemini" | "openrouter". The "openrouter" provider reaches OpenAI's
    # text-embedding-3-* models (native 1536-dim, fits the pgvector column) via the
    # OpenAI-compatible endpoint; set CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small
    # with it (the default below is Gemini-shaped). See ADR-0003 (OpenRouter addendum).
    embedding_provider: str = "stub"  # "stub" | "gemini" | "openrouter"
    embedding_model: str = "gemini-embedding-001"
    # 1536 is the largest recommended Gemini Matryoshka output size that fits
    # under pgvector's 2000-dim index limit. The pgvector column is
    # vector(embedding_dim); changing this value requires a new migration to
    # keep the column dimension in lock-step (see migration 0004).
    embedding_dim: int = Field(default=1536, ge=1, le=2000)
    embedding_timeout_seconds: float = Field(default=15.0, gt=0.0)
    embedding_max_retries: int = Field(default=2, ge=0)
    retrieval_top_k: int = Field(default=6, ge=1)
    retrieval_max_candidates: int = Field(default=20, ge=1)

    # --- "Answer when grounded" — global retrieval for unsupported-routed
    #     questions (Phase 2). A question that doesn't NAME a product routes to
    #     ``unsupported``; instead of an immediate refusal we retrieve GLOBALLY
    #     (across all product areas) and answer when the evidence is confident.
    #     Refusal safety = the confidence gate below (drops off-corpus queries at
    #     retrieval) + the existing LLM grounding-refusal (the final net). Set
    #     ``False`` to restore the old refuse-before-retrieval behavior.
    answer_when_grounded: bool = True
    # Confidence gate for the GLOBAL vector result (see app/retrieval/confidence.py).
    # An off-corpus query's nearest chunk is either barely related (below the floor)
    # or one of a muddle of ~equal weak matches (below the margin); an in-corpus
    # query has one clearly-best chunk. Tuned on the ingested corpus (answerable
    # margins >= 0.070, refusal margins <= 0.027); the eval harness validates changes.
    retrieval_global_min_top_score: float = Field(default=0.30, ge=0.0, le=1.0)
    retrieval_global_min_margin: float = Field(default=0.04, ge=0.0, le=1.0)

    # --- Conversation memory (Phase 3b) — resolve an anaphoric follow-up ("How can
    #     I raise it?") against recent turns so retrieval + the answer see the topic.
    #     Only a follow-up that names NO product AND is genuinely anaphoric/elliptical
    #     is rewritten (a self-contained off-domain sentence still reaches the refusal;
    #     see app/answer/memory.py). ``memory_recent_turns`` bounds how far back we
    #     look for the antecedent. Set ``conversation_memory=False`` to disable.
    # --- CiteVyn alias intent check (#84 follow-up) ---
    #     Single-token manglings ("sitewin") are matched deterministically by the
    #     guardrail. The two-word homophones ("site win") are ordinary English, so they
    #     get an LLM intent check over the whole utterance instead — see
    #     app/answer/alias_intent.py for why no regex can do this. Costs one short call,
    #     and ONLY on a message actually containing "site|cite|sight win", which is
    #     essentially never in normal traffic. Set False to disable (the question then
    #     refuses exactly as it did before).
    citevyn_alias_intent_check: bool = True

    conversation_memory: bool = True
    memory_recent_turns: int = Field(default=6, ge=1)

    # --- Cost controls (#153, RELEASE_PLAN section 9) ---
    # The daily budget is computed by SUMMING ``provider_calls`` since midnight UTC,
    # so it survives an API restart. An in-process counter would hand out a fresh
    # allowance on every restart -- which is exactly why the 30 q/h per-user limiter
    # below is anti-nuisance only and NOT a spend control.
    cost_budget_enabled: bool = True
    cost_soft_daily_usd: float = Field(default=5.0, ge=0.0)
    cost_hard_daily_usd: float = Field(default=10.0, ge=0.0)
    # When the meter store cannot be read we do not know what has been spent.
    # FAIL CLOSED by default: an unreadable meter must not become an unmetered
    # spending window whose only ceiling is the provider-side cap. An operator who
    # genuinely prefers availability over cost can flip this -- deliberately, and
    # visibly, rather than by accident of error handling.
    cost_budget_fail_closed: bool = True
    # Layer 2 admission control: a ceiling on paid calls IN FLIGHT at once. There is
    # no such cap today, so a burst can run up spend faster than the daily budget can
    # observe it (every in-flight call reads a spend total that predates its peers).
    cost_max_concurrent_calls: int = Field(default=8, ge=1)

    # --- Answer cache (Slice 5+) ---
    # Part of the cache-key pre-image, so bumping it invalidates EVERY cached answer by
    # design. Bump it whenever an answer-pipeline change makes previously-cached answers
    # wrong — a code fix alone cannot clear rows that are already persisted.
    #
    # v1 → v2 (#169): follow-up answers were generated from the memory CONCATENATION
    # ("What is Codex CLI? who built it?"), so the LLM answered the leading clause and the
    # follow-up was stored as a verbatim duplicate of the previous turn's answer. Those
    # rows are POISONED: they sit under their own valid keys with a correct
    # ``source_version_hash`` and ``embedder_identity``, so nothing else invalidates them
    # and they would keep replaying the wrong answer after the fix ships. A targeted
    # DELETE was rejected — it cannot be expressed as a sound predicate (a legitimate
    # multi-clause question is textually indistinguishable from a concatenation), it would
    # have to be re-run by hand against every environment, and it leaves no record in the
    # code. The version bump is declarative, applies everywhere the build is deployed, and
    # its only cost is a cold cache that refills on demand.
    answer_policy_version: str = "v2"
    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=86_400, ge=1)

    # --- Worker (Slice 8) ---
    # Out-of-process ingestion worker (see ``app/worker/cli.py``). The
    # CLI entry point polls ``ingestion_jobs`` every
    # ``worker_poll_seconds`` and processes one job at a time.
    worker_poll_seconds: float = Field(default=2.0, gt=0.0)
    worker_max_runtime_seconds: int = Field(default=0, ge=0)  # 0 = unbounded
    worker_fetch_timeout_seconds: float = Field(default=20.0, gt=0.0)
    worker_max_chunks_per_doc: int = Field(default=500, ge=1)

    # --- Index promotion gate (Slice 8) ---
    # Per ``docs/RELEASE_PLAN.md §7`` the promotion gate rejects a
    # candidate index whose latest ``EvaluationRun.metrics.pass_rate``
    # is below this threshold. Default 0.95 matches the
    # "golden pass rate >= 95%" gate.
    index_promotion_min_pass_rate: float = Field(default=0.95, ge=0.0, le=1.0)

    # (The former ``source_version_hash`` setting was removed alongside the
    # content-derived snapshot hash: it was a static placeholder that nothing
    # could usefully change. The worker now derives the hash from the actual
    # corpus bytes — see ``app.worker.cli.content_version_hash`` — so an
    # operator "bumping the constant" had no effect and only invited confusion.
    # ``model_config`` uses ``extra="ignore"``, so a leftover
    # ``CITEVYN_SOURCE_VERSION_HASH`` in an existing ``.env`` is harmless.)

    # (The former ``fixtures_root`` setting was removed in #92: it was dead config
    # — nothing read it, and it pointed at a non-existent ``backend/fixtures/sources``.
    # The worker's ``LocalFetcher`` resolves ``SourceSpec.location`` against the
    # package root, and the source docs now ship under ``app/worker/sources`` so a
    # prod ``run`` can read them without a bind mount.)

    # --- Redis key prefix (Slice 9a) ---
    # All keys created by the rate limiter are namespaced with this
    # prefix so multiple CiteVyn environments (dev / staging / prod)
    # can share a single Redis instance without colliding.
    redis_key_prefix: str = "citevyn:rl"

    # --- Response copy (Slice 4+) ---
    unsupported_refusal: str = DEFAULT_UNSUPPORTED_REFUSAL
    no_answer_fallback: str = DEFAULT_NO_ANSWER_FALLBACK

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Accept either a list (typed) or a comma-separated env string.

        Pydantic-settings reads ``CITEVYN_CORS_ALLOWED_ORIGINS`` as a
        single string even when the field type is ``list[str]``; this
        validator splits on ``,`` so the env-var path Just Works.
        """
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    # ------------------------------------------------------------------
    # Production guards
    # ------------------------------------------------------------------
    #
    # These validators are the canonical "fail at parse time" check for
    # env combinations that should never reach production. They run on
    # every ``Settings()`` construction — uvicorn, alembic, the
    # worker, an admin script, a test. The previous Slice 9a design
    # only ran the LLM-provider check in the FastAPI ``lifespan`` body
    # which meant a bare ``TestClient(app)`` (no ``with`` block) never
    # exercised the guard, and an alembic / worker bootstrap would
    # silently accept a stub provider in production.

    @model_validator(mode="after")
    def _reject_stub_llm_in_production(self) -> "Settings":
        # ``stub`` is the dev-only deterministic LLM. ``""`` is the
        # reserved router placeholder for Slice 9b. Both must never
        # reach production — the demo build would otherwise silently
        # serve the stub answer. ``anthropic`` and ``gemini`` are
        # the only production-allowed providers.
        if self.environment != "production":
            return self
        if self.llm_provider in ("stub", ""):
            raise ValueError(
                f"CITEVYN_LLM_PROVIDER={self.llm_provider!r} is not allowed "
                "when CITEVYN_ENVIRONMENT='production'. Set "
                "CITEVYN_LLM_PROVIDER to 'anthropic' or 'gemini' and "
                "provide the matching API key."
            )
        return self

    @model_validator(mode="after")
    def _require_anthropic_api_key_in_production(self) -> "Settings":
        if (
            self.environment == "production"
            and self.llm_provider == "anthropic"
            and not self.anthropic_api_key
        ):
            raise ValueError(
                "CITEVYN_ANTHROPIC_API_KEY must be set when "
                "CITEVYN_LLM_PROVIDER='anthropic' and "
                "CITEVYN_ENVIRONMENT='production'."
            )
        return self

    @model_validator(mode="after")
    def _require_gemini_key_for_embeddings_in_production(self) -> "Settings":
        # The Gemini embedder reads ``gemini_api_key``. In production with the
        # gemini embedding provider selected, a missing key would only fail on the
        # first ingest/query (lazy build); fail at parse time instead so a
        # misconfigured deploy is caught at boot.
        if (
            self.environment == "production"
            and self.embedding_provider == "gemini"
            and not self.gemini_api_key
        ):
            raise ValueError(
                "CITEVYN_GEMINI_API_KEY must be set when "
                "CITEVYN_EMBEDDING_PROVIDER='gemini' and "
                "CITEVYN_ENVIRONMENT='production'."
            )
        return self

    @model_validator(mode="after")
    def _require_openrouter_key_for_embeddings_in_production(self) -> "Settings":
        # The OpenRouter embedder reads ``openrouter_api_key``. Mirror the gemini
        # guard so a production deploy with the openrouter embedding provider and no
        # key fails at boot, not on the first ingest/query.
        if (
            self.environment == "production"
            and self.embedding_provider == "openrouter"
            and not self.openrouter_api_key
        ):
            raise ValueError(
                "CITEVYN_OPENROUTER_API_KEY must be set when "
                "CITEVYN_EMBEDDING_PROVIDER='openrouter' and "
                "CITEVYN_ENVIRONMENT='production'."
            )
        return self

    @model_validator(mode="after")
    def _reject_gemini_model_under_openrouter_embeddings(self) -> "Settings":
        # ``embedding_model`` defaults to the Gemini model name. Selecting the
        # openrouter provider without also setting an OpenAI-shaped model would POST
        # ``gemini-embedding-001`` to OpenRouter's /embeddings endpoint (a 400/404
        # with a confusing upstream error). Catch the provider/model incoherence at
        # parse time with an actionable message instead.
        if self.embedding_provider == "openrouter" and self.embedding_model.startswith("gemini"):
            raise ValueError(
                f"CITEVYN_EMBEDDING_MODEL={self.embedding_model!r} is a Gemini model but "
                "CITEVYN_EMBEDDING_PROVIDER='openrouter'. Set "
                "CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small (or another "
                "OpenAI-compatible embedding model served by OpenRouter)."
            )
        return self

    @model_validator(mode="after")
    def _reject_default_demo_key_in_production(self) -> "Settings":
        # ``local-demo-key`` is the dev default and is PUBLICLY KNOWN — it is
        # printed in this repo's README, .env.example and test suite. It is also
        # the bearer for every ``/v1/*`` route, i.e. the auth for the entire demo
        # surface, so accepting it in production means the demo is effectively
        # unauthenticated to anyone who has read the source.
        #
        # The admin key has had this guard since Slice 8; the demo key never did,
        # and ``infra/docker/prod.env.example`` did not even list the variable —
        # so a production deploy silently inherited the default. Found by actually
        # running ``make deploy-verify``, which requires the key and died without it.
        if self.environment == "production" and _is_weak_secret(
            self.demo_api_key, default="local-demo-key"
        ):
            raise ValueError(
                "CITEVYN_DEMO_API_KEY must be set to a strong secret when "
                "CITEVYN_ENVIRONMENT='production'. The value is "
                + (
                    "the publicly-known default 'local-demo-key'"
                    if self.demo_api_key.strip().lower() == "local-demo-key"
                    else "shorter than the 16-character minimum"
                )
                + " and is not allowed."
            )
        return self

    @model_validator(mode="after")
    def _reject_default_admin_key_in_production(self) -> "Settings":
        # ``local-admin-key`` is the dev default and is publicly known
        # (it lives in the open-source repo). Reject it in production
        # so a misconfigured deploy cannot accept it as the admin
        # bearer. The compose ``prod`` profile already requires the
        # var via ``${CITEVYN_ADMIN_API_KEY:?...}`` — this validator
        # is the belt-and-braces guard for non-compose entry points
        # (bare ``uv run uvicorn``, alembic, a one-off admin script).
        if self.environment == "production" and _is_weak_secret(
            self.admin_api_key, default="local-admin-key"
        ):
            raise ValueError(
                "CITEVYN_ADMIN_API_KEY must be set to a strong secret when "
                "CITEVYN_ENVIRONMENT='production'. The value is "
                + (
                    "the publicly-known default 'local-admin-key'"
                    if self.admin_api_key.strip().lower() == "local-admin-key"
                    else "shorter than the 16-character minimum"
                )
                + " and is not allowed."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
