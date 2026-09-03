# Next-session prompt (copy everything below the line)

---

Fresh-context continuation for CiteVyn. The owner's 2026-09-03 production-QA round is half
done. Work the remainder **one issue per PR, in this order, each fully built + reviewed +
merged before the next starts:**

**#302** (scroll to an existing answer from the landing sections) → **#303** (answer rendering:
markdown subset + clickable citations) → **#306** (CSP blocks every brand font file) → **ask the
owner to deploy** → **#308** (coverage tooling).

> **This supersedes the previous round's "after #303, stop."** The owner added #306 and #308
> after that instruction was written (2026-09-03), and chose ONE batched `fly deploy` after the
> QA items rather than a deploy per PR. #306 sits deliberately *before* that deploy: with a
> batched deploy, anything unfixed by then stays broken in production until the next one.

Repo: `/Users/rohitagrawal/Projects/citevyn`. Before planning anything, read `AGENTS.md`,
`code_review.md`, `docs/BACKLOG.md`, the issues (`gh issue view 302 303 306 308` — each carries
the owner-agreed fix shape; build THAT, do not redesign), `docs/API_SPEC.md`, the session memory
notes, and run `gh issue list --state open`. **Re-verify every fact below against the repo
before acting on it — this block is a snapshot, not a source of truth.**

## Where things stand — verified 2026-09-03

