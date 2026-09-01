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
  "providers": ["github"]
}
```

`providers` (ADR-0004 PR 13) lists the OAuth providers (`"github"`,
`"google"`) linked to this account, sorted, always present — `[]` when none.
`register` and `login` return the same body shape, `providers` included.

401 `auth_required` if there is no valid session cookie. Unlike the
session/message routes (which mint a fresh anonymous principal
transparently on a missing/invalid cookie), this route does NOT mint — it
answers "who is this, if anyone", so a stale cookie must be visibly 401,
not silently replaced.

## 4b. OAuth login + account linking (ADR-0004 PR 12 / PR 13)

These are **browser navigations**, not API calls: no demo bearer, no JSON.
They are rate-limited per visitor like every other public route
(`rate_limited_oauth_navigation`), and always answer with a redirect. The
`{provider}` segment is `github` or `google`; anything else, or a provider
without credentials configured, is a quiet `404`.

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
| `connect` | the starting session is no longer a live registered account | `/?connect=error&reason=session&provider={provider}` |
| `connect` | provider error after the nonce was claimed | `/?connect=error&reason=provider&provider={provider}` |
| `connect` | the user declined the provider's consent screen | `/?connect=error&reason=denied&provider={provider}` |

A declined consent (`?error=…`) consumes the caller's own nonce (same
session-bound conditional claim) and routes by its intent; a `state` that is
not the caller's own is left untouched, so it cannot be burned by a third party.

Linking writes exactly one `user_identities` row: it never creates a `users`
row, never changes `users.email`, and never rotates the caller's session
cookie. Every failure is audited as `auth_failed` with `metadata.event` one of
`oauth_state_invalid`, `oauth_expired`, `oauth_denied`, `oauth_provider_error`,
`oauth_connect_conflict`, `oauth_connect_no_session`; successes are `login`
events with `metadata.event` = `oauth_{provider}` / `oauth_connect_{provider}`.

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
