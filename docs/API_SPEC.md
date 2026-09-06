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
  "has_password": true,
  "password_step_up": false
}
```

`providers` (ADR-0004 PR 13) lists the OAuth providers (`"github"`,
`"google"`) linked to this account, sorted, always present — `[]` when none.
`has_password` (ADR-0004 PR 14) is whether the account has a password set —
`false` for an OAuth-created account that never set one (a magic link never
creates an account).
`password_step_up` (ADR-0004 PR 15, #293) is whether the **caller's own
session** may currently set a new password without the current one: `true`
only for a few minutes after that session redeemed a magic link, and
cleared on use. `register`, `login` and `me/password` return the same body
shape, all three fields included (`password_step_up` is `false` on a fresh
register/login response).

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

A third way in, and the password-recovery path: a user who cannot log in
with a password requests a sign-in link and is signed in; an account with
**no** password (OAuth-created, or one that never set one) can then set one
from the account menu. There is deliberately **no** separate "reset password
by email" flow — it would be a second token type with its own timing-oracle,
rate-limit and scanner-safety surface, for a case the magic link already
covers (the trade-off is recorded in the ADR). An account that still *has*
a password can replace it **without the old one only from the session that
just redeemed the link**, within `CITEVYN_PASSWORD_STEP_UP_WINDOW_SECONDS`
(default 600) and once — the same-session step-up from #293 (PR 15).

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
`CITEVYN_MAGIC_LINK_TTL_SECONDS` (default 600; the email and the confirm
page quote that value in minutes). 422 `validation_error` for a malformed
address. `404` when no email provider is configured (no
`CITEVYN_RESEND_API_KEY` in production; locally the file outbox is used).

Rate-limited per TARGET EMAIL by **two independent buckets**, both applied
before the account lookup and on both branches:

* a **minimum interval** — 1 request per
  `CITEVYN_RATE_LIMIT_MAGIC_LINK_INTERVAL_SECONDS` (default 60), whose 429
  reads `A link was sent moments ago — check your inbox.` This is a FLOOR
  between consecutive requests. Without it five clicks in five seconds sent
  five emails, and because the route keeps one live token per user, the first
  four were dead links by the time they arrived (#301).
* an **hourly ceiling** — the dedicated bucket below.

They are separate buckets on purpose: a request refused by the interval must
not consume one of the hourly sends (both limiters record a hit only on the
success path), and draining one must not silence the other.

The interval is also the only role whose window differs from the
limiter-wide one. Every other role is an hourly count; expressing a 60-second
floor as "1 per hour" would be a lockout, not a cooldown.

Hourly ceiling, per TARGET EMAIL in a **dedicated** bucket
(`CITEVYN_RATE_LIMIT_MAGIC_LINK_PER_HOUR`, default 5) — never the
`auth_login` bucket, or flooding link requests at a victim's address would
lock them out of password login with no credentials at all. Applied on both
branches, so it is also the email-bombing ceiling per address — and, the
flip side, anyone can spend a victim's allowance for the window (recorded as
an accepted trade-off in the ADR, follow-up #294). Its 429
message is specific ("Too many sign-in links requested for this address").

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
page with no form (also for an over-long or otherwise unparseable `token`
— never a JSON 422). `Cache-Control: no-store`; the page sets
`<meta name="referrer" content="same-origin">` so the credential-bearing URL
never leaks as a cross-origin Referer while the form POST's `Origin` header
stays intact (`no-referrer` would make Chromium send `Origin: null`).

```http
POST /v1/auth/magic-link/confirm
Content-Type: application/x-www-form-urlencoded

