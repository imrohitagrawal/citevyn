# CiteVyn Landing — Bug-Fix & Hardening Implementation Plan

> **For agentic workers:** This plan is executed across **four separate chats/context windows**, one per Deliverable. In each chat: REQUIRED SUB-SKILL `superpowers:executing-plans` (or `superpowers:subagent-driven-development`) to work task-by-task; use `superpowers:test-driven-development` for every fix; end with `superpowers:verification-before-completion` and `superpowers:requesting-code-review`. Steps use checkbox (`- [ ]`) syntax. **Do not start a Deliverable without first reading the handover note the previous chat appended to `docs/superpowers/plans/citevyn-handover-log.md`.**

**Goal:** Fix the confirmed bugs and code-quality/test-coverage gaps found by the verify / verification-before-completion / taste-check / pr-test-analyzer reviews of the CiteVyn landing page, without regressing the 94 passing UI tests, while keeping the "one theme system, zero hardcoded colors" invariant intact.

**Architecture:** React 18 + Vite SPA. One `useLandingState` hook holds all state/behavior (a `useReducer` + derived data); presentational components (`Header`, `Hero`, `landing-sections`, `ChatView`, `LandingPage`) read from it. All color/spacing lives in CSS custom properties in `src/styles/tokens.css` (light via `:root,[data-theme="light"]`, dark via `[data-theme="dark"]`); component styles in `src/styles/landing.css` consume `var(--…)` only. Design source-of-truth: `/Users/rohitagrawal/Downloads/design_handoff_citevyn_landing/CiteVyn Landing v2.dc.html` + `README.md` — the prototype runtime `support.js` is NOT ported.

**Tech Stack:** React 18, TypeScript, Vite 5 (dev server on **port 3000**), Playwright (`@playwright/test`), Vitest (unit, optional), stylelint (`color-no-hex` guardrail).

## Global Constraints (apply to every Deliverable)

- **Zero hardcoded colors in components.** Every color in `landing.css` and component `.tsx` must resolve to a `var(--…)`. Hex/rgb/hsl literals live ONLY in `tokens.css`. `npm run lint:css` must stay green.
- **Both themes.** Every user-visible change must be correct in light AND dark (toggle = `.theme-toggle`). Dark tokens: `--bg:#161618 --surface:#1e1e21 --surface-2:#26262b --ink:#f0efe9 --muted:#a4a19a --faint:#6f6d67 --border:#323238 --border-2:#3d3d44 --hl:#f6c453 --hl-soft:#3a3320 --hl-ink:#1c1b19`. Light: `--bg:#faf9f6 --surface:#ffffff --surface-2:#f3f1ea --ink:#1c1b19 --muted:#6b6862 --faint:#9a978f --border:#e7e3da --border-2:#dcd7cc --hl:#ffd75e --hl-soft:#fbe9b0 --hl-ink:#1c1b19`. Semantic (both themes): success `#1c9a5f`, warning/refusal amber `#b4732a`, error `#c25b4e`, error-strong `#b0503f`.
- **No regressions.** The full suite must be green before any Deliverable is considered done: `npm run type-check` && `npm run lint:css` && `npm run test:ui` (72) && `npm run test:visual` (22) = **94**.
- **Line numbers in this plan are anchors, not gospel.** They drift as files change. Locate code by the quoted snippet, not the line number.
- **Commit convention:** work on a branch (`fix/citevyn-landing-hardening`), commit at the end of each Deliverable. **Do NOT include the Claude Code attribution footer** (`🤖 Generated with…` / `Co-Authored-By: Claude`) in commit messages or PR bodies. Verify `origin/main` matches local `main` before branching (base-branch-drift caution).
- **Dev server:** a Vite dev server on `http://localhost:3000` is required for Playwright (config `reuseExistingServer`). Start with `npm run dev` from `frontend/`. On a cold server the first `page.goto` triggers a dependency-optimize reload — the suite's `beforeEach` already handles this with `waitUntil:"commit"`.

## Working directory

All paths below are relative to `frontend/` = `/Users/rohitagrawal/Documents/Projects/project-with-RAG/CiteVyn-AI/citevyn-ai/citevyn-ai/frontend`.

## Out of scope (explicitly do NOT fix)

