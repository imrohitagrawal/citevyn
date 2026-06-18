# GitHub Copilot Code Review — instructions for this repo

> **Source of truth:** the authoritative review instructions live at
> [`imrohitagrawal/.github/.github/review-instructions.md`](https://github.com/imrohitagrawal/.github/blob/main/.github/review-instructions.md).
> This file is a repo-local pointer plus repo-specific context. If they ever conflict, the
> shared file wins — but the shared file is intentionally generic; this file adds project
> context that helps both Copilot and Claude produce better reviews.

---

## What this repo is

**CiteVyn** — a citation-grounded RAG API. Backend in Python (FastAPI + SQLAlchemy + Pydantic),
tests with pytest, dependency management with `uv`. Frontend not yet in this repo.

When reviewing, assume:
- Async-first (`async def` everywhere on the request path).
- Pydantic models at every I/O boundary; never `dict` at the edge.
- Database access via SQLAlchemy 2.x async session.
- Errors are returned as a structured envelope (`{error: {code, message, ...}}`),
  not raised to the global handler unless truly unexpected.
- Migrations are Alembic; never hand-edit the schema.

## Hot spots to scrutinize harder

These areas have caused bugs before — read them with extra care:

- **Auth & sessions** — anything in `backend/app/core/security.py`, `backend/app/api/routes/sessions.py`.
  Look for missing ownership checks (`session.user_id != current_user.id`).
- **LLM calls** — `backend/app/llm/`. Look for missing timeouts, unbounded retries,
  prompts that could leak prior-message content.
- **Retrieval** — `backend/app/retrieval/`. Look for missing tenant/permission filters
  on the vector query (a user must only see chunks they're authorized to see).
- **Cache** — `backend/app/cache/`. Look for cache keys that omit the user/tenant
  (cross-tenant data leaks are the classic bug here).
- **Citation validation** — `backend/app/answer/`. Every claim in the answer must trace
  to a retrieved chunk. Flag any path that returns the LLM output without that check.

## Ignore patterns (don't waste review time here)

- `backend/uv.lock`
- `**/migrations/versions/*.py` (Alembic-generated, after first review)
- `**/__pycache__/**`, `**/.venv/**`, `**/dist/**`, `**/build/**`
- `**/test_*.py` files — still review for *correctness*, ignore for style

## Local conventions

- **No `print()` in production code** — use `logger.exception(...)` or `logger.info(...)`.
- **No bare `except:`** — catch specific exceptions, re-raise with context.
- **No `Any`** unless wrapped in a comment explaining why.
- **Type hints required** on every public function.
- **Tests must be deterministic** — no `time.sleep`, no real network, no real DB
  (use a test DB or `monkeypatch`).
- **PRs over 400 LOC** of non-test code → flag for splitting at the top of the review.

## Tone

Same as the shared file: senior reviewer, lead with the highest-impact finding,
prefix with `[blocking] / [should-fix] / [nit]`, no cheerleading.
