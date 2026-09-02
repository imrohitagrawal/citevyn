# ADR-0004: Real User Accounts (Login)

## Status

Accepted. Owner-approved after an explicit "does login carry any benefit?"
review recommended doing less — the owner overrode that with **full login**
(accounts, email+password, plus GitHub OAuth). This ADR records the decision
and the design that follows from it. Implementation ships as an ordered PR
sequence (see "PR sequence" below); this ADR is PR 0, the approval gate for
everything after it.

## Date

2026-08-31

## Context

CiteVyn is a citation-grounded RAG Q&A demo, live and public at
https://citevyn.stackclimb.com. Authentication already exists — it is not
*login*. `backend/app/core/security.py` ships two static shared secrets: a
demo bearer token gating every `/v1/*` route, which resolves to the
**constant** `DEMO_USER_ID = "demo_user"` for every caller on earth, and an
`X-Admin-API-Key` gating the seven `/v1/admin/*` routes. The demo bearer is
compiled into the browser bundle, so it is public by construction — an
anti-scraping speed bump, not access control.

**The gap is identity, not authentication.**

Two arguments that would normally justify adding login do **not** apply here
and must not be used to defend this work: per-visitor fairness is already
solved (#203, PR #211 — salted-HMAC-of-IP rate-limit buckets), and spend is
already capped by `docs/COST_CONTROLS.md` Layer 3 ($2/day hard limit). #197
was closed won't-build with exactly that reasoning, and this ADR does not
reopen it.

What genuinely motivates the change:

- **The Pro tier is currently dishonest.** The landing page sells Pro at
  $12/mo with "Saved history & shareable answers" — unbuildable without
  accounts — on the same page as the CTA footnote "NO ACCOUNT · NO SETUP".
- **Chat history dies on refresh.** The session id lives only in a client
  `useRef`. The backend already persists `sessions` and `messages`; the
  client just cannot find its way back to them.
- **Admin actions have no actor.** Every promote and force-override is
  stamped `user_id="admin"`, so the audit trail cannot say *who* did it, in a
  product whose pitch is provenance. `AuditAction.login` already exists in
  the enum and is emitted by nothing today.
- **Portfolio signal.** Doing account auth correctly is a stronger
  demonstration than a login box bolted onto a static site.

### The constraint that shapes the whole sequence

`backend/app/api/routers/sessions.py:231,260` each contain
`del user_id  # auth-only; ownership check is a Slice 8 concern`, and
`backend/app/api/routers/messages.py:150,197` bind `_user_id` and discard it.
Four routes take the caller's identity and throw it away. That is harmless
today because there is exactly one principal (`demo_user`). **It becomes a
live IDOR the moment a second principal exists** — any user reads or closes
any other user's transcript by guessing a UUID4 — and because the affected
rows persist, a login-before-ownership rollout leaves every row written in
the gap readable forever. It is also an exfiltration primitive, not just a
read leak: `orchestrator.py` feeds `recent_user_questions` into
`condense_question_llm`, so an attacker who can post into a victim's session
gets the victim's prior questions concatenated into a prompt whose answer the
attacker reads.

**Therefore the ownership fix (PR 1) ships first, before any code path can
create a second principal.** No later PR in the sequence is authorized to
land ahead of it.

## Decision

Ship full login as an ordered sequence of independently-mergeable PRs (below),
gated by this ADR. Four architectural decisions anchor the whole sequence:

### 1. One principal, one predicate

Every request resolves to exactly one **principal id**, logged in or not.
Anonymous visitors get a durable pseudonymous principal `anon_<uuid4hex>` — a
real `users` row — minted transparently on `POST /v1/sessions` and carried in
the same cookie the login flow uses; they never see a login screen. Registered
users are `usr_<uuid4hex>`. `user_id` stays the opaque primary key — email is
never the key, or a later email change becomes a multi-table rewrite.

Ownership becomes one SQL clause pushed into the two existing chokepoints all
four affected routes already flow through (`_get_session_or_404`,
`_require_session`):
`.where(Session.session_id == id, Session.user_id == principal_id, Session.expires_at > now())`.
A mismatch returns **404**, byte-identical to a genuine miss, never 403 — 403
would confirm the id is real, a membership oracle over the UUID space. Admin
gets no implicit ownership bypass; the admin key stays a separate, unrelated
control plane.

Rejected: a two-arm predicate (anonymous visitors with no `users` row, to
avoid table growth). The storage saving does not hold — both designs already
create a per-visitor row in `auth_sessions`, and a `users` row is ~60 bytes,
so 100k anonymous visitors costs ~6 MB against Neon's 0.5 GB free tier. A
single code path is worth far more than that saving, because a two-branch
ownership check is the same *shape* as the `del user_id` bug this ADR exists
to fix.

### 2. Opaque server-side session token, not JWT

Postgres-backed session id in an `HttpOnly` cookie
(`__Host-citevyn_session` in production). Revocation — logout-everywhere,
password change — needs server state; once state is stored, a JWT buys
nothing and costs revocation. Not Redis-backed: the rate limiter already
fails **closed** on a Redis error, so a Redis-only session store would mean
an Upstash blip logs every visitor out.

### 3. Argon2id, bounded by a semaphore, not just tuned parameters

`argon2-cffi`, OWASP parameters `m=19456 KiB, t=2, p=1` (`p=1` because the
production machine is `shared-cpu-1x`, one vCPU). Tuning alone does not bound
the worst case: at Fly's connection `hard_limit = 40`, even 19 MiB hashes
running concurrently exceed the 512 MB machine. The actual control is a
module-level `asyncio.Semaphore(2)` acquired before dispatching to the
threadpool, mirroring the existing `cost/admission.py` singleton idiom.
Unknown emails verify against a dummy hash inside the same semaphore, or
response timing becomes a free account-enumeration oracle.

### 4. No frontend router, no auth library

Auth ships as a lazy-loaded modal; history as a lazy-loaded drawer.
`react-router-dom` would cost ~11 kB gzip for one modal and force an SPA
catch-all route, which the current static-file mount (`StaticFiles(html=True)`
at `/`, 404 on unknown paths) does not have — a production routing change this
ADR does not authorize. OAuth returns to a **backend** callback that sets the
cookie and redirects to `/?auth=ok`. The existing build-time demo bearer is
kept and paired with `credentials: "include"`; it doubles as a CSRF guard,
since a cross-site form POST cannot set an `Authorization` header.

## Deliberate trade-offs, recorded not hidden

- **Password reset is out of v1** — *superseded by PR 14, recorded here
  rather than rewritten.* The original reasoning: not a cost problem — the
  blocker was a new external email dependency with no SPF/DKIM/DMARC on
  `stackclimb.com`, and password-reset flows being one of the richest
  sources of real auth CVEs. GitHub OAuth was the recovery path instead,
  and PR 13 made that path actually work (linking). The owner then hit the
  gap as a real user — recovery only works if you pre-linked a provider —
  and **PR 14 reopens this trade-off deliberately (2026-09-02):**
  - **Magic-link login, not a reset-by-email flow.** Once "email me a
    sign-in link" exists, a separate unauthenticated reset flow is a second
    token type with its own timing-oracle, rate-limit and scanner-safety
    surface for a case the first mechanism already covers. Recovery is
    therefore: request a link → sign in → set a password from the
    account menu (`POST /v1/auth/me/password`, authenticated, no email
    token). A one-time, dismissible nudge after a passwordless sign-in
    points at that action; it is never forced at that moment. **Known
    limit, found by review:** that last step is complete only for an
    account with *no* password — an account that still has one must
    supply it to change it (the requirement is decided from the stored
    hash, never from the body). **Closed by PR 15 (#293, owner-approved
    2026-09-02):** the magic-link claim stamps its new session
    (`auth_sessions.magic_link_verified_at`, migration 0013) and the
    password route waives the current password only for *that* session,
    within 10 minutes, once — a second server-held fact, never the body,
    never another session of the same account. Guardrail 2: every
    magic-link sign-in and every password set/change emails the account
    with the recovery instruction, so a stolen link or a hijacked session
    is a race the inbox owner can see and win (request a link, set a
    password, which revokes every other session). What a stolen link
    yields is therefore unchanged in kind — it already granted a full
    session — and symmetric: the inbox owner always regains exclusivity.
  - **The CVE-class risks the original decision named are addressed by
    construction, not by hope:** `request` is always-202 with equal-cost
    branches (statement-count parity, mirroring `verify_password_or_dummy`);
    `GET confirm` renders a form and consumes nothing (mail scanners
    prefetch every link); only a real `POST` claims, atomically and
    conditionally on both `token_id` and `secret_hash` (a guess cannot burn
    the real link); a dedicated rate bucket, never `auth_login` (a flood
    must not lock the victim out of password login); the link origin comes
    from config (`CITEVYN_MAGIC_LINK_BASE_URL`), never the `Host` header
    (reset-link poisoning); a present `Origin`/`Sec-Fetch-Site` on the
    claim must be same-origin (login CSRF); any password set/change revokes
    the account's other sessions; whether `current_password` is required is
    decided from the stored `password_hash`, never from body-field presence
    (a hijacked session must not be able to set a password with zero proof
    of the old one).
  - **The email dependency is real now:** Resend, behind a `Protocol`
    seam (`app.core.email_client`), with a dev-only file outbox so the
    flow is verifiable with no provider account. **Residuals, accepted:**
    the sending domain must be verified in Resend (SPF/DKIM/DMARC on
    `stackclimb.com`) before this works in production — until then the
    request route 404s and the UI says email sign-in is unavailable; the
    link (10 minutes by default) is a full-session bearer credential, so anyone who can
    read the recipient's inbox in that window can sign in (that is the
    nature of magic links, and the same bound every email-based recovery
    flow has); links are deliberately NOT bound to the requesting browser,
    so cross-device use works and there is no session-binding defence;
    the per-address `magic_link` rate bucket that bounds email-bombing
    also lets anyone spend a victim's allowance for the window with no
    credentials (5/hour by default) — accepted, follow-up #294.
