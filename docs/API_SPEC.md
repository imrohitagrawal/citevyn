# CiteVyn API Specification

## 1. Purpose

This document defines the MVP API surface for CiteVyn.

The API supports:

1. Session creation.
2. Chat-based questions.
3. Exact lookup.
4. Feedback placeholder.
5. Admin ingestion.
6. Internal evaluation.
7. Health checks.

## 2. API Principles

1. Keep public APIs simple.
2. Keep ingestion and evaluation admin-only.
3. Return citations for factual answers.
4. Return explicit no-answer and unsupported flags.
5. Include trace identifiers in responses.
6. Avoid leaking internal IDs except where useful for debugging or citation traceability.

## 3. Authentication

### MVP

Use demo auth:

```http
Authorization: Bearer <demo-token>
```

Admin endpoints require:

```http
X-Admin-API-Key: <admin-key>
```

## 4. Common Response Fields

Most API responses should include:

```json
{
  "request_id": "req_123",
  "status": "success"
}
```

Error responses:

```json
{
  "request_id": "req_123",
  "status": "error",
  "error": {
    "code": "unsupported_domain",
    "message": "I can answer questions about Claude, Claude Code, Codex, and Gemini using indexed official documentation."
  }
}
```

## 4a. Auth (ADR-0004 PR 6)

Every request already resolves to a per-visitor principal via a session
cookie (`docs/ADR/0004-user-accounts.md` PR 3), anonymous by default. These
four routes let a visitor turn that into a registered account. All four
still require the demo bearer (§3) — it doubles as a CSRF guard.

```http
POST /v1/auth/register
```

```json
{ "email": "user@example.com", "password": "at least 8 characters" }
```

201 on success, with the same body shape as `GET /v1/auth/me` below and a
rotated `Set-Cookie`. 422 `validation_error` if the email is already
registered — a deliberate email-existence leak, recorded in the ADR (the
always-202 alternative needs an email provider this project does not have).

Registering (and logging in, below) **claims** any `sessions` rows owned by
the caller's prior anonymous principal — chat history started before
signup survives it — and **rotates the session cookie**: the old cookie's
`AuthSession` row is deleted, not superseded, so reusing the old value
against `GET /v1/auth/me` afterward is a 401, not the pre-login identity.

```http
POST /v1/auth/login
```

```json
{ "email": "user@example.com", "password": "..." }
```

200 on success (same body + claim + rotation as register above). 401
`auth_required` for either a wrong password or an unknown email — the two
cases are made to look identical in both status code and response latency
(`app.core.passwords.verify_password_or_dummy`), or the difference would be
a free account-enumeration oracle.

`register`/`login` are additionally rate-limited per TARGET EMAIL (not per
client) to bound credential stuffing — a distributed attacker spreading one
password guess across many source IPs against a single account is still
capped.

```http
POST /v1/auth/logout
```

204, always — idempotent, works with no session too. Revokes the current
`AuthSession` row and clears the cookie.

```http
GET /v1/auth/me
```

```json
{
  "request_id": "req_001",
  "user_id": "usr_...",
  "email": "user@example.com",
  "anonymous": false,
  "providers": ["github"],
  "has_password": true
}
```

`providers` (ADR-0004 PR 13) lists the OAuth providers (`"github"`,
`"google"`) linked to this account, sorted, always present — `[]` when none.
`has_password` (ADR-0004 PR 14) is whether the account has a password set —
`false` for an account created by OAuth or magic link that never set one.
`register`, `login` and `me/password` return the same body shape, both
fields included.

401 `auth_required` if there is no valid session cookie. Unlike the
session/message routes (which mint a fresh anonymous principal
transparently on a missing/invalid cookie), this route does NOT mint — it
answers "who is this, if anyone", so a stale cookie must be visibly 401,
not silently replaced.

## 4b. OAuth login + account linking (ADR-0004 PR 12 / PR 13)

These are **browser navigations**, not API calls: no demo bearer, no JSON
body. They are rate-limited per visitor like every other public route
(`rate_limited_oauth_navigation`) and answer with a redirect. The
`{provider}` segment is `github` or `google`; anything else, or a provider
without credentials configured, is a quiet `404` — with one exception:
`callback` handles a provider-declined `?error=` **before** validating the
provider, so an unknown provider plus `?error=` redirects like any other
declined login (`/?auth=error`) rather than 404ing.

```http
GET /v1/auth/oauth/{provider}/start
```

Begins an OAuth **login**. Mints an anonymous session if the caller has none
(so the state nonce has a session to bind to), persists a single-use,
session-bound PKCE nonce (`oauth_nonces`, 5-minute TTL, `return_intent =
"login"`) and `302`s to the provider's consent screen.

