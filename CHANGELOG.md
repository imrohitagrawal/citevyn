# Changelog

All notable changes to CiteVyn are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **All 15 `docs/BACKLOG.md` rows that were silently losing content now render in
  full (#360).** The issue counted them correctly but named one cause; there were
  **two**, and the fix it implied would have made the larger group worse.
  Three rows in the 5-column table had literal `|` characters inside their Origin
  cell (a `hey|hello|ok|...` token list, an array of style names, a test-count
  string) — escaped to `\|`. The other **twelve** sit in the 2-column
  "Issue | Why it matters" archive table while carrying the 5-column schema they
  were pasted from when they closed, so GFM was discarding **Area, Priority and
  Origin from every one of them** — e.g. `backend / answer`, `— (fixed)`,
  `live UI testing 2026-07-20` on the #208 row alone. Escaping pipes there would
  have rendered visible backslashes instead of restoring the columns; the three
  trailing fields are folded into the "Why it matters" cell instead. Verified
  nothing was dropped: 107 issue links before and after, and every previously
  discarded fragment is present in the file.
- **`docs/COST_CONTROLS.md` no longer contradicts itself about `make budget`.**
  The Layer 5 table row said **Live**; §0, four lines below, called it "planned
  (§5, not yet implemented)". The table was right — `Makefile:326` defines the
  target and wires `scripts/check_budget.sh` into deploy-verify. The stale
  "$1.10, ~96% consumed" cap figure is now dated and marked as a historical
  reading rather than the live value.

### Changed
- **`test_backlog_table_renders.py`'s exemption list is empty, and its staleness
  partner no longer goes vacuous when it is.** The old partner was parametrized
  over `KNOWN_BROKEN`; emptying the set would have collapsed it to pytest's
  "got empty parameter set" — a test that asserts nothing, the failure mode
  tracked as #381. It is now an iterating test (executed even when the set is
  empty) plus `test_the_detector_actually_detects`, which feeds the parser
  deliberately broken tables in **both** repaired shapes and a correctly-escaped
  row it must NOT flag. Mutation-proven: blinding `_cell_count` to `return 0`
  leaves the other three tests green and reds only the new partner.