- **Registration leaks email existence** (a 422 "already registered" on
  signup). The always-202 alternative needs an email provider this ADR
  deliberately does not add. Accepted and recorded here and in §3.2 below,
  not silently shipped.
- **Pro does not become real.** "Saved history" moves down into Free, where
  login actually delivers it; Pro is marked `planned` and its CTA becomes
  "Notify me" rather than a non-functional checkout.
- **Account linking requires a fresh session, not step-up re-auth (PR 13).**
  `GET /v1/auth/oauth/{provider}/connect/start` is refused unless the
  caller's `AuthSession` was *created* within
  `oauth_connect_max_session_age_seconds` (default 20 minutes). Why: a
  session's `created_at` never refreshes and sessions live 180 days, so
  without this a stolen cookie could link the thief's own GitHub/Google
  identity to the victim's account at any point in that window — turning a
  temporary compromise into a permanent backdoor that survives logout and a
  later password change. The gate costs no new auth primitive. **Residual,
  accepted:** a cookie stolen *inside* the freshness window, right after a
  genuine login, is not caught; closing that needs true step-up
  re-authentication scoped to the connect action, which is out of scope.
- **Linking never reassigns, never creates, never rotates (PR 13).** An
  external identity already linked to a different account is rejected
  (`LinkResult.LINKED_ELSEWHERE`), never moved — including under a
  concurrent-insert race, where the loser compares the winning row's owner
  against its *own* target rather than reporting success because a row now
  exists. Linking writes one `user_identities` row and nothing else: no
  `users` row, no change to `users.email`, no session rotation.