token=<token_id>.<secret>
```

The claim, submitted by the button on the page above. Redirects (the only
JSON answer either confirm route gives is a `429` from the per-visitor
limiter): `302 /?auth=ok` on success (the same landing the OAuth login uses, with a
rotated `Set-Cookie` and the same claim-on-login of the browser's prior
anonymous history as every other login path), `302 /?auth=error` otherwise.
The new session is stamped `magic_link_verified_at` (the server-held fact
behind `password_step_up`), and the account is emailed a "New sign-in to
CiteVyn" notice with the recovery instruction (when a provider is
configured; the same per-address notice ceiling applies) — a stolen link becomes
visible to the inbox owner, who can request a link and set a password,
which revokes the intruder's session.
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
out client-side); if it has none, the field is ignored. **Step-up
(#293):** the requirement is waived when the caller's *own* session row
carries a `magic_link_verified_at` stamp younger than
`CITEVYN_PASSWORD_STEP_UP_WINDOW_SECONDS` — a second server-held fact, still
never the body; another live session of the same account, an older stamp,
or any other login method gets the normal 422. The stamp is cleared on use
(one shot), and the audit row carries `step_up: "magic_link"`. Every
successful set/change also emails the account ("Your CiteVyn password was
set/changed") with the recovery instruction — when an email provider is
configured, and at most `CITEVYN_RATE_LIMIT_MAGIC_LINK_PER_HOUR` notices
per address per hour (the change itself is never throttled by that).
Changes that supply `current_password` are capped per user at
`CITEVYN_RATE_LIMIT_PASSWORD_CHANGE_PER_HOUR` (default 3; 429
`rate_limited`); the stepped-up set is exempt. On success, 200
with the `me` body shape (`has_password: true`), **every other live
session for the account is revoked** — the caller's own session stays —
and any still-pending magic-link token is deleted, uniformly for a
first-time set and a change: the credential surface changed, so everywhere
else must re-authenticate. Passwords are 8–128 characters, as at
registration.

Audit trail: successes are `login` events with `metadata.event` one of
`magic_link_requested`, `magic_link`, `password_set`, `password_changed`
(the last two also carry `sessions_revoked`, and `step_up: "magic_link"`
when the waiver was used; `magic_link`/`password_*` carry
`notice_suppressed: true` when the per-address notice ceiling dropped the
email); failures are `auth_failed`
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

#### `answer` formatting contract (#303)

`answer` is text, not HTML, and the model is constrained by the system prompt
(`backend/app/llm/prompts.py`) to a deliberately tiny markdown subset:

| Allowed | Meaning |
|---|---|
| `**bold**` | emphasis |
| `` `code` `` | inline code, flags, file names |
| lines starting with `- ` | bullet list items |
| `[n]` | citation marker; `n` matches a `citations[].marker` |

Everything else — headings, tables, links, images, block quotes, code fences,
raw HTML — is **not** interpreted. Clients should render this subset only and
show anything else verbatim; the reference client
(`frontend/src/lib/answerFormat.ts`) parses to data and never builds an HTML
string, so unrecognised input reaches the DOM as text rather than markup.

Markers may be **gapped** (an answer can cite `[1]` and `[3]`), and a marker with
no matching `citations[]` entry may appear if a citation was dropped in
validation — render such a marker as the plain `[n]` text, never as a link.
Several citations may share one `url`; a client may collapse them into one source
card listing every marker it backs.

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

### Self-Referential Questions

A question addressed to the assistant in the second person ("who are you?",
"what can you do?", "what do you cover?", a bare "help") is a question **about
CiteVyn**, but it never says the word — so it used to route `domain:
"unsupported"` and come back as the off-domain refusal while the indexed
"About CiteVyn" source could answer it (#300).

The orchestrator now rewrites a small, **closed, whole-message-anchored** list of
such phrasings to the CiteVyn question each one means, before routing:

| Phrasing (whole message) | Rewritten to |
|---|---|
| "who are you", "what are you", "what's your name", "tell me about yourself", … | `What is CiteVyn?` |
| "what can you do", "what do you do", "what are you for", "how can you help", "help", … | `What can CiteVyn do?` |
| "what do you know", "what do you cover", "what can I ask you", … | `What does CiteVyn cover?` |

The response is then an ordinary grounded, cited answer with `domain:
"citevyn"` and `unsupported: false` — there is no new field, intent, or domain
value on the wire.

Two properties are load-bearing and tested:

* **Whole-message anchoring.** The phrase may be preceded only by a closed set
  of discourse openers (`hi`, `hey`, `hello`, `ok`, `okay`, `so`, `well`, `um`)
  and followed only by whitespace and sentence punctuation (`, . ! ?`). A listed
  phrasing carrying a substantive tail is a real product question and keeps its
  own routing — "who are the Codex maintainers?" stays `codex`, and
  "hey, what can you do with the Gemini API?" stays `gemini_api`. Apostrophes in
  `what's` / `who're` match the straight, curly and modifier forms alike, because
  dictation and phone keyboards emit the curly one.
* **The user's utterance is not rewritten.** The rewrite applies to the
  retrieval/generation query only; `GET /v1/sessions/{session_id}` still
  replays exactly what the user typed.

The rewrite runs **before** conversation memory, so a self-referential question
asked mid-session is answered about CiteVyn rather than inheriting the previous
turn's topic.

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

`vector_arm.status` is one of `empty`, `dead`, `mismatch`, `partial`, `healthy` or
`ambiguous`, decided in that precedence order. Every block also carries
`active_index_count` (#264).

`previous_good_index` names the **most recently demoted** index — the rollback
target for §13's promote API. More than one row can carry `previous_good` at once:
`promote_version` demotes every `active` row it finds and never clears the
`previous_good` rows already there, so after N promotions N-1 coexist. The route
orders by `promoted_at DESC NULLS LAST, index_version DESC` and returns the newest;
before #264 it returned whichever row the database happened to yield first, which
after a second promotion was the index demoted *longest ago*.

### `ambiguous` — more than one active index (#58/#264)

Nothing in the schema enforces a single `active` row, and a database really does
drift into two (seeding plus repeated local ingests will do it). When it happens
the read path **fails closed**: the provenance gate resolves to
`IndexStampStatus.ambiguous` and the vector arm is switched off (#226). The route
reports the same verdict rather than describing one arbitrarily-chosen row:

```json
{
  "request_id": "req_011",
  "status": "ready",
  "active_index": null,
  "previous_good_index": null,
  "vector_arm": {
    "status": "ambiguous",
    "healthy": false,
    "chunks_total": null,
    "chunks_embedded": null,
    "embedded_ratio": null,
    "embedder_match": false,
    "index_embedder": null,
    "configured_query_embedder": { "provider": "gemini", "model": "...", "dim": 1536 },
    "active_index_count": 2
  },
  "message": "2 index versions are marked active; promote one to converge."
}
```

- Top-level `status` stays `ready`. The API is still answering — retrieval falls
  back to the status-only document filter and the lexical arms still serve — and
  `vector_arm` is additive precisely so an operator-fixable data problem cannot
  pull a serving pod out of rotation. This is the same trade already made for
  `dead`.
- `active_index` is `null`. Naming one of the N rows would restate the coin flip
  at a second key, where a dashboard would read it as *the* answer.
- The three chunk counts are `null`, not `0`: measuring them would mean first
  picking one of the rows, which is the defect. `0` would read as `empty`/`dead`
  and claim something nobody checked.
- `embedder_match` is `false` because that is what
  `is_index_embedder_mismatch` returns for the ambiguous sentinel — the same
  predicate the vector arm gates on, not a second implementation of it (#71).
- Recovery is a promote: `POST /v1/admin/index_versions/{version}/promote`
  demotes every other `active` row.

`evaluation_run_id` names the newest **terminal** `EvaluationRun` for that index —
the run `citevyn-worker evaluate --index-version <v>` wrote (#216, #229). It means
the index **was evaluated**; it does **not** mean the index **passed**, because the
pointer follows a `failed` run too (otherwise "evaluated and failed" would be
indistinguishable from "never evaluated"). `null` means no evaluation has ever
completed for that index. For the verdict, read the run through
`GET /v1/admin/evaluations/{run_id}`. The promotion gate in §13 does not consult
this field; it re-derives the measurement from `evaluation_runs` on every promote.

## 14a. About page (`GET /about`)

```http
GET /about
```

Returns `200 text/html` — not JSON. This is the one HTML page the API serves in
its own right, and it exists because it is a **citation target**: the
`citevyn` and `concepts` sources in `backend/app/worker/allowlist.py` both carry
`source_url: "/about"`, so every answer grounded in them ships
`citations[].url = "/about"` (§6) and the browser renders it as a real link.

The page is rendered from those source documents themselves, so the link
resolves to the text the answer was drawn from rather than a paraphrase of it.
Adding a source with `source_url: "/about"` puts it on the page automatically.

Notes:

- Public and unauthenticated; no query parameters; no request body.
- Registered **before** the SPA's `StaticFiles` mount at `/`, which is a
  catch-all. Registered after it, the mount answers `/about` with a `307` to
  `/about/` and the page never renders.
- `/about/` (trailing slash) is **not** served — the mount suppresses
  Starlette's `redirect_slashes`, so it returns the standard §15 `not_found`
  envelope. No citation emits that form.
- Does not depend on the browser bundle: the corpus markdown ships in the API
  image, so the page renders even when `frontend_dist` is absent (its
  stylesheet, served from the bundle, is the part that would be missing).

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
