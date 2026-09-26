# ADR-0005: One Access Model

## Status

Accepted. Every product decision below was locked by the owner on 2026-09-25
(issue #488). This ADR records them and the design that follows. It amends
ADR-0004 on one point: what the public client token is for (see "The public
token" below). It does not rewrite ADR-0004.

Everything commercial ships **dark**, behind one setting,
`CITEVYN_ACCESS_MODEL_ENABLED`, which defaults to `false`. With it false,
production behaves exactly as it did before this ADR, including anonymous chat. A
test runs today's public flows with the setting false to prove it. Only the owner
turns it on.

## Date

2026-09-25

## Context

ADR-0004 added real accounts, but it was built for identity, not for paid
access. Anonymous chat stayed open on purpose. Since then, the documents that
describe access have contradicted each other, and that has caused repeated
confusion:

- `docs/SECURITY_MODEL.md` §4 item 4 said "Anonymous access stays enabled by
  design", while its §6 rate-limit table said "anonymous: disabled".
- ADR-0004 called the public client token "an anti-scraping speed bump, not
  access control". `SECURITY_MODEL.md` and `app/core/security.py` call it "a CSRF
  guard".
- Nothing in the code records which routes are meant to be free and which paid.

The owner has now decided to sell a Pro tier. That needs one written model of
who may do what, and why, before any of it is built.

### Why Pro is sold on trust, not on access

All four CiteVyn sources already publish FREE official documentation MCP servers
(an MCP server lets an AI agent query a service). Each was checked by handshake on
2026-09-25:

| Source | Free MCP server |
|---|---|
| Claude API | `platform.claude.com/docs/mcp` ("anthropic-docs") |
| Claude Code | `code.claude.com/docs/mcp` ("Claude Code Docs") |
| OpenAI | `developers.openai.com/mcp` |
| Gemini API | `gemini-api-docs-mcp.dev` |

Context7 also gives 1,000 free calls a month. So nobody will pay CiteVyn for
*access* to these docs. What they cannot get elsewhere is a set of features that make an answer
trustworthy:

- cited answers that span vendors;
- a refusal when the docs do not support an answer;
- alerts when the docs change;
- exact flag and command lookup;
- a visible "docs as of" date on every answer.

Developers' top frustration with AI answers is that they are "almost right"
(66%, Stack Overflow Developer Survey 2025). That is the problem Pro is sold on.

### Cost and market (inputs to the price, recorded so it is not reopened)

- **Cost per answer.** Measured at $0.00034 on average in production (48
  requests). At current Gemini Flash list prices it is probably $0.001–0.0022
  (see #492: the meter priced the alias at the 2.5 Flash rate, likely about 4x
  low; the alias's exact target was never verified). So 1,000 answers cost
  about $1–2.20.
- **Stripe's cut of $9** is about $0.56–0.84 (a US card vs a foreign card,
  including Billing and Tax).
- **General assistants** cost $20 a month (ChatGPT Plus, Claude Pro, Perplexity
  Pro, Gemini AI Pro). A budget tier sits at $5–8 (ChatGPT Go $8, Kagi $5–10).
- **Ref.tools** launched at $9 for 1,000 answers with 200 free credits. It later
  moved new users to $50 for 6,000 (the reason is not known).
- **No product sells a cheaper BYOK plan.** BYOK ("bring your own key": the user's
  own model-provider account pays for generation) is a same-price feature
  elsewhere (Cursor, Raycast).
- **What competitors do not offer:** "refusals are free" (no competitor states
  it), published hard counts, and "what changed" alerts for readers (no docs Q&A
  tool offers them).

## Decision

### 1. Plans

| | Anonymous visitor | Free account | Pro |
|---|---|---|---|
| Price | — | $0 | **$9/month or $91/year** |
| Live questions | **None** when the setting is on. They see the existing canned sample. | **25 answered questions, once**, per verified account | **1,000 answered questions a month**, plus the existing per-hour limit |
| What counts | — | Only answered questions. Refusals and errors never count. | Same |
| MCP answers | — | Share the 25 | Share the 1,000 |
| "What changed" alerts | — | Weekly digest | Real-time watch alerts (a flag, model, command or page) |
| Pro features | — | — | MCP server, API keys, shareable answers, usage insights, BYOK |
| Team | — | — | "Coming soon" on the pricing page; not built |

- The 25 and the 1,000 are **policy settings**, not constants in code.
- The price IDs are settings. Stripe runs in **test mode** until the owner sets
  live keys.
- The **global daily spend cap stays** (`docs/COST_CONTROLS.md` Layer 3). A plan
  allowance never lifts it.
- **"Verified account"** (a design choice made in this ADR, not by the owner)
  means the account has proved it controls its email
  address: it redeemed a magic link, or it signed in with an OAuth provider that
  reported a verified email. Password sign-up does not verify an address today
  (`SECURITY_MODEL.md` §6, `email_notice`), so the `users` table gets an
  `email_verified_at` column (Phase 4). The trial is granted once per verified
  account; it is never reset without a new verified account.
  **As built (Phase 4B):** the stamp goes only on the account that proved the
  address, never on another account found by email (OAuth never links by email).
  Google's `email_verified` must be the boolean `true`. A GitHub public email
  counts only if GitHub's `/user/emails` lists it as verified: `/user` carries no
  verified flag; if that call fails, the user still signs in, unverified. Only
  an ASCII provider address can verify (Python lower-cases look-alikes such as
  the Kelvin sign to ASCII letters). The first stamp is kept. `email` has no
  change path today; one added later must clear the stamp in the same write.
  **Known limit (by design, for the owner):** plus-addressing (`me+1@`), Gmail
  dots and catch-all domains each give a separate verified address, so one
  person can hold several trials. "Once per verified account" allows that; the
  bot check at sign-up (Phase 5) and the global spend cap bound the cost.

### 2. Six concerns, one mechanism each

The access model is six separate questions. Each has exactly one mechanism. Mixing
two of them into one check is how the previous contradictions happened.

| Concern | Question it answers | The one mechanism |
|---|---|---|
| **Authentication** | Who is this? | The session cookie (ADR-0004). |
| **Entitlement** | What have they paid for? | Our `memberships` table, written only by signed Stripe webhooks. |
| **Authorisation** | May this caller use this route? | A capability declared on every route, plus ONE policy table saying which tier has which capability. |
| **Quota** | How much may they still use? | Per-user and per-plan metering on `provider_calls`, which records who paid. |
| **CSRF** (cross-site request forgery: another site making the browser send a request) | Did this request come from our own page? | `SameSite` cookie + an `Origin` check + a fixed request header. |
| **Abuse** | Is this a flood or a bot? | Per-IP limits on the routes anyone can call without signing in (sign-up, sign-in, the sample), email verification, and a bot check at sign-up. |

#### Authorisation: capabilities and one policy table

A capability is one named thing a caller may do. Every route declares the
capability it needs. The names below are this ADR's proposal and may be refined in
Phase 1, for example `ask`, `mcp_ask`,
`api_keys`, `share_answer`, `usage_insights`, `watch_alerts`, `byok`, `admin`, or
`public` for a route that needs nothing. One table maps each tier (anonymous,
free, pro, and later team; a tier is the kind of account a caller has) to the capabilities it has and the quota for each.
A route with no declared capability fails CI. The table in the documentation is
checked against the table in the code by a test, so the two cannot drift.

The route declares **what** it needs. The table says **who** has it. No route
contains an `if tier == "pro"` check.

#### Entitlement: never ask Stripe mid-request

A request never waits on Stripe. Stripe tells us about changes through webhooks.
We verify each webhook's signature and store the result in `memberships`. Every
protected request reads that table. So an outage at Stripe can delay a new
subscription, but it can never take the product down or grant access nobody paid
for.

- Webhooks are **signed** (a forged one is rejected), **idempotent** (the same
  event twice changes nothing the second time) and **replay-safe** (an old event
  cannot overwrite newer state).
- **Cancel** takes effect at the end of the paid period.
- A **failed payment** gets a **7-day grace period**, then the account returns
  to the free-account tier. It keeps its history; it loses Pro capabilities.
- Every membership change writes an audit event. **As built (Phase 4A):** the
  `stripe_events` table is that audit log: every webhook event id, once, with its
  type, account and outcome (`applied`, `applied_account_mismatch`, `unmatched`, `ignored`). Adding
  `AuditAction` members would need `ALTER TYPE` on a native enum, which AGENTS.md
  steers away from.
- **As built (Phase 4A):** Stripe is called over HTTPS with `httpx` (already a
  dependency), not the Stripe SDK: three small calls and a documented HMAC do not
  justify a new dependency, and `httpx.MockTransport` keeps every test offline.
- **As built, after review: a webhook event is a signal, not the truth.** Stripe's
  docs say not to use an event's `created` time to order events, and to "use the
  API to retrieve any missing objects". The first build did order by `created`;
  review reproduced a paying user dropping to Free on a same-second reorder, and a
  stale grace leaving a later failure with 0 days. So each subscription or invoice
  event re-reads the subscription from Stripe and stores what it says. This call
  is on the webhook path only: no user request waits on Stripe, which is the rule
  above. A Stripe outage makes the webhook answer 503, nothing is logged, and
  Stripe's retry applies the event later.
- The grace starts the first time the subscription is seen `past_due` and clears
  whenever it is not.
- **One row per subscription** (a second review round): an account can hold two
  subscriptions (two checkout tabs); one row per ACCOUNT let the wrong one decide
  and left a paying customer on Free. Each subscription has its own row and grace;
  the account is Pro if any is paid; the membership view lists them all, so paying
  twice is visible. Handlers for one account are serialised by a row lock on the
  user, and the subscription is read again inside the lock.
- **Bound once, then the status follows Stripe** (the third review round; the
  owner approved this design on 2026-09-26). v3 read the account from subscription
  metadata on every event. Metadata can be edited, and review showed a cancelled
  subscription whose metadata had been cleared staying Pro forever, and a moved
  one leaving the payer on Free. Now a subscription is bound to an account once,
  when its row is created, from the metadata our checkout wrote, and only if that
  account exists. After that the row is found by subscription id and always takes
  Stripe's status. A metadata mismatch is recorded as `applied_account_mismatch`
  and logged; moving a subscription to another account is a manual support step.
  The view and the portal describe the paid subscription that is not ending.

**Owner checklist for Stripe (test mode first), before turning the model on:**
1. Create the product "Pro" with a monthly ($9) and a yearly ($91) recurring price;
   set `CITEVYN_STRIPE_PRICE_PRO_MONTHLY` and `CITEVYN_STRIPE_PRICE_PRO_YEARLY`.
2. Create a webhook endpoint at `https://<site>/v1/billing/webhook` for
   `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.paid` and `invoice.payment_failed`;
   set `CITEVYN_STRIPE_WEBHOOK_SECRET` to its signing secret. Register it only once
   the model is on: while it is off the route answers 404, and Stripe gives up on
   an event after about three days of retries.
3. Customer portal: allow cancelling at the end of the billing period, updating the
   payment method and viewing invoices; set a business name; save.
4. Failed payments: set the retry schedule and what happens after the last retry
   (`canceled` or `unpaid`); the 7-day grace runs from the first failure.
5. Set `CITEVYN_STRIPE_SECRET_KEY` (a server-side secret; never in the browser).
   A restricted key needs **Subscriptions: read**, **Checkout Sessions: write** and
   **Customer portal: write**. Production refuses to start with the model on and
   any of these unset. A webhook that cannot read a subscription answers 503 and
   logs `billing_webhook_stripe_read_failed`; watch for it.

#### Quota: count answers, record who paid

`provider_calls` records model calls, not answers, and has no user column today.
So metering needs three additions: a user id on each call, a "who paid" field
(the platform, or the user through BYOK), and the outcome of each question
(answered, refused or failed), which comes from the message it belongs to. The
quota counts **answered** questions per user per period. A refusal or an error
never uses allowance. BYOK answers are recorded as user-paid, so they do not
consume the platform allowance. MCP answers count against the same allowance as
chat answers.

**As built (Phase 4B-1):** `provider_calls` now has `user_id` and `paid_by`, set
from a `billed_to` context the answer route opens around the whole answer, so the
answer, condense, alias-check and query-embedding calls are all attributed. The
allowance itself (Phase 4B-2) counts ANSWERS, not model calls: a cache hit is a
cited answer that makes no model call, and a refusal can make several.

**As built (Phase 4B-2):** the allowance is counted from `answer_usage` (migration
0018), one row per answered question, written with the answer in one transaction.
This departs from "metering on `provider_calls`" above, for the reason just given.
A Pro "month" is the calendar month in UTC, not the Stripe billing period: yearly
plans have no monthly period, and the count must not depend on webhook freshness.
Free counts every answer ever; a lapsed Pro account has usually already used its
25. Each row records its tier, and Pro counts only Pro rows, so upgrading
mid-month gives the whole Pro allowance (found in review). See `docs/ACCESS_POLICY.md` for the error each case returns and the small
overshoot two simultaneous questions can cause.

**As built (Phase 5A): the sign-up bot check is a self-hosted proof-of-work, not
a vendor.** This departs from "a bot-check provider" as a new dependency. A
vendor widget (Turnstile, hCaptcha) needs a third-party script and frame, which
reopens the `'self'`-only Content-Security-Policy that #365 closed, and sends
every visitor's data to the vendor. The proof-of-work is the ALTCHA scheme in
about 60 lines of `hashlib` and `hmac` (`app/core/pow.py`): the server signs a
challenge, the browser searches for the number behind it (about 100,000 SHA-256
hashes by default), and the server checks the signature, the work and the expiry,
then records the solution so it pays for exactly one attempt, successful or not.
It guards registration and
magic-link requests (which send email); OAuth is not guarded. It raises the cost
of scripting sign-ups; it does not prove a human. If abuse shows it is too weak,
a vendor can replace it behind the same header without changing the routes.
`GET /v1/config` tells the web client whether the access model is on.

**As built (Phase 5C): draft legal pages.** `GET /legal/{terms,privacy,refunds,no-training}`
serves the drafts from `backend/app/legal/*.md`, rendered like `/about`, with a
visible "DRAFT, not in effect" notice and `X-Robots-Tag: noindex`, and a 404
while the access model is off. They are linked only from the model-on pricing
section. Every fact in them was checked against the code; every decision or
fact that is the owner's to give is marked `[OWNER: ...]` (entity, contact,
jurisdiction, refund terms, retention periods, provider data terms). The
no-training page stays a draft until the Gemini key's tier (#492) and the
OpenRouter data policy are confirmed. Drafting the refund page found that a
paying user had no way to reach the billing portal to cancel, so the usage
meter now offers Pro accounts "Manage billing".

**As built (Phase 6B): the MCP server.** `POST /v1/mcp`, written without an MCP
library (five JSON-RPC methods over one POST did not justify a dependency). An
API key authenticates it; a browser cookie never does, so it cannot be driven
cross-site. The key owner's plan, allowance and hourly limit are re-read on
every call, because a key proves identity, not the plan. Output cleaning
(`app/mcp/clean.py`, reworked over two review rounds): after NFKC
normalisation it removes what hides from a reader (control, format, bidi,
zero-width, tag, unassigned and default-ignorable characters), removes HTML and
markdown images and links to a fixed point (links keep their words), replaces
every URL with a scheme and every `//host` or `www.` host with `[link removed]`,
caps length, and frames the answer and its sources with a per-request random
marker plus a "reference material, not instructions" label. URLs reach the agent
only in the validated structured citations. Known limits: a bare domain with no
scheme (`evil.example/x`) is not removed, and a persuasive sentence in plain
words cannot be recognised; the label and the frame are the defence there. The route inventory gained a third credential
class, API key, with an executed 401 check.

#### CSRF

Browser requests are protected by three things together: the session cookie is
`SameSite=Lax`, the server checks the `Origin` header against the site's own
origin, and state-changing requests must carry a fixed custom header. A cross-site
page cannot set a custom header without first asking the server's permission (a
"CORS preflight"), and our CORS policy gives that permission only to our own
origin.