- **One identity per provider per account is not yet enforced.** The unique
  constraint is on the external identity `(provider, provider_account_id)`
  only, so one account can link two different GitHub accounts; `/me` reports
  `providers` as a de-duplicated set. Whether to add a
  `(user_id, provider)` constraint (and a matching `LinkResult`) is an open
  question to settle **before** a disconnect feature, whose
  delete-by-`(user_id, provider)` would otherwise be ambiguous.
- **Disconnect invariant — recorded now, built later.** A future "disconnect
  a provider" feature **must** keep every account with at least one access
  method: `password_hash` set **or** at least one remaining `user_identities`
  row. Removing the last one without first requiring a password to be set
  must be refused. Not built in PR 13; must not be silently designed away.

## PR sequence

Each PR is independently mergeable with its own RED→GREEN proof in the body.
**The order is a security property, not a preference** — PR 1 (ownership)
must land before PR 6 (the first PR that can create a second principal).

| # | Scope |
|---|---|
| 0 | This ADR + PRD/RELEASE_PLAN/SECURITY_MODEL amendments + BACKLOG row (this PR) |
| 1 | Ownership + expiry predicate on the 4 affected routes; one-head migration guard test |
| 2 | Security headers middleware; disable `/docs`,`/redoc`,`/openapi.json` in production; move health-detail routes behind admin |
| 3 | Migration 0007 `auth_sessions` + anonymous cookie issuance. Still no login |
| 4 | `app/core/passwords.py` (Argon2id + semaphore). No routes |
| 5 | Migration 0008 `users` identity columns; `sessions` FK RESTRICT → CASCADE |
| 6 | `/v1/auth/{register,login,logout,me}` + claim-on-login + auth rate limiters + `AuditAction.login` emission |
| 7 | Frontend: honest copy (footnote, Pro `planned`, Free gains saved-history) |
| 8 | Frontend: `authStore`/`useAuth`/401 interceptor; lazy `AuthModal`/`AccountMenu` |
| 9 | Session claim wired client-side |
| 10 | `GET /v1/me/sessions` + history drawer + resume (needs citation hydration) |
| 11 | Per-user rate tiers (authenticated key on `user_id`, anonymous keeps IP HMAC) |
| 12 | GitHub OAuth (`user_identities` table) |
| 13 | Account linking: connect GitHub/Google to an existing signed-in account (`connect/start`, freshness gate, `providers` on `/me`) — the working form of the recovery path above |
| 15 | Same-session password step-up after a magic-link sign-in (`auth_sessions.magic_link_verified_at`, migration 0013; `password_step_up` on `/me`; one shot, 10-minute window) + sign-in and password-change notification emails — closes #293 |
| 14 | Magic-link login (`magic_link_tokens`, migration 0012; `POST …/magic-link/request` always-202 equal-cost, `GET …/confirm` interstitial, `POST …/confirm` atomic claim) + Resend email seam + authenticated `POST /v1/auth/me/password` (server-decided `current_password`, revokes other sessions) + `has_password` on `/me` — the recovery path that needs no pre-linked provider |

