# Dependabot Triage Policy

This document defines the **risk tiers** for dependency updates and the
required review gates before a dependabot PR is merged. It also
documents the `release-blocker` repo label and the automation that
prevents a blocked PR from being part of a demo cut.

---

## TL;DR

| Change type | Auto-merge? | Required reviewer | Nightly CI gate? | Label |
|-------------|--------------|-------------------|------------------|-------|
| Python dev deps (`pytest`, `ruff`, `mypy`) | ✅ | None | ✅ | `dependencies/python` |
| Python runtime deps (`fastapi`, `uvicorn`, `sqlalchemy`) | ❌ | Backend tech lead | ✅ | `release-blocker` |
| Postgres/Redis client libs (`asyncpg`, `redis`) | ❌ | Backend tech lead + Ops | ✅ | `release-blocker` |
| Frontend libs (`react`, `vite`, `eslint`) | ❌ | Frontend tech lead | ✅ | `release-blocker` |
| GitHub Actions runner images (`ubuntu-*`, `actions/*`) | ❌ | Ops | ✅ | `release-blocker` |
| Docker base images — **runtime** stage (`python:*`, `pgvector/*`, `redis:*`, `caddy:*`) | ❌ | Ops | ✅ | `release-blocker` |
| Docker base images — **build-only** stage (`node:*`, `ghcr.io/astral-sh/uv:*`) | ❌ | None — CI `image-smoke` is the gate | ✅ | `dependencies` |
| Security alerts (CVE) | ❌ | **all** | ✅ + manual security review | `security`, `release-blocker` |

---

## Release-blocker label

A dependabot PR **must not be merged** while carrying the
`release-blocker` label unless the release manager explicitly waives
it in a GitHub comment with `@release-manager waive-blocker`. The
nightly CI workflow checks for any open `release-blocker` PRs and
fails the "demo-readiness" gate if it finds one.

### Why the extra guard?

Dependabot auto-merges `dependencies/python` by default (configured in
`.github/dependabot.yml`). But a change that touches `sqlalchemy` or
`uvicorn` can introduce a subtle regression (e.g., connection pool
semantics, async handler signature) that only surfaces under load. The
label is a mechanical way to say "do NOT ship the demo while this PR
is open". The nightly CI job `check-no-release-blockers` enforces it.

### Adding the label

**Never manually add `release-blocker`.** The `auto-label` workflow
scans each dependabot PR and adds it based on the changed files:

```yaml
# .github/workflows/auto-label.yml (pseudo)
- if: contains(github.event.pull_request.changed_files, 'backend/pyproject.toml')
  - contains('fastapi', 'uvicorn', 'sqlalchemy', 'asyncpg', 'redis')
  - run: gh pr edit $PR --add-label 'release-blocker'
```

If the auto-labeler is down, assign the PR to the `@citevyn/triage`
team and we'll handle it manually.

---

## Risk tiers

### 1. Safe (auto-merge)

**Examples:** `pytest==8.2.0`, `ruff==0.5.0`, `mypy==1.10.0`.

**Gate:** The nightly CI test suite (`make test && make golden`) must
still be green. If it flips red, the auto-merge is disabled and the
PR is reassigned to `@citevyn/backend`.

### 2. Moderate (manual review, not a blocker)

**Examples:** `httpx==0.28.0`, `pydantic==2.9.0`, `alembic==1.13.0`.

**Gate:** A backend engineer must sanity‑check the CHANGELOG and run
`make test && make smoke`. The PR can be merged **before** the nightly
CI run — the `release-blocker` label is NOT added.

### 3. Critical (must be merged before the demo)

**Examples:** `uvicorn==0.31.0`, `sqlalchemy==2.0.36`, `redis==5.2.0`,
`actions/checkout@v4`, `actions/upload-artifact@v4`.

**Gate:** The PR must:

1. Pass all unit tests (`make test && make typecheck && make lint`).
2. Pass the golden suite (`make golden`) — 50/50 cases.
3. Pass the smoke test (`make smoke`).
4. Pass a manual load test: `for i in {1..100}; do curl http://localhost:8000/v1/products; done`.