- **B4 — keyword matcher mis-routes some questions** (`data/knowledgeBase.ts` `matchKB`). This is the prototype canned matcher the README says is replaced by the real retrieval API in production. Leave it; do not "improve" the demo matcher.

---

## Handover protocol (READ FIRST, every chat)

There is a running log at `docs/superpowers/plans/citevyn-handover-log.md`. **Deliverable 0 creates it.**

At the **start** of each chat:
1. Read this plan and the last `## Handover — Deliverable N` entry in the log.
2. `git status` / `git log --oneline -5` to confirm you're on `fix/citevyn-landing-hardening` at the SHA the handover names.
3. Start the dev server (`npm run dev`) and confirm `curl -s localhost:3000` serves before running Playwright.

At the **end** of each chat, append a new `## Handover — Deliverable N complete` entry containing, verbatim template:
```
## Handover — Deliverable N complete  (YYYY-MM-DD)
- Branch / commit: fix/citevyn-landing-hardening @ <short-sha>
- What changed: <files + one-line each>
- Decisions made: <e.g. B3 resolution, any deviation from plan>
- Test tally: type-check ✅ | lint:css ✅ | test:ui <n> | test:visual <n>  (paste the summary lines)
- New/changed baselines: <list any visual snapshots regenerated + why>
- Gotchas for next chat: <flakes, timing, env quirks>
- START HERE next: Deliverable N+1, first task = <name>
```

---

## Deliverable 0 — Setup (fold into the start of Deliverable 1's chat; ~10 min)

**Goal:** clean branch + committed green baseline + handover log, so every later Deliverable has a known-good starting point.

- [ ] **Step 1: Confirm base is clean.** Run `git fetch origin`, then `git status`. Expected: on `main`, working tree has the current session's uncommitted changes (React recreation + tests). Confirm `git log origin/main..main` is empty (local main not ahead) — if it isn't, STOP and flag base-branch drift.
- [ ] **Step 2: Branch.** `git checkout -b fix/citevyn-landing-hardening`.
- [ ] **Step 3: Verify the baseline is green** before committing it:
  ```bash
  cd frontend
  npm run type-check && npm run lint:css && npm run test:ui && npm run test:visual
  ```
  Expected: `72 passed` then `22 passed`.
- [ ] **Step 4: Commit the green baseline.** `git add -A && git commit -m "chore(frontend): commit green CiteVyn landing baseline (94 UI tests)"`.
- [ ] **Step 5: Create the handover log** `docs/superpowers/plans/citevyn-handover-log.md` with a first entry recording the baseline SHA and tally. Commit it.

---

## Deliverable 1 — Confirmed functional & a11y bug fixes  (Chat 1)

**Fixes:** B1 (Contact-sales keyboard no-op), B2 (progress-dot active class), B3 (highlighter-band dark contrast — decision + handling), B5 (FAQ `aria-expanded`, Enterprise tab order). TDD: write the failing test first for each.

**Files:**
- Modify: `src/components/landing-sections.tsx` (Enterprise CTA, FAQ button)
- Modify: `src/components/Hero.tsx` (progress-dot active class)
- Modify: `src/hooks/useLandingState.ts` (expose `active` on `heroDots` if not already consumed for the class)
- Modify (B3, if chosen): `src/styles/landing.css` / `src/styles/tokens.css`
- Test: `tests/behavior.spec.ts`, `tests/fidelity.spec.ts`

**Interfaces produced:** none new for later Deliverables beyond corrected DOM (`.progress-dot.active` now tracks the cycle; Enterprise CTA is inert to keyboard).

### B1 — "Contact sales" must be a true no-op

Root cause: `landing-sections.tsx` Pricing renders every tier's CTA with `onClick={tier.name === "Pro" ? onGetPro : onOpenChat}` and only neutralizes Enterprise with inline `style={{ pointerEvents:"none", opacity:0.5 }}`. `pointer-events:none` blocks mouse but not keyboard focus+Enter, so Enterprise still calls `onOpenChat` → chat.