PR 5 is a one-way door: after PR 6 creates real accounts, `downgrade 0008`
destroys them. PR 5 must be verified in production before PR 6 ships.
Feedback attribution (#154) and share permalinks are genuinely unlocked by
this work but stay out of its scope.

## Consequences

- **PRD §7 "Non-Goals for MVP"** and **RELEASE_PLAN §4 "MVP Non-Goals"**
  listed Enterprise RBAC and tenant isolation as non-goals; both are amended
  in this PR to distinguish those (still out of scope) from single-tenant
  personal-account login (now in scope). No enterprise SSO/RBAC/tenancy is
  implied or added by this ADR.
- **SECURITY_MODEL §4 "MVP Authentication"** is amended to describe the real
  principal-resolution model (anonymous pseudonymous + registered accounts)
  in place of the placeholder "Demo login or demo bearer token" line.
  **§14 "MVP Security Limitations"** is amended to keep SSO/Enterprise
  RBAC/tenant isolation as out of scope while noting personal-account login
  is now in scope — the two are not the same thing.
- **SECURITY_MODEL §3.2 "Threats"** gets a new row: storing emails and chat
  history under a real account is personal data, and the ownership IDOR this
  ADR exists to close is now a named threat, not an implicit gap.
- **Storing emails and chat history is personal data**, which the prior
  threat model did not contemplate. §3.2 is amended, not just appended to.
- **Cost impact is $0/day** — no new external service, ~200 B/row against
  Neon's 0.5 GB free tier, and Redis command volume grows only with login
  traffic, which the existing $2/day cap already bounds.

## Non-negotiables carried into every PR in the sequence

1. Anonymous access keeps working, frictionlessly. Login is optional and
   only unlocks saved history — no wall in front of the demo.
2. Stays within the 512 MB single-machine, scale-to-zero, Neon free, Upstash
   free, $2/day-cap shape already in production.
3. Bundle gate: `index.js` baseline 189.92 kB / 60.43 kB gzip must not
   regress past the budget set in the PR that adds each frontend chunk.
4. Backend and frontend suites, and hermetic `provider=stub` mode, stay
   green throughout.
