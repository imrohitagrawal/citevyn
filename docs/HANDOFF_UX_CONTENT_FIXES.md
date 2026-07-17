# Handoff — CiteVyn UX + content + retrieval fixes (from live-QA session 2026-07-17)

> Paste the "PROMPT FOR NEW CHAT" block below into a fresh Claude Code chat (start it with
> the word **ultracode**). Everything needed is here — root causes are already diagnosed
> (file:line), so the new session should NOT re-investigate, only design → fix → verify.

The owner live-tested the app and surfaced UX bugs, dev-jargon, and retrieval gaps. Decisions
(owner-confirmed): (1) ADD a concepts/FAQ content source so conceptual questions get grounded
answers; (2) FIX conversation memory INCLUDING issue #112 now (do NOT defer); (3) THOROUGHLY
de-jargon the WHOLE codebase's user-visible copy (not just the flagged strings). Use ultracode
for the broad slices; localized frontend bugs can be direct edits. Every slice through the full
loop (plan → TDD → gates → eval proof where retrieval/answer changes → fan-out review → PR → CI
→ merge). Repo: /Users/rohitagrawal/Projects/citevyn. main was at `517afca` at handoff time.

## Findings (root causes already verified — fix these)

### Frontend bugs
1. **New question not scrolled into view (long chat).** `frontend/src/components/ChatView.tsx:74-82`
   — the ONLY autoscroll effect scrolls only when already within 120px of bottom; there is NO
   scroll-on-send. Fix: on an explicit submit, force the new user bubble into view (e.g. a
   `sendTick` counter bumped in `useLandingState.ts` `submitChat`/`send`, plus a `ChatView`
   effect `useEffect(()=>{ if(sendTick) list.scrollTop=list.scrollHeight },[sendTick])`).
2. **Jittery/stuck scroll-up during streaming.** Same effect re-runs on EVERY streamed token and
   re-pins to bottom while within the 120px band → fights the user's upward scroll. #122 only
   fixed a stale-ref race. Fix: replace the 120px slack with a stick-to-bottom LATCH — a `scroll`
   listener sets `stickRef=false` on any upward move, re-arms only at the true bottom (≤8px); the
   autoscroll effect pins only when armed.
3. **Stale hero composer on Back-to-landing.** `useLandingState.ts` — `heroInput` state is never
   cleared on `askHero`/`enterChat`/`backToLanding` (the chat composer IS cleared in `submitChat`
   via `SET_CHAT_INPUT:""`). Fix: dispatch `SET_HERO_INPUT:""` in `askHero` before `enterChat(q)`
   (belt-and-suspenders: also in `backToLanding`).
4. **"Grounded in official documentation from…" cut off at left edge.**
   `frontend/src/components/landing-sections.tsx:43-58` renders `.sources-strip` but the wrapper
   holding padding/max-width/centering — `.sources-strip-inner` (`landing.css:663-672`) — is
   defined in CSS but NEVER rendered in JSX; `.mono-label` has `max-width:180px` so it jams at
   x=0. Fix: wrap the strip content in `<div className="sources-strip-inner">`.
   MISSING TESTS (both bugs ship green today — `behavior.spec.ts:502` only checks atBottom after
   streams finish): add (a) scroll-up-then-send → new bubble in view; (b) scroll-up-mid-stream →
   position holds for ~500ms.

### De-jargon (THOROUGH — audit ALL user-visible copy, these are just the confirmed hits)
- `ChatView.tsx:91` badge "LIVE — backend answers" / "DEMO — canned responses";
  `ChatView.tsx:230` "Answers come from the live backend."
- `Hero.tsx:49` "Cited answers for AI dev tools".
- FAQ `landing-sections.tsx:581` "The MVP covers…", `:597` "Not in the MVP…", `:683`
  `<span class="mvp-badge">MVP</span>` (CSS `landing.css:1783`, widen for a longer word).