- [ ] **Step 1: Failing test** (`tests/behavior.spec.ts`, in the Pricing describe):
```ts
test("Contact sales is inert to keyboard too (true no-op)", async ({ page }) => {
  const cta = page.locator(".pricing-card").nth(2).locator(".cta");
  await cta.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator('[data-screen-label="Chat"]')).toHaveCount(0);
  await expect(cta).toBeDisabled();
});
```
- [ ] **Step 2: Run it, confirm it fails** (`npx playwright test -g "inert to keyboard"`). Expected: FAIL (currently navigates / not disabled).
- [ ] **Step 3: Fix.** In `landing-sections.tsx`, give the Enterprise tier no click handler and a real `disabled` attribute instead of `pointer-events`:
  - `onClick={tier.name === "Pro" ? onGetPro : tier.name === "Enterprise" ? undefined : onOpenChat}`
  - add `disabled={tier.name === "Enterprise"}` to the `<button>`, and drop the inline `pointerEvents/opacity` hack. Style the disabled state in CSS: `.cta:disabled { opacity: .5; cursor: not-allowed; }`.
- [ ] **Step 4: Re-run, confirm PASS.** Also confirm the existing "Contact sales no-op" mouse test still passes.

### B2 — Progress-dot `.active` class must track the current Q&A

Root cause: `Hero.tsx` renders `className={\`progress-dot${i === 0 ? " active" : ""}\`}` — hardcoded to index 0. The 22px pill width is driven correctly by inline `dot.style` (from `heroDots`, keyed on `state.hero.key`), so the class and the visual pill diverge on cycles 2–3.

- [ ] **Step 1: Failing test** (`tests/behavior.spec.ts`, Hero describe) — assert exactly one dot is the 22px pill AND it carries `.active`, after an advance:
```ts
test("progress pill + active class both track the current Q&A", async ({ page }) => {
  const q = page.locator(".card-content .message-text").first();
  const first = (await q.textContent())!.trim();
  await expect.poll(async () => (await q.textContent())!.trim(), { timeout: 15000 }).not.toBe(first);
  const dots = page.locator(".progress-dot");
  const widths = await dots.evaluateAll((els) => els.map((e) => getComputedStyle(e).width));
  const actives = await dots.evaluateAll((els) => els.map((e) => e.classList.contains("active")));
  const pillIdx = widths.findIndex((w) => parseFloat(w) >= 20);
  expect(actives.filter(Boolean).length).toBe(1);
  expect(actives[pillIdx]).toBe(true); // the pill is the active one
});
```
- [ ] **Step 2: Confirm it fails.**
- [ ] **Step 3: Fix.** Add an `active: boolean` field to each `heroDots` item in `useLandingState.ts` (`active: k === state.hero.key`), and in `Hero.tsx` use `className={\`progress-dot${dot.active ? " active" : ""}\`}` instead of `i === 0`.
- [ ] **Step 4: Re-run, confirm PASS.**

### B3 — Highlighter-band text contrast in dark mode → **DECIDED: Option B (darken)**

Context: `.highlight` / `.highlight-phrase` use `background: linear-gradient(transparent 60%, var(--hl) 60%)` (an underline band) and inherit `color: var(--ink)` → light text in dark mode (matches the design HTML but low-contrast where letters overlap the yellow). **The user chose Option B: highlighted text is DARK ink on yellow in dark mode too** (real-highlighter look; also makes dark mode consistent with the already-dark CTA highlight). In light mode `--ink` is already `#1c1b19`, so this is a no-op in light and only darkens dark mode.

- [ ] **Step 1: Failing test** (`tests/fidelity.spec.ts`, both-theme loop): assert the hero `.highlight` computed text `color` is dark (`rgb(28, 27, 25)` = `--hl-ink`) in BOTH themes. Confirm it FAILS today in dark (currently `rgb(240,239,233)`).
- [ ] **Step 2: Implement.** Add `.highlight, .highlight-phrase` to the existing text-on-yellow color group in `landing.css` so they resolve to `color: var(--hl-ink)`.
- [ ] **Step 3: VERIFY LEGIBILITY on every highlight instance — this is mandatory, not optional.** The highlight is an *underline band* (yellow = bottom 40% only), so dark text risks dark-on-dark where the TOP of the glyphs sits on the dark page background. In dark mode, screenshot and eyeball all four spots: hero H1 "check,", How-it-works H2 "trust.", Why H2 "I don't know.", and the ~14.5px Why body claim (`.highlight-phrase`). If ANY glyph-tops blend into the dark background, raise the yellow coverage **in dark mode only** — e.g. shift the gradient stop (`linear-gradient(transparent 40%, var(--hl) 40%)`) or use a fuller fill — until the dark text is fully legible everywhere. (The approved reference is the hero H1 rendered dark-on-yellow like a highlighter.)
- [ ] **Step 4: Regenerate the affected visual baselines** (`npm run test:visual:update`) — at least `hero-dark`, `how-it-works-dark`, `comparison-dark`. Eyeball each regenerated PNG before committing it. Note in the handover exactly which baselines changed and why.
- [ ] **Step 5: Re-run full suite, confirm green.** Also add the hero `.highlight` selector to the "text on the yellow --hl accent stays dark" fidelity sweep so it's permanently covered.

