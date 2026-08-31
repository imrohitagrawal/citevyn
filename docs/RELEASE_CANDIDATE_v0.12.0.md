# Release candidate v0.12.0 — ship/no-ship pack

Prepared autonomously (`ultracode` Wave 0). **No tag was pushed and nothing was
deployed** — both are owner-gated (`fly deploy` is classifier-blocked by
design; a version tag fires `release.yml`'s image publish). This document is
the pack the owner needs to make that call and execute it.

## One-screen ship/no-ship

| Check | Result |
|---|---|
| Commits since `v0.11.0` | **28**, all already merged to `main` (head `3042f7e`) |
| Open tracked bugs | **0** — every open bug issue is closed; 13 open issues are all V1/V2 feature work, none blocking |
| Backend tests | **1437 passed / 17 skipped** (provider=stub, repo root) — re-run in this session on `3042f7e`, matches the prior baseline exactly |
| Lint / typecheck | **Re-run in this session**: `ruff check` all green, `ruff format --check` all 193 files formatted, `pyright` 0 errors/0 warnings |
| CHANGELOG | `[Unreleased]` now covers all 28 commits (this PR) |
| `answer_policy_version` | `v6` (bumped 4 times since `v0.11.0`'s `v2`: v2→v3 #215/#236, v3→v4 #263, v4→v5 #237/#262, v5→v6 #226) — **cache invalidates automatically, no flush needed** (see below) |
| Migrations since `v0.11.0` | **None.** `git log v0.11.0..main -- db/versions` is empty, head stays `0006` — this release is a pure application-code release, no schema coordination needed |
| Security advisories | 5 open (4 high, 1 medium), all frontend-lockfile, all closed by PRs #249/#252/#244 (Wave 1, separate PR) |
| **Ship recommendation** | **Ship.** No open bugs, no schema risk, cache invalidates itself. The only real decision is *when* the owner wants to spend the `fly deploy` + Gemini re-embed cost of a corpus re-ingest (not required by this release — no corpus content changed). |

## Does `answer_cache` need flushing on deploy?

**No — verified from the code, not assumed.** `backend/app/cache/answer_cache.py`
bakes `answer_policy_version` directly into the SHA-256 cache-key pre-image
(`build_cache_key`, lines 78–113):

```
key = sha256(normalized_question || product_area || source_version_hash
             || answer_policy_version || embedder_identity)
```

A lookup after deploy uses `answer_policy_version="v6"`. Every row cached
under `v2`–`v5` sits at a **different key** and can never be read again — it
is not "stale data that might be served," it is **unreachable by
construction**. It ages out via the existing TTL and is never an active risk.
This differs from the `v0.11.0` release, where the CHANGELOG explicitly told
the operator to flush — that guidance is `v0.11.0`-specific and does not
carry forward; no flush command is needed for this release.

## Exact ordered commands (owner-run)

Run these from the repo root, on `main`, at the commit the owner has decided
to ship (`3042f7e` or later once this PR and the Wave 1 dependency PRs merge).

```bash
# 1. Pre-tag verification (repeat what CI proves, once, locally)
env -u CITEVYN_DATABASE_URL uv run --project backend pytest backend/tests -q
cd backend && ruff check . && ruff format --check . && pyright && cd ..
make golden        # golden-case suite against the shipped corpus
make eval          # RAG eval harness (judge self-skips without a real key — expected)

# 2. Rename the CHANGELOG section and tag
#    Edit CHANGELOG.md: "## [Unreleased]" -> "## [0.12.0] - <today's date>"
git add CHANGELOG.md
git commit -m "chore: release v0.12.0"
git tag -s v0.12.0 -m "v0.12.0"
git push origin main --tags   # fires release.yml: build -> boot-smoke -> push :latest + :v0.12.0

# 3. Confirm the images actually published and boot
gh run watch --exit-status $(gh run list --workflow=release.yml -L1 --json databaseId -q '.[0].databaseId')

# 4. Deploy (Fly) — owner-only, not run by this session
fly deploy --build-arg VERSION=$(git describe --tags --always)
fly logs
fly status

# 5. Post-deploy verification (RELEASE_PLAN §10 / DEPLOY_FLY §4)
make deploy-verify          # functional probe suite + both rollback drills
make budget                 # confirm §9 daily spend headroom before any re-ingest

# 6. Optional, only if re-promoting evaluation evidence on the live index:
fly ssh console -a citevyn -C "python -m app.worker.cli evaluate --index-version v1"
```

**Rollback**, if step 4 or 5 finds a problem: `make rollback TAG=v0.11.0` (see
`docs/RUNBOOK.md` §5 — the drills in step 5 above already proved this path
end to end on the current migration generation; there is no migration in
this release, so rollback is a pure code revert, no `--allow-migration-
mismatch` needed).

## What did NOT change and needs no action

- No new migration — `db/versions` head is unchanged since `v0.11.0` (still
  `0006`; verified via `git log v0.11.0..main -- db/versions`, empty).
- No corpus content changed — no re-ingest is required by this release
  (Wave 1/2 work in flight separately may change that later).
- No new environment variable or secret.
