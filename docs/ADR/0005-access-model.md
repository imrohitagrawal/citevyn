# ADR-0005: One Access Model

## Status

Accepted. Every product decision below was locked by the owner on 2026-09-25
(issue #488). This ADR records them and the design that follows. It amends
ADR-0004 on one point: what the public client token is for (see "The public
token" below). It does not rewrite ADR-0004.

Everything commercial ships **dark**, behind one setting,
`CITEVYN_ACCESS_MODEL_ENABLED`, which defaults to `false`. With it false,
production behaves exactly as it did before this ADR. Only the owner turns it on.

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
*access* to these docs. What they cannot get elsewhere is the trust layer:

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
  (see #492: the meter priced the old alias about 4x low). So 1,000 answers cost
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
| Live questions | **None.** They see the existing canned sample. | **25 answered questions, once**, per verified account | **1,000 answered questions a month**, plus the existing per-hour limit |
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
- **"Verified account"** means the account has proved it controls its email
  address: it redeemed a magic link, or it signed in with an OAuth provider that
  reported a verified email. Password sign-up does not verify an address today
  (`SECURITY_MODEL.md` §6, `email_notice`), so the `users` table gets an
  `email_verified_at` column (Phase 4). The trial is granted once per verified
  account; it is never reset without a new verified account.

### 2. Six concerns, one mechanism each

The access model is six separate questions. Each has exactly one mechanism. Mixing
two of them into one check is how the previous contradictions happened.

| Concern | Question it answers | The one mechanism |
|---|---|---|
| **Authentication** | Who is this? | The session cookie (ADR-0004). |
| **Entitlement** | What have they paid for? | Our `memberships` table, written only by signed Stripe webhooks. |
| **Authorisation** | May this caller use this route? | A capability declared on every route, plus ONE tier→capability policy table. |
| **Quota** | How much may they still use? | Per-user and per-plan metering on `provider_calls`, which records who paid. |
| **CSRF** (cross-site request forgery: another site making the browser send a request) | Did this request come from our own page? | `SameSite` cookie + an `Origin` check + a fixed request header. |
| **Abuse** | Is this a flood or a bot? | Per-IP limits on the public doors, email verification, and a bot check at sign-up. |

#### Authorisation: capabilities and one policy table

Every route declares the capability it needs, for example `ask`, `mcp_ask`,
`api_keys`, `share_answer`, `usage_insights`, `watch_alerts`, `byok`, `admin`, or
`public` for a route that needs nothing. One table maps each tier (anonymous,
free, pro, and later team) to the capabilities it has and the quota for each.
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
- Every membership change writes an audit event.

#### Quota: count answers, record who paid

`provider_calls` gains a "who paid" field: the platform, or the user (BYOK). The
quota counts **answered** questions per user per period. A refusal or an error
never uses allowance. BYOK answers are recorded as user-paid, so they do not
consume the platform allowance. MCP answers count against the same allowance as
chat answers.

#### CSRF

Browser requests are protected by three things together: the session cookie is
`SameSite=Lax`, the server checks the `Origin` header against the site's own
origin, and state-changing requests must carry a fixed custom header. A cross-site
page cannot set a custom header without a CORS preflight, and our CORS policy does
not allow one from another origin.

### 3. The public token is retired

The build-time public client token was described two ways. Both descriptions were
partly true, and neither is a reason to keep it:

- As a **CSRF guard**, it works only because a cross-site request cannot set an
  `Authorization` header. A fixed custom header does the same job without
  pretending to be a secret.
- As an **anti-scraping speed bump**, it stops nothing: it is compiled into the
  public JavaScript bundle, so anyone can read it.

It also costs something. It has to be passed as a build argument, it has leaked
into deploy logs (#489, #416), and it needs its own bundle check
(`check_bundle_key.sh`). It is retired in two steps, because the frontend and the
backend deploy together but browsers cache old bundles:

1. **Step 1:** the backend accepts EITHER the old token OR the new fixed header
   plus a valid `Origin`. The frontend sends the new header. Deploy.
2. **Step 2** (only after step 1 is live): remove the token, its Fly secret, the
   build argument and `check_bundle_key.sh`. #489 closes here.

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

### 5. "What changed" alerts

A scheduled job snapshots the four doc sets and compares them with the last
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
- "request this source" on a refusal, feeding a gap log;
- history export;
- a published "your data is not used for training" policy.

### 7. BYOK (bring your own key)

- A Pro feature at the **same price**. There is no cheaper BYOK plan.
- **OpenRouter only**, connected through **OpenRouter's OAuth PKCE flow**: the user
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
  credentials. New columns: `users.email_verified_at` and `provider_calls` "who
  paid". Each arrives with the phase that needs it.
- New dependencies, each justified in its own PR: the Stripe SDK (Phase 4) and a
  bot-check provider (Phase 5). An MCP library may be needed in Phase 6.
- **Owner-only steps**, which no session does on its own: every production deploy
  (including token step 2 after step 1 is live); Stripe account, live products and
  secrets; the BYOK master key; approval of the Terms, Privacy and Refund pages;
  #492's live Gemini check; and turning `CITEVYN_ACCESS_MODEL_ENABLED` on.

## Build order

Each phase is its own PR or PRs, merged and verified before the next starts.

| # | Ships |
|---|---|
| 0 | This ADR and the document fixes; #492 (pin and price the Gemini model) |
| 1 | Rails: a capability on every route, the policy table, the setting, and a CI guard |
| 2 | Retire the public token, step 1 only |
| 3 | Trust must-haves (not behind the setting) |
| 4 | Memberships: Stripe test mode, signed idempotent webhooks, grace, cancel, quotas, "who paid" |
| 5 | Trial and pricing (dark): the 25-answer trial, the usage meter, the upgrade screen, the pricing section, the sign-up bot check, draft legal pages |
| 6 | Pro features: MCP server, API keys, shareable answers, usage insights |
| 7 | "What changed" alerts |
| 8 | BYOK (dark) |