### B5 — a11y: FAQ `aria-expanded` + Enterprise out of tab order

- [ ] **Step 1: Failing test** (`tests/behavior.spec.ts`, FAQ describe): first item `aria-expanded="true"`, others `"false"`; after toggling item 3, item 1 → `"false"`, item 3 → `"true"`.
- [ ] **Step 2: Confirm it fails.**
- [ ] **Step 3: Fix.** In `landing-sections.tsx` FAQ, add `aria-expanded={openFaq === i}` and `aria-controls`/`id` linkage on each `.faq-toggle`. The B1 `disabled` attribute already removes Enterprise from the tab order (a disabled button is not focusable) — confirm with a test that `.pricing-card:nth(2) .cta` is not focusable (`await cta.focus()` leaves `document.activeElement` unchanged).
- [ ] **Step 4: Re-run, confirm PASS.**

### Deliverable 1 — Definition of Done
- [ ] All new tests pass; full suite green (`type-check`, `lint:css`, `test:ui`, `test:visual`).
- [ ] `superpowers:requesting-code-review` run on the diff; address findings.
- [ ] Commit: `fix(frontend): correct contact-sales no-op, progress-dot active class, FAQ a11y`.
- [ ] Append `## Handover — Deliverable 1 complete` to the log (record the B3 decision explicitly).

---

## Deliverable 2 — Test-thoroughness hardening (the safety net)  (Chat 2)

**Goal:** add the missing/strengthened assertions from the pr-test-analyzer review, so Deliverable 3's refactors are protected. **No production code changes** except where a test uncovers a genuine bug (if so, fix it TDD-style and note it). This is where you lock current correct behavior.

**Files:** `tests/behavior.spec.ts`, `tests/fidelity.spec.ts`, `tests/helpers.ts` (add helpers as needed). Possibly a small Vitest unit test file for the hook (timer cleanup) — `src/hooks/useLandingState.test.ts` with `vitest`.

