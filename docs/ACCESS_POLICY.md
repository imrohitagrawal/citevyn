# Access policy

Who may do what, route by route. This is the readable copy of the policy table in
`backend/app/access/policy.py`, and the route labels in `backend/app/api/routes/`.
`backend/tests/test_access_policy.py` fails when this page and the code disagree, in
either direction. The decisions behind it are in [ADR-0005](ADR/0005-access-model.md).

## How it works

- A **capability** is one named thing a caller may do. Every route declares exactly one,
  with `dependencies=[Depends(requires(Capability.X))]`. A route without one fails CI.
- A **tier** is the kind of account a caller has: anonymous, free or Pro. The table below
  says which tier has which capability. No route checks a tier directly.
- **Everything here is off by default.** With `CITEVYN_ACCESS_MODEL_ENABLED=false`, the
  check does nothing and every route behaves as it did before ADR-0005, anonymous chat
  included. Only the owner turns it on.
- With it on, a caller whose tier lacks the capability is refused:
  - an anonymous caller gets **401 `auth_required`**, because signing in is what helps;
  - a signed-in caller gets **403 `plan_required`**, because upgrading is what helps.
  - Both carry `details: {capability, required_tier}`.
- The tier is read from our own tables on the server, never from the browser and never
  from Stripe: a registered account is `pro` when any of its `memberships` rows (one per
  subscription) says it has paid (active, trialing, or past due inside the 7-day
  grace), else `free`. Stripe's signed webhooks keep those rows current.
- `operate` belongs to no tier. It is granted only by the admin API key, which each
  operator route already checks.

## Allowances

The numbers are settings, so they change without a code change. Only **answered**
questions count; a refusal or an error never does.

| Tier | Allowance | Setting | Default |
|---|---|---|---|
| Anonymous | None (the canned sample only) | — | — |
| Free | Once, per verified account | `CITEVYN_ACCESS_FREE_TRIAL_ANSWERS` | 25 |
| Pro | Per calendar month (UTC) | `CITEVYN_ACCESS_PRO_MONTHLY_ANSWERS` | 1,000 |

The per-hour rate limits and the global daily spend cap apply on top, to every tier.

**As built (Phase 4B-2, `app/access/quota.py`):**

- An answered question is a cited answer, fresh or from the cache. A refusal, a
  no-answer, a greeting or an error never counts. Each one is a row in
  `answer_usage`, written with the answer in one transaction.
- The check runs before any paid call. When the allowance is used up:
  - Free and not verified: 403 `verification_required`;
  - Free with the trial used: 403 `plan_required`, reason `trial_used`;
  - Pro with the month used: 429 `quota_exceeded`, with `resets_at`.
- BYOK answers (Phase 8) are recorded as user-paid and never count.
- Each row records the tier it was used on. Pro counts only Pro rows, so an
  account that upgrades mid-month gets its whole Pro allowance. Free counts
  every row ever, so a lapsed Pro account has usually used its trial.
- The tier is worked out from the principal the request already resolved, on
  the same database session; a caller the route sees as anonymous is refused,
  never answered without a check.
- Deleting an account deletes its usage rows; there is no delete route today.
  One added later must not let the same address start a fresh trial.
- Two questions sent at the same moment near the limit can both be answered: the
  check and the write are not one step, because locking the account for a whole
  model call would block its Stripe webhooks. The overshoot is at most the number
  of requests that account has in flight, and the hourly limit caps it.

## Capabilities by tier

`yes` means the tier has it. `operate` is admin-key only.

| Capability | Anonymous | Free | Pro | What it allows |
|---|---|---|---|---|
| `public` | yes | yes | yes | Health probes and public pages |
| `sign_in` | yes | yes | yes | Sign up, sign in, sign out, "who am I" |
| `manage_account` | — | yes | yes | Change one's own password |
| `chat` | — | yes | yes | Ask a live question |
| `history` | — | yes | yes | Read or delete one's own conversations |
| `exact_search` | — | yes | yes | Exact flag and command lookup |
| `feedback` | — | yes | yes | Rate an answer, report it as wrong, request a missing source |
| `billing` | — | yes | yes | Start, view or manage one's own paid plan |
| `operate` | — | — | — | Operator routes (admin API key) |
| `weekly_digest` | — | yes | yes | The weekly "what changed" email (no route yet) |
| `mcp_ask` | — | — | yes | Cited answers through the MCP server (no route yet) |
| `api_keys` | — | — | yes | Personal API keys (no route yet) |
| `share_answer` | — | — | yes | Shareable answers (no route yet) |
| `usage_insights` | — | — | yes | The usage page (no route yet) |
| `watch_alerts` | — | — | yes | Real-time "what changed" alerts (no route yet) |
| `byok` | — | — | yes | Bring your own OpenRouter key (no route yet) |