### 3. The public token is retired

The build-time public client token does three jobs today, and was described two
ways. None of them is a reason to keep it:

- As a **CSRF guard**, it works only because a cross-site request cannot set an
  `Authorization` header. A fixed custom header does the same job without
  pretending to be a secret.
- As an **anti-scraping speed bump**, it stops nothing: it is compiled into the
  public JavaScript bundle, so anyone can read it.
- As a **rate-limit turnstile**: every chat request passes through the token
  check and the rate limiter together. The limiter does not need the token to
  run; it moves onto the new check unchanged.
- As a **hashing salt**: when `CITEVYN_RATE_LIMIT_KEY_SALT` is empty, the rate
  limiter hashes client IPs and email addresses with the token as the salt
  (`app/core/rate_limit.py`). Removing the token without setting a real salt
  would leave those hashes unsalted, and an unsalted hash of an IPv4 address can
  be reversed by trying all 2^32 addresses.

It also costs something. It has to be passed as a build argument, it has leaked
into a deploy log (#489; #416 showed the same mechanism with a test value), and it
needs its own bundle check
(`check_bundle_key.sh`). It is retired in two steps, because the frontend and the
backend deploy together but browsers cache old bundles:

1. **Step 1:** the backend accepts EITHER the old token OR the new fixed header
   plus a valid `Origin`. The frontend sends the new header. Deploy.
2. **Step 2** (only after step 1 is live): the owner first sets
   `CITEVYN_RATE_LIMIT_KEY_SALT` to a new strong secret, and production refuses to
   start without it. Then remove the token, its Fly secret, the build argument and
   `check_bundle_key.sh`. #489 closes here. (Changing the salt resets every rate
   bucket once, which is harmless.)

   **Step 2 checklist**, found while reviewing step 1 (Phase 2, 2026-09-26):
   - `citevyn.fly.dev` also serves the site (checked: `/health` answers 200), but
     it is not an allowed origin. Once the token is gone, a visitor on that
     hostname gets 401. Redirect it to `citevyn.stackclimb.com`, or add it to
     `CITEVYN_CORS_ALLOWED_ORIGINS`.
   - `infra/docker/scripts/deploy_verify.sh` (`curl_demo`) sends the bearer; move
     it to the header.
   - The backend tests that send the bearer move to the header, and
     `require_public_client_token` is renamed with its messages.