- `main` is at **`4b1c5e0`** plus the #301 merge. **#300 and #301 are MERGED with post-merge CI
  green; NOTHING is deployed.** Production (https://citevyn.stackclimb.com, Fly app `citevyn`)
  still runs release **v9 = `f3e64ca`**, i.e. neither fix is live yet.
- **First action:** `git rev-parse main origin/main` must agree; `git pull --ff-only`.
- **Baselines to re-measure yourself before changing anything** (run them in the background
  while you read): backend `cd backend && uv run pytest -q` was **1730 passed / 20 skipped /
  6 failed — the 6 are pre-existing and unrelated** (a local `backend/.env` holds real keys that
  pydantic-settings feeds into `Settings()` when a test `delenv`s a var; in your own tests
  override with `monkeypatch.setenv(..., "")`, never `delenv`). Frontend `npm test -- --run` was
  **196/196**. Bundle: main chunk **198,349 B / 63,497 B gzip against a 63,500 B line — 3 BYTES
  OF HEADROOM.** Measure the exact bytes with node `zlib.gzipSync` on `dist/assets/index-*.js`
  (Vite's display rounds), and **build with `VITE_API_LIVE=true`** (`frontend/.env.local` locally,
  the Dockerfile in production) — a build without it is ~0.85 kB larger and is not what ships.
- **Open follow-ups to respect, not fix:** #294, #296, #286, #288, #289, #290, #264, #265.
  Dependabot PR #307 (browserslist) is open and is not yours. Leave the five
  `feat/adr0004-pr*` and two `worktree-agent-*` local branches alone.

## Your authority

**May do without asking:** branch from `main`, implement, run the full local battery, review,
open the PR, merge once verified. Do NOT stop to ask between items — the previous session
under-used this authority and the owner corrected it.

**Must not do without the owner:** `fly deploy`; push a version tag; touch Resend/DNS or any
real API key; any paid API call (stay on `provider=stub`); force-push (append fix commits);
`git clean -fdx`; `git stash -u`; delete any branch you did not create.

When #302, #303 and #306 are all merged, **stop and ask for the deploy "go"** — that is the one
gate. If authorised: the runbook in `docs/DEPLOY_FLY.md` §4.1 with BOTH build args, wake the
scaled-to-zero machine with `curl /health` first (or the key read is empty), and verify the
served bundle has 0 hits for `local-demo-key` afterwards — a deploy without `VITE_API_DEMO_KEY`
broke production for an hour (#296).

## The items — the issues carry the detail; this is the shape, plus what a mapping pass found

### #302 — landing re-ask does not scroll to the existing answer (frontend, small, T2)

Owner-agreed: **ChatView owns the scroll** — when it mounts with a pending highlight, scroll
then (a layout effect keyed on `highlight`), keeping the existing in-chat behaviour. Product
decision: once a conversation exists, the landing entry points **hand over to the chat** (open
it with the composer focused and any typed text carried across) rather than acting as a second
entry point.

Mapped facts, all verifiable:

- **The issue text is wrong about one entry point.** The "Live demo" chips do NOT enter chat —
  `demoQuestions[].select` only swaps the in-place demo answer. The entry points that actually
  submit are: the hero Ask button, the hero Enter key, the hero TRY chips, the **ticker/marquee
  pills**, the "Who it's for" persona buttons, and the Pricing "Get Pro" CTA. All funnel through
  `useLandingState.enterChat(q)`, which does `SET_SCREEN` then `setTimeout(() => send(q), 60)` —
  that 60 ms is the *only* thing coordinating mount and send today.
- `flashExisting` (`useLandingState.ts:708`) reaches into the DOM by id and **bails silently**
  when `#cv-msg-{i}` is absent — the exact failure. `ChatView` has **no effect keyed on
  `highlightedIndex`**; the index only drives a className. The pulse comes from an inline style
  set in the hook.
- **Zero tests — jsdom or Playwright — re-ask an answered question from a landing section.** The
  existing duplicate-guard Playwright tests all enter chat first and re-ask in-chat. The jsdom
  hook tests are structurally blind: no ChatView is mounted, so `getElementById` returns null.
  **A live browser walkthrough matters more than agent count here.**
- **No `useLayoutEffect` exists anywhere in the frontend** — this would be the first. The house
  pattern is `useEffect` plus `setTimeout`/`rAF`.
- The chat composer has **no ref and no autofocus**, and `heroInput`/`chatInput` are independent
  reducer fields with **no mechanism to carry text across** (`askHero` explicitly discards it).
  The "carry typed text across" half needs new wiring, not just a focus call.
- `domId` is index-based (`cv-msg-{i}`), which is fragile under `RESUME_SESSION`.

### #303 — markdown subset + clickable citations (frontend + one backend prompt line, medium, T2 + security)

Owner-agreed, ONE PR, **no markdown library** (the eager bundle has 3 bytes of headroom; a
library costs 8–12 kB): constrain the model to a tiny subset in the system prompt (bold, inline
code, `-` bullets); render exactly that subset with a hand-rolled converter (~40 lines) that
escapes everything else, with a test proving unknown markup is escaped; render each `[n]` as a
small numbered chip linking to the source URL (new tab, title = source name); clicking a chip
highlights the matching card, hovering a card highlights its chips; collapse cards by document
with the badge listing every marker it backs; a one-time-per-session legend under the first
cited answer; persisted/copied text keeps plain `[n]`.

**The constraint that shapes the whole design:** `dangerouslySetInnerHTML` is used **zero times
in the entire frontend** — every string reaches the DOM through React's auto-escaping text path.
The converter must return **`ReactNode[]`, never an HTML string.** That preserves the existing
safety property instead of creating the first place in the codebase that needs an escaper. A
hand-rolled markdown renderer is an injection surface by construction, so this PR gets a
**mandatory adversarial escaping/XSS pass**, not just a unit test.

Other mapped facts:

- **The issue text is wrong that the source cards "ARE links".** They are `<div>`s with the URL
  as plain text (`ChatView.tsx:179-191`). `isSafeHref` already exists in that file (used for
  `docSuggestions`) and is what a chip's `href` should go through.
- The converter runs on **every streaming chunk** (`streamBot` dispatches cumulative text), so
  it must tolerate half-written `**bold` / `[1` mid-stream.
- Markers may be **gapped**, and a marker in prose may have **no matching card** (validation can
  drop one) — the chip renderer needs a miss path.
- `Source` is `{n, title, url}` — **no document id** — so "collapse by document" can only group
  on url/title unless `citationsToSources` (`lib/citations.ts`) is widened. `key={src.n}` will
  collide once cards are collapsed.
- **Demo-mode answers contain zero `[n]` markers**, so chips are live-only and the legend must
  not appear in demo. Same for `vite.liveStub.ts`'s canned answer.
- `.citation-chip` CSS **already exists** (`landing.css:1214`) and is reusable. `.message.bot
  .message-body` carries `white-space: pre-wrap` with a comment that **anticipates this work**,
  and `tests/answer-format.spec.ts` deliberately asserts line-box **count** rather than the
  property so the guard survives a markdown container.
- Existing Playwright specs assert `.source-card` **counts** (`behavior.spec.ts:701,709`) —
  collapsing duplicates changes those numbers; `visual.spec.ts` baselines will need updating.
- **No unit test renders `ChatView` at all** today.
- Prompt lives at `backend/app/llm/prompts.py:22`. The subset instruction goes after "Do not
  invent facts…" and **before** the final "respond with exactly … and nothing else" clause,
  which must stay last and byte-exact (it is compared by `_is_no_answer_refusal` and mirrored
  byte-identically in `knowledgeBase.ts` `GENERIC_REFUSAL`).
- If the bundle ceiling must move, raise it **deliberately in this PR** and record the new number
  in the BACKLOG #270 row — do not squeeze by renaming files.

### #306 — CSP blocks every Satoshi font file (small, security-adjacent)

`backend/app/core/security_headers.py:48` allows `api.fontshare.com` (where the *stylesheet*
lives) but Fontshare serves the font *files* from `cdn.fontshare.com`, so all 12 requests are
blocked on every page load and the page silently falls back to a system font. The Google Fonts
pair beside it is correct, which is why the identical Fontshare split was missed.

Cosmetic, not a hole (the policy is too *strict*, so it fails safe). One line plus a test. **No
test pins the CSP against the origins the page actually requests** — that gap is why a one-host
omission survived; add one that parses `frontend/index.html` for external origins and asserts
each is permitted by the matching directive.

### #308 — no coverage tooling (after the deploy)

Deliberately **measurement, not a gate**: dev dependency + `make coverage` + report-not-gate in
CI, with the blocking condition stated up front (no-decrease-vs-baseline after three stable
`main` runs, never an absolute floor). See the issue for why a percentage would not have caught
the defect that motivated it.

## How to work — the discipline that actually held, do not relax it

- **Verify → implement → document. No claim without a check.** Every number you report is one
  you measured this session.
- **Order per PR:** baselines → RED test → implementation → **mutation-test every guard**
  (sequentially in the one tree, byte-copy restore, assert the mutation applied — `ruff format`
  can silently un-apply one) → docs (`API_SPEC` for any contract change incl. every new audit
  `metadata.event`; README §8 endpoint table or `tests/test_readme_endpoints.py` fails;
  `.env.example` + README §5 for any new setting; `docs/SECURITY_MODEL.md` for any new rate-limit
  bucket; BACKLOG row) → commit → **live browser walkthrough BEFORE review** → review → fixes →
  a skeptic round on the fix diff → release-readiness gate → PR → CI → plain squash-merge as a
  sole command → post-merge CI on `main` green → delete the branch local + remote → next item.

### Review sizing — right-size it, and write the dimension list BEFORE launching

An unlisted dimension is *silently* skipped, not deliberately skipped. Enumerate and mark each
run / skipped-because-N/A:

    correctness · silent-failure · security · performance · architecture/taste ·
    test-adequacy · coverage · data/persistence contract · docs-contract · completeness-critic

- **#302** — light T2, ~12 agents. The **live browser walkthrough** is worth more than agents.
- **#303** — T2 **plus a mandatory adversarial escaping/XSS pass**, plus bundle and a11y.
- **#306** — small, but it widens a CSP: a security look plus the missing CSP-origin test.

Classify per `AGENTS.md`: only reproduced CRITICAL_BLOCKER / REQUIRED_CONTRACT block; reproduce
before fixing; every fix commit gets its own skeptic pass; cap at two routine rounds; surviving
advisories you do not fix become a BACKLOG row in the same PR.

### Traps the last two sessions paid for — do not re-learn these

- **Mutation-test every guard.** Twice a guard was 97 %-covered, *executed by the suite*, and
  untested — deleting it left everything green. **Coverage is not assertion.** One of them was
  a production promotion gate; another left all 1728 backend tests green.
- **Run the mutation before writing a claim about what a test catches.** Four false claims were
  written and later corrected across those sessions. If you write "RED if X", run X.
- **Measurement beats a skeptic vote.** Findings refuted 2-of-3 were true when measured, twice.
- **Re-verify mutations after `ruff format`** — it has produced false survivals here.
- **Restore from a byte-copy (`cp` + `cmp`), never `git checkout <file>`** — that discards the
  whole feature, not just the mutation.
- **Do not edit the tree while a review is running** — a skeptic caught a dirty tree mid-review.
- **A lens that dies (content filter, API error) must be RE-RUN**, never counted as clean.
- `userEvent` works under vitest fake timers **only** with `toFake` narrowed to
  `["setInterval","clearInterval","Date"]` — faking `setTimeout` hangs it to the test timeout.
- Beware over-broad find-replace in tests: one applied a change to 12 tests when 2 needed it.

### Local live-verification facts

The docker DB `citevyn-db` is at migration head `0013`; `alembic` runs as
`CITEVYN_DATABASE_URL="$(grep '^CITEVYN_DATABASE_URL=' backend/.env | cut -d= -f2-)" uv run
--project backend alembic -c db/alembic.ini upgrade head` from the repo root. Seed with the same
URL: `python -m db.seed.seed_users` then `python -m db.seed.seed_catalog` (the Makefile's own
`DB_URL` uses a different password and fails). A stale `uvicorn` may hold `:8000` — check
`lsof -nP -iTCP:8000 -sTCP:LISTEN` first. The backend serves the built frontend only if
`backend/frontend_dist` exists: `cd frontend && npm run build`, `ln -s ../frontend/dist
backend/frontend_dist` (git-ignored; delete at cleanup), then **from `backend/`**
`CITEVYN_EMAIL_OUTBOX_DIR=<scratchpad>/outbox uv run uvicorn app.main:app --port 8000` and drive
`http://localhost:8000/` (NOT the Vite dev server). The demo bearer is
`CITEVYN_DEMO_API_KEY` in `backend/.env`; the message body field is `message`, not `content`.
The Claude-in-Chrome extension was NOT connected; the fallback that works is Playwright's
Chromium from `frontend/node_modules` (`import { chromium } from
"/Users/rohitagrawal/Projects/citevyn/frontend/node_modules/@playwright/test/index.mjs"` in a
scratchpad `.mjs`). Register throwaway accounts on localhost only; never the owner's
credentials; never click an OAuth Authorize button. **Delete throwaway rows at cleanup — but
only ones you created**: the local DB holds three 2026-09-01 accounts, two of which are the
owner's own real addresses.

### Shell gotchas

The Bash tool's cwd persists across calls — always `cd /Users/rohitagrawal/Projects/citevyn && …`
or use absolute paths; a `cd frontend` earlier in a chain will break a later `uv run`. zsh treats
a leading `=` in an argument as an expansion; `uv run` must run from `backend/` or with
`--project backend`. `timeout` is not installed on this macOS.

## Definition of done

Each item: merged **and** post-merge CI green on `main`, with a live walkthrough recorded in the
PR body. After #303 and #306, ask for the deploy go; after the deploy, do #308. Finish with
**Done / Verified myself / Cleanup / Pending / Next action**, separating what *you* ran (with its
output) from what a subagent reported, stating explicitly what is merged versus what is running
in production (nothing you merge is deployed until the owner says "go"), and ending with
"nothing pending — safe to close this session" in those words if that is true.
