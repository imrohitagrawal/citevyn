# Autonomous session handoff — 2026-07-20

Written by the agent that ran the "#153 cost controls → release blocker 9 → backlog →
v0.10.0" brief. Read this together with `docs/BACKLOG.md` and the live
`gh issue list --state open`.

---

## Ship / no-ship, per phase

| Phase | Outcome |
|---|---|
| **1-A** — Layers 0 + 6 (CI spend) | **SHIPPED** — PR #182, merged `112c3ff` |
| **1-B** — Layer 1 (metering) | **SHIPPED** — PR #184, merged `3464aa3` |
| **1-C/D** — Layers 2–5 | **NOT STARTED** |
| **1-E** — meter the embedder | **NOT STARTED** (deliberate split) |
| **2** — `make deploy-verify` (blocker 9) | **NOT RUN** — feasible; see below |
| **3** — five backlog issues | **IN FLIGHT** — branches exist, see below |
| **4** — housekeeping | **PARTIAL** — #161 and PR #39 closed |
| **5** — cut v0.10.0 | **BLOCKED ON OWNER** — deliberately not attempted |

**No paid provider call was made during this session.** Development ran entirely on
`CITEVYN_LLM_PROVIDER=stub`.

---

## What landed

### PR #182 — CI judged-eval spend, bounded by frequency

The judged eval ran on **every PR push**. It now runs on a push to `main`, or on a PR
carrying the `full-eval` label, **at full coverage**.

Case sampling was implemented, measured, and **rejected**. 42 of the 58 golden cases
carry a *zero-tolerance, judge-independent* oracle — prompt injection, the #169
multi-turn echo, per-case groundedness, refusal leaks, `judge_only`. Sampling does not
average those down; it **switches them off**. The honest saving ceiling was ~28%, paid
for by silently disabling hard gates. `--judge-subset N` survives as a *local* tool
(`backend/tests/eval/subset.py`); the runner now **fails** the run if a subset ever
excludes a zero-tolerance case.

It also fixed a pre-existing bug: the job's `if:` tested
`github.event.pull_request.head.repo.full_name`, which is **null on a push**, so the
judged gate **never ran on `main` at all**.

### PR #184 — per-call cost metering (Layer 1)

`provider_calls` + migration `0005` (additive, working downgrade, verified up→down→up
on real Postgres). Priced by **provider + model**. An unknown model records
`priced=false` with cost 0 — not guessed, not dropped; `unpriced_calls` is the
under-counting alarm.

---

## Cost reality — an explicit correction

The brief assumed CI was the largest spend line. **It is not.** A full judged CI run is
**≈ $0.026** (call volume counted at $0 by `scripts/measure_eval_spend.py`; the dollar
figure is an extrapolation). Ten CI runs ≈ $0.30 of the $1.06 consumed. A local
`make eval` is ~2.5× a CI run — it defaults to judge panel 3 while CI pins 1 — so
**repeated local judged runs plus live QA were the bigger lines.**

**Layers 2–3 (admission control + the §9 daily budget) are the controls that actually
cap exposure, and they do not exist yet.** Layer 1 is only the substrate.

---

## Traps found the hard way — do not rediscover

1. **Never wrap `StubLLMClient` in the metering decorator.** `tests/eval/runner.py`
   decides whether to run the *paid* judge with
   `isinstance(get_llm_client(...), StubLLMClient)`. Behind a decorator that check goes
   `False`, the judge activates on every hermetic run, and the free development path
   starts spending real money. `_metered()` refuses to wrap a stub; `build_llm_client`
   stays pure and only `get_llm_client` wraps.
2. **Never price a model by prefix match.** A first attempt matched the longest known
   prefix at a `-` boundary. It billed `gemini-2.5-flash-lite` at Flash rates (**6.25×
   over** → budget trips early, demo dies) and `-realtime-preview` / `-image` variants
   at the text tier (**4–12× under** → budget never trips). Only dated snapshots and
   OpenRouter `:routing` suffixes may collapse onto a base model.
3. **A `200` can carry no usage block.** Every client defaults usage to 0, so a real
   billed call recorded as `0 tokens / $0 / priced=true` — invisible to the budget.
