# CiteVyn Data Model

## 1. Purpose

This document defines the MVP data model for CiteVyn.

The model supports:

1. Official documentation ingestion.
2. Contextual chunks.
3. Exact term lookup.
4. Sessions and messages.
5. Retrieval evidence.
6. Answer cache.
7. Evaluation runs.
8. Audit events.
9. Index versioning.
10. Per-call provider cost metering.

## 2. Core Entities

```text
documents
chunks
exact_terms
sessions
messages
retrieved_evidence
answer_cache
evaluation_cases
evaluation_runs
audit_events
index_versions
ingestion_jobs
provider_calls
```

## 3. documents

Represents an official documentation page or file.

| Field | Type | Notes |
|---|---|---|
| document_id | UUID | Primary key |
| source_name | text | codex, claude_api, claude_code, gemini_api |
| product_area | text | Product area classification |
| source_url | text | Official source URL |
| title | text | Document title |
| identity_checksum | text | Hash of the source's IDENTITY (`source_name` + `title`), NOT its prose. Renamed from `content_checksum` in migration 0005 (#163) because the old name invited use as a change-detection signal. Content fingerprints: per-chunk `chunks.content_checksum`; corpus-wide `index_versions.source_version_hash` |
| last_fetched_at | timestamp | Last fetch time |
| last_indexed_at | timestamp | Last successful index time |
| status | text | active, failed, deprecated |

## 4. chunks

Represents a contextual retrievable unit.

| Field | Type | Notes |
|---|---|---|
| chunk_id | UUID | Primary key |
| document_id | UUID | FK to documents |
| product_area | text | Product area |
| section_path | text | Hierarchical heading path |
| heading | text | Current heading |
| parent_heading | text | Parent heading |
| chunk_text | text | Chunk content |
| context_summary | text | Small surrounding context |
| exact_terms | jsonb | Extracted terms |
| embedding_id | UUID | FK or reference to embedding |
| chunk_order | integer | Order within document |
| content_checksum | text | Hash for invalidation |

## 5. exact_terms

Supports exact lookup for flags, commands, config keys, and similar terms.

| Field | Type | Notes |
|---|---|---|
| term_id | UUID | Primary key |
| term_text | text | Exact term |
| term_type | text | flag, command, config_key, etc. |
| product_area | text | Product area |
| document_id | UUID | FK to documents |
| chunk_id | UUID | FK to chunks |

Term types:

```text
flag
command
config_key
model_name
api_parameter
error_message
environment_variable
file_name
slash_command
```

## 5a. users

Every request resolves to exactly one principal (`docs/ADR/0004-user-accounts.md`),
anonymous or registered, and this is that principal's row.

| Field | Type | Notes |
|---|---|---|
| user_id | text(128) | Primary key. `anon_<uuid4hex>` (anonymous, PR 3) or `usr_<uuid4hex>` (registered, PR 6) — never the email |
| role | text | Authorization tier (`demo_user`/`admin`), unrelated to anonymous vs. registered |
| created_at | timestamp | |
| email | text(255), nullable | Unique when set. `NULL` for anonymous principals; SQL uniqueness does not compare `NULL` to itself, so any number of anonymous rows coexist (ADR-0004 PR 5) |
| password_hash | text(255), nullable | Argon2id PHC string (`app.core.passwords`). `NULL` for anonymous principals and, later, OAuth-only accounts (PR 12) |

## 6. sessions

Stores bounded conversation sessions.

| Field | Type | Notes |
|---|---|---|
| session_id | UUID | Primary key |
| user_id | text(128) | FK -> `users.user_id`, `ON DELETE CASCADE` (RESTRICT before ADR-0004 PR 5 — see §5a) |
| channel | text | chat |
| summary | text | Bounded session summary |
| current_product_area | text | Last resolved product area |
| created_at | timestamp | Created time |
| expires_at | timestamp | Expiration time |

## 7. messages

Stores user and assistant messages.