- `frontend/src/data/knowledgeBase.ts:135,291,308,351,367` — user-visible demo/offline copy
  ("MVP demo", "AI dev tools", etc.).
- DRIFT RISK: FAQ copy is duplicated — `faqDefs` (`landing-sections.tsx:578-603`, RENDERED) vs
  `faqItems` (`useLandingState.ts:913-926`, DEAD/unconsumed); MVP/private-docs answer triplicated
  across `landing-sections.tsx`, `useLandingState.ts`, `knowledgeBase.ts`. Consolidate to one
  source of truth. Do a full grep sweep for user-facing "backend|dev\b|MVP|walking skeleton|
  slice|stub|hermetic" etc. across `frontend/src/**` (exclude comments/types/tests).

### Retrieval / content
5. **"what are the different models?" (follow-up) refuses; standalone answers.** RCA: memory
   rewrite `build_contextual_query` fires only for BARE anaphora (`is_anaphoric_followup`,
   `backend/app/answer/memory.py:81`), NOT a content-noun follow-up ("the different **models**")
   — deliberately conservative to avoid hijacking topic pivots (adversarial review R1). This IS
   tracked issue **#112** (entity-aware rewrite). OWNER WANTS #112 FIXED NOW. Approach: an
   entity-aware rewrite that inherits the prior turn's product entity/domain for a context-
   dependent follow-up, WITHOUT hijacking a genuine off-corpus pivot ("what's the weather?") —
   the hard constraint measured before: handing generation only the bare pronoun makes the LLM
   refuse a real follow-up. A cheap partial: when a follow-up routes to `unsupported` but a recent
   turn had a clear product domain, inherit that domain. Design adversarially; MUST NOT regress
   the locked eval (followup 3/3) nor the off-corpus-pivot refusal (#112's own residual). Update
   the `followup` golden bucket with content-noun cases; close #112.
6. **"what is an LLM?" / "is Codex an LLM?" refuse** — NO source doc contains "LLM"; refusing is
   CORRECT today. OWNER WANTS a concepts/FAQ source ADDED. Live corpus = worker-ingested
   `backend/app/worker/sources/*.md` (claude_api, claude_code, codex, gemini_api, citevyn),
   registered in the MVP source list; re-ingested via `python -m app.worker.cli run`. ADD a
   curated, license-clean `concepts.md` (what an LLM is; that Claude/Codex/Gemini are LLM-backed
   tools; a short glossary/FAQ) as a new source; keep answers grounded + cited. CRITICAL EVAL
   NUANCE: the eval golden is anchored to `backend/tests/conftest.py::seed_catalog` (a SEPARATE
   hand-written 5-chunk fixture), NOT the worker sources — so adding a worker source does NOT feed
   the eval. To eval concept questions, add matching chunks to `seed_catalog` (blast-radius
   sensitive — there's a one-chunk-per-area guard in `test_eval_semantic_discrimination`) OR use
   the distractor-style separate seed pattern. Weigh carefully; don't silently break the locked
   19/19 + one-chunk-per-area guard.
7. **Process:** don't only run predefined golden scenarios — generate NEW questions from the FAQs
   + real multi-turn follow-ups when live-testing. The 50-case golden validates in/out-corpus
   correctness, not corpus COMPLETENESS, and its followup cases are bare-anaphora only — grow it
   from real session transcripts.

## Environment / runbook (verified working this session)
```bash
export PGPW=$(grep '^POSTGRES_PASSWORD=' infra/docker/.env|cut -d= -f2-)
export DB_URL="postgresql+psycopg://citevyn:$PGPW@localhost:5432/citevyn"
export OR_KEY=$(grep '^CITEVYN_OPENROUTER_API_KEY=' infra/docker/.env|cut -d= -f2-)
make db-up && CITEVYN_DATABASE_URL=$DB_URL uv run --project backend alembic -c db/alembic.ini upgrade head
# TRUNCATE exact_terms,chunks,documents,index_versions,messages,sessions CASCADE; then:
# LIVE corpus (real embeddings): worker ingest → promote (index starts 'candidate', promote to active)
cd backend && CITEVYN_DATABASE_URL=$DB_URL CITEVYN_ENVIRONMENT=local CITEVYN_EMBEDDING_PROVIDER=openrouter \
  CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small CITEVYN_OPENROUTER_API_KEY=$OR_KEY \
  uv run python -m app.worker.cli run                       # → 33 chunks embedded, index v-local
# backend (real LLM + real embeddings):
CITEVYN_DATABASE_URL=$DB_URL CITEVYN_ENVIRONMENT=local CITEVYN_LLM_PROVIDER=router \
  CITEVYN_OPENROUTER_API_KEY=$OR_KEY CITEVYN_EMBEDDING_PROVIDER=openrouter \
  CITEVYN_EMBEDDING_MODEL=openai/text-embedding-3-small uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 &
curl -s -X POST :8000/v1/admin/index_versions/v-local/promote -H "X-Admin-API-Key: local-admin-key"
# frontend (proxies /v1 → :8000; VITE_API_LIVE=true already in frontend/.env.local):
cd frontend && npm run dev                                 # http://localhost:3000
# chat API: POST /v1/sessions {"user_id":"demo_user","channel":"chat"} (Bearer local-demo-key);
#           POST /v1/sessions/{id}/messages {"message":"..."}   (field is "message", NOT "content")
```
Gotchas: DB password comes from infra/docker/.env (NOT literal "citevyn"); `make demo`/`make seed`
default DB_URL uses citevyn:citevyn — pass DB_URL explicitly. seed_users required. Session channel
must be "chat". Frontend dev proxy handles CORS. Eval: hermetic `env -u CITEVYN_DATABASE_URL uv run
pytest -m "not postgres"`; judged `python -m tests.eval.runner --postgres`; distractor `python -m
tests.eval.distractors`. Locked numbers must NOT regress (core 19/19, multihop 3/3→5/5 after #134,
followup 3/3, refusal leaks judged 0, injection 0/2, groundedness 1.000, MRR/precision@1 1.000).
See docs/RAG_QUALITY_PLAN §8a-7/8a-8/8a-9, AGENTS.md, code_review.md, docs/BACKLOG.md, and the
session memories.

## PROMPT FOR NEW CHAT
> ultracode — CiteVyn live-QA fixes. Read docs/HANDOFF_UX_CONTENT_FIXES.md FIRST (all root causes
> diagnosed with file:line — do NOT re-investigate). Owner live-tested the app and wants: (A) fix
> the 4 frontend bugs — scroll-to-new-question, jittery scroll-up (stick-to-bottom latch), stale
> hero composer on back-to-landing, cut-off `.sources-strip` — WITH the two missing Playwright
> regression tests; (B) THOROUGHLY de-jargon ALL user-visible copy across frontend/src (backend/
> dev/MVP/etc.), consolidating the duplicated/triplicated FAQ + MVP copy to one source of truth;
> (C) ADD a curated concepts/FAQ content source so "what is an LLM / is Codex an LLM / what models
> exist" get grounded cited answers (mind the eval-vs-worker-corpus split); (D) FIX conversation
> memory INCLUDING issue #112 (entity-aware rewrite for content-noun follow-ups) — must NOT regress
> the locked eval or the off-corpus-pivot refusal; grow the followup golden bucket + close #112.
> Each slice full-loop (plan → adversarial fan-out review → TDD → gates → real-Postgres eval proof
> for any retrieval/answer change → fan-out PR review → green CI → squash-merge). Bring the live
> stack up (runbook in the handoff doc) and RE-TEST in the real UI with NEW questions you invent
> from the FAQs + multi-turn follow-ups, not just predefined golden cases. Serial merges; honor
> AGENTS.md + code_review.md + the blast-radius review policy.
