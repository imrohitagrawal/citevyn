# CiteVyn Release Plan

## 1. Purpose

This document defines the release plan for CiteVyn MVP and the roadmap to enterprise readiness.

## 2. Release Principles

1. Release only after quality gates pass.
2. Do not deploy a candidate index directly.
3. Always keep a last known good index.
4. Keep MVP small and credible.
5. Defer enterprise features until answer quality is proven.
6. Exclude voice from MVP.

## 3. MVP Release Scope

MVP includes:

1. Claude.
2. Claude Code.
3. Codex.
4. Gemini.
5. Chat Q&A.
6. Official documentation ingestion.
7. Contextual chunking.
8. Hybrid retrieval.
9. Exact lookup.
10. FAQ/cache routing.
11. Citations.
12. No-answer guardrail.
13. Basic security.
14. Basic observability.
15. 50-question evaluation suite.

## 4. MVP Non-Goals

1. ChatGPT.
2. Cursor.
3. Voice.
4. Private docs.
5. Enterprise RBAC.
6. Tenant isolation.
7. Reviewer workflow.
8. Automated freshness.
9. General web search.

## 5. Phased Release Plan

### Phase 0: Architecture and Planning

Exit criteria:

1. Architecture package approved.
2. ADR-0001 approved.
3. Source list locked.
4. Golden dataset template approved.
5. Demo cost limit defined.

### Phase 1: Foundation Build

Scope:

1. FastAPI backend.
2. PostgreSQL + pgvector.
3. Redis cache.
4. Demo auth.
5. Admin API key.
6. Basic frontend.
7. Health endpoints.

Exit criteria:

1. Services run with Docker Compose.
2. Health endpoints pass.
3. Auth and rate limits work.
4. Admin endpoints are protected.

### Phase 2: Ingestion and Indexing

Scope:

1. Source fetchers.
2. Parsers.
3. Contextual chunker.
4. Exact term extractor.
5. Embedding generation.
6. Candidate index creation.
7. Ingestion job status.

Exit criteria:

1. All MVP sources ingest successfully.
2. Failed ingestion is visible.
3. Candidate index is not promoted automatically.
4. Last known good index exists.

### Phase 3: Retrieval and Answer Engine

Scope:

1. Domain guardrail.
2. Intent router.
3. Exact lookup.
4. Keyword search.
5. Vector search.
6. Reranker.
7. Answer generator.
8. Citation validator.
9. No-answer fallback.

Exit criteria:

1. Exact lookup works.
2. Unsupported questions are refused.
3. Factual answers include citations.
4. No-answer behavior works.

### Phase 4: Evaluation and Observability

Scope:

1. 50-question golden suite.
2. Evaluation runner.
3. Quality metrics.
4. Structured logs.
5. Basic dashboard.
6. Alert thresholds.

Exit criteria:

1. Golden pass rate >=95%.
2. Domain guardrail critical failures = 0.
3. Citation correctness >=95%.
4. Retrieval hit rate >=95%.
5. P95 latency is acceptable for demo.

### Phase 5: MVP Demo Release

Scope:

1. Package demo.
2. Deploy locally or on single VM.
3. Run release checklist.
4. Record known limitations.
5. Prepare next-phase backlog.

Exit criteria:

1. Release gates pass.
2. No critical security gaps.
3. No stale diagram links.
4. Rollback tested.
5. Demo script ready.

## 6. Deployment Strategy

MVP deployment:

```text
Docker Compose
FastAPI
PostgreSQL + pgvector
Redis
React or Next.js frontend
Background worker
Structured logs
```

Optional cloud demo:

```text
Single VM
Docker Compose
Reverse proxy
TLS
Environment-based secrets
```

## 7. Index Promotion Strategy

```text
Fetch docs
 -> Build candidate index
 -> Run ingestion validation
 -> Run golden evaluation
 -> Promote if gates pass
 -> Otherwise keep active index
```

Promotion gates:

1. Golden pass rate >=95%.
2. Citation correctness >=95%.
3. Retrieval hit rate >=95%.
4. Domain guardrail critical failures = 0.
5. Ingestion errors = 0.

## 8. Rollback Strategy

1. Keep previous good index.
2. Do not overwrite active index during candidate build.
3. Promote indexes explicitly.
4. Revert by promoting previous good index.
5. Continue serving from last known good index if candidate fails.

## 9. Cost Controls

MVP defaults:

```text
soft daily limit: $5
hard daily limit: $10
```

On soft limit:

1. Prefer cache.
2. Log warnings.
3. Optionally disable expensive reranking.

On hard limit:

1. Stop LLM generation.
2. Allow cached answers.
3. Allow exact lookup.
4. Return controlled limit message.

## 10. Release Blockers

Do not release if:

1. Golden pass rate is below 95%.
2. Domain guardrail fails critical unsupported tests.
3. Citation validator fails.
4. Exact lookup fails for known commands or flags.
5. No-answer behavior fails.
6. Cache serves answer without citations.
7. Admin endpoints are unprotected.
8. Ingestion failures are hidden.
9. Rollback is not tested.

### Blocker 9 — what "rollback is tested" means, exactly

There are **two** rollback paths, and only one of them is generally provable.
`make deploy-verify` runs them as separate drills and its summary states which
of the two an individual run actually exercised. It never reports a path it did
not run (that ambiguity is what [#195](https://github.com/imrohitagrawal/citevyn/issues/195)
was about).

| # | Path | What it is | Status |
|---|---|---|---|
| A | **Data-recovery rollback** (RUNBOOK §4.2) | `backup.sh` → stop `api`/`worker` → `restore.sh` → api healthy → full functional re-verify | **PROVEN end to end** — run 2026-07-20, see the evidence below. Drill A always runs. |
| B | **Code rollback to the previous tag** | `rollback.sh <prev>` → re-verify → roll forward → re-verify | **PROVEN end to end** — same run. Runs only when `PREV_VERSION` is the same migration generation; otherwise the run says so and does not claim it. |

#### The evidence

A full `deploy_verify.sh` run against a real local prod stack on **2026-07-20**
scored **42 passed / 0 failed**, exercising both drills end to end:

```
 [PASS] drill A: backup -> pg_restore -> stack healthy
 [PASS] post-restore …: in-corpus question returns a CITED answer   (+7 more probes)
 [PASS] drill B: rollback to v0.10.1-drillbase
 [PASS] rolled-back …: in-corpus question returns a CITED answer    (+7 more probes)
 [PASS] roll forward to v0.10.2-drilltop
 [PASS] restored …: in-corpus question returns a CITED answer       (+7 more probes)
 passed: 42   failed: 0
 production: api container UP and healthy, serving v0.10.2-drilltop
 rollback coverage:
   ✓ data-recovery rollback (RUNBOOK §4.2) — PROVEN end to end
   ✓ code rollback to v0.10.1-drillbase + roll forward — PROVEN end to end
```

The full probe suite — cited answer, refusal, exact lookup, admin 401 — was
re-run against the **rolled-back** stack, not just a health check. A rollback
that boots but cannot answer is not a rollback.

**What this does and does not establish.** `v0.10.1-drillbase` and
`v0.10.2-drilltop` are **local, unpushed** drill tags (a pushed `v*` tag triggers
an image publish via `release.yml`). They are a genuine same-migration-generation
pair — both ship `0001`–`0006` — so the drill exercised the real mechanism:
`rollback.sh` checked out the older tree, rebuilt the images, redeployed,
re-verified, and rolled forward again. What it does **not** establish is that any
particular *published* release pair is rollback-compatible; that is a property of
the tags, checked per release by the same gate. `v0.9.0` in particular is neither
same-generation nor bootable (#195).

**Why B is conditional.** `rollback.sh` rolls back code by checking out an older
tag and rebuilding; the database is untouched, so it stays stamped at the newest
applied alembic revision. If that revision's file does not exist in the older
tree, `alembic upgrade head` cannot build the version graph and dies with
`Can't locate revision identified by '0006'` — inside a one-shot container,
mid-deploy. **A code-only rollback across a forward-only migration boundary is
impossible.** No tag choice fixes it.

What changed as a result:

- `rollback.sh` now **refuses before touching anything** when the target tree is
  missing a migration `HEAD` ships, and names the recovery path (§4.2). It used
  to warn and then proceed into that failure. `--allow-migration-mismatch`
  overrides it — the correct use is *after* restoring a dump from the target
  release, or when you know the intervening migrations are additive-only.
- `deploy_verify.sh` runs drill A always, and drill B only when it can succeed.
  When it cannot, the gate **asserts the refusal is fast** and then FAILS, unless
  the operator narrows the scope with `--data-rollback-only` — in which case the
  summary prints `blocker 9 is PARTIAL, not closed`.

**Still not proven, and honestly so:** the **cross-generation** recovery — restore
a dump taken while the OLDER release was live, then
`rollback.sh <target> --allow-migration-mismatch` — has not been run end to end.
Drill A restores a dump it took seconds earlier at the *same* schema generation,
which proves the backup/restore plumbing and that the stack survives its database
being dropped and rebuilt underneath it. It does not prove the §4.2 sequence for a
dump that predates a migration. `--allow-migration-mismatch` has argument-level
coverage only.

There is also no **published** release pair in the same migration generation
(`v0.9.0` predates four migrations *and* cannot boot — #195), so the drill above
used local tags. The first release pair that ships no migration makes drill B
reproducible by anyone.

**Test coverage vs drill coverage.** `tests/shell/test_drill_crash_safety.sh`
drives the real drill (`_drill_lib.sh`) with `docker`, `backup.sh` and
`restore.sh` stubbed, and asserts the property that matters — every exit path
leaves the writers running — including a failed restore, a failed restart, an
unhealthy api, a failed `stop`, and an abort between the stop and the restart.
`test_rollback_migration_guard.sh` covers the refusal logic and the `--base-ref`
contract. Neither needs docker. What still needs a live prod stack is the drill
itself, which is what `make deploy-verify` is for.

## 11. V1 Roadmap

V1 is deliberately **depth over breadth**: portfolio-grade polish, a reachable live
demo, and the answer-quality/feedback flywheel — no new content domains and no new
heavy surfaces (those are V2). Tracked under the **V1** GitHub milestone.

1. **Live hosted public demo + cost guardrails** — deploy the existing stack to a
   reachable HTTPS URL; wire the §9 soft/hard daily cost limits (a hard prerequisite
   before any public URL) and confirm the abuse rate-limiter on the public path. This
   is the highest-ROI V1 item and also completes the Phase-5 live deploy-verify +
   rollback gate ([#153](https://github.com/imrohitagrawal/citevyn/issues/153)).
2. **Real SSE streaming for chat answers** — stream tokens as generated instead of the
   client-side reveal. Verified on `main` @ v0.10.0: **no streaming route exists today**
   (`messages.py` has only POST/GET; no `StreamingResponse`/`text/event-stream`), so this
   is a real backend build (new endpoint + frontend consumer), not a rewire
   ([#61](https://github.com/imrohitagrawal/citevyn/issues/61)).
3. **Feedback capture → eval loop** — 👍/👎 (+ reason) per answer, persisted, and piped
   into the golden eval harness. NB: the value is the eval/data flywheel and corpus-gap
   detection, **not** model retraining (the LLMs are hosted, not ours to fine-tune). Most
   invasive V1 item — touches DB + API ([#154](https://github.com/imrohitagrawal/citevyn/issues/154)).
4. **Evaluation + live-ops dashboard** — surface the existing eval metrics (hit-rate,
   judge, groundedness, refusal leaks, MRR/precision@1, distractor precision/recall) plus
   live cost/latency/refusal-rate ([#155](https://github.com/imrohitagrawal/citevyn/issues/155)).
5. **Better re-ranking** — a re-rank stage after candidate retrieval; feature-flagged,
   cost-aware, and proven on the golden + distractor eval sets
   ([#156](https://github.com/imrohitagrawal/citevyn/issues/156)).
6. **Frontend hardening: composer gating** — gate the composer while a live answer is in
   flight to prevent concurrent-send stream interleave
   ([#62](https://github.com/imrohitagrawal/citevyn/issues/62)).

## 12. V2 Roadmap

Breadth and heavier surfaces, deferred until V1 depth is proven. Tracked under the
**V2** GitHub milestone.

1. **ChatGPT (OpenAI) official docs** — a 5th product domain. Deferred for two real
   reasons: it is *breadth, not depth* (low portfolio signal), and it is
   *licensing-gated* (ADR-0003 requires curated, license-clean docs; OpenAI doc
   redistribution terms must be checked first). Not deferred for UI risk — the UI delta
   is small and test-covered ([#157](https://github.com/imrohitagrawal/citevyn/issues/157)).
2. **Voice output (TTS)** — large surface (TTS, audio UI, latency) that does not
   reinforce the core retrieval-with-citations story; also an explicit MVP non-goal
   ([#158](https://github.com/imrohitagrawal/citevyn/issues/158)).
3. Voice input.
4. Cursor docs.
5. Reviewer-agent workflow.
6. Automated freshness / scheduled source refresh — low value while the corpus is
   curated, static, and license-clean (`HttpFetcher` is a deliberately-unwired seam);
   a manual `make refresh` runbook suffices until the corpus actually needs automation.
7. Cache invalidation by document version.
8. Browser extension.

## 13. Enterprise Roadmap

1. SSO.
2. RBAC and ABAC.
3. Tenant isolation.
4. Private source connectors.
5. Admin portal.
6. Audit exports.
7. Compliance controls.
8. Slack and Teams integrations.
9. Cost controls.
10. Multi-source governance.