This amends ADR-0004's "anti-scraping speed bump" line. ADR-0004 is not rewritten;
it carries a note pointing here.

### 4. Pro features

- **MCP server** that returns finished, cited answers (not raw doc pages, which
  the free vendor servers already give). It shares the plan's allowance. Its
  output is **cleaned** so documentation text cannot carry instructions into the
  calling agent. (Context7's "ContextCrush" incident, February 2026, showed doc
  content injecting instructions into agents.)
- **Per-user API keys**: shown once, stored only as a hash, scoped, revocable.
- **Shareable answers**: a frozen copy of the answer, its citations and the crawl
  date; revocable.
- **Usage insights**: a page showing the user's own use against their allowance.
- **Install snippets** for Claude Code and Codex.

### 5. Differentiators: "what changed" alerts and cross-vendor comparisons

**Cross-vendor comparison answers.** A question such as "how do Claude and Gemini
each set a system prompt?" gets one answer that cites both vendors' docs side by
side. Retrieval already spans all four sources; this makes comparison a supported
answer shape, with citations from each vendor it names. It is built with the Pro
features (Phase 6).

**"What changed" alerts.** A scheduled job snapshots the four doc sets and compares them with the last
snapshot. Free accounts get a weekly digest by email; Pro accounts can watch a
specific flag, model, command or page and get an alert when it changes. Email goes
through Resend. Every email has a working unsubscribe link, and sends are
rate-capped.

