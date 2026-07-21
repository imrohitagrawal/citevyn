# Next-session prompt (copy everything below the line)

---

ultracode — full authority. Repo: `/Users/rohitagrawal/Projects/citevyn` (branch `main`, clean and
synced with origin, at `6a18709`).

Read `AGENTS.md`, `code_review.md`, `docs/BACKLOG.md`, `docs/RAG_QUALITY_PLAN.md` and session
memory FIRST. Small reviewable PRs off main; adversarial review before each merge; merge each PR
yourself once CI is green. No Claude attribution footer.

**GOAL: close #215, #216, #217 and the missing favicon (Task 4) — in that order.**
**#215 is the one that matters; the rest are contained.**

Work autonomously: open a small PR per task, review it adversarially, merge it yourself once CI is
green, and move to the next. Do not batch unrelated tasks into one PR. If a task turns out to be
bigger than its description (this is called out explicitly for #215), STOP and report rather than
half-doing it.

**Repo hygiene is already done — do not redo it.** As of 2026-07-21 `main` is the ONLY branch,
local and remote; 18 local and 11 remote branches were deleted after verifying each was merged or
superseded. Eight pre-existing stashes remain UNTOUCHED and are the owner's to decide on — do not
drop them.

═══════════════════════════════════════════════════════════════
COST DISCIPLINE — READ FIRST
═══════════════════════════════════════════════════════════════
OpenRouter has ~**$1.12** of credit remaining. It is a balance, not a monthly allowance.

- `backend/.env` is on `stub` for both providers. **Leave it there.** Opt in per-run:
  `CITEVYN_LLM_PROVIDER=router CITEVYN_EMBEDDING_PROVIDER=openrouter <cmd>`.
- The judged eval runs **only** on a `v*` tag, `workflow_dispatch`, or a `full-eval`-labelled PR.
  Merging to main costs $0. **Do not restore the push-to-main trigger.**
- The **live demo** now spends real money on every question (Gemini free tier primarily, with
  OpenRouter as the LLM fallback). App-side ceiling is $2/day. Do not loop questions against
  production to "test" retrieval — that is what the eval harness is for.
- Before ANY paid call, state why the free path cannot answer the question.

═══════════════════════════════════════════════════════════════
TASK 1 — #215: retrieval refuses a well-covered question (THE PRIORITY)
═══════════════════════════════════════════════════════════════
Reproduced against the live demo on 2026-07-21:

| Query | confidence | citations |
|---|---|---|
| "What are the rate limit**s** **on** the Claude API?" | **none** | **0 — REFUSES** |
| "What is the Claude API rate limit?" | low | 1 |
| "How many requests per minute does the Claude API allow?" | low | 1 |
| "What does `CLAUDE_API_RATE_LIMIT` do?" | low | 1 |

`backend/app/worker/sources/claude_api.md` has a substantive `## Rate limits` section (50 req/min,
`CLAUDE_API_RATE_LIMIT`, HTTP 429, retry-after). Routing is correct in all four cases
(`domain: claude_api`, `intent: how_to`) and `retrieval_strategy` is `hybrid_reranked` throughout.
**This is retrieval, not routing.**

**Two facts must shape the investigation. Do not skip past them:**

1. The **failing** phrasing is the one closest to the source heading (`## Rate limits` — plural,
   same as the query). The phrasings that succeed are paraphrases. That is backwards from expected
   ranking behaviour and suggests something structurally wrong, not ordinary lexical variance.
2. **Every success is `confidence: low` with exactly one citation.** The whole corpus is retrieving
   at the floor, so the gap between "answers" and "refuses" is a rounding error. **This query is a
   symptom; the thin margin is the disease.** A change that makes only this query pass is the WRONG
   outcome and should not be merged as a fix.

**Method — in this order:**

- **Reproduce in the eval harness BEFORE touching anything.** Use the `rag-eval` skill. Add all
  four phrasings as golden cases (one expected-answer plus three paraphrases) so the fix is
  measured, not asserted, and so the regression is caught next time.
- Get the **actual retrieval scores** for all four queries before forming a theory. If three sit
  just above the confidence floor and one just below, the question is whether the floor is right —
  not whether this query is special.
- Then investigate, in rough order of suspicion:
  - **confidence gating at the margin** — where is the floor, and is it defensible?
  - **chunk composition** — the chunker prefixes every chunk with the document H1 (see #162). With
    a 43-63 line source, an H1 prefix plus a short section may be drowning the section's own signal.
  - **the reranker** — compare pre- and post-rerank ordering; was the right chunk retrieved and
    then dropped?
  - **the lexical arm** — plurals, and the `on the` preposition.

**Constraints:**
- The locked golden set must NOT regress.
- The eval harness builds from `conftest.seed_catalog` and therefore **cannot** see shipped-corpus
  regressions — check `backend/app/worker/sources/` separately (this is the #162 lesson).
- Do **not** "add more corpus". The content is present and correct; retrieval is not finding it.
- Do **not** tune thresholds against the live demo — that is measuring noise, and it costs money.
- Mutation-test any threshold or comparison you touch, and **grep to confirm the mutation applied**
  (ruff-format can silently un-apply one).

**If #215 turns out to be a corpus-wide chunking or threshold change:** STOP, write up what you
found with the numbers, and report before merging. A change that moves every answer in the product
is a different decision from a bug fix, and it is the owner's call.

═══════════════════════════════════════════════════════════════
TASK 2 — #216: the #210 promotion gate is inert
═══════════════════════════════════════════════════════════════
`promote_version` gates on the candidate's newest completed `EvaluationRun` — but **nothing in the
deployed app writes those rows**. `app/services/evaluations.py` is read-only and the only
`EvaluationRun(...)` constructions in the repo are in tests. So every production promote hits
`reason: no_evaluation_run` and needs the audited `?force=true`.

PR #212 bought an audit trail and a deliberate speed bump, **not** a live threshold. That was a
documented trade, but it must not be permanent or `force` becomes muscle memory.

"Done" = a promote refused because the candidate genuinely measured below threshold, with no human
typing `force`. The open design question (deliberately left open in #210) is **which runner
persists the result**: `backend/tests/golden/runner.py` already emits the right shape
(`scoring.py:266`) but measures `conftest.seed_catalog`, not the deployed corpus; a worker-side pass
would measure the real index but is new machinery. Pick one and justify it.

**Trap to preserve:** `scoring.py` scores an EMPTY suite as `pass_rate: 1.0`. The gate already
rejects a zero case count — keep it that way, and never persist a zero-case run as passing.

Also covers `db/seed/seed_catalog.py:152`, which promotes by direct ORM write and bypasses the gate.
Correct today (bootstrap has no eval run, and it is how `v1` was promoted on the first Fly deploy),
but it should be a documented exception rather than an accident.

═══════════════════════════════════════════════════════════════
TASK 3 — #217: README lists endpoints that do not exist
═══════════════════════════════════════════════════════════════
`POST /v1/ask`, `POST /v1/admin/ingest` and `GET /metrics` are all absent from the live
`app.openapi()`. PR #212 fixed the admin rows and the API_SPEC §13 phantom path but left these.

Real paths: `/health`, `/health/dependencies`, `/health/index`, `/v1/sessions`,
`/v1/sessions/{id}`, `/v1/sessions/{id}/messages`, `/v1/sessions/{id}/messages/{message_id}`,
`/v1/search/exact`, `/v1/admin/budget`, `/v1/admin/evaluations[/{run_id}]`,
`/v1/admin/index_versions[/{v}][/promote]`, `/v1/admin/ingestion_jobs[/{job_id}]`.

**Add a test asserting the README table against `app.openapi()`.** Two separate documents have now
shipped invented endpoint paths; a check is cheaper than a third discovery.

Note wherever the README shows a request: auth is `Authorization: Bearer <key>` (NOT
`X-Demo-API-Key`) and the body field is `message` (NOT `content`).

═══════════════════════════════════════════════════════════════
TASK 4 — the demo has NO favicon (browser tabs show a blank page icon)
═══════════════════════════════════════════════════════════════
`frontend/index.html` contains `<link rel="icon" type="image/svg+xml" href="/favicon.svg" />`, but
**`frontend/public/` does not exist**, so the file was never shipped. Verified against production:

```
/favicon.svg          -> 404
/favicon.ico          -> 404
/apple-touch-icon.png -> 404
```

Every browser tab, bookmark and history entry for the live demo therefore shows a generic blank
icon. On a public portfolio piece that is the first thing a visitor sees.

**The fix is `frontend/public/`** — Vite copies that directory verbatim into `dist/`, which the API
then serves from the StaticFiles mount. No build config change is needed.

**Scope — keep it proportionate, but do not ship only the SVG:**
- `favicon.svg` — modern browsers, and the one already referenced.
- `favicon.ico` (32x32 + 16x16) — Windows, pinned tabs, and anything that still asks for `/favicon.ico`
  by convention regardless of the `<link>`.
- `apple-touch-icon.png` (180x180) — iOS home screen; without it iOS renders a screenshot.
- a minimal `site.webmanifest` + `<meta name="theme-color">`.

**Design constraints (no brand mark exists — this must be created):**
- There is NO logo asset anywhere in the repo. The wordmark is plain text: "CiteVyn" followed by a
  small yellow `01` badge. Check `~/Downloads/design_handoff_citevyn_landing/` for the palette
  (see the `citevyn-landing-design-source` memory) and take the accent colour from there rather
  than inventing one.
- **It must read at 16x16.** That rules out the wordmark and anything with fine detail. A single
  monogram or a citation-bracket motif is the right level of complexity.
- It must be legible against BOTH light and dark browser chrome — test both, do not assume.

**The engineering lesson matters more than the icon.** `index.html` referenced a file that never
existed, through a full build, a Docker image, CI, and a production deploy, and nothing caught it.
**Add a test that parses `frontend/index.html`, extracts every local asset href/src, and asserts
each one resolves to a real file in the build output.** That closes the whole class, not just this
instance. A favicon that 404s is cosmetic; a *hero image* that 404s the same way would not be.

═══════════════════════════════════════════════════════════════
GOTCHAS — do not rediscover these
═══════════════════════════════════════════════════════════════
- **Verify agent self-reports against the running system.** A parallel agent's #208 fix reported
  "7 tests passed, 4 mutations killed" — all true — and still failed live. Parallelise
  implementation, never verification.
- **GREEN != COVERED.** Mutation-test anything you call a guard, and grep to confirm the mutation
  actually applied.
- Run pytest **FROM REPO ROOT**; from `backend/` a gitignored `.env` bleeds `CITEVYN_*`.
- Check the test **COUNT**, not the exit status. Baseline: **1316 passed, 15 skipped**.
- `git commit -m` with no pathspec commits the whole index. Always pass explicit paths.
- Never `git checkout` to undo a mutation on uncommitted work — restore from a `cp` backup.
- Do NOT modify `backend/.env` or `infra/docker/.env`.
- A **refusal is often correct** — the corpus is six documents. Before calling one a regression,
  `grep -ri "<term>" backend/app/worker/sources/`. #215 is a bug precisely because the content IS
  there.
- A question with no product noun ("what is prompt caching?") routes to `domain: unsupported` BY
  DESIGN. Test with the product named.

═══════════════════════════════════════════════════════════════
LIVE-OPS FACTS (the demo is PUBLIC — treat production carefully)
═══════════════════════════════════════════════════════════════
- **https://citevyn.stackclimb.com** — live, TLS issued, UI served by the API at `/`.
- Fly app `citevyn`, org `personal`, region `iad`, ONE `shared-cpu-1x:512MB`, scale-to-zero.
  Fly creates 2 machines for HA on first deploy — `fly scale count 1` if that recurs.
  **Do not change machine memory without asking the owner** (`fly.toml` says so, and
  `test_fly_config.py` pins it).
- Neon Postgres 18 + **pgvector 0.8.1**, project `citevyn` (`empty-band-46458527`). App uses
  `postgresql+psycopg://`; Alembic migrates fine over the POOLED DSN.
- Upstash Redis provisioned via `fly redis create` (not a separate Upstash account). **Lua `EVAL`
  is verified working** — do not re-litigate. `fly redis create` / `proxy` require a TTY and cannot
  be automated. `fly redis reset` is a BREAKING change: it needs a matching `fly secrets set` and
  ~30s, during which rate-limited routes return `rate_limiter_unavailable`.
- Rate limiter is per-visitor (`citevyn:rl:demo_<hash>`) plus a global backstop — #203, verified in
  production.
- Frontend: `VITE_API_LIVE=true` MUST be passed at image build time or the chat silently answers
  from its canned in-bundle `knowledgeBase` and never touches the backend.
- Uptime probe: `.github/workflows/uptime.yml`, every 30 min. **Do not tighten the cron** — every
  probe wakes the scale-to-zero machine, and a 5-minute cadence would cost more than it monitors.
- `fly apps create` does NOT allocate IPs; `fly ips allocate-v4 --shared` + `allocate-v6` do.

═══════════════════════════════════════════════════════════════
OWNER-GATED (cannot be done by an agent — remind, do not attempt)
═══════════════════════════════════════════════════════════════
- The Cloudflare MCP token is **read-only** (writes return `10000: Authentication error`). DNS
  automation needs a token with `Zone:DNS:Edit` on `stackclimb.com`.
- Neon / Upstash / UptimeRobot signups and any browser OAuth.
- Never paste a credential into the chat. Read it from `fly redis status` / the provider console
  and pipe it straight into `fly secrets set`, echoing only its length and host.

REPORT: ship/no-ship per task with REAL command output. State plainly anything you could not
verify, and anything you decided NOT to do and why.