4. **Migrations must `from sqlalchemy.dialects import postgresql` explicitly.**
   `import sqlalchemy as sa` does not pull the submodule; `0005` worked only because
   alembic eagerly loads `0002`, which happens to import it.
5. **`github.event.pull_request` is null on a `push`.** A PR-only `if:` silently skips
   the job on `main`. `postgres-migrations` still has this — issue **#183**.

---

## Phase 2 — `make deploy-verify` (release blocker 9): NOT RUN, but feasible

Verified as prerequisites, not assumed:

- `infra/docker/Caddyfile:52` honours `CITEVYN_ACME_CA`, and `CITEVYN_PUBLIC_HOST` is
  already `localhost` — **no public domain needed**.
- The prod env guard passes locally (`_env_guard.sh` exits 0).
- `--dry-run` executes and prints a coherent plan.

**The one blocker:** the gate requires `VERSION` to be an existing git **tag** and a
second `v*` tag for the rollback drill. Only `v0.9.0` exists, so `--dry-run` reports
`rollback target: <none found>`.

**To run it:** create a **local-only** tag (never pushed — a pushed tag triggers image
publish via `release.yml`), then:

```bash
git tag -a v0.10.0-drill -m "local rollback drill"      # DO NOT PUSH
CITEVYN_ACME_CA=internal VERSION=v0.10.0-drill PREV_VERSION=v0.9.0 \
  CURL_OPTS=-k make deploy-verify
```

**Expect the rollback leg to fail.** It checks out `v0.9.0`'s tree and builds it, and
`v0.9.0` predates the deploy-path repairs in #94 and the Python 3.14 base-image fix in
#34 (dependabot's runtime-only bump was non-booting). If it fails there, that is a
**finding about rollback reachability**, not a harness bug — record it rather than
papering over it.

It was not run in-session because it does `git checkout` in the main worktree, which
would have disrupted the verifier agents running there.

---

## Phase 3 — five backlog issues, in flight

Branches exist locally (created by a workflow, each in an isolated worktree, each
followed by an adversarial verifier):

| Branch | Issue | State |
|---|---|---|
| `fix/167-ratelimit-code` | #167 misleading `index_unavailable` | committed |
| `fix/163-checksum-misnomer` | #163 `content_checksum` misnomer | committed (adds migration **0006**) |
| `fix/168-demo-checklist` | #168 stale routes/port | committed |
| `fix/84-citevyn-meta` | #84 offline-copy convergence | committed |
| `fix/178-corpus-single-source` | #178 corpus in four places | **not committed — incomplete** |

**None has been reviewed by me, opened as a PR, or merged.** Before merging any of
them: read the diff, run `make lint && make typecheck` and the suite **from the repo
root**, and mutation-test anything claimed as a guard. `fix/163` adds a migration —
verify its downgrade on real Postgres, and check `EXPECTED_TABLES` in
`backend/tests/test_migrations.py`.

---

## Phase 5 — deliberately left for the owner

Not attempted, and should not be automated:

1. The OpenRouter cap is ~exhausted ($1.06 of $1.10) and **only you can raise it**.
2. A final judged eval costs real money and needs the `CITEVYN_OPENROUTER_API_KEY`
   repo secret restored.
3. **Pushing the tag triggers an image publish** (`release.yml`) — and there is no host
   to deploy to yet.

Before cutting: `pyproject` already says `0.10.0`, only a **draft** `v0.9.0` release
exists, and `CHANGELOG.md` needs everything since. All nine §10 blockers must be
evidenced individually — blocker 9 in particular is **still open** (see Phase 2).

---

## Open follow-ups created this session

- **#183** — `postgres-migrations` never runs on push to `main` (same null-payload bug).
- **Meter the embedder** — the seam is identical to the LLM one; embeddings are ~1/10th
  the per-token price, which is why it was split. `CallSite.ingest` and `CallSite.eval`
  are defined but unwired, so eval-driven calls currently attribute to `answer`.
- **Eval `--postgres` runs now leave durable `provider_calls` rows.** This is
  *intentional* — spend is a fact about money already gone and must not be rolled back
  by our own teardown — and is documented where the "zero residue" claim is made.