## Routes

| Method | Path | Capability |
|---|---|---|
| `GET` | `/about` | `public` |
| `GET` | `/health` | `public` |
| `GET` | `/health/dependencies` | `public` |
| `GET` | `/health/index` | `public` |
| `GET` | `/v1/config` | `public` |
| `GET` | `/v1/auth/challenge` | `public` |
| `POST` | `/v1/auth/register` | `sign_in` |
| `POST` | `/v1/auth/login` | `sign_in` |
| `POST` | `/v1/auth/logout` | `sign_in` |
| `GET` | `/v1/auth/me` | `sign_in` |
| `POST` | `/v1/auth/magic-link/request` | `sign_in` |
| `GET` | `/v1/auth/magic-link/confirm` | `sign_in` |
| `POST` | `/v1/auth/magic-link/confirm` | `sign_in` |
| `GET` | `/v1/auth/oauth/{provider}/start` | `sign_in` |
| `GET` | `/v1/auth/oauth/{provider}/connect/start` | `sign_in` |
| `GET` | `/v1/auth/oauth/{provider}/callback` | `sign_in` |
| `POST` | `/v1/auth/me/password` | `manage_account` |
| `POST` | `/v1/sessions` | `chat` |
| `POST` | `/v1/sessions/{session_id}/messages` | `chat` |
| `GET` | `/v1/me/sessions` | `history` |
| `GET` | `/v1/me/export` | `history` |
| `GET` | `/v1/sessions/{session_id}` | `history` |
| `DELETE` | `/v1/sessions/{session_id}` | `history` |
| `GET` | `/v1/sessions/{session_id}/messages/{message_id}` | `history` |
| `POST` | `/v1/search/exact` | `exact_search` |
| `PUT` | `/v1/sessions/{session_id}/messages/{message_id}/feedback` | `feedback` |
| `POST` | `/v1/source-requests` | `feedback` |
| `GET` | `/v1/admin/feedback` | `operate` |
| `POST` | `/v1/billing/checkout` | `billing` |
| `POST` | `/v1/billing/portal` | `billing` |
| `GET` | `/v1/billing/membership` | `billing` |
| `POST` | `/v1/billing/webhook` | `public` |
| `GET` | `/v1/admin/source_requests` | `operate` |
| `GET` | `/v1/admin/budget` | `operate` |
| `GET` | `/v1/admin/evaluations` | `operate` |
| `GET` | `/v1/admin/evaluations/{run_id}` | `operate` |
| `GET` | `/v1/admin/index_versions` | `operate` |
| `GET` | `/v1/admin/index_versions/{index_version}` | `operate` |
| `POST` | `/v1/admin/index_versions/{index_version}/promote` | `operate` |
| `GET` | `/v1/admin/ingestion_jobs` | `operate` |
| `GET` | `/v1/admin/ingestion_jobs/{job_id}` | `operate` |

## Known limits (recorded, not fixed)

- **The check runs before the bearer check and the rate limiter.** Route-level
  dependencies run first in FastAPI. With the setting on, a request that carries a
  session cookie costs one primary-key read before the rate limiter sees it, and a refused
  request is not counted against the hourly limit. With the setting off it costs nothing.
  Also, with the setting on, a caller with no bearer token gets the capability's 401
  rather than the bearer 401. Revisit when Phase 2 replaces the bearer token.
- **A signed-in caller's cookie is looked up twice** with the setting on: once for the
  tier, once by the route itself. Same row, performance only.
- **Turning the setting on hides pre-launch anonymous history.** A visitor's `anon_`
  conversations become unreadable (`history` needs an account). Intended; tested.
- **A 401 from `POST /v1/sessions` signs the frontend out** (`frontend/src/lib/api.ts`), and
  the frontend does not handle `plan_required` yet. The sample-only and upgrade screens are
  Phase 5.

## Adding a route

1. Give it `dependencies=[Depends(requires(Capability.X))]`.
2. Add it to `EXPECTED_CAPABILITY` in `backend/tests/test_route_auth_inventory.py`, and to
   the credential class it belongs to there.
3. Add its row to the Routes table above.

A new capability also needs a row in `POLICY` (`backend/app/access/policy.py`) and in the
"Capabilities by tier" table. The tests fail until all of these agree.