| Field | Type | Notes |
|---|---|---|
| message_id | UUID | Primary key |
| session_id | UUID | FK to sessions |
| role | text | user or assistant |
| content | text | Message text |
| normalized_query | text | Normalized form |
| domain | text | Product/domain classification (or `unsupported`, or neutral `general` on a greeting) |
| intent | text | faq, exact_lookup, how_to, etc. |
| created_at | timestamp | Created time |

## 8. retrieved_evidence

Stores retrieval trace.

| Field | Type | Notes |
|---|---|---|
| evidence_id | UUID | Primary key |
| message_id | UUID | FK to messages |
| chunk_id | UUID | FK to chunks |
| rank | integer | Rank after reranking |
| score | numeric | Retrieval/rerank score |
| retrieval_type | text | exact, keyword, vector, hybrid |
| used_in_answer | boolean | Whether cited or used |

## 9. answer_cache

Stores safe cached answers.

| Field | Type | Notes |
|---|---|---|
| cache_key | text | Primary key |
| normalized_question | text | Normalized question |
| product_area | text | Product area |
| answer | text | Cached answer |
| citations | jsonb | Required citations |
| source_version_hash | text | Index/source version |
| answer_policy_version | text | Policy version |
| confidence | text | high, medium, low |
| ttl_expires_at | timestamp | Expiration |
| created_at | timestamp | Created time |
| last_used_at | timestamp | Last hit |

## 10. evaluation_cases

Stores golden test cases.

| Field | Type | Notes |
|---|---|---|
| case_id | UUID | Primary key |
| question | text | Test question |
| expected_domain | text | Expected product area |
| expected_intent | text | Expected intent |
| expected_sources | jsonb | Expected docs/chunks |
| required_answer_points | jsonb | Must include |
| forbidden_answer_points | jsonb | Must not include |
| expected_behavior | text | answer, no_answer, unsupported, clarify |

## 11. evaluation_runs

Stores evaluation results.

| Field | Type | Notes |
|---|---|---|
| run_id | UUID | Primary key |
| suite_name | text | mvp_golden_50 |
| index_version | text | Candidate index |
| started_at | timestamp | Start time |
| completed_at | timestamp | End time |
| status | text | running, passed, failed |
| metrics | jsonb | Score details |
| failure_summary | jsonb | Failed cases |

## 12. audit_events

Stores security and operational audit events.

| Field | Type | Notes |
|---|---|---|
| event_id | UUID | Primary key |
| user_id | text | Actor |
| role | text | demo_user or admin |
| action | text | ask_question, trigger_ingestion, promote_index |
| resource_type | text | session, message, index, job |
| resource_id | text | Resource ID |
| timestamp | timestamp | Event time |
| metadata | jsonb | Redacted metadata |

## 13. ingestion_jobs

Stores ingestion pipeline status.

| Field | Type | Notes |
|---|---|---|
| job_id | UUID | Primary key |
| source_name | text | codex, claude_api, etc. |
| status | text | pending, running, completed, failed |
| stage | text | fetching, parsing, chunking, embedding, indexing |
| started_at | timestamp | Start time |
| completed_at | timestamp | End time |
| error_type | text | Optional |
| error_message | text | Sanitized |
| retryable | boolean | Retry flag |

## 14. index_versions

Supports candidate index and rollback.