```http
GET /v1/auth/oauth/{provider}/connect/start
```

Begins **account linking** (PR 13): attaches the provider identity to the
account the caller is *already* signed in as. Requires a registered
(`usr_`-prefixed) session that was **created within
`CITEVYN_OAUTH_CONNECT_MAX_SESSION_AGE_SECONDS`** (default 20 minutes — a
stolen cookie must not be able to plant a permanent backdoor identity late
in a 180-day session). Never mints a session. Anonymous / no session / stale
session → `302 /?connect=error&reason=session&provider={provider}` and no
nonce is written. Otherwise identical to `start` with `return_intent =
"connect"`.

```http
GET /v1/auth/oauth/{provider}/callback?code=...&state=...
```

The provider's redirect target for **both** flows. Atomically claims the
nonce (`DELETE … WHERE nonce_id AND provider AND auth_session_id … RETURNING`
— the completing browser must be the one that started the flow), exchanges
the code (PKCE), fetches the provider identity, then dispatches on the
claimed nonce's `return_intent`:

| intent | outcome | redirect |
|---|---|---|
| `login` | identity found → that account logs in; not found → a **new** account (never matched by email) | `/?auth=ok` |
| `login` | any failure (bad/expired/replayed state, provider error, consent denied) | `/?auth=error` |
| `connect` | identity newly linked, or already linked to this same account | `/?connect=ok&provider={provider}` |
| `connect` | identity already linked to a **different** account — never reassigned | `/?connect=error&reason=already_linked&provider={provider}` |
| `connect` | the claimed nonce's session is no longer a live registered account (defense in depth — a session that dies during the provider round trip) | `/?connect=error&reason=session&provider={provider}` |
| `connect` | the nonce expired (5-minute TTL) before the callback | `/?connect=error&reason=provider&provider={provider}` (event `oauth_expired`) |
| `connect` | provider error after the nonce was claimed | `/?connect=error&reason=provider&provider={provider}` |
| `connect` | the user declined the provider's consent screen | `/?connect=error&reason=denied&provider={provider}` |

Failures **before** the nonce is claimed — a missing `code`, a missing,
malformed or replayed `state`, or a starting session that was revoked or
rotated in the meantime — cannot know the intent and redirect to
`/?auth=error` for both flows (nonce left intact where it exists). An
unrecognised `return_intent` on a claimed nonce also fails closed to
`/?auth=error`.

A declined consent (`?error=…`) consumes the caller's own nonce (same
session-bound conditional claim) and routes by its intent; a `state` that is
not the caller's own is left untouched, so it cannot be burned by a third party.

Linking writes exactly one `user_identities` row: it never creates a `users`
row, never changes `users.email`, and never rotates the caller's session
cookie. Every failure is audited as `auth_failed` with `metadata.event` one of
`oauth_state_invalid`, `oauth_expired`, `oauth_denied`, `oauth_provider_error`,
`oauth_connect_conflict`, `oauth_connect_no_session`; successes are `login`
events with `metadata.event` = `oauth_{provider}` / `oauth_connect_{provider}`.

## 4c. Magic-link login + set/change password (ADR-0004 PR 14)

A third way in, and the password-recovery path: a forgotten password is
solved by requesting a sign-in link, then setting a new password from the
account menu. There is deliberately **no** separate "reset password by
email" flow — it would be a second token type with its own timing-oracle,
rate-limit and scanner-safety surface, for a case the magic link already
covers (the trade-off is recorded in the ADR).

```http
POST /v1/auth/magic-link/request
```

```json
{ "email": "user@example.com" }
```

Requires the demo bearer (§3). **Always `202`**, with
`{ "request_id": "...", "status": "accepted" }`, whether or not the address
is registered — and the two cases are made to cost the same before the
response is decided (the same statement count against the database; only
the network send is deferred, as a background task registered on both
branches), so neither status nor latency is an account-enumeration oracle.
A registered address receives one email with a link to `GET …/confirm`;
issuing a new link deletes the user's prior unexpired ones, so only the
newest email is ever redeemable. Links expire after
`CITEVYN_MAGIC_LINK_TTL_SECONDS` (default 600). 422 `validation_error` for
a malformed address. `404` when no email provider is configured (no
`CITEVYN_RESEND_API_KEY` in production; locally the file outbox is used).

Rate-limited per TARGET EMAIL in a **dedicated** bucket
(`CITEVYN_RATE_LIMIT_MAGIC_LINK_PER_HOUR`, default 5) — never the
`auth_login` bucket, or flooding link requests at a victim's address would
lock them out of password login with no credentials at all. Applied on both
branches, so it is also the email-bombing ceiling per address.

