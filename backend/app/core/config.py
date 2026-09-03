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
    # ADR-0004 PR 11: a signed-in caller gets a higher limit, keyed on their
    # own user_id rather than an IP-derived key -- makes the pricing page's
    # "sign in for a higher limit" pitch structurally true, not copy alone.
    rate_limit_demo_user_registered_per_hour: int = Field(default=100, ge=1)
    rate_limit_admin_per_hour: int = Field(default=100, ge=1)
    rate_limit_window_seconds: int = Field(default=3600, ge=1)

    # --- Per-visitor rate-limit identity (#203) ---
    # The demo API key is SHARED by construction, so it can never identify a
    # visitor: ``require_demo_api_key`` returns a constant, which meant every
    # visitor on earth shared one bucket and 30 questions from one person locked
    # out everyone else for an hour.
    #
    # The rate-limit key is therefore derived from the client IP, separately from
    # the AUDIT identity (which stays ``DEMO_USER_ID`` — attribution is unchanged).
    #
    # ``rate_limit_client_ip_header`` names the header to trust for the client
    # address. Trusting a header is only safe when the app cannot be reached
    # except THROUGH that proxy, which is true on Fly (the app has no public port
    # of its own). Set it to "" to trust nothing and use the socket peer address.
    #   * Fly, no CDN in front  -> "Fly-Client-IP"  (default)
    #   * Cloudflare proxying   -> "CF-Connecting-IP"
    #   * plain reverse proxy   -> "X-Forwarded-For" (leftmost entry is used)
    rate_limit_client_ip_header: str = "Fly-Client-IP"
    # Buckets are keyed on a SALTED HASH of the address — a raw IP is personal
    # data and must not sit in Redis. An unsalted hash of an IPv4 address is
    # trivially reversible (2^32 candidates), so the salt is what makes this
    # meaningful. Empty means "fall back to the demo API key", which production
    # already requires to be a strong secret (>=16 chars, not the default).
    rate_limit_key_salt: str = ""
    # Anti-nuisance backstop across ALL visitors, so a distributed source still
    # meets a ceiling. Deliberately generous — it must not bind on ordinary use.
    # 0 disables it. NB this bounds REQUEST VOLUME, not money; the §9 daily budget
    # is the only control that caps spend.
    rate_limit_global_per_hour: int = Field(default=600, ge=0)

    # Credential-stuffing guard for ``POST /v1/auth/login`` (ADR-0004 PR 6). Keyed
    # per TARGET EMAIL (salted hash, same idiom as the IP key above), not per
    # client — a distributed attacker trying one password across 200 source IPs
    # against the SAME account must still be stopped, and an IP-keyed limiter lets
    # exactly that through. Deliberately much lower than the general demo limit:
    # this bucket exists to slow credential stuffing, not to serve ordinary
    # traffic. ``register`` uses the same bucket (keyed on the email being
    # claimed) so a registration-spam loop against one address is bounded too.
    rate_limit_auth_login_per_hour: int = Field(default=10, ge=1)

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

    # --- OAuth login: GitHub + Google (ADR-0004 PR 12) ---
    # Flat fields, matching the gemini_api_key/openrouter_api_key idiom above --
    # no nested OAuthConfig object, no precedent for nesting anywhere in this
    # class. "Available" is always bool(client_id and client_secret), computed
    # at the call site -- never a parallel _enabled flag.
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    # The exact-match redirect-URI requirement means the backend must send a
    # fixed, config-derived https://<host>/v1/auth/oauth/{provider}/callback to
    # the provider -- NEVER derived from request.base_url/Host at request time
    # (a request through a proxy/CDN could present a spoofed Host; deriving the
    # redirect URI from client-influenced input is exactly the open-redirect
    # risk the exact-match requirement exists to close). Falls back to
    # http://localhost:8000 when unset, for local dev.
    oauth_redirect_base_url: str | None = None
    # ADR-0004 PR 13 (account linking): `GET .../oauth/{provider}/connect/start`
    # is refused unless the caller's AuthSession was CREATED within this many
    # seconds. A session row's created_at never refreshes and sessions live
    # up to auth_session_ttl_seconds (180 days), so without this gate a stolen
    # cookie could link the thief's own GitHub/Google identity to the victim's
    # account at any point in those 180 days -- turning a temporary session
    # compromise into a permanent backdoor that survives logout and a later
    # password change. 20 minutes bounds that to "right after a genuine
    # login", forcing a fresh credential check before linking, with no new
    # auth primitive. Residual (recorded in the ADR): a cookie stolen INSIDE
    # this window is not caught; closing that needs true step-up re-auth.
    oauth_connect_max_session_age_seconds: int = Field(default=20 * 60, ge=1)

    # --- Magic-link login + transactional email (ADR-0004 PR 14) ---
    # Resend is the only delivery provider wired today (``app.core.email_client``
    # keeps the seam so a second one is a class, not a rewrite). "Configured"
    # is ``bool(resend_api_key)``, computed at the call site -- never a
    # parallel ``_enabled`` flag, same idiom as the OAuth credentials above.
    resend_api_key: str | None = None
    # The From: header ("CiteVyn <login@example.com>"). Must be on a domain the
    # Resend account has verified (SPF/DKIM/DMARC), so there is deliberately
    # no default -- a wrong default would fail silently at the provider.
    email_from: str | None = None
    # Dev-only delivery path: when no ``resend_api_key`` is set and
    # ``environment != "production"``, each email is written to a file under
    # this directory instead of sent (defaults to a ``citevyn_email_outbox``
    # folder under the system temp dir). Refused in production by
    # ``_reject_email_outbox_in_production`` -- an outbox there would mean
    # magic links silently go nowhere.
    email_outbox_dir: str | None = None
    # Absolute origin the emailed link points at, e.g.
    # https://citevyn.stackclimb.com. Same reasoning as
    # ``oauth_redirect_base_url``: NEVER derived from request.base_url/Host at
    # request time (a spoofed Host would put an attacker's origin into a
    # victim's email -- the textbook password-reset-poisoning CVE). Also the
    # origin the confirm POST's ``Origin`` header must match. Falls back to
    # http://localhost:8000 for local dev.
    magic_link_base_url: str | None = None
    # 10 minutes: long enough to survive real email delivery latency, short
    # enough to bound the window -- and shorter than a password-reset link
    # would want, since a magic link grants a full session immediately.
    magic_link_ttl_seconds: int = Field(default=600, ge=60)
    # Per TARGET EMAIL, a DEDICATED bucket -- never the ``auth_login`` one.
    # Sharing it would let an attacker lock a victim out of their own
    # password login by flooding link requests at their address with zero
    # credentials. Low on purpose: every hit under the cap is one email
    # sent to that address, so this is also the email-bombing ceiling.
    rate_limit_magic_link_per_hour: int = Field(default=5, ge=1)
    # Minimum gap between two magic-link requests for ONE address (#301). The
    # hourly bucket above caps the day's damage but sets no floor between
    # requests, so five clicks in five seconds sent five emails -- and because
    # the route keeps a single live token per user, each one killed the previous
    # link, leaving four dead links in the inbox above the only working one.
    # This is a WINDOW, not a count: the limit is fixed at 1 by definition.
    rate_limit_magic_link_interval_seconds: int = Field(default=60, ge=1)
    # ADR-0004 PR 15 (#293): how long after redeeming a magic link the SAME
    # session may set a new password without the current one (one shot).
    # Short: the window is the whole exposure a stolen link adds beyond the
    # session itself, and the owner is emailed on every use.
    password_step_up_window_seconds: int = Field(default=600, ge=60)
    # ADR-0004 PR 15 review: per-USER cap on current-password-supplied
    # changes (the stepped-up recovery set is exempt). Bounds an intruder
    # who learned the password camping the account by revoking the owner's
    # sessions in a loop. Notices per ADDRESS share the magic-link limit.
    rate_limit_password_change_per_hour: int = Field(default=3, ge=1)

    # --- Auth session cookie (ADR-0004 PR 3) ---
    # 180 days: long enough that a returning anonymous visitor keeps their
    # pseudonymous identity (and, once ADR-0004 PR 6 ships, stays logged in)
    # across normal browsing gaps, short enough that a stolen/abandoned
    # cookie does not grant access forever.
    auth_session_ttl_seconds: int = Field(default=60 * 60 * 24 * 180, ge=1)

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
    # v3 -> v4 (#237): the citation scanner changed. Answers cached under v3 had their
    # markers extracted by a regex that counted ``[n]`` inside code spans and fences, so
    # a v3 row can carry a phantom card the current build would never attach. Nothing
    # else in the key invalidates them -- the question, source hash and embedder identity
    # are unchanged -- so without this bump the pre-fix rows replay the phantom for the
    # 24h TTL, which is the same failure mode the v2 -> v3 bump fixed for #236.
    # v4 -> v5 (#237): the scanner changed AGAIN. v4 rows were produced by a build
    # whose fence scanner discarded every citation after a sloppy closing fence, so a
    # v4 row can be MISSING a card the current build attaches -- and some v4 rows are
    # no-answer refusals for questions this build answers. That is the opposite
    # direction from the v3 -> v4 bump, and equally invisible to the rest of the key.
    # v5 -> v6 (#226): the Tier-3 vector-arm gate changed, so the same question can
    # now produce a DIFFERENT answer. The reachable-through-the-cache case is the
    # DUAL-ACTIVE one: it used to resolve to "unknown provenance -> allow" and the
    # arm ran, and it now fails closed. A v5 row written from that state was built
    # by a vector arm scoring a possibly FOREIGN vector space, and nothing else in
    # the key invalidates it -- on a dual-active DB ``_retrieve_active_index``
    # returns ``("", "")``, so even ``source_version_hash`` is a constant empty
    # string across the fix. Without this bump those rows replay the pre-fix answer
    # for the full 24h TTL, and the corrected gate never runs because a cache hit
    # returns before retrieval.
    # (#226's other half -- a retrieval scoped to a non-active index -- never
    # reached the cache: only ``promotion_eval`` passes a candidate version, and
    # that path never touches ``answer_cache``. It is fixed for correctness, not
    # for cache hygiene.)
    answer_policy_version: str = "v6"
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

    # --- Index promotion gate (Slice 8) — ENFORCED (#210) ---
    # ``docs/RELEASE_PLAN.md §7`` gate 1. Read by ``promote_version``
    # (app/services/index_versions.py): it resolves the candidate's newest
    # COMPLETED ``EvaluationRun`` (status in {passed, failed} — a ``running``
    # run is not evidence), ordered ``started_at`` DESC, reads
    # ``metrics["pass_rate"]`` (falling back to ``cases_passed/cases_total``),
    # and refuses with ``IndexPromotionBlocked`` → HTTP 409
    # ``promotion_blocked`` unless ``rate >= this value``. Equality promotes.
    # No usable run means refuse: "unevaluated" is not "passing".
    #
    # This comment used to say NOTHING READS THIS SETTING, and said it in
    # capitals, because an earlier version described the gate in the present
    # tense and a deploy runbook was then written promising operators a safety
    # check that did not exist. Its counterpart warning — that nothing WROTE
    # ``EvaluationRun`` rows, so every honest promote was refused — is also
    # resolved: ``citevyn-worker evaluate --index-version <candidate>``
    # (:mod:`app.worker.promotion_eval`) measures the candidate against the
    # shipped corpus and persists the run this threshold is compared against
    # (#216). So this setting is now read by the gate AND fed by a producer;
    # changing it changes which indexes may go live.
    #
    # ``force=true`` remains for the bootstrap and emergency-rollback cases that
    # genuinely have no evidence, recorded in the ``promote_index`` audit row
    # along with the measured rate and this threshold. Gates 2-5 of §7 remain
    # operator-verified, not machine-enforced.
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
    def _reject_half_configured_oauth_providers(self) -> "Settings":
        # A half-configured provider (client_id set, secret missing, or vice
        # versa) is a config bug in ANY environment, not just prod --
        # unconditional, unlike the production-only guards above. A caller
        # would otherwise 404 or fail mysteriously depending on which half
        # `bool(client_id and client_secret)` happens to have.
        for label, client_id, client_secret in (
            ("GITHUB", self.github_oauth_client_id, self.github_oauth_client_secret),
            ("GOOGLE", self.google_oauth_client_id, self.google_oauth_client_secret),
        ):
            if bool(client_id) != bool(client_secret):
                raise ValueError(
                    f"CITEVYN_{label}_OAUTH_CLIENT_ID and CITEVYN_{label}_OAUTH_CLIENT_SECRET "
                    "must be set together, or both left unset."
                )
        return self

    @model_validator(mode="after")
    def _require_oauth_redirect_base_url_in_production(self) -> "Settings":
        # A missing/wrong base URL is otherwise a silent-until-someone-
        # clicks-the-button failure: `start` would send a redirect_uri the
        # provider was never registered with, and the provider rejects the
        # whole flow with no code path in this app to explain why.
        oauth_configured = bool(self.github_oauth_client_id or self.google_oauth_client_id)
        if (
            self.environment == "production"
            and oauth_configured
            and not self.oauth_redirect_base_url
        ):
            raise ValueError(
                "CITEVYN_OAUTH_REDIRECT_BASE_URL must be set when an OAuth provider "
                "is configured and CITEVYN_ENVIRONMENT='production'."
            )
        return self

    @model_validator(mode="after")
    def _require_email_from_with_resend_key(self) -> "Settings":
        # A key without a sender is a config bug in ANY environment (the
        # provider rejects every send with no From:), so this is unconditional
        # like the half-configured-OAuth guard above.
        if bool(self.resend_api_key) and not self.email_from:
            raise ValueError("CITEVYN_EMAIL_FROM must be set when CITEVYN_RESEND_API_KEY is set.")
        return self

    @model_validator(mode="after")
    def _require_magic_link_base_url_in_production(self) -> "Settings":
        # Mirrors ``_require_oauth_redirect_base_url_in_production``: without
        # it every emailed link would point at localhost.
        if (
            self.environment == "production"
            and bool(self.resend_api_key)
            and not self.magic_link_base_url
        ):
            raise ValueError(
                "CITEVYN_MAGIC_LINK_BASE_URL must be set when CITEVYN_RESEND_API_KEY "
                "is set and CITEVYN_ENVIRONMENT='production'."
            )
        return self

    @model_validator(mode="after")
    def _reject_email_outbox_in_production(self) -> "Settings":
        # The file outbox is a local-development delivery path. In production
        # it would write every sign-in link to the machine's disk and deliver
        # nothing -- a silent outage of the feature, not a degraded mode.
        if self.environment == "production" and self.email_outbox_dir:
            raise ValueError(
                "CITEVYN_EMAIL_OUTBOX_DIR is a development-only delivery path and is not "
                "allowed when CITEVYN_ENVIRONMENT='production'. Set CITEVYN_RESEND_API_KEY "
                "instead, or leave both unset to disable magic-link login."
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