| Field | Type | Notes |
|---|---|---|
| index_version | text | Primary key |
| status | text | candidate, active, previous_good, failed |
| source_version_hash | text | Combined source hash |
| created_at | timestamp | Created time |
| promoted_at | timestamp | Promotion time |
| evaluation_run_id | UUID | FK to evaluation_runs — the newest TERMINAL run for this index (#229) |

`evaluation_run_id` is written by `app.services.index_versions.link_evaluation_run`,
called from `evaluate_index` when a run reaches `passed` **or** `failed`. It is a
display convenience surfaced by `/health/index` and `GET /v1/admin/index_versions`,
and it says *which run measured this index*, **not** *that the index passed* — read
the run itself (`GET /v1/admin/evaluations/{run_id}`) for the verdict. Linking only
passing runs would make "evaluated and failed" indistinguishable from "never
evaluated", which is the defect #229 fixed. The promotion gate deliberately does
**not** read this column; it re-derives evidence from `evaluation_runs` on every
promote (`_latest_completed_run`), so a single mutable pointer can never become the
authority for a security-relevant gate.

## 15. provider_calls

Stores one row per paid provider call. This is the metering substrate the
`RELEASE_PLAN.md` section 9 daily budget (soft $5 / hard $10) is computed from:
token counts on `LLMResult` are discarded once an answer is returned, so spend has
no other record.

| Field | Type | Notes |
|---|---|---|
| call_id | UUID | Primary key |
| occurred_at | timestamp | Call completion time; the daily budget buckets on this |
| kind | text | llm, embedding |
| call_site | text | answer, condense, alias_intent, ingest, eval, unknown |
| provider | text | Provider name |
| model | text | Model identifier |
| input_tokens | integer | Prompt tokens |
| output_tokens | integer | Completion tokens |
| attempts | integer | Provider HTTP requests issued, including retries (>= 1) |
| cost_usd | numeric(14,6) | Stored cost, never recomputed |
| input_price_per_1m | numeric(12,6) | Rate applied; NULL exactly when `priced` is false |
| output_price_per_1m | numeric(12,6) | Rate applied; NULL exactly when `priced` is false |
| priced | boolean | False means the price book had no entry |
| tokens_estimated | boolean | True means token counts are a local estimate |
| request_id | text | Correlation id; nullable (ingest and eval run outside a request) |

Design decisions:

1. `cost_usd` is stored, not derived. Prices are snapshots, so recomputing from
   today's price book would silently rewrite yesterday's spend when a provider
   changes its rates. The rates applied are stored alongside for audit.
2. `priced = false` means the price book had no entry for `(provider, model)`, so
   `cost_usd` is 0 and the budget is **under-counting** that call. It is a signal
   to add a price, not a free call.
3. `attempts` counts provider HTTP requests including retries, so a flaky provider
   can cost several times a naive per-call count.
4. No prompt or answer text is stored — only counts and identifiers — so the table
   cannot become a second copy of user questions in a log or backup.

Numeric, not float: these values are summed across thousands of rows and compared
against a dollar threshold, and 6 decimal places resolve a single cheap call.

## 15a. auth_sessions (ADR-0004 PR 3)

Backs the persistent per-visitor cookie identity described in
`docs/ADR/0004-user-accounts.md`. Every request resolves to exactly one
principal — anonymous today, a real logged-in user once ADR-0004 PR 6 ships
— and this table is what the cookie is checked against.

| Field | Type | Notes |
|---|---|---|
| auth_session_id | UUID | Primary key; also the public "id" half of the cookie value |
| secret_hash | text(64) | SHA-256 hex digest of the cookie's secret half — never the secret itself |
| user_id | text(128) | FK -> `users.user_id`, `ON DELETE CASCADE` |
| created_at | timestamp | |
| expires_at | timestamp | `auth_session_ttl_seconds` from creation (default 180 days) |

Design decisions:

1. Only `secret_hash` is stored. The cookie carries `<auth_session_id>.<secret>`;
   a leaked database row (or backup, or careless log line) is not enough to
   forge a session, because recovering the secret from its digest is
   infeasible.
2. `ON DELETE CASCADE`, not `RESTRICT` — an auth session is a credential FOR a
   user, not an independent record, so it must not block deleting the user it
   authenticates. Contrast `sessions.user_id`, which ADR-0004 PR 5 moves the
   other way (RESTRICT -> CASCADE) for the same underlying reason: user
   deletion must actually be possible.

## 16. Indexing Recommendations

1. Index `documents.source_name`.
2. Index `documents.product_area`.
3. Index `chunks.product_area`.
4. Full-text index `chunks.chunk_text`.
5. Vector index embeddings.
6. Unique index on `exact_terms.term_text + product_area + chunk_id`.
7. Index `answer_cache.cache_key`.
8. Index `messages.session_id`.
9. Index `retrieved_evidence.message_id`.
10. Index `provider_calls.occurred_at` (the budget's hot "spend since midnight UTC" query).
