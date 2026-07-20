# Cost Controls

Operational cost policy for CiteVyn's paid model providers, implementing
`RELEASE_PLAN.md` §9 (soft $5 / hard $10 daily).

This document is the map of the layers. It is written defence-in-depth: each layer
assumes the one above it has failed.

| Layer | Control | Status |
|---|---|---|
| 0 | Provider-side per-key spend cap | **Live** (owner-configured, outside the app) |
| 1 | Per-call metering (tokens + priced cost) | **Live** (LLM only; embedder open) |
| 2 | Admission control (concurrency + budget check) | **Live** — see §2 |
| 3 | Global daily budget (§9 soft/hard) | **Live** — see §3 |
| 4 | Per-user rate limit behind a persisted store | Partial (in-process today) |
| 5 | Spend visibility + `make budget` | Planned — #153 |
| 6 | CI spend bounding | **Live** — see §6 |

---

## 0. Provider-side cap — the only layer app code cannot bypass

The OpenRouter key carries a **per-key spend limit set at the provider**. It is the
outermost backstop: a bug in the metering, a runaway retry loop, a leaked key, or a
process that never reaches our own budget check all still stop at this ceiling,
because it is enforced by OpenRouter, not by us.

Two properties matter and neither is automatic:

* **It must reset DAILY.** A lifetime cap is a one-shot fuse: once burned the demo
  is dead until someone notices and raises it by hand. §9 is a *daily* policy, so
  the provider cap must be daily to correspond to it.
* **It must be raised to match §9's $10 hard limit before going public.** The cap
  currently in force is a development-scale figure ($1.10, ~96% consumed as of
  2026-07-20). Layer 3 stops paid calls at $10/day; a provider cap below that makes
  Layer 3 unreachable and turns every overage into an opaque upstream 402 instead of
  our controlled response.

Read the live value — free, no inference. `make budget` is **planned** (§5, not yet
implemented); today, read it directly:

```bash
curl -sS https://openrouter.ai/api/v1/key \
  -H "Authorization: Bearer $CITEVYN_OPENROUTER_API_KEY"
```

A 402 from the provider already degrades correctly today and needs no code change:
the LLM path collapses any `>=400` to `LLMUnavailable`, and the embedder's `>=400`
branch raises `EmbedderUnavailable` **without retrying**. The request surfaces as a
transient 5xx, not as a content refusal — which is the right shape (see §3).

---

## 1-3. Metering, admission control, and the daily budget — LIVE

**Layer 1 — metering.** `provider_calls` (migration 0005) records tokens, priced
cost, provider, model, call site and attempts for every paid LLM call. Priced from
`app/cost/pricing.py`, keyed by **provider + model**. Covers the LLM only; the
embedder seam is identical but ~1/10th the per-token price and is still open.

**Layer 2 — admission control.** `app/cost/admission.py` caps paid calls **in
flight** at `CITEVYN_COST_MAX_CONCURRENT_CALLS` (default 8). This exists because of
how the budget reads: every in-flight call sees a spend total that excludes its
peers, so an unbounded burst can collectively overshoot a limit each member
individually satisfies. Scope is per process; under multi-worker uvicorn the
effective cap is `workers x limit`. The daily budget is the cross-process control.

**Layer 3 — the §9 daily budget.** `app/cost/budget.py`, enforced on the same seam
as metering, **before** the provider call:

| Setting | Default |
|---|---|
| `CITEVYN_COST_SOFT_DAILY_USD` | `5.0` |
| `CITEVYN_COST_HARD_DAILY_USD` | `10.0` |
| `CITEVYN_COST_BUDGET_FAIL_CLOSED` | `true` |
| `CITEVYN_COST_BUDGET_ENABLED` | `true` (kill switch) |

* **Soft** warns and biases toward cache. Correctness is deliberately unchanged —
  a $5 day must not silently start degrading answers.