### 6. Trust must-haves, for every tier

These are product improvements, not a paywall, so they ship **without** the
setting:

- a freshness stamp on every answer and citation ("docs as of <date>");
- thumbs up/down and "report wrong answer" with a reason;
- copy code, and copy the answer as Markdown;
- "request this source" on a refusal, feeding a log of missing sources;
- history export;
- a published "your data is not used for training" policy.

### 7. BYOK (bring your own key)

- A Pro feature at the **same price**. There is no cheaper BYOK plan.
- **OpenRouter only**, connected through **OpenRouter's OAuth PKCE flow** (a
  standard sign-in handshake that lets an app get a key without ever seeing the
  user's password): the user
  approves CiteVyn in OpenRouter and OpenRouter hands us a key. Users never paste
  a key.
- Used for **answer generation only**. Embeddings (the vectors used for search)
  stay on the platform key, because the index needs one fixed embedding model.
- Only **evaluated models** may be chosen.
- The key is encrypted with a master key (a Fly secret the owner sets), decrypted
  only in memory, never logged and never returned. The UI shows only its last four
  characters. The user can revoke it.
- **No fallback.** If the user's key fails, the answer fails with a clear message.
  It never silently switches to the platform key.
- BYOK lifts the monthly cap and unlocks premium models.
- **Caching:** BYOK answers may **read** the shared answer cache but are **never
  written** to it, so one user's paid model choice never becomes another user's
  answer.