Add coverage for (each its own `- [ ]` task: write test → run → confirm it passes against current code, or fails → real bug → fix):
- [ ] **Hero card `min-height:322`** — assert `.hero-card` (or the answer container) rendered height ≥ 322 so it can't collapse between empty and streamed states.
- [ ] **Streaming rate + ordering** — sample `.bot-message`/`.demo-answer` text length at two points during streaming and assert it grows incrementally (not 0→full in one tick); assert `.source-card` count is 0 while `.typing-cursor` is present and >0 only after it disappears.
- [ ] **Duplicate-pulse iteration count** — assert `#cv-msg-0` computed `animationIterationCount === "3"`; then re-ask the same duplicate a second time and confirm the pulse re-fires (guards the reset-to-restart logic in `flashExisting`).
- [ ] **Dark-theme parity** for flows currently light-only: demo refusal badge readable + 0 sources on dark surface; hero empty-Ask amber border resolves to `#b4732a` on dark `.hero-input-box`; a sourced demo answer shows readable source cards in dark; Get-Pro flow in dark.
- [ ] **Full section-background sweep on theme flip** — iterate `#who`, `#how`, `#why`, `#demo`, `#pricing` and assert each computed `backgroundColor` equals the theme `--bg`/`--surface` token (no transparent, no cross-theme constant).
- [ ] **Ticker 80px fade masks** — assert `.ticker-strip` `::before`/`::after` width `80px` and `background-image` contains a gradient referencing the theme `--bg`.
- [ ] **Marquee infinite + true duplication** — `animationIterationCount === "infinite"`; first 8 chip labels equal chips 9–16.
- [ ] **Card hover-lift** — hover a `.persona-card`/`.step-card`/`.feature-card`, assert `transform` becomes a non-identity matrix (≈ translateY(-3px)); reverts on unhover.
- [ ] **Placeholder cadence** — capture placeholder, wait ~1.5s → unchanged; wait past 3.2s total → changed (bracket the interval).
- [ ] **All 4 chat suggestions + both persona buttons per card** — parametrize; assert the resulting `.user-msg` text equals the clicked button's label (catches a wiring mismap).
- [ ] **Send-button path (not just Enter)** — click `.send-button` with text → bubble; click with empty input → no new bubble (no-op guard).
- [ ] **Reduced-motion full coverage** — under `reducedMotion:"reduce"`: empty-Ask shake disabled; duplicate cv-pulse duration 0ms; `.typing-cursor` animation `none`.
- [ ] **Mobile hit-target sweep** — on 390px viewport, assert `.nav-link`, `.faq-toggle`, `.ticker-chip`, `.demo-question`, `.suggestion-btn` height ≥ 44px. (If any are < 44, that's a real gap → extend the mobile touch-target CSS rule and note it.)
- [ ] **Timer cleanup** — a Vitest unit test on `useLandingState` (or a landing→chat→back navigation loop) asserting no post-unmount state updates / no leaked intervals.
- [ ] **Refusal + dedup content** — assert refusal badge text is `"⚠ NO SOURCE — REFUSED"` and the refusal body is non-empty; assert the duplicate guard is case/whitespace-insensitive (`"  what is claude code?  "` adds no new bubble).
- [ ] **Get-Pro duplicate guard** — click Get Pro twice → only one Pro user bubble.

### Deliverable 2 — Definition of Done
- [ ] All new tests green; full suite green. If any test surfaced a real bug, it was fixed TDD-style and is documented.
- [ ] `test:ui` count increased; note the new total in the handover.
- [ ] Code review + commit: `test(frontend): harden UI coverage (streaming order, dark parity, hover, a11y, cleanup)`.
- [ ] Append `## Handover — Deliverable 2 complete` (new test count; any bug found).

---

## Deliverable 3 — Code-quality / taste refactors (behavior-preserving)  (Chat 3)

**Goal:** apply the taste-check refactors. **Behavior must not change** — the now-hardened suite (Deliverables 1–2) is the regression net; run the FULL suite after each refactor task, not just at the end. Use `superpowers:systematic-debugging` if any test goes red.

**Files:** `src/hooks/useLandingState.ts`, `src/components/LandingPage.tsx`, `src/components/Hero.tsx`, `src/components/ChatView.tsx`.

- [ ] **H1 — `askHero` self-contained.** Make `askHero` perform navigation itself: on valid input call `enterChat(q)` and return void; on empty do the focus+nudge. Note the temporal-dead-zone constraint: `enterChat` must be defined *before* `askHero` in the hook (or restructure so `askHero`'s `useCallback` dep on `enterChat` is valid). Update the hook's `onHeroKey` to call `askHero()`. In `LandingPage.tsx`, collapse the duplicated inline `onHeroKey`/`onAskHero` wiring to just pass the hook's handlers through (mirror how `ChatView` uses `submitChat`). Run full suite.
- [ ] **H2 — single duplicate-question guard.** Move the canned Pro answer into `knowledgeBase.ts` as a KB entry keyed like the others, and route `getPro` through `enterChat("What do I get with CiteVyn Pro?")` so `send` (with its one dedup guard) handles it. Delete the second guard + `streamBot`/`flashExisting` duplication in `getPro`. Run full suite (esp. the Get-Pro + Get-Pro-twice tests from Deliverable 2).
- [ ] **M1 — placeholder count in one place.** Move the 5 placeholder strings from `LandingPage.tsx` into `useLandingState.ts` as a `const PLACEHOLDERS`, expose the current placeholder via the hook, and derive the modulus from `PLACEHOLDERS.length` in the reducer (drop the dead `index` payload; rename action to `ADVANCE_PLACEHOLDER`). Run full suite (placeholder-rotation + cadence tests).
- [ ] **M2 — typed timers, correct clearer.** Have `streamText` return `{ stop() }` closing over `clearInterval`; store every timer ref as `{ stop } | null`; cleanup becomes `Object.values(timers.current).forEach(t => t?.stop())`. Remove the `clearTimeout(... as any)` on interval IDs. Run full suite + the timer-cleanup unit test.
- [ ] **M3 — extract `scrollToId`.** Factor the duplicated `getBoundingClientRect().top + pageYOffset - 72; window.scrollTo(...)` into one `scrollToId(id)`; the chat branch does `setScreen` then `setTimeout(() => scrollToId(id), 80)`, the landing branch calls it directly. Run full suite (nav tests).
- [ ] **M4 — scroll ownership to the view.** Remove the triplicated `document.getElementById("chat-list")` scroll calls from `streamBot`; rely on `ChatView`'s `useEffect([messages])` (extend its dep so it also fires per streamed chunk if needed for pinning). Run full suite (autoscroll + duplicate-scroll tests) — this is the highest-risk refactor; verify autoscroll still pins.
- [ ] **Lows** (batch, then full suite): move `KB` import to the top of `LandingPage.tsx` (and consider moving KB-derived demo props into the hook's derived block); drop the pointless `chatListRef` alias + unused `chatListRef?` prop in `ChatView`; remove the `Hero` `startHeroLoop` SSR-ceremony boolean (there is no SSR — use a constant `min-height`); reconsider the defensive `|| KB["claude-code"]` / `?? 0` fallbacks on trusted in-code keys (leave if removing them would require broader changes — note the decision).

### Deliverable 3 — Definition of Done
- [ ] **Zero behavior change:** the full suite (now including Deliverable-2 hardening) is green with no test modifications made to accommodate a refactor (if a test needed changing, that's a behavior change — stop and reconsider).
- [ ] `superpowers:requesting-code-review` + `superpowers:receiving-code-review` on the diff.
- [ ] Commit: `refactor(frontend): self-contained handlers, single dedup guard, typed timers, view-owned scroll`.
- [ ] Append `## Handover — Deliverable 3 complete`.

---

## Deliverable 4 — Final verification, docs & PR  (Chat 4)

**Goal:** whole-system verification, update the deliverables doc, open the PR.

- [ ] **Step 1: Clean-room verification.** From a cold state: stop the dev server, `rm -rf node_modules/.vite`, restart `npm run dev`, and run `npm run type-check && npm run lint:css && npm run test:ui && npm run test:visual` with `--workers=1` twice to confirm stability (no flakes) on a cold server. Use `superpowers:verification-before-completion` discipline — paste the real output.
- [ ] **Step 2: Manual dark/light pass.** Re-run the `verify` skill (in the main session, where it's available) against the running app; confirm B1/B2/B5 are fixed and nothing regressed. Screenshot chat + dark hero + how-it-works for the record.
- [ ] **Step 3: Update deliverables doc.** Refresh any status doc / the original work-order deliverables (defect table, contrast numbers, passing test output) to reflect the final state.
- [ ] **Step 4: Squash-or-keep + PR.** Ensure `origin/main` still matches before opening. Push `fix/citevyn-landing-hardening`; open a PR with a body summarizing the four deliverables, the bug table, and the final test tally. **No Claude attribution footer.**
- [ ] **Step 5:** Append the final `## Handover — Deliverable 4 complete` (PR URL, final tally).

### Deliverable 4 — Definition of Done
- [ ] Cold, `--workers=1` full suite green **twice**; type-check + lint clean.
- [ ] PR open with complete body; handover log closed out.

---

## Self-review (author's checklist against the findings)

- B1 ✅ D1 · B2 ✅ D1 · B3 ✅ D1 (decision) · B4 ✅ out-of-scope (documented) · B5 ✅ D1
- Taste H1,H2 ✅ D3 · M1–M4 ✅ D3 · Lows ✅ D3
- Coverage gaps (progress-dot, min-height, streaming order, pulse count, dark parity, section sweep, fade masks, hover, cadence, suggestions/personas, send-button, reduced-motion, mobile, cleanup, refusal/dedup, get-pro-dup) ✅ D2
- Verification & completion claim ✅ D4
- Every Deliverable ends green + code-reviewed + handover written; refactors are gated on "no test changes."