Only after all four gates are green can a maintainer remove the
`release-blocker` label. The removal must be accompanied by a comment
that lists the commit SHA where the nightly run confirmed green.

### 4. Docker base images — split by whether the image SHIPS

This table previously had no row for base images at all, so a `FROM` bump fell
through every category and got triaged by analogy. Base images are not one risk
tier — they are two, and the split is "does this end up in the running
container?"

**Build-only stages — LOW.** `node:*` (the `frontend` stage of
`Dockerfile.api:55`) and `ghcr.io/astral-sh/uv:*` (the `builder` stages) produce
artifacts that are copied out; the image itself never ships. A broken bump
cannot reach production because it cannot get past the build. The
`image build+boot smoke` CI job (issue #82, `infra/docker/scripts/image_smoke.sh`)
genuinely builds and boots the images, so a green run IS the evidence — no
human reviewer adds signal on top of it.

*Residual gap worth knowing:* a Dockerfile-only PR does not touch `frontend/**`,
so the frontend workflow (type-check, unit tests, Playwright) does **not**
trigger. The bundle is built but never exercised in a browser. Prefer merging a
build-toolchain bump **after** substantive frontend work lands, so the new
tests exercise it — and so a bundle oddity is not entangled with a feature
change.

**Runtime stages — CRITICAL, treat as tier 3.** `python:3.14-slim-bookworm`
(`Dockerfile.api:118`, `Dockerfile.worker:52`), `pgvector/pgvector:pg18`,
`redis:*` and `caddy:*` all ship. This is not theoretical: in **#34** dependabot's
runtime-only Python 3.14 bump produced a **non-booting image** and was green all
the way through, because at the time no CI job built or booted these images. It
needed the *builder* stage moved in lockstep (`uv:python3.14`) plus a
`python -m` CMD. `docker build` does not catch an interpreter/CMD break — only
booting does.

Two consequences that are load-bearing:

1. `.github/dependabot.yml` groups all `docker` `FROM` refs into
   `docker-base-images` **on purpose**, so a builder and its matching runtime
   cannot drift apart across two separate PRs.
2. Never split a grouped base-image PR into per-stage merges for the same
   Dockerfile.

### 5. Security (CVE) — immediate escalation

**Example:** Dependabot security advisory for `fastapi<0.110.0`.

**Gate:** The `security` label is auto‑added. The PR is reassigned to
`@citevyn/backend` AND `@citevyn/ops`. A security write‑up must be
attached to the PR (impact, exploit scenario, mitigation). The PR
cannot be merged until the write‑up is approved.

---

## Nightly CI "check-no-release-blockers"

The workflow at `.github/workflows/nightly.yml` has a step that runs
after the golden suite:

```yaml
- name: Check for release-blocker PRs
  run: |
    blockers=$(gh pr list --label "release-blocker" --json number --jq '.[].number')
    if [ -n "$blockers" ]; then
      echo "::error::Open release-blocker PRs: $blockers"
      exit 1
    fi
```

If this step fails, the "demo-readiness" badge in the README flips
to amber (the workflow updates the commit status).

---

## Waiving a blocker

In exceptional cases (e.g., the blocker PR is a documentation fix
that got mis‑labeled), the release manager can waive:

1. Comment on the PR with `@release-manager waive-blocker` and the
   justification.
2. Re-run the `check-no-release-blockers` step manually (GitHub UI →
   Re-run jobs).
3. Document the waiver in `docs/RELEASE_NOTES.md`.

---

## Questions?

- **Who is `@citevyn/triage`?** See `CODEOWNERS.md`.
- **How do I add a new safe‑pattern?** Edit `auto-label.yml` and add a
  `contains` clause; commit it to `main` (no PR needed).
- **What if auto‑merge merges a bad PR?** Revert immediately, then
  open a `release-blocker` PR with the fix.