**OpenRouter terms.** OpenRouter's terms §7.4 forbid "reselling API access".
OAuth PKCE is OpenRouter's documented route for apps to use a user's own account,
and in that flow the user pays OpenRouter directly; CiteVyn resells nothing. The
exact reading is re-checked and recorded here before Phase 8 ships.

### 8. Designed for, not built

- **Teams and Enterprise.** A membership belongs to an **account**, which is one
  user today and can later be an organisation. No user-to-tier shortcut is built
  that would block that.
- **Later:** version- and deprecation-aware answers; a Team tier (Slack bot, SSO,
  pooled usage); private docs.

## Permanent rules

These also live in `docs/DELIBERATE_DECISIONS.md`, so they are seen outside this ADR.

1. **No secret in the browser, ever.** Real secrets live only on the server.
2. **Entitlement is checked on the server**, from our own table, on every protected
   request. The frontend never decides it. It may *display* a tier, never
   *enforce* one.
3. **A request never waits on Stripe.**
4. **Six concerns, one mechanism each** (the table above). A new feature reuses
   them; it does not add a seventh.

### Rejected, with reasons

The owner rejected these designs. The reasons are this ADR's summary of why.

| Rejected | Why |
|---|---|
| A secret token in the bundle | Anything in the bundle is public. It protects nothing and leaks into logs. |
| Entitlement checks in the frontend | The user controls the frontend. |
| Calling Stripe mid-request | Couples our uptime to Stripe's and adds latency to every answer. |
| An anonymous cookie or IP quota as the gate | Cookies are cleared and IPs are shared or rotated; neither identifies a person, so neither can hold a one-time trial. |
| A silent BYOK→platform key fallback | The user would be billed on our key without knowing, and a broken key would hide. |
| A cheaper BYOK plan | Nobody else sells one, and our cost is not only generation (retrieval, embeddings, hosting). |
| Pasted BYOK keys | A pasted key is a long-lived secret we must receive and guard; OAuth PKCE gives a key the user approved and can revoke in OpenRouter. |