```http
GET /v1/auth/magic-link/confirm?token=<token_id>.<secret>
```

A **browser navigation** (no bearer). Renders a plain HTML page whose only
control is a `<form method="post">` holding the token in a hidden field —
it never consumes the token, never sets a cookie, never auto-submits (no
script, no meta-refresh; the app CSP forbids inline script anyway). This is
what makes the link survive corporate mail scanners and link prefetchers,
which GET every URL in an inbound email before the human opens it. An
invalid or expired token renders a "this link is invalid or has expired"
page with no form. `Cache-Control: no-store`; the page sets
`<meta name="referrer" content="no-referrer">` so the credential-bearing URL
never leaks as a Referer.

```http
POST /v1/auth/magic-link/confirm
Content-Type: application/x-www-form-urlencoded

token=<token_id>.<secret>
```

The claim, submitted by the button on the page above. Always redirects:
`302 /?auth=ok` on success (the same landing the OAuth login uses, with a
rotated `Set-Cookie` and the same claim-on-login of the browser's prior
anonymous history as every other login path), `302 /?auth=error` otherwise.
The token is consumed by one atomic `DELETE … WHERE token_id AND secret_hash
… RETURNING`: a replay finds no row, and a wrong secret deletes nothing (a
guess cannot burn the real user's link). Expiry is checked on the claimed
row. Not bound to the requesting browser's session — magic links are
cross-device by design. A present `Sec-Fetch-Site` must be
`same-origin`/`none`, and a present `Origin` must match
`CITEVYN_MAGIC_LINK_BASE_URL`'s origin (the literal `Origin: null` — what
Chromium sends under some referrer policies — is accepted only when
`Sec-Fetch-Site` vouched), or the request is refused before anything is
consumed (a login-CSRF guard: without it a hostile page could log a
victim's browser into the attacker's account by auto-posting the
attacker's own token). uvicorn's access log redacts the `token=` value of
the confirm URL (`app.core.logging.RedactQueryCredentialsFilter`), so the
credential never reaches the server log.

```http
POST /v1/auth/me/password
```

```json
{ "new_password": "at least 8 characters" }
```

```json
{ "current_password": "...", "new_password": "at least 8 characters" }
```

Requires the demo bearer and a **registered** (`usr_`-prefixed) session;
401 `auth_required` otherwise (an anonymous session has no account to
protect). Sets a password for an account that has none, or changes an
existing one. **Whether `current_password` is required is decided by the
server from the stored account, never from whether the body contains the
field:** if the account already has a password, a missing
`current_password` is a 422 `validation_error` ("Enter your current
password.") and a wrong one is a 422 ("Current password is incorrect." —
not a 401, since the caller *is* authenticated and a 401 would sign them
out client-side); if it has none, the field is ignored. On success, 200
with the `me` body shape (`has_password: true`) and **every other live
session for the account is revoked** — the caller's own session stays —
uniformly for a first-time set and a change: the credential surface
changed, so everywhere else must re-authenticate. Passwords are 8–128
characters, as at registration.

Audit trail: successes are `login` events with `metadata.event` one of
`magic_link_requested`, `magic_link`, `password_set`, `password_changed`
(the last two also carry `sessions_revoked`); failures are `auth_failed`
with `magic_link_unknown_email`, `magic_link_invalid`, `magic_link_expired`,
`magic_link_origin_rejected`, `password_current_mismatch`. No email address
is ever written to audit metadata.

## 5. Create Session

```http
POST /v1/sessions
```

### Request

```json
{
  "user_id": "demo_user",
  "channel": "chat"
}
```

### Response

```json
{
  "request_id": "req_001",
  "session_id": "sess_001",
  "expires_at": "2026-06-07T12:00:00Z"
}
```

## 5a. Session History (ADR-0004 PR 10)

```http
GET /v1/me/sessions
```

Lists the caller's own sessions, newest first, capped at 50. Works for an
anonymous visitor too — it lists whatever exists under the current cookie;
signing in is what makes that history durable across visits, not a
requirement to see it in the current tab.

```json
{
  "request_id": "req_001",
  "sessions": [
    {
      "session_id": "sess_001",
      "created_at": "2026-06-07T12:00:00Z",
      "expires_at": "2026-06-14T12:00:00Z",
      "current_product_area": "claude_code",
      "message_count": 4
    }
  ]
}
```

```http
GET /v1/sessions/{session_id}
```

Already existed (Slice 7); as of migration 0009 each message in the
`messages` array carries its own `citations` — the exact wire-shaped
citations (marker included) the live answer had, persisted at write time,
not reconstructed from `retrieved_evidence` on read (a cache-hit answer
persists zero evidence rows, so that reconstruction would silently show no
sources for a resumed cache-hit message). `[]` for a user message or an
assistant reply with none (e.g. a no-answer refusal).

## 6. Ask Question

```http
POST /v1/sessions/{session_id}/messages
```

### Request

```json
{
  "message": "How do I configure Claude Code permissions?",
  "answer_style": "short"
}
```

Allowed `answer_style` values:

```text
short
step_by_step
```

### Response

```json
{
  "request_id": "req_002",
  "message_id": "msg_001",
  "answer": "Short citation-backed answer.",
  "citations": [
    {
      "source_name": "Claude Code Docs",
      "title": "Permissions",
      "url": "https://example.com/docs",
      "chunk_id": "chunk_123",
      "marker": 1
    }
  ],
  "domain": "claude_code",
  "intent": "how_to",
  "confidence": "high",
  "cache_hit": false,
  "retrieval_strategy": "hybrid_reranked",
  "unsupported": false,
  "no_answer": false
}
```

**`citations[].marker`** is the 1-based evidence index the model actually wrote
in `answer` — **not** the position of the citation in the array. The two differ
whenever the model skips a bullet: an answer citing `[1]` and `[3]` returns two
citations with markers `1` and `3`. Clients MUST render `marker`, because
numbering by array position would label those cards `1` and `2` while the answer
text still says `[3]`, pointing the reader at a card that does not exist.

Markers are in range (`1 <= marker <= len(evidence)`) and strictly increasing,
but are **not** guaranteed contiguous. Requiring contiguity used to discard the
whole answer (#215).

### Unsupported Response

```json
{
  "request_id": "req_003",
  "message_id": "msg_002",
  "answer": "I can answer questions about Claude, Claude Code, Codex, and Gemini using indexed official documentation. I do not have credible source material in this assistant to answer that.",
  "citations": [],
  "domain": "unsupported",
  "intent": "unsupported",
  "confidence": "none",
  "cache_hit": false,
  "retrieval_strategy": "none",
  "unsupported": true,
  "no_answer": true
}
```

### Greeting Response

A bare social greeting (`hi`, `hello`, `hello CiteVyn`, `good morning`) is
neither a grounded answer nor a refusal. The orchestrator short-circuits it —
before retrieval and before the unsupported guardrail — to a friendly static
reply with `intent: "greeting"`, `unsupported: false`, and `no_answer: false`.
A real question that merely opens with a greeting ("hello, how do I get the
Gemini API key?") is not short-circuited and flows through the normal pipeline.

A greeting never carries `domain: "unsupported"` — that would break the
`domain == "unsupported"` ⟺ `unsupported == true` invariant. A bare greeting
carries the neutral `domain: "general"`; a `CiteVyn`-addressed greeting
("hi CiteVyn") keeps `domain: "citevyn"`.

```json
{
  "request_id": "req_005",
  "message_id": "msg_003",
  "answer": "Hi! I'm CiteVyn. I answer questions about Claude API, Claude Code, Codex, and Gemini API using cited official documentation. What would you like to know?",
  "citations": [],
  "domain": "general",
  "intent": "greeting",
  "confidence": "none",
  "cache_hit": false,
  "retrieval_strategy": "none",
  "unsupported": false,
  "no_answer": false
}
```

## 7. Exact Lookup

```http
GET /v1/search/exact?q=--some-flag
```

### Response

```json
{
  "request_id": "req_004",
  "query": "--some-flag",
  "matches": [
    {
      "term": "--some-flag",
      "term_type": "flag",
      "product_area": "codex",
      "document_title": "CLI Reference",
      "source_url": "https://example.com/cli",
      "chunk_id": "chunk_456",
      "snippet": "Relevant official documentation snippet."
    }
  ]
}
```

## 8. Feedback Placeholder

Not active in MVP, but the contract exists for V1.

```http
POST /v1/feedback
```

### Request

```json
{
  "session_id": "sess_001",
  "message_id": "msg_001",
  "rating": "incorrect",
  "comment": "The citation does not support the answer."
}
```

### Response

```json
{
  "request_id": "req_005",
  "status": "accepted"
}
```

## 9. Admin: Trigger Ingestion

```http
POST /internal/v1/ingestion/jobs
```

Admin-only.

### Request

```json
{
  "source_name": "codex",
  "mode": "full"
}
```

### Response

```json
{
  "request_id": "req_006",
  "job_id": "ing_001",
  "status": "pending"
}
```

## 10. Admin: Get Ingestion Job

```http
GET /internal/v1/ingestion/jobs/{job_id}
```

### Response

```json
{
  "request_id": "req_007",
  "job_id": "ing_001",
  "source_name": "codex",
  "status": "completed",
  "stage": "indexing",
  "started_at": "2026-06-07T10:00:00Z",
  "completed_at": "2026-06-07T10:05:00Z",
  "errors": []
}
```

## 11. Admin: Latest Ingestion Status

```http
GET /internal/v1/ingestion/latest
```

## 12. Admin: Run Evaluation

```http
POST /internal/v1/evaluations/run
```

### Request

```json
{
  "index_version": "index_v12",
  "suite": "mvp_golden_50"
}
```

### Response

```json
{
  "request_id": "req_008",
  "evaluation_run_id": "eval_001",
  "status": "running"
}
```

## 13. Admin: Promote Index

```http
POST /v1/admin/index_versions/{index_version}/promote?force=false
```

(The path was previously documented as `/internal/v1/indexes/{index_version}/promote`,
which has never existed and 404s — see `docs/DEPLOY_FLY.md` §4.3. Corrected here
against the implemented route in `backend/app/api/routes/admin.py`.)

Promotion is gated on evaluation quality. The service resolves the candidate's
newest **completed** `EvaluationRun` and refuses with **409 `promotion_blocked`**
unless the measured pass rate is at least `CITEVYN_INDEX_PROMOTION_MIN_PASS_RATE`
(default `0.95`; a rate exactly equal to the threshold promotes). A candidate with
no completed run, or whose run metrics cannot be read, is refused as well —
"unevaluated" is not "passing". Only gate 1 of `docs/RELEASE_PLAN.md` §7 is
machine-enforced; gates 2-5 remain operator-verified.

`?force=true` promotes regardless. It is not a hole: the `promote_index` audit row
records `force`, `measured_pass_rate`, `threshold` and `evaluation_run_id` — on the
non-forced path too.

Re-promoting the index that is already active is a no-op that returns 200 and is
never gated.

```json
{
  "request_id": "req_009",
  "index_version": "index_v12",
  "status": "active",
  "promoted_at": "2026-06-07T10:05:00Z",
  "already_active": false,
  "forced": true,
  "measured_pass_rate": null
}
```

## 14. Health Checks

```http
GET /health
GET /health/index
GET /health/dependencies
```

### `/health/index` Response

Corrected here against the implemented route in `backend/app/api/routes/search.py`;
the shape previously documented (bare index-name strings, `status: "healthy"`,
`last_successful_ingestion`) has never been emitted.

```json
{
  "request_id": "req_010",
  "status": "ready",
  "active_index": {
    "index_version": "v1",
    "source_version_hash": "sha256:...",
    "created_at": "2026-06-07T10:00:00Z",
    "promoted_at": "2026-06-07T10:05:00Z",
    "evaluation_run_id": "7f1c...-..."
  },
  "previous_good_index": null,
  "vector_arm": { "status": "healthy", "healthy": true, "...": "..." },
  "message": null
}
```

`status` is `pre_index` (nothing promoted yet), `ready` (an active index exists) or
`degraded` (only a previous-good index remains). `vector_arm` is additive and does
not change `status` — read it for the vector-arm verdict.

`evaluation_run_id` names the newest **terminal** `EvaluationRun` for that index —
the run `citevyn-worker evaluate --index-version <v>` wrote (#216, #229). It means
the index **was evaluated**; it does **not** mean the index **passed**, because the
pointer follows a `failed` run too (otherwise "evaluated and failed" would be
indistinguishable from "never evaluated"). `null` means no evaluation has ever
completed for that index. For the verdict, read the run through
`GET /v1/admin/evaluations/{run_id}`. The promotion gate in §13 does not consult
this field; it re-derives the measurement from `evaluation_runs` on every promote.

## 15. Error Codes

| Code | Meaning |
|---|---|
| unsupported_domain | Query outside supported scope |
| weak_evidence | Not enough source evidence |
| citation_validation_failed | Answer not supported by citations |
| rate_limited | User exceeded rate limit |
| auth_required | Missing or invalid auth |
| ingestion_failed | Ingestion job failed |
| evaluation_failed | Evaluation gate failed |
| index_unavailable | Active index unavailable (reserved — not currently emitted) |
| cost_limit_reached | Demo daily cost cap reached |
| rate_limiter_unavailable | Rate limiter backend (Redis) unreachable — request rejected fail-closed |
| promotion_blocked | Index promotion refused: the candidate has no completed evaluation run, or measured a pass rate below `CITEVYN_INDEX_PROMOTION_MIN_PASS_RATE` |