* **Hard** raises `CostLimitReached`, which subclasses `LLMUnavailable` so every
  existing caller surfaces it as a **transient 5xx, never a content refusal**.
  A no-answer envelope would teach the client the corpus lacks an answer and
  suppress retry (#142).
* Spend is a **SQL sum over `provider_calls` since midnight UTC**, so a restart
  cannot reset it — the exact flaw that makes the 30 q/h per-user limiter
  anti-nuisance only rather than a spend control.
* **Fail-closed** when the meter store is unreadable: if we cannot tell what has
  been spent, we cannot tell whether we are over budget, and fail-open turns a
  database blip into an unmetered spending window. Flip
  `CITEVYN_COST_BUDGET_FAIL_CLOSED=false` to trade cost for availability —
  deliberately, not by accident of error handling.

`budget_snapshot()` exposes today's spend, remaining budget and the 60% / 85% warn
flags for the Layer-5 admin surface.

## 4-5. Still open

Tracked on
[#153](https://github.com/imrohitagrawal/citevyn/issues/153). The design constraints
that the implementation must honour:

* **Metering is the prerequisite.** Token counts already ride on `LLMResult`
  (`input_tokens` / `output_tokens`) but are persisted nowhere, so there is no data
  to meter from. Cost must be priced from a table keyed by **provider + model**, so
  swapping a model cannot silently mis-bill, and every call must record its **call
  site** — `answer`, `condense`, `alias_intent`, `ingest`, `eval`.
* **A hard-limit trip is a transient failure, never a content refusal.** Returning
  a no-answer/refusal envelope teaches the client that the corpus lacks an answer and
  suppresses retry — this is exactly the #142 bug. The hard limit must return the
  transient 5xx envelope.
* **Fail-closed when the meter store is unavailable.** If we cannot tell how much has
  been spent, we cannot tell whether we are over budget. Fail-open converts a Redis
  or Postgres blip into an unmetered spending window. This must be an explicit,
  documented setting — not an emergent property of the error handling.
* **The per-user limiter is anti-nuisance only.** 30 q/h per user does not bound
  total spend on an anonymous public demo: a user who exhausts it starts a new
  session. The **global daily cap is the real protection**; the per-user limit exists
  to stop one client monopolising the demo.

### Spend rows are durable on purpose — including under the eval harness

The meter (`app/cost/meter.py`) commits each `provider_calls` row on **its own
session**, not the caller's. That is what makes a spend record survive a request that
later rolls back — a validation error, a failed citation check, a 500. The money left
the account before our transaction failed; a spend ledger that unwinds with our own
failures under-reports exactly the paths that go wrong most.

The same rule holds for tests, and it has one visible consequence worth stating
plainly, because it contradicts a claim made elsewhere in the repo:

> `tests/eval/retrieval.py` and `tests/eval/runner.py` advertise **"zero residue"**
> for the judged `--postgres` eval — it seeds under a unique `index_version` with
> `commit=False` and rolls back on every exit path.

That guarantee covers the **catalog** (documents, chunks, index_versions, sessions,
messages) and nothing else. A judged `--postgres` run makes real paid calls, so it now
also writes `provider_calls` rows, and those **persist after the rollback by design**.
Erasing them would mean the run that spends the most real money is the one the §9
daily budget cannot see. The eval's pre-flight "target catalog must be empty" check
counts `Chunk` only, so leftover spend rows never block a subsequent run.

If you genuinely want to forget spend history (a throwaway scratch DB, a
reproducibility experiment), truncate `provider_calls` explicitly. It is never done
for you.

---

## 6. CI spend — measured, then bounded

With zero users, CI is a large share of total spend, so it was measured rather than
guessed.

### What a judged run actually costs

The judged eval (`answer-quality-eval` in `.github/workflows/ci.yml`) drives the real
orchestrator per golden case and then scores each answer with a judge panel. Call
volume was **counted at $0** with `scripts/measure_eval_spend.py`, which swaps in a
counting fake provider (no network, no key). Reproduce it with:

```bash
cd backend && PYTHONPATH=. CITEVYN_EVAL_JUDGE_PANEL=1 \
    uv run python ../scripts/measure_eval_spend.py
```

| Panel size | Judged cases (hermetic) | Answer calls | Judge calls | Total paid calls |
|---|---|---|---|---|
| `CITEVYN_EVAL_JUDGE_PANEL=1` (CI) | 42 | 21 | 84 | **105** |
| `=3` (local default) | 42 | 21 | 168 | **189** |

Reading the table: 42 = 58 golden cases − 16 `postgres_only`. Only 21 of those 42
make an answer call, because this is the *hermetic* run — with the vector arm dead,
the other cases retrieve nothing and the orchestrator short-circuits to a refusal
before reaching the LLM. The judge is `N + 1` calls per case (N framings + the
adversarial veto), so it dominates: **~80% of calls** at the CI panel size and ~89%
at the local default.

CI runs `--postgres`, where the live vector arm lets far more cases reach the LLM and
real answers run ~300 output tokens rather than the fake's stub. Extrapolating:

* ~63 orchestrator asks (58 cases + 5 replayed `followup` history turns) + ~116
  judge calls
* ~88k input / ~21k output tokens
* **≈ $0.026 per judged CI run** at `openai/gpt-4o-mini` list price ($0.15 / $0.60
  per 1M tokens)

The **call counts are measured; the dollar figure is an extrapolation** — a fake
provider cannot produce real answer lengths. Token counts use the repo's own
4-chars/token convention. Treat it as an order of magnitude, not a bill.

**An honest correction to the working assumption.** At ~$0.03 per run, ten CI runs
cost ~$0.30 — real, but *not* the dominant share of the $1.06 that was consumed
before this was written. A local `make eval` is ~2.5× a CI run (it defaults to panel
3, and CI pins panel 1), so repeated local judged runs, plus live QA traffic, were
the larger lines. "CI is the largest spend line" was not borne out by measurement,
and Layers 1–3 remain the controls that actually cap exposure.

### The bound: frequency, not sampling

CI runs the judged eval **at full coverage, less often**:

* **push to `main`** → runs (full set).
* **pull request labelled `full-eval`** → runs (full set). The `labeled` event is in
  the workflow's trigger `types`, so adding the label to an open PR starts a run on
  its own.
* **any other pull request** → does not run.

Previously it ran on every PR *push*, so a five-push PR cost ~$0.13. Bounding
frequency is where the saving actually is.

#### Why sampling cases was rejected

`--judge-subset N` exists in the runner (`tests/eval/subset.py`) and is a useful
*local* tool, but CI does not use it. The reason is arithmetic: **42 of the 58 golden
cases carry a zero-tolerance, judge-independent oracle** —

* `must_not_contain` — prompt-injection resistance (any leak fails),
* `kind: followup` — the multi-turn echo oracle, #169 (any echo fails),
* `expected_facts` — per-case groundedness at coverage 1.0 on `--postgres` (one
  wrong install command or auth header fails),
* `kind: refusal` — the judged refusal-leak gate,
* `judge_only` — no other validation path exists.

Sampling does not average those down; it **switches them off**. Only the judge's 1–5
mean (`MIN_MEAN_JUDGE`) degrades gracefully. So the honest saving ceiling from
sampling is ~28%, bought by silently disabling hard gates — strictly worse than
running less often at full strength.

The refusal case deserves a specific note, because it is easy to get wrong:
`gate_failures` uses the *judged* refusal-leak count when the LLM ran and falls back
to the retrieval count only in an `elif`. On a judged run the judged count is
therefore the **only** refusal gate — the retrieval half does not back it up. A
dropped refusal case would be checked by neither. `is_priority` retains all of them,
and the runner fails the run outright if a subset ever excludes a zero-tolerance case
(`judge.subset.dropped_zero_tolerance`), so narrowing that rule is loud, not silent.

**Tradeoff, stated.** An unlabelled PR gets no judged gate at all; a judged
regression surfaces on merge to `main`. Label a PR `full-eval` to gate it before
merge.

The **retrieval half is unaffected** — it runs over every case on every run, and it
is what gates literal/overall hit-rate, MRR/precision@1, multihop and followup. It
costs one short query embedding per case (~$0.00002 total).

### Developing without spending

Set `CITEVYN_LLM_PROVIDER=stub` and `CITEVYN_EMBEDDING_PROVIDER=stub`. The judge
self-skips under the stub while the retrieval half stays fully hermetic, so hit-rate,
MRR, precision@1, refusal leaks and the multi-turn echo oracle all still run — free.
Iterate with:

```bash
python -m tests.eval.runner --no-judge
```

Paraphrase cases score 0.000 under the stub. That is the documented hermetic
baseline, not a regression.

**Never set a placeholder key to "save money."** The judge activates on any non-stub
provider; every call then 401s, and the run fails the judge-coverage gate. `stub` is
the designed mechanism.

### Retry amplification — audited

Both embedders retry a transient failure up to `embedding_max_retries` (default 2),
so a worst case is **3 attempts**, not unbounded. Retries are:

* **bounded** — `for attempt in range(self._max_retries + 1)`, with exponential
  backoff (`0.5s`, `1.0s`) so a 429 is not hammered;
* **transient-only** — a fatal 4xx (401, 402, 403) does **not** retry, so an
  exhausted or revoked key costs exactly one call, not three.

The LLM path does not retry in-provider at all; `app/llm/fallback.py` tries the
secondary provider once. There is no amplification loop.

Retries are *not* yet counted in spend, because nothing is — that is Layer 1's job,
and the metering must count **attempts**, not logical calls, or a flaky provider will
under-report by up to 3×.

---

## References

* `docs/RELEASE_PLAN.md` §9 — the soft/hard daily limit policy this implements.
* [#153](https://github.com/imrohitagrawal/citevyn/issues/153) — live demo + cost guardrails.
* [#142](https://github.com/imrohitagrawal/citevyn/issues/142) — why a provider outage
  must not surface as a content refusal.
