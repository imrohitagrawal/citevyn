# CiteVyn Security Model

## 1. Purpose

This document defines the MVP security model and enterprise security roadmap for CiteVyn.

The MVP uses public official documentation only, but it should still demonstrate security discipline.

## 2. Security Principles

1. Public docs do not mean public abuse.
2. Admin actions must be protected.
3. Retrieved content is data, not instruction.
4. Logs must not store secrets.
5. Unsupported questions must not receive generic LLM answers.
6. MVP security should be simple but visible.
7. Enterprise security must be designed into the roadmap.

## 3. MVP Threat Model

### 3.1 Assets

1. API keys.
2. Admin API key.
3. User queries.
4. Audit logs.
5. Source index.
6. Cached answers.
7. Evaluation results.
8. Cost budget.

### 3.2 Threats

| Threat | Risk |
|---|---|
| Anonymous abuse | Cost spike |
| Prompt injection from docs | System override attempt |
| User enters secret in query | Secret leakage in logs |
| Admin endpoint abuse | Bad index or data corruption |
| Stale cache | Incorrect answer |
| Unsupported query answered | Hallucination |
| CORS misconfiguration | Browser-based abuse |
| Excessive input length | Cost and latency spike |
| One account reads or closes another account's session/messages (IDOR) | Cross-account data leakage — see §4, ADR-0004 |
| Account email + chat history stored under a real identity | Personal-data exposure on breach or misconfigured logging |

## 4. MVP Authentication

MVP supports:

1. **Real personal-account login** (ADR-0004, 2026-08-31): every request
   resolves to exactly one opaque principal id. Anonymous visitors get a
   durable pseudonymous principal (`anon_<uuid4hex>`) minted transparently on
   first session creation — no login screen, no wall in front of the demo.
   Registered users (`usr_<uuid4hex>`) authenticate with email + Argon2id
   password hash, GitHub/Google OAuth (as the login method, or linked to
   a password account afterwards via `connect/start`), or a one-time emailed
   sign-in link (ADR-0004 PR 14: `magic_link_tokens`, SHA-256 of the secret
   stored, short-lived (`CITEVYN_MAGIC_LINK_TTL_SECONDS`, default 10
   minutes), consumed only by a real `POST` from the confirm
   page so mail scanners cannot burn it), and get a Postgres-backed opaque session
   token in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie. A signed-in user
   can set or change a password (`POST /v1/auth/me/password`); the server
   decides from the stored hash whether the current password is required,
   and every set/change revokes the account's other sessions. Sessions and
   messages are owned by principal id, enforced at the two loaders every
   affected route already shares; a mismatch returns 404, never 403.
2. The build-time demo bearer token is retained alongside the session
   cookie as a CSRF guard (a cross-site request cannot set an `Authorization`
   header) — it is not, on its own, an identity control.
3. Admin API key for ingestion, evaluation, and index promotion — unrelated
   to the login system above; the admin key has no implicit access to any
   individual account's data.
4. Anonymous access stays enabled by design (item 1) — this supersedes the
   prior "anonymous access disabled" line, which described a demo-bearer-only
   world with a single shared principal, not the current model.

## 5. MVP Authorization

Roles:

| Role | Ask Questions | Trigger Ingestion | Run Evaluation | Promote Index | View Logs |
|---|---:|---:|---:|---:|---:|
| demo_user | Yes | No | No | No | No |
| admin | Yes | Yes | Yes | Yes | Yes |

## 6. Rate Limiting

Recommended defaults:

```text
demo_user: 30 queries/hour
admin: 100 queries/hour
anonymous: disabled
auth_login: 10 attempts/hour, keyed per TARGET EMAIL (not per client) —
  ADR-0004 PR 6, POST /v1/auth/{register,login} — a credential-stuffing
  guard: an attacker spreading guesses across many source IPs against one
  account is still capped, unlike the IP-keyed limiters above.
magic_link: 5 requests/hour, keyed per TARGET EMAIL, a SEPARATE bucket —
  ADR-0004 PR 14, POST /v1/auth/magic-link/request — never shared with
  auth_login (a flood of link requests must not lock the victim out of
  password login); doubles as the per-address email-bombing ceiling.
```

