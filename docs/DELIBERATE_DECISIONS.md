# Deliberate decisions: what this audit chose NOT to test, and why

Produced by the four-lens test-maturity audit of 2026-09-16 (software test engineer,
principal architect, principal automation engineer, engineering test manager), read-only
at `0a7e1b5`, with every finding re-checked by a hostile skeptic. The ranked plan that
came out of it is tracked as GitHub issues #447-#464, indexed in
[`BACKLOG.md`](BACKLOG.md). Issue #467 is indexed alongside them but did not come from this
audit: it was found reviewing PR #466, and concerns a rule (`TEST_STRATEGY` §8b) that did not
exist at `0a7e1b5`.

A silent hole reads as an oversight. A recorded decision reads as a decision. Each item
below was considered, measured where measurement was cheap, and deliberately left undone.
Revisit any of them by disagreeing with the reason given, not by rediscovering the gap.

Not claimed to be exhaustive.

### 1. Load, stress, soak or throughput testing against production or any deployed environment

The demo runs on Fly free-tier with min_machines_running = 0 and a $2/day LLM hard cap (fly.toml:120). A load test would spend the cap, page nobody (five of six CRITICAL alerts in OBSERVABILITY.md §7.1 have no automated detection path), and measure a cold-started free machine. There is no staging environment to point it at.

### 2. Absolute wall-clock p95 budgets in CI

The repo already tried this shape and rejected it: test_guardrails_domain.py:700-705 records a ratio reading 6.33 on a contended machine against a 3.0 threshold. Use size ratios (best-of-N at two input sizes) or statement counts, never a clock.

### 3. A Lighthouse job on the demo-e2e path

playwright.config.ts:31 starts `npm run dev`, so a Lighthouse score there measures an unminified dev build. A `vite preview` variant against the built dist is a separate, noisier job and is not the cheapest budget available.

### 4. Adding a WebKit (or any second) Playwright project to the required demo-e2e job

Measured: the suite STEP runs 698s and the whole job 747s, inside timeout-minutes: 20 (which applies to the job) with workers: 1 (run 35019295524), so a second project overruns the cap. Adding it to playwright.demo-ci.config.ts alone breaks the `ci_total == all_total - all_visual` differential; adding it to the shared base doubles the four `if (!isLive)` self-skips to eight, reddening frontend.yml's `stats["skipped"] != 4` and then the backend regex that pins that literal (test_gating_workflows.py:1877). If cross-engine signal is wanted it must be a separate advisory job with its own name, and it must exclude contrast-floor.spec.ts, faint-contrast.spec.ts and every contrast-kit.ts consumer, which are Chromium-pinned by construction (contrast-kit.ts:376 font-size quantisation, :444-447 -webkit-text-fill-color resolution, focus-ring.spec.ts:166 colour serialisation) and would produce false reds rather than signal.

### 5. Emitting the six missing audit events now

`AuditAction` defines ten members and writers exist for exactly four: `ask_question`, `auth_failed`, `login` and `promote_index`. Two of the six without writers are already durably recorded elsewhere — `IngestionJob` rows (`worker/runner.py:253`) and `EvaluationRun` rows (`worker/promotion_eval.py:387`), both readable through admin GETs — so emitting them as audit rows duplicates a record that already exists. The rest need either an auth-dependency signature change (`require_admin_api_key` is synchronous and has no `AsyncSession`, so an admin-auth-failure row is not implementable in place) or surfaces this round put out of bounds. Tracked in #462 instead, with the cheap half — a failed-password-login row, which is RED on today's HEAD and needs no mutation — folded into a later package.

An earlier version of this entry said FIVE missing events and THREE already recorded. Both were counts of something enumerable, and both were wrong.

### 6. Changing the daily-cost-cap response from 500 to 503

That is a public error-contract change and owner-gated, and would need a new APIErrorCode row in API_SPEC §15 to keep the both-ways parity test green. The parts that are fine: a route test pinning today's behaviour, and correcting the three stale comments (orchestrator.py:799, llm/errors.py:4, test_answer_orchestrator.py:1976) that promise a 429->503 mapping which _orchestrator_error_handler does not implement.

### 7. Chasing the `empty` vector-arm state as a live production bug

New index rows are created `candidate` with their chunks written in the same transaction (worker/runner.py:610), and db/seed/seed_catalog.py:298-302 activates only after a clean full drive, so no path to an ACTIVE chunk-less index was found. Fix the uptime probe's logic anyway -- for `partial`, which IS reachable through the RUNBOOK's own dead-arm repair.

### 8. Retrying a regex guard for the two-word 'site win' speech-to-text alias

Proven un-disambiguable over three adversarial rounds ('may the best site win!'). It needs an intent classifier; the current miss is deliberate.

### 9. Capping how many tokens feed the keyword arm's SQL