### Added
- **Every `Settings` field is now declared in an env example, or exempted by
  name with a reason — enforced by a test (#332).** The issue's headline said 29
  of 84 fields were undocumented; the real number is **23**. The 29 came from a
  substring search, which counts `CITEVYN_FOO` as documented when only
  `CITEVYN_FOO_BAR=` appears. Re-measured with exact `NAME=` declaration parsing
  and with each variable's environment name **asked of pydantic-settings rather
  than hand-built** — that second part is not pedantry: `env_prefix` is
  `CITEVYN_` and one field is named `citevyn_alias_intent_check`, so its variable
  is `CITEVYN_CITEVYN_ALIAS_INTENT_CHECK`, and because `model_config` uses
  `extra="ignore"` an operator who writes the intuitive `CITEVYN_ALIAS_INTENT_CHECK`
  gets no error, changes nothing, and believes the feature is off.

  17 fields documented, 6 exempted. The headline is **`CITEVYN_ENVIRONMENT`**,
  which was written down nowhere despite being the single flag that turns on HSTS,
  the `Secure` and `__Host-` session cookie, withdrawal of `/docs` and the OpenAPI
  schema, refusal of a `stub` LLM or embedding provider, and refusal of a default
  or under-16-character demo/admin key (those two keys only; the OAuth,
  Resend and LLM keys are never strength-checked). Consulted in 19 places:
  10 read sites outside `config.py` plus 9 startup validators inside it —
  counted precisely, because an earlier draft of this entry said "11" by
  counting a line that turned out to be a docstring. Anyone
  starting the service by a path other than `fly.toml` or `docker-compose.yml`
  (a bare `uvicorn`, another orchestrator) got development-mode behaviour with
  no error and no warning. It is documented in `.env.example` and the README
  env table, and deliberately **not**
  as a settable line in `infra/docker/prod.env.example`: `docker-compose.yml` pins
  it in its `environment:` block, which overrides `env_file:` (verified with
  `docker compose config`), so a line there would have been inert — which is the
  precise failure this change exists to stop. The prod template gets a prose note
  saying so instead. Also newly written down: the registered-user and
  auth-login rate limits, all three cost-control switches beside the dollar
  ceilings they gate, the index-promotion pass-rate gate, session TTL, and the
  retrieval/memory/embedding tuning knobs.

  **The guard is the deliverable; the documentation is the smaller half.**
  `backend/tests/test_settings_env_vars_are_documented.py` (31 tests) asserts
  coverage, exact-match declaration (a mention in prose does not count — that hole
  made the first #289 guard pass while the variable it guarded was gone), a
  **reverse** rule that no documented name is fictional, non-vacuity partners on
  every collector, and that no exemption is stale or contradicted by the docs. It
  is a separate file rather than a widening of the OAuth guard, because that one
  demands a declaration in all four operator-facing files — right for six
  credentials, wrong for a retrieval-tuning float nobody puts in a Fly secrets
  block.

  An adversarial review round found the exemption list was weaker than its own
  docstring claimed: `test_every_exemption_carries_a_reason` measured string
  LENGTH, so forty characters of plausible prose could have silenced the guard
  for a live setting. `test_every_exemption_is_actually_dead_config` now checks
  the CLAIM instead — an exempted field must have no reader under `backend/app`
  — with a partner proving the read-site scanner is not blind. Exempting
  `environment` now fails naming all eleven of its read sites.

  Mutation-tested with 10 mutants; 9 killed. One **survived**, and was worth
  more than the six that died: hand-building `"CITEVYN_" + field.upper()` turns
  nothing red while `env_prefix` is `CITEVYN_`, because for that one field the two
  methods coincide. A documented red-bite line claiming otherwise was therefore
  false and was corrected. The mutant that does bite is a changed `env_prefix`,
  under which the coverage rule goes **green** — blind, still blessing
  documentation for variables the app no longer reads — and only the behavioural
  `Settings()` round-trip goes red.

- **Password hashing module, no routes yet (ADR-0004 PR 4).**
  `app.core.passwords` wraps `argon2-cffi` (Argon2id, OWASP baseline
  `m=19456 KiB, t=2, p=1` — `p=1` because the production machine is Fly's
  single-vCPU `shared-cpu-1x`). Tuning the parameters alone does not bound
  worst-case memory: at Fly's connection `hard_limit=40`, even 19 MiB
  concurrent hashes would exceed the 512 MB machine, so a module-level
  `asyncio.Semaphore(2)` (mirroring `app.cost.admission`'s idiom) is the
  actual control, acquired before dispatching the CPU-bound hash to a
  thread. `verify_password_or_dummy` verifies against a hardcoded dummy
  Argon2id hash (same parameters, so the same real CPU cost) when the
  caller has no real hash for an account — otherwise "unknown email" would
  return near-instantly while "wrong password" pays the full hash cost, a
  free account-enumeration oracle over response latency. Security-reviewed
  (subagent): parameters confirmed against OWASP's current baseline, no
  distinguishing signal found between the dummy and real verify paths, no
  findings. New dependency `argon2-cffi` (not `passlib`, unmaintained
  since 2020) — explained in `docs/ADR/0004-user-accounts.md`.

- **Persistent per-visitor cookie identity (ADR-0004 PR 3).** Every request
  to a session/message route now resolves to a real per-visitor principal
  instead of the shared constant `demo_user`. A first-time visitor gets an
  anonymous principal (`anon_<uuid4hex>`) minted transparently — no login
  screen — and a cookie (`<auth_session_id>.<secret>`, new `auth_sessions`
  table via migration `0007`, storing only `sha256(secret)`) that resolves
  back to the same principal on later requests. This is what makes the
  ADR-0004 PR 1 ownership predicate actually differentiate visitors instead
  of being a no-op: two anonymous visitors cannot read or close each
  other's sessions. Deliberately separate from the demo bearer's AUDIT
  identity, which stays the constant `DEMO_USER_ID` — the bearer proves
  "legitimate demo client", the cookie proves "same visitor as last time";
  conflating them would mean a bearer-key rotation silently reassigns every
  session's owner. `__Host-` cookie prefix + `Secure` in production only
  (both cookies over plain HTTP are dropped by the browser); `HttpOnly`,
  `SameSite=Lax`, no `Domain=` always. `CORS allow_credentials=False` is
  now pinned by a regression test — the plan flagged this as the most
  likely accidental flip once cookies exist to fix a local CORS error.

### Security
- **Response security headers + docs endpoints disabled in production
  (ADR-0004 PR 2).** Every response — including `GET /` served from the
  frontend `StaticFiles` mount, a different response path from the
  router-generated JSON responses — now carries `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
  strict-origin-when-cross-origin`, and a `Content-Security-Policy` scoped
  to `'self'` plus the two third-party origins the shipped frontend
  actually loads (Google Fonts, Fontshare — verified against a real `npm
  run build` output, not assumed). `Strict-Transport-Security` is added
  only when `CITEVYN_ENVIRONMENT=production` (HSTS over local plain HTTP
  would be a lie the browser acts on). Shipped ahead of any login code so
  there is no window where a session cookie (ADR-0004 PR 3+) is live
  without the headers that protect it from XSS exfiltration and
  clickjacking. Separately, `/docs`, `/redoc`, and `/openapi.json` are now
  `None` in production — free reconnaissance of every route and field name
  otherwise. **Deliberately deferred from this PR:** moving `/health/index`
  and `/health/dependencies` behind the admin key, per the original plan —
  both are currently polled *without* the admin key by
  `.github/workflows/uptime.yml` and `infra/docker/scripts/deploy_verify.sh`
  (the release gate), so gating them needs coordinated secret plumbing
  across a GitHub Actions secret and the ops script, not just a route
  change. Tracked under #270 rather than done as a rushed cross-cutting
  edit in this PR.

  **Found by a security review of this PR and fixed before merge:** the
  middleware alone missed one response class. Starlette wires a bare
  `Exception` handler into `ServerErrorMiddleware` — the framework's
  outermost ASGI layer, added around every `add_middleware` registration —
  so a genuinely unexpected exception (not `StarletteHTTPException` /
  `OrchestratorError` / `RequestValidationError`, the three types actually
  registered with `ExceptionMiddleware`) produced a 500 with **none** of
  these headers; reproduced with a route raising a bare `ValueError`. No
  ASGI middleware added via `add_middleware` can wrap outside
  `ServerErrorMiddleware`, so the fix stamps the headers directly onto the
  response inside `_unhandled_exception_handler`
  (`app.core.security_headers.apply_security_headers`, shared with the
  middleware) rather than restructuring the middleware itself. Regression
  test reproduces the exact gap.

- **The 4 routes nested under `/v1/sessions/{session_id}` discarded the
  caller's identity instead of checking it (ADR-0004 PR 1).**
  `sessions.py:231,260` and `messages.py:150,197` each took the
  authenticated `user_id` and threw it away (`del user_id` / an unused
  `_user_id` parameter), then loaded the session by primary key alone.
  Harmless while every caller resolves to the single constant `demo_user`
  principal; it would have been a live cross-account IDOR the instant a
  second principal existed, and because the affected rows persist, every
  session created before an ownership fix landed would have stayed
  readable by anyone who could guess its UUID for the rest of its life.
  `_get_session_or_404` and `_require_session` now push
  `.where(Session.user_id == user_id, Session.expires_at > now())` into
  the query; a mismatch returns the same 404 as a genuine miss, never
  403 — 403 would confirm the id is real, a membership oracle over the
  UUID space. The expiry half of the predicate is a real bug fix on its
  own: `DELETE /v1/sessions/{id}` only ever set `expires_at` to now (the
  schema has no separate `closed` column) and neither loader filtered on
  it, so a "closed" conversation stayed fully readable and writable for
  the rest of its original TTL. Also fixed in the same PR: the demo
  bearer comparison used `!=` instead of `secrets.compare_digest`
  (`security.py`), a timing oracle the admin key comparison already
  avoided; `cookie` and `email` added to the log-redaction key list ahead
  of ADR-0004's later PRs, which introduce both. A new route-inventory
  test asserts every `{session_id}` route depends on the dependency that
  resolves the ownership principal, so the next such route cannot repeat
  this silently. Behaviourally a no-op today — this is PR 1 of the
  ADR-0004 login sequence, and must land before any later PR in that
  sequence can create a second real principal.

### Changed
- **Below-the-fold marketing strip is fetched on scroll (#358).**
  `landing-sections.tsx` split in two: `QuestionTicker`, `SourcesStrip` and
  `InteractiveDemo` stay eager; `Personas` → `Footer` move to
  `landing-strip.tsx`, one chunk mounted behind an `IntersectionObserver`.
  Eager graph **66,394 → 62,873 B gzip (−3,521 B)**, measured with
  `npm run check:bundle` on the shipping variant, and
  `frontend/bundle-budget.json` ratcheted 68,000 → 64,479 in the same change so
  the win is banked rather than spent — headroom held at exactly the 1,606 B the
  previous decision chose. The issue's headline −4,669 B was for the whole strip
  including the two at-the-fold sections and does not apply to what shipped.

  **Where the win actually lands, stated plainly.** On a 1280×720 desktop the
  sentinel sits inside the 400 px lead margin, so the observer fires on first
  paint and the chunk is fetched anyway: that reader downloads
  62,873 + 4,721 = **67,594 B, i.e. 1,200 B MORE** than before. The gain there is
  ordering — a smaller entry chunk that paints sooner, with the strip on a
  second non-blocking request. The genuine byte saving is on phone-sized
  viewports, where the sentinel starts outside the margin and a reader who does
  not scroll never fetches those 4,721 B. Bytes matter most on metered, slow
  connections, so that is the deliberate direction of the trade.

  Six regressions the split would otherwise have shipped are fixed with it, each
  with a test: `scrollToSection` waits for a lazy target instead of returning
  silently (four of the five header nav links point into the deferred chunk and
  would have looked dead) and now waits on a MutationObserver rather than a
  ~2 s frame budget, which a 3 s chunk delay was reproduced defeating; a
  `/#pricing` deep link reveals the strip; the reveal gate fails OPEN where
  `IntersectionObserver` is absent *or its constructor throws*, so the page is
  never hidden by an unusable API; the observer follows the sentinel across the
  landing→chat→landing remount, which a `useEffect` over a stable ref did not —
  reproduced leaving the whole strip unreachable on a phone for the rest of the
  session; a new `LazyChunkBoundary` keeps a failed chunk fetch from unmounting
  the React root, reproduced as a white screen on the ordinary scroll path; and
  the nav list and the deferred-id set are now one source
  (`src/data/navSections.ts`), after review proved a new nav link could silently
  become a dead link with `tsc` green.

  Measured accessibility cost: nothing is unreachable (a real keyboard walk
  reaches every deferred control) and CLS is 0.0001 with the chunk instant and
  0.0001 with it delayed 3 s, but at phone widths heading navigation can skip
  the 12 headings inserted above the eager demo, and find-in-page cannot match
  deferred copy until the reader scrolls. Both are recorded in
  `docs/UI_DESIGN.md` §5.1. All 22 visual baselines pass unchanged.

- **Frontend base image Node 22 → 26 (#227).** Moved deliberately, not as a
  routine bump: all three pins (`frontend/.nvmrc`, `package.json`
  `engines.node`, `Dockerfile.api`'s `FROM`) together, per the #231 guard's
  requirement, and boot-verified (`make image-smoke`: frontend build succeeds
  under Node 26, api `/health` → 200, worker `list-sources` exit 0) rather
  than assumed — the #34 Python base-image bump shipped non-booting once
  already for skipping exactly this step. Corroborates the #231 measurement
  that Node 20/22/26 produce a byte-identical bundle.

### Fixed
- **The README told operators to set a variable that does nothing (#332).** The
  configuration table and the production walkthrough both named a
  `CITEVYN_LLM_API_KEY`, which is not a `Settings` field and is read by nothing
  anywhere in the repo; the field that actually gates the `anthropic` provider is
  `CITEVYN_ANTHROPIC_API_KEY`. A wrong name is worse than a missing one, because
  `extra="ignore"` means the operator is never told: they set it, and believe live
  answer generation is configured. The new reverse guard fails on any recurrence,
  and caught this instance on its first run — including on the replacement
  wording, which had backticked the dead name while explaining it.

- **Production logs print their `extra=` fields again, without leaking the
  upstream bodies those fields carry (#361).** `configure_logging` formatted
  with `LOG_FORMAT = "%(message)s"`, so every field passed as `extra=` was
  discarded before it reached stdout — **31 logging call sites across 18 files**
  (counted with an AST walk, not a grep; the raw `grep -rn "extra="` count of 41
  includes comments, `SettingsConfigDict(extra="ignore")`, and two DB-audit
  kwargs that share the keyword). `_logger.warning("resend_send_error",
  extra={"status_code": 422})` printed the single word `resend_send_error`, and
  an operator reading `fly logs` could not tell an unverified sending domain
  from a revoked key from a rate limit.

  Simply emitting the fields would have converted a dropped log into a **PII
  leak**, so it was not a one-line change. Two shapes were *measured* to survive
  `redact_value`: an email address nested inside an upstream `body` field (the
  redactor matches on the KEY NAME, and `body` is in neither `SECRET_KEY_PARTS`
  nor `RAW_TEXT_KEYS`), and a Resend-shaped `re_` + 24-character key, which at 27
  characters is under `HIGH_ENTROPY_RE`'s 32-character floor. Five call sites
  log an upstream response body, including the magic-link path (the user's own
  address) and the OAuth token exchange (which can echo the `code`).

  The safety is therefore **structural, not pattern-based** — a denylist of
  secret shapes cannot be made complete, because every added regex is a guess
  about the next provider's key format. `RedactingExtraFormatter` fails closed:
  a bool/int/float/None is bounded by construction and always prints; a string
  prints verbatim only if its key is on an explicit, reviewed allowlist
  (`EMITTED_TEXT_KEYS`) *and* still survives `redact_value`; everything else
  becomes a shape summary — `body=<str len=57>`. The summary is the point: an
  unlisted field is **visibly suppressed rather than silently dropped**, so an
  operator can see the field exists and ask for it, and adding a key becomes a
  conscious review decision instead of an accident. Free text from an upstream
  provider cannot reach the log line at all, whatever shape the secret inside it
  takes. `RESERVED_RECORD_ATTRS` is derived from a real `LogRecord` on the
  running interpreter rather than hard-coded — a stale list would have appended
  `taskName=None` (3.12+) to every production line.

  Three call sites that passed a free-text `str(exc)` now pass a bounded
  `cause_type`/`error_type` beside it (`main.py`, `llm/fallback.py`,
  `worker/cli.py`), which is what an SRE actually correlates on. `main.py`'s
  `_orchestrator_error_handler` docstring promised "the full cause chain is
  logged SERVER-SIDE"; it was logged nowhere, and the docstring now says what is
  really emitted and why the text is withheld.

  Every test renders a real `LogRecord` through the formatter object
  `configure_logging` **installs** and asserts on the output string — never on
  `LOG_FORMAT`'s contents, a dict key, or "was `redact_value` called". Both
  measured leak shapes have a test, each with a partner proving the redaction is
  not vacuous (a `status_code` and a `provider` that must survive intact, so a
  "redact everything" implementation fails). Twelve mutants killed, including
  installing a plain `logging.Formatter` with an identical `_fmt` — which the
  old partner test would have accepted while every `extra=` field vanished.

  Two blockers found by review, after the first version was green:

  * **`request_id` printed as `[REDACTED]` on every line.** A real id is
    `f"req_{uuid.uuid4().hex}"` — 36 characters, all inside `HIGH_ENTROPY_RE`'s
    class — so the entropy sweep ate it whole, while the API handed the caller
    that same id to grep the logs with. The test missed it because it used
    `req_abc123`, a value the application never produces. `OPAQUE_ID_KEYS`
    (`request_id`, `index_version`, `source_version_hash`, matched on the whole
    key, never as a substring) now skips the entropy sweep and keeps the
    `Bearer` sweep. The same fix repairs the per-request `request_completed`
    line, which had been broken on `main` before any of this.
  * **A Redis password reached the log.** `_redact_url` peeled only the
    `user:pass@host` form; redis-py also honours
    `redis://host:6379/0?password=…`, which has no `@`, so that DSN printed in
    full at INFO on every boot once extras rendered.
    `QUERY_CREDENTIAL_RE` gained `password`/`auth`/`secret`/`api_key` and
    `_redact_url` applies it first.

  Three latent bypasses closed with them — none reachable at any current call
  site, each falsifying the module's own stated invariant: a container under an
  allowlisted key printed its contents (the allowlist branch is `str`-only now),
  an `int` subclass with a hostile `__str__` printed verbatim under any key
  (scalars are exact types now), and an `extra=` KEY containing a newline could
  forge a log line (`repr` guarded the value, not the key).

- **`deploy_verify.sh` records a verdict for every probe; there is no silent
  third state (#362).** The frontend-bundle check sat inside
  `if [[ -d "${REPO_ROOT}/frontend/dist" ]]` with no `else`, and `record` is the
  only thing that moves `PASS_COUNT`/`FAIL_COUNT` — so an absent `dist` left no
  row, moved no counter and vanished from the summary. The operator read a green
  gate that had quietly run one fewer probe than they thought.

  `record` gains a third verdict, `SKIP`. It is **not** a pass: it moves neither
  counter and never changes the exit code, but it appears in the summary with
  the reason it was skipped, and a `SKIP` with no reason is labelled a bug in
  the output rather than printed blank. The three branches are now explicit —
  the new `--frontend-built-in-image` flag records SKIP (the Fly image builds the
  bundle in `Dockerfile.api` stage 0, so the host tree is a different artifact
  and grading it would be meaningless in both directions); a present `dist` runs
  the real check unchanged; an absent `dist` records FAIL with the
  `make demo-frontend` instruction, which is the correct answer on the compose
  path this gate actually serves. The flag is required rather than inferred:
  nothing in the script or `.env` distinguishes the two deploy paths.

  Two sibling silent skips named in the same issue are closed the same way: the
  provider-budget "could not be checked" branch, and the whole of §1b under
  `--verify-only`. Both summaries now print `skipped:` alongside the counts.

  The zero-probe guard ("no probes executed — the harness is broken") moved from
  `${#RESULTS[@]} -eq 0` to `$((PASS_COUNT + FAIL_COUNT)) -eq 0`, because SKIP
  rows populate `RESULTS` and a row count would have made that guard unfireable
  — restoring the exact defect its sibling fix was closing. The **main** summary
  never had that guard at all and now does. It is exercised by extracting it
  from the shipped script and running it against fabricated tallies, so what is
  asserted is the exit code an operator gets.

  Review then found that three of these — the `--verify-only` SKIPs, the
  `skipped:` tally, and the `SKIP_COUNT=0` initialisation (fatal on bash 3.2
  under `set -u`) — survived every extraction-based assertion, i.e. half of what
  the fix claims had no test. The suite now drives the shipped script end to end
  under `--verify-only` against a dead port — one refused connection to
  `127.0.0.1:1`, and a stubbed `docker` binary on PATH, because the gate's
  preflight requires that binary to exist and the macOS CI leg ships none; an
  earlier version of this case omitted the stub and would have reddened that leg
  on arrival, with two of its assertions passing for the wrong reason. It
  asserts both SKIP rows, their reasons and the printed count. Verified on bash 3.2.57, the macOS CI leg; shellcheck
  reports no new findings; the new zero-probe guard was proved unable to fire on
  a legitimate run (minimum 7 executed probes at the main summary, 5 under
  `--verify-only`).

- **SQLite foreign-key enforcement is on, and it caught a production bug in the
  chat hot path (#286).** SQLite ships with `PRAGMA foreign_keys` OFF, per
  connection, and nothing in this codebase ever turned it on — so for the whole
  life of the suite, ~1900 hermetic tests silently accepted child rows pointing
  at parents that did not exist, while real Postgres always rejected them. A
  `PRAGMA foreign_keys=ON` hook now fires on the SQLAlchemy `Engine` *class*
  (`app/core/db.py`), which is what reaches the ~30 test modules that build
  their own `create_async_engine` as well as the worker, seed and alembic entry
  points; an engine-instance listener inside `build_engine` would cover none of
  them. The SQLite check is a deliberately broad substring on the DBAPI
  connection's module, because under aiosqlite that class lives in
  `sqlalchemy.dialects.sqlite.aiosqlite`, **not** `sqlite3` — a
  `startswith("sqlite3")` test silently misses every async engine, which is
  every engine this app uses.

  Turning it on surfaced 147 failures and 4 errors. One was a real production
  bug: `Orchestrator._ensure_user` inserted the `sessions` row — which carries
  a foreign key onto `users` — and flushed it **before** creating the `users`
  row it points at. That is the same shape as the `_mint_principal` bug this
  issue was filed from, in the path every chat message takes. It has never
  fired in production only because `demo_user` happens to be seeded
  (`db/seed/seed_users.py`); a fresh, unseeded Postgres 500s on the first
  message. The remaining 146 were test-fixture debt of two shapes — a
  fabricated id whose parent row was never inserted, and (more interesting) a
  test that inserted the *correct* parent but in the same flush as its child.
  SQLAlchemy's unit of work orders inserts by declared `relationship()`s, not
  by raw foreign-key columns, and this schema declares none between `User` and
  its dependents — so those tests were reproducing the very defect they should
  have caught. No assertion was weakened to make anything pass; one test in
  `test_magic_link_routes.py` that asserted a `magic_link_tokens` row outlived
  a deleted user was rewritten, because migration 0012 declares that key
  `ON DELETE CASCADE` and it had only ever passed by exploiting this gap.
  Backend: 1974 passed / 23 skipped, foreign keys enforced.

- **A refused composer submit produced no observable change at all (#356 gap
  2).** With an answer in flight the composer correctly refuses a second
  question and correctly keeps the typed text — but measured with a
  MutationObserver over the whole body, the refusal produced **zero DOM
  mutations**, on the Enter path and the click path alike. A screen-reader user
  pressed Enter and nothing whatsoever happened. The persistent `role="status"`
  region beside the composer now says "Not sent. CiteVyn is still answering
  your previous question. Your text is kept." A full stop rather than an em
  dash: NVDA's `symbols.dic` files `—` at level `most`, above the default, so a
  reader running punctuation at "most" or "all" hears the words "em dash" — and
  every other sr-only string on this screen already ends its clauses with a full
  stop. The signal is `refusedInFlight`, set at the
  `inFlight` **ref** inside `submitChat`, never derived from the rendered
  `pending`: `pending` is a render behind the ref by construction (#62), so a
  `pending`-derived announcement would announce refusals that never happened
  and miss ones that did — pinned by the two-submits-in-one-React-batch test.
  The flag lives in the reducer beside `pending` and is cleared by the same
  `SET_PENDING(0)` action, in the same state object, so the two cannot
  disagree; the view keeps no copy of it and has no effect for it. Two
  view-side shapes were tried first and an adversarial round broke both: a
  latch cleared by an effect keyed on `pending` transitioning left a stale
  refusal masking the arrival announcement for the rest of the mount, and
  gating the render on `pending` only hid that latch, so the next genuine
  request announced "Not sent" about a question that was sent. One region
  rather than two: the refusal copy is a superset of the in-flight sentence, so
  nothing is lost while it holds the region, and the window closes on its own
  when the answer lands, at which point the arrival announcement takes over. The send button's handler is now
  wired unconditionally, because `onClick={pending ? undefined : onSendClick}`
  meant a click while gated never reached the hook and no signal could exist;
  `aria-disabled` and the 0.7 dim still carry the state, and the hook stays the
  single gate. Stated limitation, pinned as a test: a *second* refusal inside
  the *same* in-flight window is silent, since `role="status"` fires on a text
  change and nothing changed; a refusal in a *later* window does announce. An
  empty submit is never announced — "your text is kept" about an empty box is a
  false statement. +101 B gzip, measured (62,873 -> 62,974 against a 64,479 budget, headroom 1,505). This closes #356; gaps 1 and 3 shipped
  in #359.

- **`npm run type-check` type-checked no Playwright spec at all (#366).**
  `frontend/tsconfig.json` said `"include": ["src", "e2e"]` and there has never
  been a `frontend/e2e/`. tsc ignores an `include` entry that matches nothing
  **silently** — no warning, no error, exit 0 — so the required `type-check +
  unit tests + build` job was green while loading zero of the 9 spec files,
  `helpers.ts` and `fixtures.ts`. Playwright transpiles with esbuild, which
  erases types without checking them, so nothing else was looking either.
  Measured with `npx tsc -p tsconfig.json --noEmit --listFiles` filtered to
  `/frontend/tests/`: **0 files before, 11 after**, with `src/` unchanged at 67.
  Proved by the consumer rather than by the config — a deliberate type error in
  `tests/visual.spec.ts` turns `npm run type-check` red and its removal turns it
  green. Turning it on surfaced **seven** pre-existing errors (the issue lists
  six; `tests/fonts-offline.spec.ts` arrived with #365 afterwards), all cleared:
  five dead locals, a `Set` inferred with a literal-union type where the value
  tested is a computed style read out of the browser, and
  `measureFocusIndicator`'s `adjacent === null` early return omitting
  `strongPixels` — a type gap rather than a live defect, because
  `focus-ring.spec.ts` checks `ratio === null` three lines before the
  `undefined < 8` that would otherwise have failed the focus sweep open.
  Deliberately **one** `noEmit` project rather than a second referenced one: a
  referenced project needs `composite: true`, which forbids `noEmit` (TS6310)
  and forces emit — measured, it writes 22 `.js`/`.d.ts` files into
  `frontend/tests/`, and Playwright's default `testMatch` matches `.js`, so
  every spec would run twice with half of it from a stale snapshot. That is
  #343 with a bigger blast radius. Guarded in `src/test/buildGuards.test.ts` by
  asking `ts.parseJsonConfigFileContent` which files the config resolves to and
  comparing against the list `playwright --list` selects; the mutants that prove
  it bites are in `frontend/scripts/mutate-typecheck-guard.sh`.

- **Retrieval's Tier-3 provenance check could read a stamp for the wrong index,
  and failed open on a dual-active database (#226).** `_active_index_stamp`
  resolved the active row by `status == active` rather than by
  `active_index_version`, so a caller retrieving against a non-active index (the
  #216 promotion evaluator) got the vector arm enabled against a mismatched
  index — meaningless cosine similarity scored as real evidence. Fixed to read
  the row named by `active_index_version` by primary key, whatever its status,
  so doc scoping and the provenance gate can no longer disagree about which
  index is being read. Second half: on a **dual-active** database (two rows
  both `status == active`) the old fallback returned `None`, which the shared
  predicate reads as "unknown provenance ⇒ allow" — the arm ran without
  knowing whose vectors it was scoring. A distinct `IndexStampStatus.ambiguous`
  now fails **closed** for that case only, leaving the legitimate "no stamp
  recorded" meaning (stub-seeded/local, load-bearing for hermetic tests)
  unchanged. Two siblings found during review and left deliberately open:
  **#265** (zero active rows: the arms still filter on document status alone,
  with no row for the gate to compare against) and **#264** (`/health/index`
  reports the vector arm healthy on a dual-active DB at the exact moment the
  read path fails closed).

  > **Operator note.** `answer_policy_version` bumped **v5 → v6** — answers
  > cached while either defect was live replay identically after the fix
  > (nothing else in the key changes), so the cache is cold on deploy and
  > refills on first ask. No migration.

- **A malformed closing code fence silently deleted every citation after it
  (#237 follow-up).** The #237 code-span scanner treated any closing fence
  as valid, so a non-bare closer (e.g. a fence immediately followed by text)
  left the fence "open" in the scanner's view — every citation marker past
  that point was invisible to validation and dropped from the answer with no
  refusal and no audit reason. Now closes a sloppy fence only as a last
  resort, when no bare closer is reachable later in the text, without
  breaking fence nesting or misreading a fenced info-string opener as a
  closer. Measured against a labelled fuzz of 39,002 well-formed CommonMark
  answers (ground truth assigned by construction, not inferred after the
  fact): 0 citation losses, 0 phantoms, 0 new refusals.

  > **Operator note.** `answer_policy_version` bumped **v4 → v5**.

- **`_CITATION_RE` counted `[n]` inside code spans and fenced code blocks as
  real citations (#237).** A bracketed array index or footnote marker inside
  a code span (` `arr[9]` `) or fenced block could pass or fail citation
  validation for reasons unrelated to the prose — including a live false
  refusal, where a code span with three evidence bullets tripped the
  out-of-range check and discarded a correct answer. `validate_citations` now
  scans a code-stripped copy of the answer via `_cited_markers = findall(
  strip_code(t)) or findall(t)` — the `or` fallback is mandatory so the strip
  can never empty the cited set and manufacture a new refusal or a second paid
  `exact_lookup` retry. Two invariants are pinned as a property test: the
  cited set is always a subset of the raw scan, and it is empty iff the raw
  scan is empty. Covers ` ``` ` and `~~~` fences (including unterminated) and
  inline spans; deliberately does **not** cover 4-space/tab-indented blocks or
  bracketed indices in ordinary prose — a line scanner cannot separate
  indented code from indented prose without a block parser, and every
  misjudgement there *deletes* a real citation, strictly worse than the
  phantom being fixed.

  > **Operator note.** `answer_policy_version` bumped **v3 → v4**.

- **`/health/index` and `GET /v1/admin/index_versions` always reported
  `evaluation_run_id: null`, even for an index with passing evaluation
  evidence (#229).** Migration `0001`, the model, and the admin schema all
  declared the column; nothing had ever written it. `app.services
  .index_versions.link_evaluation_run` is now its single writer, called by
  `evaluate_index` in the same transaction as the terminal run. It links the
  newest **terminal** run — passing or failing — because linking only passing
  runs would make "evaluated and failed" indistinguishable from "never
  evaluated"; non-null means *evaluated*, not *passed*. No migration; no new
  `/health/index` payload key. Production's `v1` index stays `null` until
  `citevyn-worker evaluate` is re-run there post-deploy — this fix writes
  forward, it does not backfill.

- **CI tested the frontend on Node 20 while the shipped bundle was built on
  Node 22, and nothing reconciled the two (#231).** `frontend/.nvmrc` (`22`)
  is now the single source of truth: both workflows read it via
  `setup-node`'s `node-version-file:`, `package.json` declares the matching
  `engines.node` floor, and `Dockerfile.api` already built on 22. Enforced by
  `backend/tests/test_node_version_pin.py`, which scans every `setup-node`
  step, every Dockerfile `FROM node:` line, and the `engines` floor — running
  in the required `test` job with no `paths:` filter, so it fires even on a
  Dependabot PR touching only `Dockerfile.api`. Consequence: PR #227
  (node 22→26) now goes red until `.nvmrc` moves in the same PR, not silently.
  Measured while fixing: Node 20, 22, and 26 all produce a byte-identical
  frontend bundle, so the drift had not yet corrupted a shipped artifact.

- **Answers whose citations skip a bullet are served instead of discarded
  (#215).** `app/llm/validation.py` hard-failed whenever the cited indices had a
  gap, so a correct, grounded answer citing `[1]` and `[3]` was thrown away and
  the user saw "I couldn't find a grounded answer" — indistinguishable from a
  retrieval failure, which is why #215 was filed as one. Three occurrences are in
  the production audit trail. The rule was never requested by `prompts.py`, and
  hallucinated markers are still caught by the range check above it, whose two
  boundaries are now pinned by tests (contiguity had been catching an off-by-one
  there as a side effect).

### Changed
- **License is MIT + Attribution, not plain MIT (#253).** Plain MIT let a
  downstream redistribution drop the author's name entirely.
- **README and social-preview sharpened for public presentation (#248).**
  No functional change.
- **`pr-quality.yml`'s reusable workflow repinned to `imrohitagrawal/.github@v7`
  (#254, #255).**

### Added
- **`citations[].marker` on the wire (#236).** Each citation now carries the
  1-based evidence index the model actually wrote, and the frontend renders that
  instead of the array position. Shipping the #215 fix alone would have traded a
  false refusal for a **wrong citation**: `[1]`+`[3]` produced two citations
  rendered as cards **1** and **2** while the prose still said `[3]`.
  Documented in `docs/API_SPEC.md` §5.

  **Operator note:** `answer_policy_version` is bumped **v2 → v3**, so the answer
  cache is cold on deploy and refills on first ask — pre-marker rows are evicted
  rather than replayed as unnumbered cards. `docs/RUNBOOK.md` §5.3a previously
  offered `v3` as a rollback escape hatch; it now uses a dated, operator-scoped
  token, because a bare `vN` can collide with a future shipped default.

- **The browser UI is served from the API at `/`.** `infra/docker/Dockerfile.api`
  gained a Node stage that builds `frontend/dist` (the bundle is gitignored, so a
  host-built copy would be absent or stale in a remote `fly deploy`), and
  `app/main.py` mounts it with `StaticFiles(html=True)` **after** every router.
  One origin serves both the UI and the API: no CORS, and the deployment stays one
  subdomain deep, which matters because Cloudflare's free Universal SSL covers only
  one level. A missing bundle is a no-op, so tests and local runs are unaffected.
  `VITE_API_LIVE=true` is passed explicitly at build time — anything else leaves the
  chat answering from its canned in-bundle `knowledgeBase`, a demo that looks
  perfect and never reaches the backend.

### Changed
- **`fly.toml` machine memory 256mb -> 512mb**, on the owner's explicit instruction
  for the first real deploy. The measured serving RSS is ~103 MiB; the peak is
  ingestion, not serving. `test_fly_config.py` pins the new value so the size (and
  the bill) cannot move silently.

### Fixed
- **`docs/DEPLOY_FLY.md` had four errors that only surface when you actually run
  it**, all found during the first live deploy:
  - `fly apps create` does **not** allocate IPs (only `fly launch` does). Without
    `fly ips allocate-v4 --shared` + `allocate-v6` the deploy succeeds, health
    checks pass, and every request fails `Could not resolve host` — which reads as
    a DNS or TLS fault and is neither. Added as §1b.
  - §2.2 said to use the `rediss://` TLS URL. Wrong for a Fly-provisioned Upstash
    database, which lives on Fly's private 6PN network (`fdaa:…`, unreachable from
    the internet) and has no public TLS endpoint. The section now documents
    `fly redis create` (no separate Upstash account), its TTY requirement, the
    $200/mo ProdPack prompt to decline, and records that Lua `EVAL` is **verified
    working** against a live instance with the real limiter script.
  - §4.4 said "ask a question through the UI" without saying where the UI was
    hosted — it was not hosted anywhere. Now points at `/` and documents the two
    request shapes that cost a debugging round-trip (`Authorization: Bearer`, not
    `X-Demo-API-Key`; body field `message`, not `content`), plus the fact that a
    refusal is often correct rather than a regression.
  - §5.1 recommended a `CNAME`; `fly certs add` asks for `A` + `AAAA`.

### Added
- **Index promotion is now gated on evaluation quality (#210).**
  `CITEVYN_INDEX_PROMOTION_MIN_PASS_RATE` (default `0.95`) had been declared
  since Slice 8 and read by nothing, while `RELEASE_PLAN` §7 and the deploy
  runbook described promotion gates as if they were enforced. `promote_version`
  now resolves the candidate's newest **completed** `EvaluationRun` (a `running`
  run is not evidence), reads `metrics["pass_rate"]` — falling back to
  `cases_passed / cases_total` — and refuses unless the measured rate is at
  least the threshold. A rate exactly equal to the threshold promotes. A
  candidate with no completed run, or with metrics that cannot be read, is
  refused as well: "unevaluated" is not "passing". The gate lives in the
  service, not the route, so every caller is gated.

  Refusals surface as a new error code **`promotion_blocked` → HTTP 409**
  (`docs/API_SPEC.md` §15), whose body names both the measured rate and the
  required threshold. `PromoteIndexResponse` gains `forced` and
  `measured_pass_rate`.

  > **Operator note.** Nothing in the deployed application writes
  > `EvaluationRun` rows — the evaluation service is read-only and the golden
  > suite runs on a laptop and in CI. So the promote step in
  > `docs/DEPLOY_FLY.md` §4.3 now returns **409** on a real stack, and so do the
  > corpus-correction promote (`RUNBOOK` §3.7) and the emergency index rollback
  > (`RELEASE_PLAN` §8). Use `POST .../promote?force=true`, which promotes
  > anyway and records `force`, `measured_pass_rate`, `threshold` and
  > `evaluation_run_id` in the `promote_index` audit row — those fields are
  > recorded on the non-forced path too, so a clean promote is evidenced as
  > loudly as an override. Run `make golden` yourself before forcing. Only gate
  > 1 of `RELEASE_PLAN` §7 is machine-enforced; gates 2-5 remain
  > operator-verified. Re-promoting the already-active index is still a no-op
  > and is never blocked — but note that this no-op is *not* the dual-active
  > repair. Converging a dual-active database means promoting a **different**
  > version, which runs the demotion loop below the gate; that repair is
  > therefore gated too, and needs `?force=true` on a stack with no evaluation
  > runs.

### Fixed
- **`docs/API_SPEC.md` §13 documented a promote path that does not exist.**
  It said `POST /internal/v1/indexes/{index_version}/promote`, which 404s;
  `docs/DEPLOY_FLY.md` had already recorded the discrepancy. Corrected to the
  implemented route, `POST /v1/admin/index_versions/{index_version}/promote`.

## [0.11.0] — 2026-07-21

> **Operator summary for this release.** Four items need action, in this order:
> rotate the database password if you hold deploy/CI logs from before #165;
> deploy the API and worker **together** with migration `0006`; **re-seed** to
> retire stale `docs.test` rows; and **flush `answer_cache`** (the
> `answer_policy_version` bump below does this for you). Any client parsing the
> error envelope must also move to the flat shape — see *Changed*.

### Security
- **`make migrate` / `make seed` echoed the database password (#165).** The
  recipes were not `@`-prefixed, so make printed the full command line —
  including `CITEVYN_DATABASE_URL` with credentials — to stdout. `deploy.sh` and
  CI both run these targets, so the password landed in deploy and CI logs, one
  line before the output #93 had already redacted. Both recipes are now
  `@`-prefixed with a redacted progress line (`citevyn:***@host:5432`).

  > **Operator note.** Rotate the database password if you have deploy or CI
  > logs from before this release.

- **Production entry points now refuse weak `CITEVYN_DEMO_API_KEY` and
  `CITEVYN_ADMIN_API_KEY` (#200).** `_env_guard.sh` applies the same test the app
  applies at boot (non-empty, not the published default, ≥16 chars), so a
  half-filled `.env` fails in a second instead of crash-looping the api after a
  60-second health poll. The admin key — which promotes an index and reads the
  budget — was previously only compared against the Makefile bootstrap stub, so
  empty, absent, and the published `local-admin-key` default all passed.

  > **Operator note.** A `.env` that used to pass may now be rejected if either
  > key is under 16 characters. That is intended; the app would refuse to boot on
  > it anyway. `prod.env.example` no longer ships `CITEVYN_LLM_PROVIDER=stub`,
  > which the guard rejects — a literal `cp prod.env.example .env` could not pass
  > its own guard even when fully filled in.

### Added
- **`deploy-verify` proves the rollback it claims (#195).** Two drills: **A**, a
  data-recovery rollback (`backup.sh` → stop writers → `restore.sh` → api healthy
  → full functional re-verify), which always runs; and **B**, the code rollback to
  `PREV_VERSION` plus roll-forward, which runs only when that tag is the same
  migration generation. When it is not, the gate asserts the refusal is fast and
  FAILS unless narrowed with `--data-rollback-only`. The summary reports which
  path a run actually proved and reports production's real state
  unconditionally — it no longer prints "rollback proven" for a path it did not
  run. Both drills are now proven end to end: 42 passed / 0 failed against a real
  prod stack, with the full probe suite (cited answer, refusal, exact lookup,
  admin 401) re-run against the **rolled-back** stack. See RELEASE_PLAN §10.

- **Cost controls, §9 (#153).** Per-call metering into `provider_calls` priced by
  provider **and** model (#184, migration `0005`); a daily budget summed in SQL
  since midnight UTC — soft $5 warn, hard $10 stop, fail-closed — plus a
  concurrency cap (#188); `GET /v1/admin/budget` and `make budget` (#189); and
  embedding spend metered on the `Embedder` seam at both production construction
  sites (#196). An unknown model records `priced=false` with cost 0 rather than a
  guess; `unpriced_calls` is the under-counting alarm.

  > **Deploy note.** A full corpus re-ingest now counts against the §9 budget and
  > can trip the $10 hard limit, which stops paid calls on the answer path too.
  > Check `make budget` before re-ingesting. `make budget` is also wired into
  > `deploy-verify` preflight and its exit codes are load-bearing: `0` headroom,
  > `1` LOW (blocks the deploy), `2` could-not-check (warns). A **missing**
  > provider-side cap exits `1`, not `0`.

- **One-command deploy-verify and rollback** (`make deploy-verify`,
  `make rollback TAG=…`, #160), plus `infra/docker/scripts/rollback.sh` — the
  incident tool DEMO_CHECKLIST §6 referenced but which did not exist.
- **CI builds and BOOTS the api and worker images (#82).** A container-runtime
  break (interpreter/CMD) is invisible to `docker build`, which is how a
  non-booting image once shipped green. `image_smoke.sh` runs as a PR gate and as
  a release gate before `:latest` publishes.
- **A runner for `tests/shell/`, matrixed on bash 3.2 and 5.x (#181)**, so the
  operator scripts are verified against both the macOS system bash and modern
  Linux bash. The suite had no runner and was not executed in CI at all.
- **Dictated "site win" is recovered as CiteVyn (#84, #175).** Speech-to-text
  mangles the product name and the two-word forms were proven un-disambiguable by
  regex. A deterministic prefilter keeps ordinary traffic free of cost, and the
  LLM is asked only when the question would otherwise refuse as `unsupported`.

### Changed
- **Error responses are now flat on the wire (#167).** `error_response` put the
  envelope inside `HTTPException.detail`, and FastAPI serialized it as
  `{"detail": {...}}` — so `body.error.code` was `undefined` for every client, in
  violation of `docs/API_SPEC.md` §4, and the frontend branches that read it were
  dead code. A dedicated handler now emits the envelope flat and wraps
  framework-raised 404/405 in the same shape.

  > **Deploy note.** This is a wire-format change. Any client parsing
  > `{"detail": {"error": …}}` must move to the flat `{"error": {"code": …}}`.

- **`answer_policy_version` `v1` → `v2`** — invalidates every cached answer, which
  is how the answers poisoned by #169 are evicted. They sit under valid keys with
  a correct `source_version_hash` and `embedder_identity`, so nothing else would
  clear them.

  > **Rollback caveat.** Reverting past this release restores `v1` and brings
  > those poisoned rows back into key scope for the remainder of the cache TTL
  > (default 24h). When rolling back across this bump, set
  > `CITEVYN_ANSWER_POLICY_VERSION=v3` rather than accepting the reverted default
  > — a cold cache costs a refill, re-serving a known-bad answer is silent. See
  > RUNBOOK §5.3a.

- **The shipped source docs are the single source of truth for the corpus
  (#178).** `db/seed/seed_catalog.py` no longer carries a hand-written copy; it
  runs the real ingestion pipeline over `app/worker/sources/*.md`, so seeding *is*
  the ingest. The two copies that cannot be derived (the conftest fixture and the
  frontend offline KB) are held by drift guards that fail the build when a corpus
  edit invalidates them. `npm test` now runs in CI.

  > **Deploy note.** A re-seed **retires** `v1` documents no allowlisted source
  > owns. A database seeded before this change keeps stale `source_name="docs.test"`
  > rows (11 docs / 47 chunks) serving old text with fabricated citation URLs —
  > re-seed to clear them. The seed fails loud and leaves `v1` unpromoted if any
  > source fails.

- **`documents.content_checksum` renamed to `identity_checksum` (#163).** The
  column hashed name+title, not content, and `IngestionRunner` defaulted to a
  retired placeholder; `source_version_hash` is now a required kwarg.

  > **Deploy note.** Migration **`0006`** is a rename, so the API and worker must
  > be deployed **together** with it. Migration `0005` (`provider_calls`, #184) is
  > additive and safe to apply ahead of the app.

- **The judged answer-quality eval is frequency-bounded, not sampled (#153,
  #182).** It runs on a push to `main` or on a PR labelled `full-eval`, at full
  coverage. Sampling was implemented, measured and rejected: 42 of 58 golden cases
  carry zero-tolerance, judge-independent oracles that sampling switches *off*
  rather than degrading, for a ceiling of ~28% saving. New `docs/COST_CONTROLS.md`.

### Fixed
- **Rolling back across a migration boundary died inside a container (#195).**
  `rollback.sh` warned that the target predated applied migrations and then
  proceeded anyway; the live DB stays stamped at the newest revision, which the
  older tree does not contain, so `alembic upgrade head` failed with
  `Can't locate revision identified by '0006'` mid-deploy. It now REFUSES before
  the checkout, names the backup-restore path (RUNBOOK §4.2), and offers
  `--allow-migration-mismatch`. It also takes `--base-ref`, because the check
  compares against the *deployed* tree and a previous rollback leaves you on a
  detached HEAD — where the boundary would be invisible. `make restore` had never
  been executed and was missing `PGPASSWORD` — the defect that made `make backup`
  unusable — so it is now a real script, `infra/docker/scripts/restore.sh`, and it
  restores in a **single transaction** so a failure cannot leave the live database
  half-dropped.

- **The rollback drill could leave production stopped (#195).** The data-recovery
  drill stopped `api`/`worker` and, on any downstream failure, returned without
  restarting them — and the summary only mentioned production state on a path that
  never ran. The drill now raises its stopped-flag *before* the stop (`stop` is not
  atomic) and restores the writers from an `EXIT`/`INT`/`TERM` trap, so every exit
  path — including Ctrl-C — brings production back.

- **Anaphoric follow-ups returned the previous answer verbatim (#169).**
  Conversation memory resolved a follow-up by CONCATENATION
  (`"What is Codex CLI? who built it?"`), and the leading clause is a complete
  self-contained question — so the LLM answered that and ignored the follow-up.
  Routing still uses the concatenation (it is what pulls a bare anaphor onto the
  right product domain); retrieval, generation and the cache key now get a
  condensed standalone question. Every multi-turn eval case now asserts its answer
  is not byte-identical to the previous turn's.

- **An uncited answer was returned with every retrieved chunk attached at
  `confidence=high` (#174)** — citations strongest exactly where grounding was
  weakest. An answer with no cited indices is now a no-answer and is never cached.

  > **Deploy note.** Flush `answer_cache`; pre-deploy ungrounded answers otherwise
  > replay for the 24h TTL. The `answer_policy_version` bump above does this.

- **Index promotion returned 500 on a dual-active database (#166).**
  `promote_version()` used `scalar_one_or_none()` to find the row to demote, so
  two active rows raised `MultipleResultsFound`. Promotion is the only API that
  can repair index state, so a drifted database was unrecoverable through the API
  — the one call that fixes it was the one that crashed. It now demotes every
  active row.

- **A Redis outage reported the search index as down (#167).** The limiter failed
  closed with `index_unavailable`, so the code contradicted its own message. New
  `rate_limiter_unavailable` (503) is returned instead, with its own frontend copy
  — a transport notice, never the content-refusal badge.

- **The Postgres migration gate never ran on `main` (#183).**
  `github.event.pull_request` is null on a push, so the fork guard evaluated false
  post-merge and the job that applies every migration against real Postgres was
  silently skipped — leaving `main` with no schema-drift signal. A new test parses
  every workflow and fails any job gated on the pull_request payload without a
  push escape, so this class of silent skip cannot recur.

- **"How do I install Claude Code?" refused (#170, #177).** `claude_code.md`
  carried no installation content at all, while `codex.md` always had a CLI
  install section. Surfaced once #169 stopped concatenating follow-ups, which had
  been masking it.

  > **Deploy note.** Corpus content only reaches the live index via re-ingest +
  > admin promote (RUNBOOK §3.7).

- **Questions about CiteVyn refused when the name was mangled (#84, #172, #193).**
  `citevyn.md` was already shipped and ingested — routing never reached it, because
  the guardrail matched a bare `\bcitevyn\b`. The guardrail and the frontend
  offline matcher now share an alias list, pinned by a cross-language drift guard.

- **`tests/shell/test_env_guard.sh` had four silently-failing cases (#161, #179)**,
  and the suite read the caller's exported environment, so a case that
  deliberately omitted a key tested the operator's value instead of empty (#200).

- **`docs/DEMO_CHECKLIST.md` referenced routes and ports that do not exist
  (#168)**, now corrected and pinned by a guard that is scoped to the disclaimed
  token and is verb-aware.

- **CI flake: `db-verify` raced the pgvector:pg18 first-boot restart (#85).** Both
  `docker exec psql` calls now retry in a bounded loop that rides out the transient
  window; the cap still hard-fails a genuinely broken boot.

### Known issues
- **The demo rate-limit bucket is GLOBAL (#203).** `require_demo_api_key` returns a
  constant user id, so every demo visitor shares one bucket. Thirty successful
  queries from one visitor deny the demo to everyone for a rolling hour, and
  because the bucket lives in Redis an api restart no longer clears it. Needs
  per-visitor identity (session and/or IP). **Blocks publishing a public URL**; it
  does not block cutting this tag. `deploy_verify.sh` now names this explicitly
  when it rate-limits itself, since one gate run spends ~16 of the 30/hour cap.
- **#153 Layer 4 (persist the per-user limiter) was closed as won't-build (#197).**
  The persistence half already shipped; the missing half is identity, not
  durability — see #203.

## [0.10.0] — 2026-07-19

MVP demo release. Closes the RAG quality plan (Phases 0–4), turns the vector
arm back on with real embeddings and real prod ingestion, hardens the
answer-quality eval into a CI gate, ships the live chat UI end-to-end, and
clears the Phase-5 release blockers (#81, #82, #87, #93).

### Added
- **RAG quality plan complete (`docs/RAG_QUALITY_PLAN.md`, Phases 0–4).**
  - Phase 0 — RAG eval harness (#96): golden set + retrieval hit-rate +
    opt-in LLM-as-judge, CI-gated (`make eval`).
  - Phase 1 — revived the dead vector arm (#97) with an OpenRouter/OpenAI
    `text-embedding-3-small` embedder behind the seam, embedding-aware
    seeders, and read-time index provenance; real prod ingestion ships
    source docs as worker package data (#92).
  - Phase 2 — answer-when-grounded (#107): global retrieval + confidence
    gate + an LLM grounding-refusal net, with a judged eval metric.
  - Phase 3 — multi-hop query decomposition by product domain (#109) and
    conversation memory that resolves anaphoric and content-noun follow-ups
    (#112, #113).
  - Phase 4 — graceful degradation: nearest-doc suggestions on in-domain
    no_answer (#117, 4a), a distinct 429 rate-limit toast (#116, 4b), and
    vector-arm health on `/health/index` (#115, 4c).
- **Real LLM + embeddings providers** (#47, #51/#56): Gemini primary +
  OpenRouter fallback for chat, real Gemini/pgvector embeddings, exact-lookup
  fallback, and CiteVyn-meta answers.
- **Live chat UI wired to the backend** behind a live/demo toggle (#45), plus
  the full React landing page and polished Q&A chat surface.
- **Answer-quality eval hardening**: a trustworthy judge (panel + adversarial
  veto + groundedness + prompt-injection resistance, #124/#126), enforced in
  CI behind the OpenRouter secret (#127); chunk-level retrieval identity with
  MRR/precision@1 (#132), a distractor corpus with context precision/recall
  (#133), and golden-set growth to 50 cases (#134).
- **CI image build+boot smoke gate** (#82): `make image-smoke` builds and
  *boots* the api (GET /health=200) and worker (`python -m app.worker.cli`)
  images as a PR gate and a release-publish gate, so a non-booting container
  can no longer ship green.
- RAG eval harness (Phase 0 of `docs/RAG_QUALITY_PLAN.md`, #96): a JSONL
  golden set at `tests/eval/golden.jsonl` (20 cases across all 5 product
  areas × {literal, paraphrase} plus out-of-corpus refusals) scored by
  two outcome metrics — retrieval hit-rate (hermetic, via the live
  `HybridRetriever` path) and an opt-in LLM-as-judge (1–5 vs an expected
  gist; reports "unavailable" under the stub, never a fabricated score).
  The CI gate (`backend/tests/test_eval_harness.py`) runs in the existing
  hermetic `pytest + lint` job and fails on retrieval regression, refusal
  leaks, degenerate golden sets, or a total/partial judge outage. Exposed
  via `make eval`. Baseline recorded in `RAG_QUALITY_PLAN` §8a (literal
  hit-rate 1.0, paraphrase 0.0 — the dead vector arm, #97).
- 50-case golden evaluation suite under `tests/golden/cases/` plus a
  runner module that boots the FastAPI app against the in-memory
  SQLite seed and exercises the full public surface. Exposed via
  `make golden` (full run) and `make golden-smoke` (3-case sanity).
- Production guard: `Settings._reject_stub_llm_in_production` now
  rejects the Slice 9b router placeholder (`CITEVYN_LLM_PROVIDER=""`)
  in addition to `"stub"`. Two new unit tests
  (`test_settings_constructor_rejects_empty_llm_provider_in_production`,
  `test_settings_constructor_accepts_empty_llm_provider_in_development`)
  pin the contract.
- `docs/DEMO_CHECKLIST.md` — single source of truth for what the demo
  must demonstrate and the gate the team uses to declare the build
  "demo-ready".
- `scripts/refresh_sources.sh` — operator script that runs
  `make refresh` and pipes the new docs index through the same path
  the prod worker uses. The script is idempotent and refuses to run
  with an unset `CITEVYN_REDIS_URL` so a partial refresh cannot leave
  the index in a half-built state.
- `docs/DEPENDABOT_TRIAGE.md` and the `release-blocker` repo label so
  dependabot PRs touching rate-limit, security, or DB-migration code
  cannot be auto-merged.
- Frontend CI (`.github/workflows/frontend-ci.yml`) that builds the
  Vite bundle, runs ESLint, type-checks, and uploads the build output
  as a workflow artifact.

### Changed
- `Makefile` now lists `golden`, `golden-smoke`, and `eval` (the RAG eval
  harness) in the developer workflow header. The `make demo` target
  resolves `demo-frontend` so the chat UI comes up alongside the API.
- `README.md` §13 ("Demo Build Status") flips from amber to green once
  the golden suite is green on the cut commit. The badge link now
  points at the latest nightly run.

### Fixed
- LLM model retirement (#99): the configured chat model `gemini-2.5-flash`
  is retired for new Google API projects (404 "no longer available to new
  users"), which broke — or silently forced onto the paid fallback —
  grounded-answer generation. The primary default is now
  `gemini-flash-latest` (free tier; alias auto-tracks the current Flash GA)
  and the OpenRouter fallback is `openai/gpt-4o-mini` — a *different*
  provider family, so one Google-side retirement can no longer take out
  both arms at once. Ordering is cost-driven (free primary, paid backstop).
  Live-verified end-to-end. Surfaced by the Phase 0 eval baseline run.
- `runner.py` (golden): the in-memory cache and the rate limiter were
  leaking state between cases. The runner now builds a fresh
  `TestClient` per case (configurable via
  `fresh_client_per_case=False`) and pins
  `CITEVYN_RATE_LIMIT_ENABLED=false` for the run.
- The `runner.py` CLI was wired to `--report-path` but the argparse
  flag is `--report`. Make targets corrected.
- `make demo` on a fresh clone: `${CITEVYN_ACME_EMAIL:?…}` aborted
  compose parsing on the caddy service even when caddy was behind
  the `prod` profile, and every service's `env_file: - .env`
  required the gitignored file to exist. The ACME interpolation now
  falls back to a dev default; `make db-up` bootstraps
  `infra/docker/.env` from `prod.env.example` with clearly-marked
  stub secrets; and `infra/docker/scripts/_env_guard.sh` is sourced
  by `deploy.sh`/`refresh.sh`/`backup.sh`/`make restore` to refuse
  any prod entry point while the stubs are still in place.
- Frontend live-mode e2e test was permanently skipped under the demo
  Playwright config because `state.pending` only goes true on the
  `sendLive` path. Added `frontend/vite.liveStub.ts` (in-process
  Vite plugin that stubs `/v1/sessions` and `/v1/sessions/*/messages`
  when `VITE_LIVE_STUB=1`), `frontend/playwright.live.config.ts`
  (companion config with `grep: /live only/i` plus `VITE_API_LIVE=true`
  so the previously-skipped loading-indicator test now runs and
  asserts), and `.github/workflows/frontend-live-e2e.yml` to wire it
  into CI on PRs that touch `frontend/**`. The demo config still
  skips the test (intended — the demo path is instant by design) and
  continues to gate merges via the 57-case demo suite.

## [0.9.1] — 2026-05-12

### Fixed
- Slice 9.1 follow-up: the `x-anthropic-billing-header` env var name
  was case-sensitive in code but lower-cased on Linux containers
  (imrohitagrawal/citevyn#11 follow-up, commit `4a01850`).

## [0.9.0] — 2026-04-30

### Added
- Slice 8: Redis-backed sliding-window rate limiter with a Lua
  `EVAL` script that does `ZREMRANGEBYSCORE` + a conditional
  `ZADD` + `EXPIRE` in a single round trip. Replaces the
  in-process limiter when `CITEVYN_REDIS_URL` is set.
- Slice 7: Server-Sent Events streaming on
  `POST /v1/sessions/:id/messages/stream`.

## [0.8.0] — 2026-04-02

### Added
- Slice 6: admin key + admin routes (`/v1/admin/products`, `/v1/admin/
  sources`, `/v1/admin/sources/refresh`).
- Slice 5: orchestrator + grounded answer shape with `request_id`,
  `domain`, `intent`, `confidence`, `retrieval_strategy`, and
  `citations[]`.

---

### Release procedure

1. Cut a release branch: `git switch -c release/vX.Y.Z`.
2. Update the version in `backend/pyproject.toml`. (This step used to say
   "update `version.txt` and `pyproject.toml`" — there is no `version.txt` in
   this repo and there never was, so the procedure could not be followed as
   written.)
3. Add a fresh `[X.Y.Z]` section to this file, dated today.
4. Run `make lint && make typecheck && make test && make golden` and
   attach the `golden_report.json` to the PR.
5. Open the PR with the `release-blocker` label removed (re-add it
   after merge if a hot-fix is required).
6. On merge, tag: `git tag -a vX.Y.Z -m "vX.Y.Z — <one-liner>"`.
7. Push: `git push origin main --follow-tags`.
