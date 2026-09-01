# AGENTS.md

## Building a feature likely needed across future projects

Applies to infrastructure-shaped concerns (auth, payments, email, logging) more than ordinary
business logic — the same correctness/security mistakes recur every time these get rebuilt from
scratch, so they earn earlier investment in reusability than typical feature work does.

- **Don't extract a shared package before a second real consumer exists** (rule of three) — a
  library's boundaries, guessed from one implementation, are usually wrong and cost more to rework
  than building it twice would have.
- **Do draw internal seams cheaply while building the first implementation.** Keep pure
  protocol/mechanism logic (HTTP transport, cryptographic primitives, wire-format parsing) in files
  with zero app-specific imports — check this holds with `grep "^from app\." <file>` (or your
  project's own import prefix) returning nothing but trivial exceptions. Give a "flow kind"/"variant"
  column a plain string type, not a native-DB enum, when the set of values may grow — adding an enum
  member needs a migration; a string doesn't.
- **Route "same action, different flavor" through a free-form metadata field on an existing
  action/event type**, not a new enum member per variant — cheaper, and avoids an enum-migration
  trap most Postgres-backed projects have.
- **Write the security invariants down explicitly** (an ADR or design doc), not just in code — these
  transfer across languages and stacks even when the code doesn't. Treat this document, not the
  implementation, as the actually-reusable asset until a second consumer justifies extracting code.
- **A fix for an adversarial-review finding gets its own review round** — a from-scratch reimplementation
  elsewhere risks repeating exactly the mistake the fix corrected if the fix isn't itself verified.

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

Before shipping, run `/review`. Use `code_review.md` as the review standard (what blocks
a ship and what does not). This section adds *how much* review to run and *which* agents.

### Blast-radius-aware review orchestration

After every substantive change, size the review to the blast radius — not "always one
reviewer" and not "always many". Selection = *(surface changed) × (dependents × severity
× reversibility)*. Prioritize the pre-installed plugins/skills below; fall back to a
`general-purpose` agent only if none fit. **Always pass sub-agents the explicit absolute
repo path `/Users/rohitagrawal/Projects/citevyn`** — their shell otherwise defaults to a
stale path and they review the wrong tree.

| Tier | Trigger (blast radius) | Orchestration |
|---|---|---|
| **T0 Trivial** | docs / comments / formatting; no runtime effect | self-verify (lint/build); `comment-analyzer` for large doc changes |
| **T1 Localized** | one function / small fix / refactor / tests-only | ONE matched agent: logic → `code-reviewer`; refactor → `code-simplifier` / `taste-check`; tests → `pr-test-analyzer`; add `verify` if it has a runtime surface |
| **T2 Moderate** | multi-file feature / new module / error-handling / new types | parallel: `code-reviewer` + (errors → `silent-failure-hunter`) + (types → `type-design-analyzer`) + (behavior → `pr-test-analyzer`) + (UI → `verify` + `webapp-testing`); synthesize findings |
| **T3 High/Critical** | security / auth / guards / config / migrations / public API / cross-cutting | full T2 fan-out + `security-review` + adversarial verify (a skeptic per finding); escalate to a multi-agent Workflow (ultracode) when large; close with `release-readiness-review` as the ship/no-ship gate |

Run the review proactively — before declaring done, pushing, or opening/merging a PR —
without waiting to be asked. Address findings before moving on. Read `AGENTS.md`,
`code_review.md`, and the session memory files at the start of every work session.

## Open-work awareness

At the start of every work session, and before planning any new change, review the
**open follow-ups** so nothing already tracked is re-implemented, overlooked, or
contradicted:

1. Read [`docs/BACKLOG.md`](docs/BACKLOG.md) — the in-repo index of tracked follow-ups.
2. Check the live list: `gh issue list --state open` (GitHub is the source of truth).

Cross-check both against the task at hand. When you open, close, or supersede a GitHub
issue, update the matching row in `docs/BACKLOG.md` in the same change so the index and
GitHub never drift.