## Consequences

- Every route gets a capability label, and CI fails on a route without one.
- New tables: `memberships`, Stripe webhook events (for idempotency), API keys,
  shared answers, feedback, gap requests, doc snapshots and watches, and BYOK
  credentials. New columns: `users.email_verified_at`, and a user id and "who paid"
  on `provider_calls`. Each arrives with the phase that needs it.
- New dependencies, each justified in its own PR: the Stripe SDK (Phase 4) and a
  bot-check provider (Phase 5). An MCP library may be needed in Phase 6.
- **Owner-only steps**, which no session does on its own: every production deploy
  (including token step 2 after step 1 is live); Stripe account, live products and
  secrets; the BYOK master key; approval of the Terms, Privacy and Refund pages;
  #492's live Gemini check; topping up OpenRouter or confirming it is only a
  fallback; setting `CITEVYN_RATE_LIMIT_KEY_SALT` before token step 2; and turning `CITEVYN_ACCESS_MODEL_ENABLED` on.

## Build order

Each phase is its own PR or PRs, merged and verified before the next starts.

| # | Ships |
|---|---|
| 0 | This ADR and the document fixes; #492 (pin and price the Gemini model) |
| 1 | Groundwork, no behaviour change: a capability on every route, the policy table, the setting, and a CI guard |
| 2 | Retire the public token, step 1 only |
| 3 | Trust must-haves (not behind the setting) |
| 4 | Memberships: Stripe test mode, signed idempotent webhooks, grace, cancel, quotas, "who paid" |
| 5 | Trial and pricing (dark): the 25-answer trial, the usage meter, the upgrade screen, the pricing section, the sign-up bot check, draft legal pages |
| 6 | Pro features: MCP server, API keys, shareable answers, usage insights, cross-vendor comparison answers |
| 7 | "What changed" alerts |
| 8 | BYOK (dark) |