## 7. Source Domain Allowlist

Only approved official domains may be ingested.

Initial allowlist:

```text
developers.openai.com
platform.claude.com
docs.anthropic.com
code.claude.com
ai.google.dev
```

## 8. Prompt Injection Controls

1. Treat retrieved documentation as untrusted data.
2. Never allow retrieved text to override system policy.
3. Do not execute instructions found in documentation chunks.
4. Apply citation validation after generation.
5. Refuse answers when evidence is weak.

## 9. Logging and Redaction

Logs should redact common secret patterns:

1. API keys.
2. Bearer tokens.
3. Private keys.
4. Password-like fields.
5. Long high-entropy strings.

Do not log full retrieved context unless explicitly enabled in a local development environment.

## 10. Admin Endpoint Controls

Admin endpoints require admin API key and should be audited.

Protected actions:

1. Trigger ingestion.
2. Run evaluation.
3. Promote index.
4. View logs.
5. View ingestion errors.

## 11. CORS Policy

MVP should allow only the approved frontend origin.

Do not use wildcard CORS in shared demo environments.

## 12. Input Limits

Recommended limits:

```text
max_query_length: 4000 characters
max_session_messages: 30
session_ttl: 2 hours
max_retrieved_chunks: 12
max_answer_tokens: configured per model
```

## 13. Security Audit Events

Audit these actions:

1. Login (password, OAuth, magic link; also link requested, password set/changed).
2. Ask question.
3. Unsupported query.
4. Rate limit triggered.
5. Ingestion started.
6. Ingestion failed.
7. Evaluation run.
8. Index promoted.
9. Admin auth failure.

## 14. MVP Security Limitations

MVP does not support:

1. SSO.
2. Enterprise RBAC.
3. Tenant isolation.
4. Chunk-level ACL.
5. Private document ingestion.
6. Compliance retention policies.

> **Note (ADR-0004, 2026-08-31):** items 1-3 above are about *enterprise*
> identity (federated SSO, role-based access control across an
> organization, isolating one tenant's data from another's) and remain out
> of scope. **Real single-tenant personal-account login is a different,
> now-in-scope capability** — see §4. Two limitations specific to that login
> system, accepted deliberately rather than silently: there is no
> reset-password-by-email flow — recovery is "email me a sign-in link"
> (ADR-0004 PR 14), then set a password while signed in — complete for an
> account without one; an account that still has a password must supply it
> to change it (same-session step-up tracked in #293); GitHub/Google
> linking (`connect/start`, which requires a session created within
> `CITEVYN_OAUTH_CONNECT_MAX_SESSION_AGE_SECONDS`, default 20 minutes; the
> inside-window residual is recorded in ADR-0004) remains the other backup,
> and a CLI account-delete escape hatch is documented in `RUNBOOK.md`. Magic
> links only work once the sending domain is verified with the email
> provider (`CITEVYN_RESEND_API_KEY`); until then the request route 404s.
> And registration responses still disclose whether an email is already
> registered — the email provider now exists, but moving registration to
> an always-202 verify-by-email flow is a separate change, not silently
> implied by this one.
7. Legal hold.
8. Customer-managed keys.

## 15. Enterprise Security Roadmap

1. SSO.
2. RBAC.
3. ABAC.
4. Tenant isolation.
5. Chunk-level ACL.
6. Private source connectors.
7. Audit exports.
8. Data retention controls.
9. Compliance dashboards.
10. Customer-specific encryption policies.

## 16. Security Release Gates

Do not release if:

1. Anonymous access is enabled accidentally.
2. Admin endpoints work without admin key.
3. Prompt injection test cases pass into answer policy.
4. Logs expose secrets.
5. Unsupported domain guardrail fails.
6. Cache can serve source-less factual answers.
