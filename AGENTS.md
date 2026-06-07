# AGENTS.md

## Project operating model

Follow this workflow for non-trivial changes:

1. Understand the requirement.
2. Inspect relevant files before editing.
3. Use planning mode for complex or risky changes.
4. Identify impacted frontend, backend, database, API, auth, config, CI/CD, and observability areas.
5. Do not implement until the plan is clear.
6. Keep changes small and reviewable.
7. Add or update tests for changed behavior.
8. Run relevant lint, type checks, tests, and security checks.
9. Run review before finalizing.
10. Provide a ship/no-ship summary with evidence.

## Repository layout

- `frontend/`: UI code and client-side behavior.
- `backend/`: APIs, services, workers, business logic.
- `db/`: schema, migrations, seed data, queries.
- `infra/`: Docker, deployment, CI/CD, environment configuration.
- `tests/`: unit, integration, regression, and end-to-end tests.

## Engineering rules

- Do not modify unrelated files.
- Do not introduce new dependencies without explaining why.
- Do not change public APIs, auth, permissions, schemas, migrations, infrastructure or production config without explicit approval.
- Prefer existing project patterns over new abstractions.
- Keep business logic outside controllers/routes where possible.
- Add regression tests for bug fixes.
- Add happy-path, failure-path and edge-case tests for new behavior.
- Add happy-path, failure-path, and edge-case tests for features.
- Never fake test results.
- Never log secrets, tokens, passwords, API keys, or private environment values.
- Never mark work complete unless verification commands were run or clearly skipped with reason.

## Validation commands

Discover exact commands from `README.md`, `package.json`, `pyproject.toml`, `Makefile`, CI config, or Docker files.

Typical commands may include:

- Backend tests: `pytest` or `uv run pytest`
- Backend lint: `ruff check .`
- Type checks: `mypy .` or `pyright`
- Frontend tests: `npm test` or `pnpm test`
- Frontend lint: `npm run lint`
- Docker validation: `docker compose config`

## Definition of done

A task is complete only when:

1. Acceptance criteria are satisfied.
2. Relevant tests pass.
3. Lint/type checks pass or failures are explained.
4. Risky areas are reviewed.
5. Changed files are summarized.
6. Risks and rollback notes are documented.

## Review policy

Before shipping, run `/review`.

Use `code_review.md` as the review standard.