Order-preserving dedupe before the ilike list is a pure cost fix and is fine (measured 1004 bind parameters for 'api ' * 1000). Capping the token COUNT changes which rows retrieval returns and belongs to #370, which is owner-gated. Note the >1664-column Postgres failure is latent: it needs one active product area to reach retrieval_max_candidates (20) chunks, and the largest today holds 10.

### 10. Running the cross-generation restore drill this round

It is L, needs a local Postgres with pg_dump/pg_restore, and any fix changes ops procedure (restoring into an emptied schema rather than --clean onto a newer one) -- owner-gated. File it with the predicted mechanism: a v0.11.0-era dump lacks auth_sessions, user_identities, oauth_nonces and magic_link_tokens, so pg_restore's DROP CONSTRAINT on users_pkey should fail while those FKs exist, and --single-transaction aborts the whole restore. Note RELEASE_CANDIDATE_v0.12.0.md:18 claims 'Migrations since v0.11.0: None' while head is 0013 -- an operator trusting it would not know they crossed a migration boundary.

### 11. Fixing the backup partial-dump hazard in this round

docker-compose.yml's backup service writes pg_dump straight to the final citevyn-<ts>.dump name with no temp file and no verification, so a failed dump leaves a truncated file matching the pattern RUNBOOK §4.2 tells operators to restore from. Both halves of the fix (a .partial + mv, or a pg_restore --list check) change infra scripts or compose config and need approval. The database is never corrupted -- restore.sh uses --single-transaction -- so the cost is incident time, not data.

### 12. Opening a separate PR to delete the unused frontend-dist artifact upload

It is one measured 403 in 100 runs (run 34249221368, attempt 1, 'Failed to FinalizeArtifact ... 403 Forbidden'), and nothing downloads the artifact. Delete the step together with its _SANCTIONED_STEP_CONDITIONS entry inside WP-3, which already owns frontend.yml -- the two must change together or test_every_sanctioned_step_condition_still_names_a_real_step goes red.

### 13. Rewriting the focus trap to always preventDefault as urgent work

The gap is real (useFocusTrap.ts:124-130 claims no interior Tab, so native order decides the middle of the cycle) but the trigger -- an engine whose default Tab order excludes buttons -- is an external premise nobody verified in a browser, the escape is leak-and-recover rather than a permanent trap-out, and nothing in the repo can execute the path. If it is picked up later, the jsdom dispatchTab() test is the primary fix (engine-independent, red today) and a WebKit project is not.

### 14. Writing the SQL statement-count budget as first specified

Three corrections are mandatory or it passes vacuously: retrieval_top_k defaults to 6 so evidence is capped regardless of corpus size (assert the two runs wrote DIFFERENT numbers of retrieved_evidence rows first); seed_catalog has no chunks-per-source parameter, so the doubled seed must be hand-built; and the cache-hit leg needs a fresh session_id plus an explicit `cache_hit is True` assertion, because the key is built from the memory-condensed retrieval_query and a same-session repeat can miss.

## Access model: permanent rules (ADR-0005, owner, 2026-09-25)

The entries above record what the 2026-09-16 audit chose not to test. This section is
different: it records rules about access and payment that hold for every future change.
They are decided. Disagree with the reason given in
[ADR-0005](ADR/0005-access-model.md); do not route around it.

### 15. No secret in the browser, ever

Real secrets live only on the server. Anything compiled into the bundle is public, however
it is named. The public client token was the last exception (it is also the fallback
rate-limit salt), and ADR-0005 retires it once `CITEVYN_RATE_LIMIT_KEY_SALT` is set.

### 16. Entitlement is checked on the server, from our table, on every protected request

The frontend may show a tier; it never enforces one. A request never waits on Stripe:
Stripe webhooks write our `memberships` table, and requests read that table.

### 17. Six concerns, one mechanism each

- **Authentication:** the session cookie.
- **Entitlement:** the `memberships` table, fed by signed Stripe webhooks.
- **Authorisation:** a capability on every route, plus ONE policy table saying which tier
  (kind of account) has which capability.
- **Quota:** per-user and per-plan metering on `provider_calls`, which records who paid.
- **CSRF:** a `SameSite` cookie + an `Origin` check + a fixed request header.
- **Abuse:** per-IP limits on the routes anyone can call without signing in, email
  verification, a bot check at sign-up.

A new feature reuses these. It does not add a second mechanism for any of them.

### 18. Rejected designs

- A secret token in the bundle: the bundle is public.
- Entitlement checks in the frontend: the user controls the frontend.
- Calling Stripe mid-request: it couples our uptime and latency to Stripe's.
- An anonymous cookie or IP quota as the gate: neither identifies a person.
- A silent BYOK→platform key fallback: it bills our key without the user knowing and hides
  a broken key.
- A cheaper BYOK plan: generation is not our only cost.
- Pasted BYOK keys: OpenRouter's OAuth PKCE flow gives a key the user approved and can revoke.
