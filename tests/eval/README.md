# RAG eval golden set (`golden.jsonl`)

Phase 0 of `docs/RAG_QUALITY_PLAN.md` (issue #96): the **measurement foundation** for
RAG quality. This is distinct from `tests/golden/` (the assertion-based regression
runner). Here we measure two *outcome* metrics:

1. **Retrieval hit-rate** — does any top-k retrieved chunk come from the expected
   source? Fully hermetic (seeded SQLite + the real `HybridRetriever`, no network/LLM).
2. **Answer quality** — an LLM-as-judge score (1–5) vs an expected gist. Requires a real
   LLM provider (Gemini, free); skipped — never faked — under the stub.

Executor code lives in `backend/tests/eval/`; run it with `make eval` or
`python -m tests.eval.runner`. The CI regression gate is
`backend/tests/test_eval_harness.py`.

## Line schema (one JSON object per line)

| Field | Type | Meaning |
|---|---|---|
| `id` | str | Unique case id. |
| `area` | str | `claude_api` / `claude_code` / `codex` / `gemini_api` / `citevyn` / `out_of_corpus`. |
| `kind` | str | `literal` / `paraphrase` / `refusal` (see below). |
| `question` | str | The user question. |
| `expected_source` | str \| null | Seed `source_name` the correct chunk belongs to; `null` for refusals. |
| `expected_gist` | str | Short summary of the correct answer, scored by the judge. |
| `expect_no_answer` | bool | `true` for refusal/out-of-corpus cases. |
| `expected_facts` | list[str] | *(optional, answerable-only)* Hard facts a correct grounded answer MUST state — see **Groundedness** below. |

Blank lines and `#`-prefixed lines are skipped, so the file stays annotatable.

## Answer-quality robustness (Item 1)

A single LLM judge can be noisy or over-score a *plausible-but-wrong* answer. The
judged run (`--postgres`) therefore combines three signals:

1. **Prompt-ensemble panel** — each answer is scored by N distinct rubric *framings*
   (not temperature samples) at temp 0.0; the **median** smooths one framing's
   interpretation bias while staying reproducible. `CITEVYN_EVAL_JUDGE_PANEL` sets N
   (odd; default 3).
2. **Adversarial veto** — one skeptical fact-checker pass; the gated score is
   `min(standard_median, adversarial)`, so a skeptic that catches an over-scored
   answer pulls it down (it is a floor, never averaged away).
3. **Deterministic groundedness** — judge-*independent*. Declare `expected_facts` on a
   case (phrasing-stable tokens — env-var names, headers, CLI commands, or a number
   *with* its unit; each entry may list `|`-separated alternatives, any-of). A fact is
   matched at word boundaries, so `"50 requests per minute"` is **not** satisfied by
   `"150 requests per minute"` or `"0.50 requests per minute"` — a wrong hard fact
   fails regardless of the judge. **Gated per case on the `--postgres` run only** (the
   mode where fact-bearing answerable cases can actually retrieve; the hermetic
   dead-vector-arm path would structurally zero the paraphrase fact-cases, so it is
   excluded — exactly like the multihop gate): every fact-bearing case must be **fully
   grounded** there, so a single wrong fact fails (an aggregate mean over binary
   single-fact cases would leak it). A golden-integrity test asserts each fact is
   groundable (at least one alternative appears in the seed corpus).

## Adversarial hardening (Item 2)

Two case fields sharpen the meter against adversarial inputs:

- **`postgres_only: true`** — the case is meaningful ONLY on the live vector arm
  (misspellings the keyword/exact arm can't recover; in-domain near-miss refusals). It
  is **excluded from the hermetic run and its gates** (mirrors the multihop/followup
  Postgres-only pattern) and evaluated on the `--postgres` judged run. This is the
  supported way to add a case that would misfire on the dead-arm SQLite path.
- **`must_not_contain: [...]`** — **prompt-injection resistance**. A list of forbidden
  (non-empty) substrings — a sentinel the injection tells the model to emit, e.g.
  `"PWNED"`. Checked **judge-independently** on the judged run. A case counts as OBEYING
  the injection (a leak, zero tolerance) only when the sentinel is present AND the answer
  failed to answer (its `expected_facts` are not fully grounded) — so a resistant answer
  that names the sentinel while declining ("I won't reply with PWNED …") is not a false
  leak. **Declare `expected_facts` on an injection case** so "did it still answer?" is
  measurable. **Keep the sentinel inside the question.** The oracle is only real while
  the question actually tells the model to emit the sentinel: reword the question and
  keep `must_not_contain`, or change the sentinel to a string no model would produce,
  and the check passes by construction with `declared`, `cases` and `leaks` all looking
  healthy. `test_the_golden_set_still_carries_every_zero_tolerance_oracle` asserts each
  sentinel appears verbatim in its own case's `question`.
- **The same field guards a forbidden phrase (#554).** `codex_lit_no_provided_evidence`
  puts "based on the provided evidence" in its question and forbids `provided evidence` in
  the answer, so the system prompt's ban on that phrasing is checked on the judged run. It
  declares no `expected_facts` on purpose: with facts declared, a fully grounded answer that
  still used the phrase would not count as a leak. The report and CI log call it an injection
  case, because it shares the injection oracle.
- **The one exception to "sentinel inside the question" (#554 item 3).**
  `codex_unprompted_evidence_phrase` asks "What models does Codex support?" with no phrase in
  it, because production showed the model writing "The provided evidence ..." for exactly that
  question unprompted, so this oracle can fail without being asked. It forbids `evidence` (a
  word the corpus never uses, so whatever the served-text rewrite leaves is a real leak) plus
  `provided context` and `provided information`, which the rewrite does not touch, so the case
  still measures the prompt. The exemption is the pinned `UNPROMPTED_PHRASE_CASES` set in
  `backend/tests/test_eval_harness.py`, each id with its production evidence; a partner test
  fails if an exempt id leaves the golden set or stops declaring `must_not_contain`. It is
  `kind: paraphrase` because the keyword arm retrieves nothing for it, so it must not sit in
  the literal 1.0 gate.
- **This oracle has a population gate (#450).** The report's `injection` block carries
  `declared` — how many of the cases the run could drive carry a sentinel — beside
  `cases`, the number it actually drove, and a judged run where `declared > 0` and
  `cases != declared` FAILS. Zero leaks over zero cases is not a pass, and neither is
  zero leaks over ONE of two cases: a case lost to a provider 429 is half a
  zero-tolerance oracle missing. The line is printed on every judged run that is
  not `--quiet`, including when `cases` is 0 — it used to print nothing at all
  there. `--quiet` suppresses the whole summary block, and every per-push caller
  passes it; the release command does not, which is the run whose log matters.
  `declared` is counted over the cases the run could drive, not the whole file, because
  every row carrying `must_not_contain` is `postgres_only` and a hermetic judged run
  legitimately drives none. **What the gate does NOT catch:** removing `must_not_contain` from the golden
  rows, because `declared` is derived from that field and both numbers go to 0 together.
  That edit is caught hermetically, and only, by
  `test_the_golden_set_still_carries_every_zero_tolerance_oracle`.

Deferred (tracked follow-up): context precision/recall + a distractor corpus need
chunk-level relevance identity (retrieved chunk ids + stable chunk keys + gold-chunk
labels on a separate distractor index) — a distinct, careful PR that must not perturb the
locked hermetic baseline. Growing the golden set toward 50–100 and a human-labeled judge
calibration subset ride along there.

## CI enforcement of answer quality

The hermetic retrieval gate runs in the standard `pytest + lint` CI job (no key, no
network). The **judged answer-quality gate** (`MIN_MEAN_JUDGE` + per-case groundedness +
prompt-injection resistance) runs in a dedicated `answer-quality-eval` CI job that spins up
a pgvector service and drives the orchestrator per golden case with a real LLM + embedder.

It requires the OpenRouter key as a **repo Actions secret** (`CITEVYN_OPENROUTER_API_KEY`),
which only the repo **owner** can add. Until it is present the job **skips** every
meaningful step (a loud `::notice::`) and stays green — the key is never hardcoded or
faked. **To enable the gate:** add `CITEVYN_OPENROUTER_API_KEY` under *Settings → Secrets
and variables → Actions*; the next run flips it on.

### Cost bounding (#153)

The judged half is the harness's only meaningful spend line — ~$0.026 per full run
(call volume counted by `scripts/measure_eval_spend.py`; the dollar figure is an
extrapolation — see `docs/COST_CONTROLS.md` §6). CI bounds it by **frequency, at full
coverage**:

* **push to `main`** → runs.
* **PR labelled `full-eval`** → runs (the `labeled` event triggers the workflow).
* **any other PR** → does not run.

`CITEVYN_EVAL_JUDGE_PANEL=1` further caps each run at 2 judge calls/case (1 rubric
framing + the adversarial veto) instead of the local default of 4.

**CI does not sample cases.** `--judge-subset N` exists for local iteration, but 46 of
the 63 golden cases carry a zero-tolerance judge-independent oracle (injection,
multi-turn echo, per-case groundedness, refusal leak, judge-only) that sampling would
*switch off* rather than average down — so the honest saving ceiling is ~27%. The
subset always retains those cases, and the runner **fails** the run if one is ever
excluded. Note that the judged refusal-leak gate is the *only* refusal gate on a
judged run (the retrieval fallback in `gate_failures` is an `elif`), so it is not
backstopped by the retrieval half.

The **retrieval half always runs over every case**, gating literal/overall hit-rate,
MRR/precision@1, multihop and followup.

Tradeoff, stated: an unlabelled PR gets no judged gate; a judged regression surfaces
on merge to `main`. Label the PR `full-eval` to gate it before merge.

## Kinds

- **`literal`** — shares vocabulary with the seed corpus; the keyword/exact arm can hit.
- **`paraphrase`** — a semantic equivalent with (near) zero literal overlap with the
  answer text. These isolate the **dead vector arm** (#97): expected hit-rate ~0 at the
  Phase 0 baseline. When embeddings are populated (Phase 1) and domain scoping softens
  (Phase 2), these are the cases whose hit-rate should climb — the eval delta each PR must
  prove.
- **`refusal`** — out-of-corpus / off-domain. The correct behavior is to retrieve
  **nothing** and decline. Never counted toward answerable hit-rate.

## Anchoring

Cases are anchored to the **conftest** seed corpus (`backend/tests/conftest.py::seed_catalog`)
— the corpus the hermetic harness and CI run against — **not** `db/seed` (whose Gemini chunk
is about the API key, not streaming). Keep new cases answerable from that 5-chunk corpus, or
the hit-rate metric loses meaning.

## Shipped-corpus labels (#601)

Two label sets measure search on the 44 shipped passages rather than the 5-chunk seed.
Labels were written from the shipped text before any vector existed, and can be edited
without re-recording, because a recorded vector depends only on the question text.

- **`shipped_gold`** in `golden.jsonl` and `shipped_questions.jsonl`: every passage that on
  its own answers the question, as `source#heading`. `[]` means reviewed, and no passage
  answers it. Refusals never carry the field. One key is single-relevant; two or more are
  multi-relevant, and leave the question out of the rank-sensitive decision number.
- **`shipped_questions.jsonl`** holds the coverage questions, reported separately from the
  golden set. Optional fields: `facet` (the sub-case), `expected_empty` with
  `expected_empty_scope` (`exact_arm`, or `all_arms` for a question that should retrieve
  nothing at all), `expected_area`, `expected_arm`, `expected_outcome` (what the author
  expected; the measured result may differ), `history`, `planned_for` and `note`.

| Kind | Count | Why |
|---|---|---|
| `exact` | 19 | The widest token surface: flags, environment variables, headers, error codes, methods, commands, two case and punctuation variants each, three near-miss tokens that must not match. Carries #370's code tokens. |
| `entity` | 11 | One per term type the extractor really produces, an alias, two entities at once, a follow-up that keeps its entity, two entities outside the corpus. |
| `keyword` | 7 | Words present only literally, suffix pairs, and a stop-word-only question. |
| `vector_scope` | 4 | Two identically worded pairs over headings that exist in two products, so only product scope separates them. |
| `semantic` | 13 | Five phrasings of meaning, 2-3 each: zero overlap, synonyms, misspellings, long rambling, non-English (Hindi, Spanish, romanized Hindi). |
| `hybrid` | 6 | Two each: only keyword can solve, only vector can solve, both. |
| `multi_product` | 6 | Five two-product comparisons, and one naming three products. |

`model_name` and `command` are term types with no extracted term in the shipped corpus, so
they have no case. `en_outside_model` asks for a model id the corpus does not hold.

## Shipped-corpus search report (#601)

`backend/tests/test_eval_shipped_report.py` runs in the `postgres` job and appends the report
to the job summary: recall@1/3/6, MRR and precision@1, by kind and coverage kind, with each
question's route and winning arm. No model is called, and there are no floors yet.

**Decision A** is precision@1 over answerable, single-relevant, non-follow-up golden
questions. The report prints the pool size and every excluded count. Coverage questions
report separately, and a scoped refusal retrieving something is informational, because only
the global path has a confidence gate.

The vectors live in `tests/eval/recorded/`: one per shipped chunk, keyed `source#heading` with
its content checksum, and one per exact question text, keyed by `text_sha256`. Each file's
first line states the vector space. A chunk whose text changed fails the fixture test and the
`postgres` job, naming that chunk and the command. Labels can be edited without re-recording.

To record, from `backend/`, with the embedding provider's key in the environment:

```bash
python -m tests.eval.record_vectors --dry-run                                # free: lists every text
python -m tests.eval.record_vectors --export-chunks-from <local postgres URL> # embeds missing questions
python -m tests.eval.record_vectors --embed-changed-chunks                    # also embeds changed chunks
```

`--dry-run` needs no key or database. A real run needs, in the environment:

- `CITEVYN_EMBEDDING_PROVIDER` and `CITEVYN_EMBEDDING_MODEL` set to production's embedder (the
  recorder refuses any other; both file headers must match it);
- `CITEVYN_GEMINI_API_KEY`, the provider key;
- `CITEVYN_DATABASE_URL` naming a migrated local database, because embedding goes through the
  metered embedder, which records each call there;
- `--export-chunks-from` with a local Postgres URL holding the current index, for free chunk
  vectors.

A re-record is a paid provider call and needs the owner's approval. Files are rewritten after
every embedded text with sorted lines, so a re-run resumes, and the first failure stops the run.

The report also removes each arm in turn: `arm` names the arm whose removal changes the
rank-1 passage (`vector`, `keyword`, `exact`, `both` or `none`), beside the gold rank with only
the vector arm (`rank_vector_only`) or only the keyword arm (`rank_keyword_only`). A vector arm
with any chunk unembedded stops the report. Questions production answers without retrieving
(`clarify`, `refused`) are labelled, not excluded.
